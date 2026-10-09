"""㉑ 限流与配额的计数落库 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_rate_limit_store.py

为什么必须有这个脚本：接口用例能证明「打满之后返回 429」，但证明不了 ㉑ 真正要解决的那件事 ——
**这本账到底是谁在记**。进程内的 `deque` 也能让用例全绿，然后在多 worker 下变成
「每个 worker 各 10 次」、重启一次就清零。所以这里只用真机看得见的事实来判：

- `rate_limit_hits` 表与 `(bucket, hit_at)` 复合索引真在库里，启动体检不点它的名；
- **两个真 Python 子进程**（各自全新解释器、不共享任何内存）看的是同一本账：
  子进程 A 写下一行，父进程立刻数得到；父进程把这个桶填满，子进程 B 立刻被判拒；
- **脚本进程 ↔ uvicorn 进程**共享同一本账：脚本直接往库里填满某个登录桶，
  后端进程的下一次登录立刻 429 —— 这就是「多 worker 不再各记一份」的等价证据；
  反过来把库里的行删掉，同一个请求立刻恢复 401：判定读的是库，不是进程内存；
- 真机 HTTP 打满导出配额（默认 30 次 / 60 秒）→ 429 带 `Retry-After` → **等窗口滑过去**同一个请求 200；
  以及运维救急那条路：`purge` 掉计数后立刻 200（不必等一分钟）；
- 被拒的尝试**不落库**（拒绝不延长封锁，这是 `decide()` 的既有语义）；
- 两类 429 在审计上**有意不一样**：登录被限流留一条 `user.login_blocked`（「有人在打这个
  账号」的证据，`backend/auth.py:129`）；导出/问答的出口配额一个字都不写
  （`backend/limits.py:12`：写审计反而成了「用便宜请求灌满审计表」的放大器）；
- `/system/status` 的 `rate_limits` 段如实说「共享、跨重启」，且**只报聚合桶数**：
  分桶键里有用户名与来源 IP，普通账号也能看这个接口，不该把别人的摊位摆出来。

刻意不碰演示账号的数据：脚本只在自己注册的临时账号（`verify_ratelimit_*`）上试，
被限流的都是它们，结尾把临时账号与临时桶清干净 —— 最后一项就是拿「演示账号的
失败次数与会话版本没被动过、还能正常登录」当断言。脚本开头也会清掉上次崩溃留下的现场，可反复跑。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend import locks, ratelimit  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.db import engine  # noqa: E402
from backend.migrations import missing_columns, missing_indexes  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "verify-ratelimit-2026"
PREFIX = "verify_ratelimit_"
OUT_DIR = ROOT / "output"
TABLE = "rate_limit_hits"
INDEX = "ix_rate_limit_hits_bucket_hit_at"
RESULTS: list[tuple[str, bool, str]] = []
SETTINGS = get_settings()


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    """发一次真请求，返回 (状态码, 解析后的正文, 响应头小写字典)。"""
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=120) as response:
            status, payload, headers = response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        status, payload, headers = exc.code, exc.read(), dict(exc.headers)
    raw = payload.decode("utf-8", "replace")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw
    return status, parsed, {key.lower(): value for key, value in headers.items()}


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def hits_of(bucket: str) -> int:
    return int(scalar(f"SELECT count(*) FROM {TABLE} WHERE bucket = :b", b=bucket) or 0)


def login(username: str, password: str = PASSWORD):
    return call("POST", "/auth/login", body={"username": username, "password": password})


def register(username: str) -> None:
    status, body, _headers = call("POST", "/auth/register",
                                  body={"username": username, "password": PASSWORD})
    assert status == 200, body


def token_of(username: str) -> str:
    status, body, _headers = login(username)
    assert status == 200, body
    return body["access_token"]


def account_row(username: str) -> dict:
    found = rows("SELECT id, failed_attempts, locked_until, token_version FROM users"
                 " WHERE username = :u", u=username)
    if not found:
        return {}
    return {"id": found[0][0], "failed_attempts": found[0][1], "locked_until": found[0][2],
            "token_version": found[0][3]}


def fill_bucket(bucket: str, count: int) -> None:
    """直接往库里写 count 条「刚刚的尝试」：等价于这个桶被打了 count 次。

    用 SQL 而不是循环调 `consume()` 是为了让窗口压在一秒内 —— 否则 30 次导出请求
    会让「最早那次」老掉几秒，`Retry-After` 的断言就得跟着放宽，测出来的东西也变模糊。
    """
    with engine.begin() as conn:
        for _ in range(count):
            conn.execute(text(f"INSERT INTO {TABLE} (bucket, hit_at) VALUES (:b, now())"),
                         {"b": bucket})


def sweep() -> None:
    """清掉上次崩溃留下的临时账号与临时桶（直接删库行；历史审计行不动）。"""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    for name in ratelimit.NAMES:
        ratelimit.purge(bucket_prefix=f"{name}:{PREFIX}")


def child_consume(bucket: str, out: Path) -> int:
    """子进程模式：对指定桶消费一次，把判定写到文件里（stdout 留给父进程重定向，不抢管道）。"""
    name, _, key = bucket.partition(":")
    decision = ratelimit.consume(name, key)
    out.write_text(json.dumps({"allowed": decision.allowed, "remaining": decision.remaining,
                               "retry_after": decision.retry_after}, ensure_ascii=False),
                   encoding="utf-8")
    return 0


def spawn_consume(bucket: str, tag: str) -> tuple[dict, int, Path]:
    """起一个真 Python 子进程消费一次，读回它的判定。"""
    out = OUT_DIR / f"_ratelimit_child_{tag}.txt"
    out.unlink(missing_ok=True)
    log = OUT_DIR / f"_ratelimit_child_{tag}.log"
    with log.open("w", encoding="utf-8") as handle:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--consume-once", bucket,
             "--out", str(out)],
            cwd=str(ROOT), stdout=handle, stderr=subprocess.STDOUT,
        )
    code = process.wait(timeout=120)
    if not out.exists():
        raise RuntimeError(f"子进程没有写出判定：{log.read_text(encoding='utf-8', errors='replace')}")
    return json.loads(out.read_text(encoding="utf-8")), code, log


def main() -> int:
    sweep()
    status, body, _headers = login(*ADMIN)
    assert status == 200, body
    admin_token = body["access_token"]
    demo_before = account_row(ADMIN[0])

    # ---------------------------------------------------------- 结构：表与索引真在库里
    columns = {row[0]: row for row in rows(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns"
        " WHERE table_name = :t", t=TABLE)}
    check(f"真库有 {TABLE} 表，三列齐且 bucket/hit_at 都是 NOT NULL",
          set(columns) == {"id", "bucket", "hit_at"}
          and columns["bucket"][2] == "NO" and columns["hit_at"][2] == "NO",
          f"列={sorted(columns)}")

    index_def = scalar("SELECT indexdef FROM pg_indexes WHERE tablename = :t AND indexname = :i",
                       t=TABLE, i=INDEX)
    check("(bucket, hit_at) 复合索引在库里（每次判定都按它裁窗口、取最早那条）",
          bool(index_def) and "bucket" in index_def and "hit_at" in index_def, index_def or "缺索引")

    column_gaps = [gap for gap in missing_columns() if TABLE in gap]
    index_gaps = [gap for gap in missing_indexes() if TABLE in gap]
    check("启动体检（migrations）不会点这张表的名：列与索引都对得上",
          not column_gaps and not index_gaps, f"列缺口={column_gaps} 索引缺口={index_gaps}")

    # ---------------------------------------------------------- 计数就是表里的行
    key = f"{PREFIX}probe_{uuid.uuid4().hex[:8]}@10.9.9.9"
    bucket = f"login:{key}"
    ratelimit.purge(bucket_prefix="login:" + PREFIX)
    allowed = [ratelimit.consume("login", key).allowed for _ in range(3)]
    check("三次尝试 = 表里三行（计的不是一个数字，是一次次尝试的时间戳）",
          allowed == [True, True, True] and hits_of(bucket) == 3,
          f"allowed={allowed}，表里 {hits_of(bucket)} 行")

    snap = ratelimit.snapshot(prefix=bucket)
    check("snapshot 能读出桶的条数与最早/最晚尝试（排障入口：谁在被限、还差多久）",
          len(snap) == 1 and snap[0]["hits"] == 3
          and snap[0]["oldest_seconds_ago"] < 30 and snap[0]["newest_seconds_ago"] < 30,
          f"snapshot={snap}")

    # 本进程内改小上限来验证「满 / 拒 / 不落库」这条链（只影响本进程，不碰服务端配置）
    ratelimit.install_quota("login", 2, 60)
    try:
        key_small = f"{PREFIX}small_{uuid.uuid4().hex[:6]}@10.9.9.9"
        bucket_small = f"login:{key_small}"
        first = ratelimit.consume("login", key_small)
        second = ratelimit.consume("login", key_small)
        third = ratelimit.consume("login", key_small)
        check("窗口里攒够上限就拒绝，并给出等多久（按最早那条滑出窗口算）",
              first.allowed and second.allowed and not third.allowed
              and 1 <= third.retry_after <= 60 and third.remaining == 0,
              f"三次判定={[first.allowed, second.allowed, third.allowed]}，"
              f"retry_after={third.retry_after}")
        check("被拒的那次**不落库**：窗口里仍是那两次尝试（拒绝不延长封锁）",
              hits_of(bucket_small) == 2, f"表里 {hits_of(bucket_small)} 行")
        check("到了上限仍不越界：再打两次还是拒绝，行数不动",
              not ratelimit.consume("login", key_small).allowed
              and not ratelimit.consume("login", key_small).allowed and hits_of(bucket_small) == 2,
              f"表里 {hits_of(bucket_small)} 行")
        check("reset() 只清这一个桶，并把删掉的行数报出来",
              ratelimit.reset("login", key_small) == 2 and hits_of(bucket_small) == 0
              and hits_of(bucket) == 3, f"probe 桶仍在（{hits_of(bucket)} 行）")
    finally:
        ratelimit.reset_quotas()

    # ---------------------------------------------------------- 清扫与哈希
    stale = f"login:{PREFIX}stale_{uuid.uuid4().hex[:6]}@10.9.9.9"
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {TABLE} (bucket, hit_at)"
                          " VALUES (:b, now() - interval '2 hours')"), {"b": stale})
    removed = ratelimit.purge_expired()
    check("purge_expired() 只删一小时前的行（窗口最长一小时，更早的一定是死的）",
          hits_of(stale) == 0 and removed >= 1, f"删除 {removed} 条")

    sample = f"login:{PREFIX}x@10.9.9.9"
    check("桶名 → advisory lock key：稳定、落在有符号 64 位、与期间锁不撞号",
          ratelimit.bucket_lock_key(sample) == ratelimit.bucket_lock_key(sample)
          and -(2**63) <= ratelimit.bucket_lock_key(sample) < 2**63
          and ratelimit.bucket_lock_key(sample) != locks.lock_key(sample),
          f"key={ratelimit.bucket_lock_key(sample)}，期间锁={locks.lock_key(sample)}")

    # ---------------------------------------------------------- 两个真子进程共享同一本账
    cross_key = f"{PREFIX}cross_{uuid.uuid4().hex[:6]}@10.9.9.9"
    cross_bucket = f"login:{cross_key}"
    first_child, code_a, log_a = spawn_consume(cross_bucket, "a")
    check("子进程 A（全新解释器）的第一次尝试被允许，且它写下的那行父进程立刻数得到",
          code_a == 0 and first_child["allowed"] and hits_of(cross_bucket) == 1,
          f"子进程判定={first_child}，父进程读到 {hits_of(cross_bucket)} 行")

    limit, window = ratelimit.quota("login")
    fill_bucket(cross_bucket, limit - 1)
    second_child, code_b, log_b = spawn_consume(cross_bucket, "b")
    check("父进程把这个桶填满后，子进程 B 立刻被判拒（进程内计数在这里一定是放行的）",
          code_b == 0 and second_child["allowed"] is False
          and 1 <= second_child["retry_after"] <= window,
          f"子进程 B 判定={second_child}，窗口={window:g}s")
    check("子进程 B 的拒绝也没有落库：表里就是 limit 行",
          hits_of(cross_bucket) == limit and second_child["remaining"] == 0,
          f"表里 {hits_of(cross_bucket)} 行 / limit={limit}")
    ratelimit.purge(bucket_prefix=cross_bucket)
    third_child, _code_c, _log_c = spawn_consume(cross_bucket, "c")
    check("清掉计数后同一个子进程立刻又能过：判定读的是库，不是任何进程的内存",
          third_child["allowed"] is True and hits_of(cross_bucket) == 1,
          f"子进程判定={third_child}")

    # ---------------------------------------------------------- 脚本进程 ↔ 后端进程
    http_user = f"{PREFIX}http_{uuid.uuid4().hex[:6]}"
    register(http_user)
    status, _body, _headers = login(http_user, "wrong-password-2026")
    assert status == 401, _body
    http_snap = ratelimit.snapshot(prefix=f"login:{http_user}@")
    check("真 HTTP 登录在库里留下一行，桶名形状是 login:用户名@来源IP",
          len(http_snap) == 1 and http_snap[0]["hits"] == 1
          and http_snap[0]["bucket"] == f"login:{http_user}@127.0.0.1",
          f"桶={[row['bucket'] for row in http_snap]}")
    http_bucket = http_snap[0]["bucket"]
    fill_bucket(http_bucket, limit)
    # 填满之后这个桶里是 limit + 1 行（前面那次真 HTTP 登录自己写了一行），所以不能拿它跟
    # limit 比大小 —— 要看的是「被拒的这一下有没有再写一行」：拒绝不是一次尝试。
    before_blocked = hits_of(http_bucket)
    blocked = login(http_user, "wrong-password-2026")
    check("脚本进程把桶填满后，**后端进程**的下一次登录立刻 429（多 worker 不再各记一份）",
          blocked[0] == 429 and int(blocked[2].get("retry-after", 0)) >= 1
          and "重试" in str(blocked[1].get("detail")),
          f"status={blocked[0]}，Retry-After={blocked[2].get('retry-after')}")
    check("被拒的这一次也没往桶里补行：拒绝本身不算一次尝试（否则越被拒锁得越久）",
          hits_of(http_bucket) == before_blocked,
          f"被拒前 {before_blocked} 行、被拒后 {hits_of(http_bucket)} 行")
    ratelimit.purge(bucket_prefix=http_bucket)
    recovered = login(http_user, "wrong-password-2026")
    check("把库里的计数清掉之后，同一个请求立刻恢复 401（运维救急路径也是这条路）",
          recovered[0] == 401, f"status={recovered[0]}")

    # 登录的 429 与出口配额的 429 在审计上是**有意不一样**的两件事：
    # 登录被限流是「有人在打这个账号」的证据，必须留痕（user.login_blocked，auth.py:129）；
    # 导出/问答的配额是便宜请求的粗筛，写审计反而成了「用便宜请求灌满审计表」的放大器
    # （limits.py:12 的注释），所以那里一个字都不写。
    login_blocked_rows = int(scalar("SELECT count(*) FROM audit_logs"
                                   " WHERE actor = :u AND action = 'user.login_blocked'",
                                   u=http_user) or 0)
    check("登录被限流**留痕**：429 那次在审计里是一条 user.login_blocked（打账号的证据）",
          login_blocked_rows == 1, f"该账号的 user.login_blocked 审计 {login_blocked_rows} 条")

    # ---------------------------------------------------------- /system/status 的口径
    status, status_body, _headers = call("GET", "/system/status", admin_token)
    rate_limits = status_body.get("rate_limits") if isinstance(status_body, dict) else None
    expected_quotas = {
        "login": {"limit": SETTINGS.login_max_attempts,
                  "window_seconds": float(SETTINGS.login_window_seconds)},
        "export": {"limit": SETTINGS.export_max_per_minute, "window_seconds": 60.0},
        "ai": {"limit": SETTINGS.ai_max_per_minute, "window_seconds": 60.0},
    }
    check("/system/status 有 rate_limits 段：计数存在哪、三组配额的上限与窗口",
          status == 200 and isinstance(rate_limits, dict)
          and rate_limits.get("store") == f"postgres:{TABLE}"
          and rate_limits.get("shared_across_processes") is True
          and rate_limits.get("survives_restart") is True
          and rate_limits.get("quotas") == expected_quotas,
          f"rate_limits={rate_limits}")
    check("这一段只给**聚合桶数**，不给分桶名（普通账号也能看这个接口，不能摆别人的摊位）",
          isinstance(rate_limits.get("buckets"), int) and rate_limits["buckets"] >= 1
          and set(rate_limits) == {"store", "shared_across_processes", "survives_restart",
                                   "quotas", "buckets"},
          f"buckets={rate_limits.get('buckets')}，键={sorted(rate_limits)}")
    plain_user = f"{PREFIX}plain_{uuid.uuid4().hex[:6]}"
    register(plain_user)
    plain_token = token_of(plain_user)
    plain_status, plain_body, _headers = call("GET", "/system/status", plain_token)
    check("普通账号也能看到这个段（它只是口径说明，不是管理信息）",
          plain_status == 200 and isinstance(plain_body.get("rate_limits"), dict)
          and "login:" not in json.dumps(plain_body.get("rate_limits") or {}, ensure_ascii=False),
          f"status={plain_status}，buckets={plain_body.get('rate_limits', {}).get('buckets')}")

    # ---------------------------------------------------------- 真机导出配额打满与恢复
    export_user = f"{PREFIX}export_{uuid.uuid4().hex[:6]}"
    register(export_user)
    export_token = token_of(export_user)
    export_limit, export_window = ratelimit.quota("export")
    codes = [call("GET", "/export/metrics", export_token)[0] for _ in range(export_limit)]
    check(f"真机 HTTP 导出 {export_limit} 次全部 200（配额默认 {export_limit} 次 / {export_window:g} 秒）",
          codes == [200] * export_limit,
          f"状态码分布={ {code: codes.count(code) for code in set(codes)} }")

    export_bucket = f"export:{export_user}"
    check("这 30 次导出在库里是 30 行（导出的账也在同一张表里）",
          hits_of(export_bucket) == export_limit, f"表里 {hits_of(export_bucket)} 行")

    blocked_export = call("GET", "/export/metrics", export_token)
    retry_after = int(blocked_export[2].get("retry-after", 0) or 0)
    check("第 N+1 次导出 429 + Retry-After，文案说明是导出配额（不是账号问题）",
          blocked_export[0] == 429 and 1 <= retry_after <= int(export_window)
          and "导出过于频繁" in str(blocked_export[1].get("detail")),
          f"status={blocked_export[0]}，Retry-After={retry_after}")
    check("被拒的这次导出也没有落库：仍是 30 行",
          hits_of(export_bucket) == export_limit, f"表里 {hits_of(export_bucket)} 行")

    export_audit = int(scalar("SELECT count(*) FROM audit_logs"
                              " WHERE actor = :u AND (action LIKE '%blocked%'"
                              " OR action LIKE '%quota%' OR action LIKE '%limit%')",
                              u=export_user) or 0)
    check("出口配额的 429 **不写审计**：便宜请求不该被拿来放大审计表（登录那条是另一码事）",
          export_audit == 0, f"该账号的限流类审计 {export_audit} 条")

    export_snap = ratelimit.snapshot(prefix=export_bucket)
    check("最早的尝试还在半个窗口内：说明记的是逐次尝试（真滑动窗口，不是「一个计数 + 固定边界」）",
          len(export_snap) == 1 and export_snap[0]["oldest_seconds_ago"] < export_window / 2
          and export_snap[0]["newest_seconds_ago"] < export_window / 2,
          f"snapshot={export_snap}")

    other_user = f"{PREFIX}other_{uuid.uuid4().hex[:6]}"
    register(other_user)
    other_token = token_of(other_user)
    check("配额按账号分桶：另一个账号此刻导出照样 200（一本账不等于一荣俱荣）",
          call("GET", "/export/metrics", other_token)[0] == 200, f"账号={other_user}")

    rescue = ratelimit.purge(bucket_prefix=export_bucket)
    check("运维救急：清掉这个桶之后**不用等窗口**，同一个请求立刻 200",
          rescue >= export_limit and call("GET", "/export/metrics", export_token)[0] == 200,
          f"清理 {rescue} 行")

    fill_bucket(export_bucket, export_limit)
    blocked_again = call("GET", "/export/metrics", export_token)
    wait_for = int(blocked_again[2].get("retry-after", 0) or 0)
    print(f"      （等窗口滑过去：Retry-After={wait_for} 秒，这一项是唯一要等的地方）", flush=True)
    time.sleep(wait_for + 2)
    after_wait = call("GET", "/export/metrics", export_token)
    check("等过 Retry-After 之后同一个请求恢复 200：窗口是滑动的，没人需要重启服务",
          blocked_again[0] == 429 and after_wait[0] == 200,
          f"等待前={blocked_again[0]}，等 {wait_for + 2}s 后={after_wait[0]}")

    # ---------------------------------------------------------- 收尾：临时桶与临时账号清干净
    removed_rows = 0
    for name in ratelimit.NAMES:
        removed_rows += ratelimit.purge(bucket_prefix=f"{name}:{PREFIX}")
    left_buckets = sum(ratelimit.tracked_buckets(prefix=f"{name}:{PREFIX}")
                       for name in ratelimit.NAMES)
    check("临时桶清干净（残留的计数会变成下一轮的「已经用掉几次」）",
          left_buckets == 0, f"清理 {removed_rows} 行，残留桶 {left_buckets} 个")

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    left_users = int(scalar("SELECT count(*) FROM users WHERE username LIKE :p",
                            p=f"{PREFIX}%") or 0)
    check("临时账号全部删掉", left_users == 0, f"残留 {left_users} 个")

    demo_after = account_row(ADMIN[0])
    still_ok = login(*ADMIN)
    check("演示账号没被本脚本碰过：失败次数与会话版本一字未变，且仍能登录",
          demo_after == demo_before and still_ok[0] == 200,
          f"之前={demo_before} 之后={demo_after} 登录={still_ok[0]}")

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="㉑ 限流计数落库真机冒烟")
    parser.add_argument("--consume-once", metavar="BUCKET", help="子进程模式：对指定桶消费一次")
    parser.add_argument("--out", help="子进程模式：把判定写到这里")
    args = parser.parse_args()
    if args.consume_once:
        raise SystemExit(child_consume(args.consume_once, Path(args.out)))
    raise SystemExit(main())
