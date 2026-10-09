"""⑱ 后台任务的跨进程执行权与启动接续 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_job_claim.py

为什么必须有这个脚本：单元/接口用例只能证明「代码里写了条件 UPDATE，也写了 advisory lock」，
证明不了「两个**真进程**同时抢同一条任务时，真的只有一个跑到处理函数」。这里做两件用例做不到的事：

1. **真进程抢占**：脚本以 `--worker <job_id>` 自调用起两个**独立 Python 进程**，同时执行同一条
   `queued` 任务（走生产路径 `run_job(job_id)`，不传 db）。处理函数会往一个计数文件追加一行，
   所以「到底跑了几遍」是数出来的，不是推断出来的 —— ⑱ 之前这里必然出现两行。
2. **接续不打断在跑的活**：拿一条**进程外真连接**持住某条 `running` 任务的执行权锁（= 那个进程还活着），
   再调 `resume_interrupted_jobs()`：这条必须原样不动（进 `still_running`），只有锁是空的
   （= 进程已死）那条才允许重排。以前这里一律重排，于是第二个副本一启动就把别人的活又排了一遍。

   ⚠️ 这里的「进程外真连接」**必须用 `NullPool`**（见 `separate_engine()`）：默认连接池下
   `Connection.close()` 只是把连接还回池子，会话级 advisory 锁会留在那条 DBAPI 连接上，于是
   「进程死了」这个前提根本没被模拟出来。这个坑本脚本首跑就踩了（4 个 FAIL 全是池子假象，
   产品代码无辜），也正好印证 `backend/locks.py` 里那条「必须显式解锁」的代价。

另外走一次真 HTTP：导入一个小批次 → `POST /admin/jobs/batch-revert` → 轮询任务，必须看到
`running` 时带着 `worker_id`（谁在跑），最终 `succeeded` 且 `worker_id` 被清空。

测试数据只用 2097 年（演示数据是 2026 年）与 `verify-job-claim` 名下的任务行，脚本开头结尾都清干净。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from backend import jobs, locks  # noqa: E402
from backend.db import SessionLocal, engine, settings  # noqa: E402
from backend.models import BackgroundJob  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PROBE_KIND = "verify.claim_probe"
PROBE_ACTOR = "verify-job-claim"
PERIOD = "2097-12"                      # 测试用月份（与演示数据的 2026 年错开）
MARK = "⑱执行权冒烟"
CSV_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"
OUT_DIR = ROOT / "output"
COUNTER = OUT_DIR / "_job_claim_counter.txt"
POLL_SECONDS = 40.0                     # HTTP 任务轮询上限（不存在的批次要重试，这里留足）
SLEEP_MS = 300                          # 探针处理函数持锁时长：让「抢输」发生在锁上而不是时序上

RESULTS: list[tuple[str, bool, str]] = []


# ------------------------------------------------------------------ 探针处理函数

@jobs.handler(PROBE_KIND)
def _probe(db, payload, context):
    """抢到执行权的进程才会跑到这里：追加一行「我跑过」，再假装干一会儿活。"""
    counter = Path(payload["counter_file"])
    with counter.open("a", encoding="utf-8") as handle:
        handle.write(f"{jobs.WORKER_ID} {datetime.now(UTC).isoformat()}\n")
    time.sleep(int(payload.get("sleep_ms", 0)) / 1000)
    return {"probe": MARK, "ran_by": jobs.WORKER_ID}


# ------------------------------------------------------------------ 小工具

def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def execute(sql: str, **params) -> int:
    with engine.begin() as conn:
        return conn.execute(text(sql), params).rowcount or 0


def insert(sql: str, **params) -> int:
    with engine.begin() as conn:
        return int(conn.execute(text(sql), params).scalar())


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=180) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    raw = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def upload(path: str, filename: str, content: bytes, token: str):
    boundary = "----verifyjobclaim"
    head = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
    body = head + content + f"\r\n--{boundary}--\r\n".encode()
    request = urllib.request.Request(f"{BASE}{path}", data=body, method="POST")
    request.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=180) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    raw = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def advisory_locks() -> int:
    """当前库里会话级 advisory 锁条数。用来验「释放干净」——残留的锁会让这条任务永远抢不动。"""
    return int(scalar("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'") or 0)


def separate_engine():
    """不共享连接池的引擎：模拟「另一个进程」的连接。

    默认池下 `Connection.close()` 只是把连接还回池子，会话级 advisory 锁仍然留在那条 DBAPI 连接上，
    所以「关掉连接 = 进程没了 = 锁没了」只有在 `NullPool` 下才成立（真进程死掉时连接是断开的，
    锁由数据库释放）。
    """
    return create_engine(settings.database_url, poolclass=NullPool)


def seed_job(*, status: str = "queued", attempts: int = 0, max_attempts: int = 3,
             worker_id: str | None = None, started: bool = False, error: str | None = None,
             payload: dict | None = None) -> int:
    return insert(
        "INSERT INTO background_jobs (kind, status, actor, payload, attempts, max_attempts, "
        "worker_id, error, created_at, started_at) VALUES (:kind, :status, :actor, "
        "CAST(:payload AS JSONB), :attempts, :max_attempts, :worker_id, :error, now(), "
        "CASE WHEN :started THEN now() ELSE NULL END) RETURNING id",
        kind=PROBE_KIND, status=status, actor=PROBE_ACTOR,
        payload=json.dumps(payload or {}), attempts=attempts, max_attempts=max_attempts,
        worker_id=worker_id, error=error, started=started,
    )


def job_row(job_id: int) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT id, kind, status, attempts, max_attempts, worker_id, started_at, "
                 "next_attempt_at, finished_at, error, result FROM background_jobs WHERE id = :id"),
            {"id": job_id},
        ).mappings().first()
    return dict(row) if row else {}


def snapshot(job_id: int) -> tuple:
    row = job_row(job_id)
    return tuple(str(row.get(key)) for key in
                 ("status", "attempts", "worker_id", "started_at", "next_attempt_at",
                  "finished_at", "error"))


def purge() -> None:
    """清掉 2097 的测试明细与派生数据、以及本脚本名下的任务行（幂等，跑前跑后各来一次）。"""
    execute("DELETE FROM financial_transactions WHERE transaction_date >= '2097-01-01' "
            "AND transaction_date < '2098-01-01'")
    execute("DELETE FROM financial_metrics WHERE period LIKE '2097%'")
    execute("DELETE FROM financial_alerts WHERE period LIKE '2097%'")
    execute("DELETE FROM financial_rollups WHERE period LIKE '2097%'")
    execute("DELETE FROM background_jobs WHERE kind = :kind OR actor = :actor",
            kind=PROBE_KIND, actor=PROBE_ACTOR)


# ------------------------------------------------------------------ 子进程模式

def worker_main(job_id: int, out: Path) -> int:
    """一个真进程抢这条任务。结论不靠日志推断：跑完读库，看 `result.ran_by` 是不是自己。"""
    jobs.run_job(job_id)
    with SessionLocal() as db:
        row = db.get(BackgroundJob, job_id)
        ran = bool(row and (row.result or {}).get("ran_by") == jobs.WORKER_ID)
    out.write_text(f"{'ran' if ran else 'skipped'} {jobs.WORKER_ID}\n", encoding="utf-8")
    return 0


# ------------------------------------------------------------------ 主流程

def main() -> int:
    purge()
    COUNTER.unlink(missing_ok=True)

    # ---------------------------------------------------------- 结构闸门（真模块）
    keys = {job_id: locks.job_lock_key(job_id) for job_id in (1, 2, 424242, 2 ** 40)}
    check("任务锁 key 稳定、不同任务不撞号，且都在 bigint 范围内",
          len(set(keys.values())) == len(keys)
          and locks.job_lock_key(424242) == keys[424242]
          and all(-(2 ** 63) <= value < 2 ** 63 for value in keys.values()),
          f"命名空间 {locks.JOB_NAMESPACE}，{len(keys)} 个任务算出 {len(set(keys.values()))} 个 key")
    check("接续锁、任务锁、期间锁三族互不撞号（否则会互相挡住）",
          locks.resume_lock_key() not in keys.values()
          and locks.lock_key(PERIOD) not in keys.values()
          and locks.resume_lock_key() != locks.lock_key(PERIOD),
          f"resume={locks.resume_lock_key()}，期间 {PERIOD}={locks.lock_key(PERIOD)}")

    # ---------------------------------------------------------- 起始状态
    details_before = int(scalar("SELECT count(*) FROM financial_transactions") or 0)
    check("起始状态干净：worker_id 列已建好、没有残留 advisory 锁、没有上次留下的探针任务",
          int(scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = "
                     "'background_jobs' AND column_name = 'worker_id'") or 0) == 1
          and advisory_locks() == 0
          and int(scalar("SELECT count(*) FROM background_jobs WHERE kind = :kind",
                         kind=PROBE_KIND) or 0) == 0,
          f"明细 {details_before} 行，advisory 锁 {advisory_locks()} 条")

    # ---------------------------------------------------------- 两个真进程抢同一条任务
    probe_id = seed_job(payload={"counter_file": str(COUNTER), "sleep_ms": SLEEP_MS})
    outs = [OUT_DIR / f"_job_claim_child_{index}.txt" for index in (1, 2)]
    handles, processes = [], []
    for out in outs:
        out.unlink(missing_ok=True)
        handle = out.open("w", encoding="utf-8")
        handles.append(handle)
        processes.append(subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--worker", str(probe_id),
             "--out", str(out)],
            cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        ))
    exits = [process.wait(timeout=180) for process in processes]
    for handle in handles:
        handle.close()

    lines = COUNTER.read_text(encoding="utf-8").splitlines() if COUNTER.exists() else []
    parsed = []
    for out in outs:
        fields = out.read_text(encoding="utf-8").split() if out.exists() else []
        parsed.append((fields[0] if fields else "?(无输出)", fields[1] if len(fields) > 1 else None))
    verdicts = [verdict for verdict, _worker in parsed]
    winner = next((worker for verdict, worker in parsed if verdict == "ran"), None)
    row = job_row(probe_id)

    check("两个真进程同时抢同一条 queued 任务：处理函数只跑了一遍",
          len(lines) == 1 and winner is not None and lines[0].startswith(winner),
          f"计数文件 {len(lines)} 行：{lines}")
    check("两个子进程一个 ran 一个 skipped（抢输的那个确实什么都没做）",
          sorted(verdicts) == ["ran", "skipped"] and all(code == 0 for code in exits),
          f"结论 {verdicts}，退出码 {exits}")
    check("任务收在 succeeded：attempts 只加了 1、worker_id 已清空、result 记着谁跑的",
          row.get("status") == "succeeded" and row.get("attempts") == 1
          and row.get("worker_id") is None and (row.get("result") or {}).get("ran_by") == winner,
          f"status={row.get('status')} attempts={row.get('attempts')} "
          f"worker_id={row.get('worker_id')} result={row.get('result')}")

    # ---------------------------------------------------------- claim 挡住「已经在跑」的任务
    execute("UPDATE background_jobs SET status = 'running', worker_id = 'verify-ghost-host-9999', "
            "attempts = 1, started_at = now() WHERE id = :id", id=probe_id)
    with SessionLocal() as db:
        claimed = jobs.claim(db, probe_id)
    row = job_row(probe_id)
    check("已经在跑（status=running）的任务抢不动：claim 返回 None 且 attempts 不变",
          claimed is None and row.get("attempts") == 1 and row.get("worker_id") == "verify-ghost-host-9999",
          f"claim={claimed} attempts={row.get('attempts')} worker_id={row.get('worker_id')}")
    with SessionLocal() as db:
        jobs.run_job(probe_id, db)
    row_after = job_row(probe_id)
    check("同一条任务再走一遍 run_job(db=...) 也不会重复执行（不再「running 再跑一遍」）",
          row_after.get("attempts") == 1 and row_after.get("status") == "running",
          f"status={row_after.get('status')} attempts={row_after.get('attempts')}")
    with SessionLocal() as db:
        missing = jobs.claim(db, 99999999)
    check("claim 一条不存在的任务返回 None（不抛异常）", missing is None, f"claim(99999999)={missing}")

    # ---------------------------------------------------------- 接续：僵尸 vs 真在跑
    live_id = seed_job(status="running", attempts=1, worker_id="verify-live-host-1111", started=True,
                       error="进程重启前留下的")
    dead_id = seed_job(status="running", attempts=1, worker_id="verify-dead-host-2222", started=True,
                       error="进程重启前留下的")
    live_engine = separate_engine()
    live_conn = live_engine.connect()
    live_held = locks.try_session_lock(live_conn, locks.job_lock_key(live_id))
    live_before = snapshot(live_id)
    foreign_active = int(scalar("SELECT count(*) FROM background_jobs WHERE status IN ('queued', "
                                "'running') AND kind <> :kind", kind=PROBE_KIND) or 0)
    with SessionLocal() as db:
        resumed = jobs.resume_interrupted_jobs(db, schedule=False)
    live_after = snapshot(live_id)
    dead = job_row(dead_id)
    check("持着执行权锁的任务（真有人在跑）原样不动，只进 still_running",
          live_held and live_id in resumed["still_running"] and live_id not in resumed["requeued"]
          and live_before == live_after,
          f"still_running={resumed['still_running']} requeued={resumed['requeued']} "
          f"行未变={live_before == live_after}（库里另有 {foreign_active} 条非测试的进行中任务）")
    check("执行权锁为空的任务（进程已死）才被重排，worker_id/started_at 清空、error 说明是重启重排",
          dead_id in resumed["requeued"] and dead.get("status") == "queued"
          and dead.get("worker_id") is None and dead.get("started_at") is None
          and "重新排队" in str(dead.get("error")),
          f"status={dead.get('status')} worker_id={dead.get('worker_id')} error={dead.get('error')}")

    live_conn.close()       # NullPool：真的关连接 → 数据库释放会话锁（等价于那个进程死了）
    live_engine.dispose()
    with SessionLocal() as db:
        resumed_again = jobs.resume_interrupted_jobs(db, schedule=False)
    check("锁的连接一关（进程真的没了），这条才被重排",
          live_id in resumed_again["requeued"] and job_row(live_id).get("worker_id") is None,
          f"requeued={resumed_again['requeued']} worker_id={job_row(live_id).get('worker_id')}")
    check("探测执行权锁不会留下残留锁（重排后接手的进程还抢得到）",
          not jobs.is_job_running(dead_id) and advisory_locks() == 0,
          f"advisory 锁 {advisory_locks()} 条")

    # 池化连接 close() 不放锁：这正是「必须显式解锁」的实证（locks.py 里写明的代价）。
    # run_job 生产路径拿的是池化连接，所以它在 finally 里 release_session_lock，而不是靠关连接。
    pooled = engine.connect()
    pooled_held = locks.try_session_lock(pooled, locks.job_lock_key(probe_id))
    pooled.close()
    probe_engine = separate_engine()
    with probe_engine.connect() as conn:
        leaked = not locks.try_session_lock(conn, locks.job_lock_key(probe_id))
    engine.dispose()        # 把池子连同那条还回去的连接一起关掉，锁才真的没了
    with probe_engine.connect() as conn:
        freed = locks.try_session_lock(conn, locks.job_lock_key(probe_id))
        if freed:
            locks.release_session_lock(conn, locks.job_lock_key(probe_id))
    probe_engine.dispose()
    check("池化连接 close() 之后锁还在（所以执行权只能显式解锁，不能靠关连接）",
          pooled_held and leaked and freed and advisory_locks() == 0,
          f"抢到={pooled_held} close 后仍占着={leaked} 池子关掉后放掉={freed} "
          f"advisory 锁 {advisory_locks()} 条")

    # ---------------------------------------------------------- 接续的全局锁
    running_id = seed_job(status="running", attempts=1, worker_id="verify-guard-host-3333",
                          started=True, error="进程重启前留下的")
    guard_engine = separate_engine()
    guard = guard_engine.connect()
    guard_held = locks.try_session_lock(guard, locks.resume_lock_key())
    guard_before = snapshot(running_id)
    with SessionLocal() as db:
        skipped = jobs.resume_interrupted_jobs(db, schedule=False)
    check("另一个进程正在接续时，本进程一个字都不改（skipped=True、三个列表全空）",
          guard_held and skipped == {"requeued": [], "failed": [], "still_running": [],
                                     "skipped": True, "total": 0}
          and guard_before == snapshot(running_id),
          f"返回 {skipped}，行未变={guard_before == snapshot(running_id)}")
    guard.close()
    guard_engine.dispose()
    with SessionLocal() as db:
        after_guard = jobs.resume_interrupted_jobs(db, schedule=False)
    check("接续锁释放后，同一个函数就能正常干活（不是被永久锁死）",
          running_id in after_guard["requeued"], f"requeued={after_guard['requeued']}")

    # ---------------------------------------------------------- 端到端：真 HTTP 提交真任务
    token = login(*ADMIN)
    status, listed = call("GET", "/admin/jobs?limit=5", token)
    rows = listed.get("data") if isinstance(listed, dict) else None
    check("GET /admin/jobs 的每一行都带 worker_id（谁在跑这个字段真的到了客户端）",
          status == 200 and isinstance(rows, list) and rows
          and all("worker_id" in item for item in rows),
          f"status={status}，{len(rows or [])} 行，字段 {sorted((rows or [{}])[0])[:6]}…")

    csv = (f"{CSV_HEADER}\n{PERIOD}-09,6001,{MARK},部门-097,执行权冒烟,400,0,0,0,0\n").encode("utf-8-sig")
    status, imported = upload("/financial/import", "verify-job-claim.csv", csv, token)
    batch = imported.get("batch") if isinstance(imported, dict) else None
    check("先导入一个 2097 的小批次（撤销任务要有真批次可撤）",
          status == 200 and imported.get("imported") == 1 and batch,
          f"status={status} batch={batch} imported={imported.get('imported')}")

    status, submitted = call("POST", "/admin/jobs/batch-revert", token, {"batch": batch})
    http_job = submitted.get("id") if isinstance(submitted, dict) else None
    check("POST /admin/jobs/batch-revert 返回 202 且响应里就带 worker_id 字段",
          status == 202 and http_job and "worker_id" in submitted and not submitted["deduplicated"],
          f"status={status} job={http_job} status字段={submitted.get('status')} "
          f"worker_id={submitted.get('worker_id')!r}")

    seen: list[tuple[str, str | None]] = []
    deadline = time.monotonic() + POLL_SECONDS
    row = {}
    while time.monotonic() < deadline:
        with engine.connect() as conn:
            row = dict(conn.execute(
                text("SELECT status, attempts, worker_id, result, error FROM background_jobs "
                     "WHERE id = :id"), {"id": http_job},
            ).mappings().first() or {})
        if row.get("status"):
            seen.append((row["status"], row["worker_id"]))
        if row.get("status") in ("succeeded", "failed"):
            break
        time.sleep(0.02)
    running_seen = [(state, worker) for state, worker in seen if state == "running"]
    check("轮询期间抓到「运行中」且那一刻写着执行它的进程（谁在跑看得见）",
          bool(running_seen) and all(worker for _state, worker in running_seen),
          f"见过的状态 {[state for state, _w in seen]}，运行中带的 worker_id "
          f"{sorted({worker for _s, worker in running_seen})}")
    check("后端把这个任务跑完了：succeeded、deleted_rows≥1、worker_id 又清空了",
          row.get("status") == "succeeded" and (row.get("result") or {}).get("deleted_rows", 0) >= 1
          and row.get("worker_id") is None,
          f"status={row.get('status')} result={row.get('result')} worker_id={row.get('worker_id')}")
    check("撤销真的删掉了那行导入（2097 里没有这个标记了）",
          int(scalar("SELECT count(*) FROM financial_transactions WHERE description = :mark",
                     mark=MARK) or 0) == 0,
          f"剩 {int(scalar('SELECT count(*) FROM financial_transactions WHERE description = :m', m=MARK) or 0)} 行")

    # ---------------------------------------------------------- 收尾
    execute("DELETE FROM background_jobs WHERE id = :id", id=http_job)
    purge()
    left_jobs = int(scalar("SELECT count(*) FROM background_jobs WHERE kind = :kind OR actor = :actor",
                           kind=PROBE_KIND, actor=PROBE_ACTOR) or 0)
    details_after = int(scalar("SELECT count(*) FROM financial_transactions") or 0)
    check("跑完把测试任务与 2097 数据清干净，演示明细一行没动、也没有残留 advisory 锁",
          left_jobs == 0 and details_after == details_before and advisory_locks() == 0
          and int(scalar("SELECT count(*) FROM financial_transactions WHERE transaction_date >= "
                         "'2097-01-01' AND transaction_date < '2098-01-01'") or 0) == 0,
          f"残留任务 {left_jobs} 条，明细 {details_before} → {details_after} 行，"
          f"advisory 锁 {advisory_locks()} 条")

    COUNTER.unlink(missing_ok=True)
    for out in outs:
        out.unlink(missing_ok=True)

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="⑱ 后台任务跨进程执行权冒烟")
    parser.add_argument("--worker", type=int, help="子进程模式：抢并执行这条任务 id")
    parser.add_argument("--out", type=Path, help="子进程模式：把结论写进这个文件")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])
    OUT_DIR.mkdir(exist_ok=True)
    try:
        if args.worker:
            assert args.out is not None, "--worker 必须配 --out"
            sys.exit(worker_main(args.worker, args.out))
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、数据库容器是否在跑", flush=True)
        sys.exit(1)
