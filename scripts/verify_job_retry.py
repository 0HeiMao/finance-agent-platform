"""真机验证「后台任务幂等重试 + 启动重跑」（⑩）：走真实 HTTP 接口 + 真库，两阶段跑。

为什么分两阶段：这个功能有一半发生在**进程启动的那一刻**（`resume_interrupted_jobs()` 把上次
留下的 `queued`/`running` 重新排队）。一个进程没法自己验证「重启之后」。所以：

    # ① 后端在跑（127.0.0.1:8000）时跑第一阶段：查幂等提交/键重放/退避重排/人工重试，
    #    并在最后往库里埋一条「半途而废」的任务，把它的 id 写进 output/_job_retry_state.json
    .\\.venv\\Scripts\\python.exe scripts\\verify_job_retry.py

    # ② 重启后端（看 output/_backend.log 里的「启动接续」），再跑第二阶段：
    #    埋下的任务应当已经被接续跑完（attempts 涨了一次、批次的行被删干净）
    .\\.venv\\Scripts\\python.exe scripts\\verify_job_retry.py --phase 2

为什么有的检查直接连库：本功能有两条只在数据库里成立的保证 —— 部分唯一索引
`uq_background_jobs_idempotency_key`（并发窗口的兜底），以及「启动接续」需要一个真实的
`running` 残骸。HTTP 接口造不出「一条已经躺在库里、但没有任何人在跑它」的任务，
所以这两处直接写库；其余一律走接口。

覆盖的检查（两阶段合计约 23 项）：
  登录 → 探针批次导入 → 无键去重（队列里已有同批次任务）→ 键去重（首次/重放/成功后再放）→
  换键 + 批次已删 = 404 → 超长键 422 → 部分唯一索引存在且真的拦重复 →
  人工重试的 404/409 → 失败任务重试后追加预算并保留原因 → 重跑仍失败会退避重排
  （attempts 涨、next_attempt_at 落在 2·2^(n−1) 附近）→ job.retry 审计 →
  【重启后】启动接续日志、埋在库里的 running 任务被接续跑完、批次行清空。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

from backend.db import engine  # noqa: E402
from backend.jobs import retry_delay  # noqa: E402

PROBE_PERIOD = "2096-03"
TAG = "JOB10冒烟"
CSV_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"
STATE_PATH = PROJECT_ROOT / "output" / "_job_retry_state.json"
LONG_KEY_LEN = 201

RESULTS: list[tuple[bool, str, str]] = []


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def check(name: str, ok, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)
    return bool(ok)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def execute(sql: str, **params) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql), params)


def csv_bytes(suffix: str) -> bytes:
    body = "\n".join(
        [
            CSV_HEADER,
            f"{PROBE_PERIOD}-01,6001,{TAG}-{suffix}-A,018,探针A,500.00,0,0,0,0",
            f"{PROBE_PERIOD}-02,6002,{TAG}-{suffix}-B,097,探针B,0,0,200.00,0,0",
            f"{PROBE_PERIOD}-03,6003,{TAG}-{suffix}-C,,探针C,100.00,0,0,0,0",
        ]
    )
    return ("\ufeff" + body + "\n").encode("utf-8")


async def login(client: httpx.AsyncClient, username: str, password: str) -> bool:
    response = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
    if response.status_code != 200:
        return False
    client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
    return True


async def import_probe(client: httpx.AsyncClient, suffix: str, label: str) -> str:
    response = await client.post(
        "/api/v1/financial/import", files={"file": (f"job10-{suffix}.csv", csv_bytes(suffix), "text/csv")}
    )
    if response.status_code != 200:
        check(f"导入探针数据（{label}）", False, f"HTTP {response.status_code} {response.text[:120]}")
        return ""
    body = response.json()
    batch = str(body.get("batch") or "")
    check(
        f"导入探针数据（{label}）",
        body.get("imported") == 3,
        f"batch={batch[:12]}… imported={body.get('imported')}",
    )
    return batch


async def submit(client: httpx.AsyncClient, batch: str, key: str | None = None):
    headers = {"Idempotency-Key": key} if key else None
    response = await client.post("/api/v1/admin/jobs/batch-revert", json={"batch": batch}, headers=headers)
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, response.text[:200]


async def job_of(client: httpx.AsyncClient, job_id: int) -> dict:
    response = await client.get(f"/api/v1/admin/jobs/{job_id}")
    return response.json() if response.status_code == 200 else {}


async def wait_job(client: httpx.AsyncClient, job_id: int, deadline: float = 120.0) -> dict:
    spent = 0.0
    job: dict = {}
    while spent < deadline:
        job = await job_of(client, job_id)
        if job.get("status") not in ("queued", "running"):
            return job
        await asyncio.sleep(0.5)
        spent += 0.5
    return job


async def wait_attempts(client: httpx.AsyncClient, job_id: int, least: int, deadline: float = 30.0) -> dict:
    """等这条任务被真机后台线程又跑了一次（attempts 涨到 least）。"""
    spent = 0.0
    job: dict = {}
    while spent < deadline:
        job = await job_of(client, job_id)
        if int(job.get("attempts") or 0) >= least:
            return job
        await asyncio.sleep(0.3)
        spent += 0.3
    return job


async def sweep(client: httpx.AsyncClient) -> None:
    """清掉上次崩溃留下的探针批次（明细按关键字查得到批次号）。"""
    response = await client.get("/api/v1/financial/transactions", params={"query": TAG, "limit": 200})
    if response.status_code != 200:
        return
    batches = {row["import_batch"] for row in response.json().get("data", [])}
    for batch in batches:
        status, job = await submit(client, batch)
        if status in (200, 202):
            finished = await wait_job(client, int(job["id"]))
            print(f"  清理遗留批次 {batch[:12]}… → {finished.get('status')}")


def read_text_any(path: Path) -> str:
    """读日志：PowerShell 的 Tee-Object 在不同宿主里会写成 UTF-16LE（带 BOM），显式判一下。"""
    if not path.exists():
        return ""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in raw[:64]:
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8", "replace")


def plant_job(*, kind: str, payload: dict, status: str, attempts: int, max_attempts: int,
              error: str | None = None, started: bool = False, key: str | None = None) -> int:
    """直接写一条任务残骸（HTTP 造不出来「躺在库里、却没有任何人在跑」的任务）。"""
    fragments = [
        "INSERT INTO background_jobs",
        "(created_at, kind, status, actor, payload, idempotency_key, attempts, max_attempts, error, started_at)",
        "VALUES (now(), :kind, :status, 'verify_job_retry', CAST(:payload AS jsonb), :key, :attempts,",
        ":max_attempts, :error, :started_at) RETURNING id",
    ]
    with engine.begin() as conn:
        row = conn.execute(
            text(" ".join(fragments)),
            {
                "kind": kind,
                "status": status,
                "payload": json.dumps(payload, ensure_ascii=False),
                "key": key,
                "attempts": attempts,
                "max_attempts": max_attempts,
                "error": error,
                "started_at": datetime.now(UTC) if started else None,
            },
        ).scalar()
    return int(row)


async def phase1(args) -> int:
    suffix = secrets.token_hex(4)
    planted_ids: list[int] = []
    key = f"verify-job-retry-{suffix}"
    async with httpx.AsyncClient(base_url=args.base_url, timeout=180.0, trust_env=False) as client:
        if not await login(client, args.username, args.password):
            print("FAIL: 管理员登录失败", file=sys.stderr)
            return 3
        check("管理员登录成功", True, args.username)

        await sweep(client)

        # ---- 幂等：没有键时，队列里已有同一批次的任务就不再排第二条 --------------------
        batch_a = await import_probe(client, f"{suffix}-a", "批次 A")
        if not batch_a:
            return 4
        decoy = plant_job(kind="batch.revert", payload={"batch": batch_a, "ip": "127.0.0.1"},
                          status="queued", attempts=0, max_attempts=3)
        planted_ids.append(decoy)
        status, body = await submit(client, batch_a)
        check(
            "无键提交：队列里已有同批次任务 → 复用那条（deduplicated）",
            status == 202 and body.get("id") == decoy and body.get("deduplicated") is True,
            f"HTTP {status} id={body.get('id')}（埋的是 {decoy}）deduplicated={body.get('deduplicated')}",
        )
        check(
            "复用那条任务的 payload.ip 不参与去重指纹",
            body.get("payload", {}).get("batch") == batch_a,
            f"payload={body.get('payload')}",
        )
        execute("DELETE FROM background_jobs WHERE id = :id", id=decoy)

        # ---- 幂等：带 Idempotency-Key（首次 / 重放 / 成功之后再放） --------------------
        batch_b = await import_probe(client, f"{suffix}-b", "批次 B")
        status, first = await submit(client, batch_b, key)
        job_id = int(first.get("id") or 0)
        check(
            "带键提交：新任务 attempts=0 / max_attempts=3 / next_attempt_at 为空",
            status == 202
            and first.get("deduplicated") is False
            and first.get("idempotency_key") == key
            and first.get("attempts") == 0
            and first.get("max_attempts") == 3
            and first.get("next_attempt_at") is None,
            f"HTTP {status} id={job_id} attempts={first.get('attempts')}/{first.get('max_attempts')}",
        )
        status, replay = await submit(client, batch_b, key)
        check(
            "同一个键紧接着再提交 → 拿回同一条任务",
            status == 202 and replay.get("id") == job_id and replay.get("deduplicated") is True,
            f"HTTP {status} id={replay.get('id')} deduplicated={replay.get('deduplicated')}",
        )

        done = await wait_job(client, job_id)
        result = done.get("result") or {}
        check(
            "任务真的跑完：删掉 3 行并重算了探针期间",
            done.get("status") == "succeeded" and result.get("deleted_rows") == 3 and PROBE_PERIOD in (result.get("recalculated") or []),
            f"status={done.get('status')} deleted_rows={result.get('deleted_rows')} recalculated={result.get('recalculated')}",
        )
        status, after_success = await submit(client, batch_b, key)
        check(
            "批次已经删掉了，同一个键重放仍回那条已成功的任务（不是 404）",
            status == 202
            and after_success.get("id") == job_id
            and after_success.get("status") == "succeeded"
            and after_success.get("deduplicated") is True,
            f"HTTP {status} id={after_success.get('id')} status={after_success.get('status')}",
        )
        status, missing = await submit(client, batch_b, f"{key}-other")
        check(
            "换一个键、批次又已经没了 → 404",
            status == 404 and "批次不存在" in str(missing),
            f"HTTP {status} {str(missing)[:80]}",
        )
        status, too_long = await submit(client, batch_b, "k" * LONG_KEY_LEN)
        check(
            f"{LONG_KEY_LEN} 字符的键 → 422 并点名请求头",
            status == 422 and "Idempotency-Key" in str(too_long),
            f"HTTP {status} {str(too_long)[:90]}",
        )

        # ---- 部分唯一索引：存在、且真的拦得住并发窗口 --------------------------------
        definition = scalar(
            "SELECT indexdef FROM pg_indexes WHERE tablename = 'background_jobs'"
            " AND indexname = 'uq_background_jobs_idempotency_key'"
        )
        check(
            "库里存在部分唯一索引 UQ（只约束 queued/running/succeeded）",
            bool(definition) and "WHERE" in definition and "succeeded" in definition,
            str(definition or "没找到")[:150],
        )
        dup_key = f"verify-job-dup-{suffix}"
        first_dup = plant_job(kind="batch.revert", payload={"batch": batch_a, "ip": "127.0.0.1"},
                              status="queued", attempts=0, max_attempts=3, key=dup_key)
        planted_ids.append(first_dup)
        try:
            plant_job(kind="batch.revert", payload={"batch": batch_a, "ip": "127.0.0.1"},
                      status="queued", attempts=0, max_attempts=3, key=dup_key)
            check("插入第二个同名键的任务被索引拦下", False, "第二条居然插进去了")
        except IntegrityError as exc:
            check("插入第二个同名键的任务被索引拦下", "uq_background_jobs_idempotency_key" in str(exc),
                  str(exc).split("\n")[0][:120])
        # 已失败/已成功的任务不占坑：同一个键现在应当能再用一次
        execute("UPDATE background_jobs SET status = 'failed' WHERE id = :id", id=first_dup)
        reuse = plant_job(kind="batch.revert", payload={"batch": batch_a, "ip": "127.0.0.1"},
                          status="queued", attempts=0, max_attempts=3, key=dup_key)
        check("那条任务失败后，同一个键可以再排一条（部分索引只占活跃状态）", reuse != first_dup, f"新任务 #{reuse}")
        execute("DELETE FROM background_jobs WHERE id IN (:a, :b)", a=first_dup, b=reuse)

        # ---- 人工重试：404 / 409 / 追加预算 / 真机退避重排 ----------------------------
        unknown = await client.post("/api/v1/admin/jobs/999999999/retry")
        check("重试一条不存在的任务 → 404", unknown.status_code == 404 and "没有这条后台任务" in unknown.text,
              f"HTTP {unknown.status_code} {unknown.text[:60]}")
        succeeded_retry = await client.post(f"/api/v1/admin/jobs/{job_id}/retry")
        check(
            "重试一条已成功的任务 → 409（会说清当前状态）",
            succeeded_retry.status_code == 409 and "只有失败的任务" in succeeded_retry.text and "已完成" in succeeded_retry.text,
            f"HTTP {succeeded_retry.status_code} {succeeded_retry.text[:90]}",
        )

        # payload 里**故意不带 batch**：批次的删除逻辑走不下去（KeyError），
        # 于是这次重试会真的失败 —— 正好用来观察真机上的「退避重排」，
        # 而不是被 `allow_missing` 的幂等自愈直接判成功。
        broken = plant_job(
            kind="batch.revert", payload={"ip": "127.0.0.1"}, status="failed",
            attempts=3, max_attempts=3, error="第 3 次尝试失败：批次不存在或已被撤销",
        )
        planted_ids.append(broken)
        retried_raw = await client.post(f"/api/v1/admin/jobs/{broken}/retry")
        status = retried_raw.status_code
        retried = retried_raw.json() if status in (200, 202) else {}
        check(
            "人工重试失败任务 → 202、放回队列、追加预算、保留上次原因",
            status == 202
            and retried.get("status") == "queued"
            and retried.get("attempts") == 3
            and retried.get("max_attempts") == 6
            and "批次不存在或已被撤销" in str(retried.get("error")),
            f"HTTP {status} attempts={retried.get('attempts')}/{retried.get('max_attempts')} error={str(retried.get('error'))[:40]}",
        )

        retried_job = await wait_attempts(client, broken, 4)
        backoff_ok = retried_job.get("status") == "queued" and retried_job.get("next_attempt_at")
        check(
            "真机跑第 4 次尝试仍失败 → 回队列等下一次（attempts 涨到 4）",
            backoff_ok and int(retried_job.get("attempts") or 0) == 4,
            f"status={retried_job.get('status')} attempts={retried_job.get('attempts')} next={retried_job.get('next_attempt_at')}",
        )
        check(
            "失败原因记在任务上（第 4 次尝试失败 + 缺少 batch 键）",
            "第 4 次尝试失败" in str(retried_job.get("error")) and "batch" in str(retried_job.get("error")),
            str(retried_job.get("error"))[:120],
        )
        wait_seconds = None
        if retried_job.get("next_attempt_at"):
            moment = datetime.fromisoformat(str(retried_job["next_attempt_at"]))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            wait_seconds = (moment - datetime.now(UTC)).total_seconds()
        expected = retry_delay(4)
        check(
            f"退避时间落在 2·2^(n−1) 附近（第 4 次 → {expected:.0f} 秒）",
            wait_seconds is not None and expected - 12 <= wait_seconds <= expected + 12,
            f"还剩 {wait_seconds:.1f} 秒" if wait_seconds is not None else "没有 next_attempt_at",
        )

        audit_response = await client.get("/api/v1/admin/audit", params={"action": "job.retry", "limit": 50})
        entries = (audit_response.json().get("data") or []) if audit_response.status_code == 200 else []
        mine = [row for row in entries if row.get("target_id") == str(broken)]
        check(
            "人工重试留了审计（job.retry，带 kind 与上次失败原因）",
            bool(mine) and mine[0]["detail"].get("kind") == "batch.revert" and "批次不存在" in str(mine[0]["detail"].get("previous_error")),
            f"命中 {len(mine)} 条 detail={mine[0]['detail'] if mine else ''}"[:150],
        )
        # 这条任务会一直往后重试（每失败一次就再排一次），清理阶段把它判死
        execute(
            "UPDATE background_jobs SET status = 'failed', next_attempt_at = NULL, finished_at = now() WHERE id = :id",
            id=broken,
        )

        # 批次 A 的行还在（那条「复用」的任务没被执行过），跑一次真撤销清掉
        status, cleanup = await submit(client, batch_a)
        cleaned = await wait_job(client, int(cleanup.get("id") or 0)) if status in (200, 202) else {}
        check(
            "撤掉探针批次 A（不留演示库垃圾）",
            cleaned.get("status") == "succeeded" and (cleaned.get("result") or {}).get("deleted_rows") == 3,
            f"HTTP {status} status={cleaned.get('status')}",
        )

        # ---- 埋一个「进程重启」的现场：跑过一次、还没跑完 ------------------------------
        batch_c = await import_probe(client, f"{suffix}-c", "批次 C")
        planted = plant_job(kind="batch.revert", payload={"batch": batch_c, "ip": "127.0.0.1"},
                            status="running", attempts=1, max_attempts=3, started=True)
        check(
            "埋了一条 running 残骸（等价于上次进程跑到一半被 kill）",
            scalar("SELECT status FROM background_jobs WHERE id = :id", id=planted) == "running",
            f"任务 #{planted} 批次 {batch_c[:12]}…",
        )
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps({"job_id": planted, "batch": batch_c, "suffix": suffix}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n  现场已埋好（{STATE_PATH}）：任务 #{planted}、批次 {batch_c[:12]}…")
        print("  接下来重启后端，然后跑 --phase 2：")
        print("    .\\.venv\\Scripts\\python.exe scripts\\verify_job_retry.py --phase 2\n")

    return 0


async def phase2(args) -> int:
    if not STATE_PATH.exists():
        print(f"FAIL: 没有找到现场文件 {STATE_PATH}，先跑第一阶段", file=sys.stderr)
        return 3
    state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    job_id, batch = int(state["job_id"]), str(state["batch"])

    log_path = Path(args.log)
    log_text = read_text_any(log_path)
    check(
        f"启动日志里有「启动接续」（{log_path.name}）",
        "启动接续" in log_text,
        "已找到重新排队的那行" if "启动接续" in log_text else ("日志是空的" if not log_text else "日志里没有这行"),
    )

    async with httpx.AsyncClient(base_url=args.base_url, timeout=180.0, trust_env=False) as client:
        if not await login(client, args.username, args.password):
            print("FAIL: 管理员登录失败", file=sys.stderr)
            return 3
        check("管理员登录成功", True, args.username)
        job = await wait_job(client, job_id, deadline=args.wait)
        result = job.get("result") or {}
        check(
            "埋下的任务被接续跑完（attempts 从 1 涨到 2、状态 succeeded）",
            job.get("status") == "succeeded" and int(job.get("attempts") or 0) == 2,
            f"status={job.get('status')} attempts={job.get('attempts')}/{job.get('max_attempts')}",
        )
        check(
            "接续那次真的把批次的行删了（deleted_rows=3）",
            result.get("deleted_rows") == 3 and result.get("already_reverted") is None,
            f"result={result}",
        )
        left = scalar("SELECT count(*) FROM financial_transactions WHERE import_batch = :batch", batch=batch)
        check("该批次的行已经清干净", left == 0, f"还剩 {left} 行")
        stale = scalar(
            "SELECT count(*) FROM background_jobs WHERE id = :id AND status IN ('queued', 'running')", id=job_id
        )
        check("这条任务不再挂在排队/运行中", stale == 0)

    passed = sum(1 for ok, _name, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    return 0 if passed == len(RESULTS) else 1


def summarize(code: int) -> int:
    if code:
        return code
    passed = sum(1 for ok, _name, _detail in RESULTS if ok)
    failed = [name for ok, name, _detail in RESULTS if not ok]
    if failed:
        print("失败项：" + "；".join(failed))
    print(f"总用时 {time.monotonic() - START:.1f} 秒")
    return 0 if passed == len(RESULTS) else 1


async def run(args) -> int:
    _console()
    if args.phase == 1:
        return await phase1(args)
    return await phase2(args)


def main() -> int:
    parser = argparse.ArgumentParser(description="真机验证后台任务幂等重试与启动重跑（⑩）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--phase", type=int, default=1, choices=[1, 2], help="1=后端在跑时；2=重启后端之后")
    parser.add_argument("--wait", type=float, default=60.0, help="第二阶段等接续任务跑完的秒数")
    parser.add_argument("--log", default=str(PROJECT_ROOT / "output" / "_backend.log"))
    args = parser.parse_args()
    code = asyncio.run(run(args))
    if args.phase == 2:
        return code
    return summarize(code)


if __name__ == "__main__":
    START = time.monotonic()
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要把原因打人话
        print(f"\n异常终止：{type(exc).__name__}: {exc}")
        print("检查后端是否在 127.0.0.1:8000、数据库容器是否在跑")
        sys.exit(1)
