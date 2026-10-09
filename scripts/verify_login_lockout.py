"""④ 账号锁定 + 口令强度 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_login_lockout.py

为什么必须有这个脚本：接口用例只能证明「连续失败会把 `users.failed_attempts` 加一」，
证明不了这几件只有真机才看得见的事：

- **锁上之后口令正确也进不来**（429 而不是 401），且 `Retry-After` 给得出还能等几秒；
- 两道闸的 429 文案**一模一样**：拿它区分不出「账号被锁了」还是「你打太快了」——
  这正是防用户名枚举的那条设计，脚本里两种 429 都造出来对比；
- 锁定**落库**，所以换一个来源 IP、或者重启后端都绕不过去；
  （㉑ 之前这句只对「账号锁定」成立 —— 那一道闸是按「用户名 + 来源」计数的进程内窗口，
  重启就清零、多 worker 各算一份；㉑ 把计数也搬进了 `rate_limit_hits`，这句现在两闸都成立。）
- 静默期之后从 0 重新数（不是「一辈子攒着」），以及解锁之后口令不用改就能立刻登录。

刻意不碰演示账号 `demo_analyst`：本脚本只在自己注册的临时账号（`verify_lockout_*`）上试错，
被锁定、被解锁的都是它们，结尾删干净——最后一项检查就是拿「演示账号的失败次数和会话版本
没被动过」当断言。脚本开头也会清掉上次崩溃留下的临时账号，可以反复跑。
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend import lockout, ratelimit  # noqa: E402
from backend.config import get_settings  # noqa: E402
from backend.db import engine  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "verify-lockout-2026"
PREFIX = "verify_lockout_"
THROTTLE_USER = f"{PREFIX}throttle_{uuid.uuid4().hex[:8]}"
RATE_LIMIT_MESSAGE = re.compile(r"^登录尝试过于频繁，请在 \d+ 秒后重试$")
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
        with OPENER.open(request, timeout=60) as response:
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


def user_row(username: str) -> dict:
    found = rows("SELECT id, failed_attempts, locked_until, last_failed_at, token_version "
                 "FROM users WHERE username = :u", u=username)
    if not found:
        return {}
    return {"id": found[0][0], "failed_attempts": found[0][1], "locked_until": found[0][2],
            "last_failed_at": found[0][3], "token_version": found[0][4]}


def wrong_login(username: str, password: str = "definitely-not-the-password"):
    return call("POST", "/auth/login", body={"username": username, "password": password})


def audit_entries(action: str, username: str, token: str) -> list[dict]:
    status, payload, _headers = call("GET", f"/admin/audit?action={action}&limit=200", token)
    assert status == 200, payload
    target = str(user_row(username).get("id"))
    return [row for row in payload.get("data", [])
            if row.get("actor") == username or row.get("target_id") == target]


def sweep() -> None:
    """清掉上次崩溃留下的临时账号（直接删库行；它们留下的审计行属于历史，不动）。"""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{PREFIX}%"})
    # 顺手把限流桶清空：㉑ 之后计数在库里，而这个脚本跑在**另一个进程**里，
    # 只能这样清掉正在跑的那个后端进程攒下的尝试次数（以前那种进程内 reset 是清不掉的）。
    removed = ratelimit.purge(bucket_prefix="login:")
    print(f"      （清理限流桶：删除 {removed} 条登录尝试记录）", flush=True)


def main() -> int:
    sweep()
    status, body, _headers = call("POST", "/auth/login",
                                  body={"username": ADMIN[0], "password": ADMIN[1]})
    assert status == 200, body
    admin_token = body["access_token"]

    # ---------------------------------------------------------- 结构闸门（真库 + 真模块）
    columns = {row[0]: row for row in rows(
        "SELECT column_name, data_type, is_nullable, column_default FROM information_schema.columns "
        "WHERE table_name = 'users'")}
    ok_columns = (
        columns.get("failed_attempts") and columns["failed_attempts"][1] == "integer"
        and columns["failed_attempts"][2] == "NO" and str(columns["failed_attempts"][3]) == "0"
        and columns.get("locked_until") and columns["locked_until"][2] == "YES"
        and columns.get("last_failed_at") and columns["last_failed_at"][2] == "YES"
    )
    check("真库 users 三列齐了：failed_attempts（NOT NULL 默认 0）、locked_until、last_failed_at",
          ok_columns,
          f"failed_attempts={columns.get('failed_attempts')} locked_until={columns.get('locked_until')}")

    sequence = [lockout.lock_seconds(n) for n in range(11)]
    check("退避公式：阈值前不锁，从阈值起翻倍、到顶封住",
          sequence[:5] == [0, 0, 0, 0, 0] and sequence[5:11] == [60, 120, 240, 480, 900, 900],
          f"threshold={SETTINGS.login_lockout_threshold} 序列={sequence}")

    # ---------------------------------------------------------- 临时账号：连续失败把账号锁上
    name_a = f"{PREFIX}a_{uuid.uuid4().hex[:8]}"
    status, body, _headers = call("POST", "/auth/register",
                                  body={"username": name_a, "password": PASSWORD})
    assert status == 200, body
    id_a = body["id"]

    threshold = int(SETTINGS.login_lockout_threshold)
    below: list[tuple[int, int, str]] = []
    for _ in range(1, threshold):
        status, body, _headers = wrong_login(name_a)
        row = user_row(name_a)
        below.append((status, row["failed_attempts"], str(row["locked_until"])))
    check(f"阈值前的 {threshold - 1} 次失败都是 401「用户名或密码错误」，库里次数一一对上、且不写锁定时间",
          all(status == 401 and attempts == attempt and locked == "None"
              for attempt, (status, attempts, locked) in enumerate(below, start=1)),
          f"逐次 status/次数/locked_until={below}")

    status, body, _headers = wrong_login(name_a)
    row = user_row(name_a)
    check(f"第 {threshold} 次失败把账号锁上：库里次数到 {threshold}、locked_until 落库、剩余锁定时间在 (0, 60] 秒",
          status == 401 and row["failed_attempts"] == threshold and row["locked_until"] is not None
          and 0 < (row["locked_until"] - datetime.now(UTC)).total_seconds() <= 60,
          f"status={status} 次数={row['failed_attempts']} locked_until={row['locked_until']}")

    # ---------------------------------------------------------- 锁上之后：口令正确也进不来
    status, body, headers = call("POST", "/auth/login",
                                 body={"username": name_a, "password": PASSWORD})
    retry_after = headers.get("retry-after")
    detail = body.get("detail") if isinstance(body, dict) else str(body)
    check("锁着的时候**口令正确也进不来**：429 + Retry-After，且不说「账号被锁了」",
          status == 429 and retry_after is not None and 0 < int(retry_after) <= 60
          and RATE_LIMIT_MESSAGE.match(str(detail)),
          f"status={status} Retry-After={retry_after} detail={detail}")
    check("这条 429 不泄露账号是否存在：正文里没有用户名、没有「锁」也没有「密码」",
          name_a not in str(detail) and "锁" not in str(detail) and "密码" not in str(detail),
          f"detail={detail}")

    status, payload, _headers = call("GET", f"/admin/users?query={name_a}", admin_token)
    listed = (payload.get("data") or [{}])[0] if status == 200 else {}
    check("管理页读得到锁定状态：failed_attempts=阈值、locked=true、locked_until 非空",
          status == 200 and listed.get("username") == name_a
          and listed.get("failed_attempts") == threshold and listed.get("locked") is True
          and listed.get("locked_until"),
          f"listed={ {k: listed.get(k) for k in ('username', 'failed_attempts', 'locked', 'locked_until')} }")

    failed = audit_entries("user.login_failed", name_a, admin_token)
    check("审计里正好留下阈值条 user.login_failed，且 detail 里没有口令字段",
          len(failed) == threshold and all("password" not in json.dumps(row["detail"]).lower()
                                           for row in failed),
          f"{len(failed)} 条")
    locked_rows = audit_entries("user.login_locked", name_a, admin_token)
    reasons = sorted(str(row["detail"].get("reason")) for row in locked_rows)
    check("审计里另有「账号锁定」条目：既记下是哪次失败锁上的，也记下锁着时有人还在试",
          len(locked_rows) == 2 and all(row["action_label"] == "账号锁定" for row in locked_rows)
          and any("连续失败达到阈值" in reason for reason in reasons)
          and any("账号已锁定" in reason for reason in reasons),
          f"reasons={reasons}")

    # ---------------------------------------------------------- 管理员提前解锁（趁账号还锁着）
    # 顺序很要紧：先解锁、再验静默期。反过来的话解锁时账号已经被手工清空，`was_locked_for`
    # 会是 0——检查虽然还是「过」，但它就没在验「解除一个真的锁」了。
    version_before = user_row(name_a)["token_version"]
    status, body, _headers = call("POST", f"/admin/users/{id_a}/unlock", admin_token)
    row = user_row(name_a)
    check(f"管理员点「解锁」：把还锁着的账号放开，清掉 {threshold} 次失败记录、锁定时间置空",
          status == 200 and body.get("cleared_attempts") == threshold
          and body.get("locked") is False and body.get("locked_until") is None
          and row["failed_attempts"] == 0 and row["locked_until"] is None,
          f"status={status} body={body}")
    check("解锁不碰会话版本（清失败次数 ≠ 让人掉线）",
          row["token_version"] == version_before,
          f"token_version {version_before} → {row['token_version']}")

    status, body, _headers = call("POST", "/auth/login",
                                  body={"username": name_a, "password": PASSWORD})
    check("解锁后**不改口令**就能立刻登录，且响应说明这次没清掉任何失败记录",
          status == 200 and body.get("failures_cleared") == 0 and body.get("access_token"),
          f"status={status} failures_cleared={body.get('failures_cleared') if isinstance(body, dict) else body}")

    unlocked = audit_entries("user.unlock", name_a, admin_token)
    check("解锁也留痕：actor 是管理员、target 是被解锁的账号，且记下原本还要锁多少秒",
          len(unlocked) == 1 and unlocked[0]["actor"] == ADMIN[0]
          and unlocked[0]["action_label"] == "解除账号锁定"
          and unlocked[0]["detail"].get("cleared_attempts") == threshold
          and int(unlocked[0]["detail"].get("was_locked_for") or 0) > 0,
          f"entry={unlocked[0] if unlocked else None}")

    # ---------------------------------------------------------- 静默期：过了就重新数
    # 用一个干净的临时账号，免得跟上面那批尝试挤同一个限流桶（限流是按「用户名 + 来源」分桶的，
    # 10 次 / 5 分钟；换个用户名就是一个新桶，所以这里天然互不影响）
    name_b = f"{PREFIX}b_{uuid.uuid4().hex[:8]}"
    status, body, _headers = call("POST", "/auth/register",
                                 body={"username": name_b, "password": PASSWORD})
    assert status == 200, body
    id_b = body["id"]
    for _attempt in range(3):
        wrong_login(name_b)
    before = user_row(name_b)["failed_attempts"]
    with engine.begin() as conn:
        conn.execute(text("UPDATE users SET last_failed_at = :t WHERE id = :i"),
                     {"t": datetime.now(UTC)
                      - timedelta(seconds=int(SETTINGS.login_failure_ttl_seconds) + 60), "i": id_b})
    status, _body, _headers = wrong_login(name_b)
    row = user_row(name_b)
    check("静默期（默认 15 分钟）之后重新从 0 数：连错 3 次、隔 16 分钟再错 1 次，账上只算 1 次",
          status == 401 and before == 3 and row["failed_attempts"] == 1 and row["locked_until"] is None,
          f"静默期前={before} 之后 status={status} 次数={row['failed_attempts']}")

    # ---------------------------------------------------------- 口令强度：弱口令注册直接被拒
    weak_cases = [
        (f"{PREFIX}weak_len_{uuid.uuid4().hex[:6]}", "short123", "长度不够"),
        (f"{PREFIX}weak_word_{uuid.uuid4().hex[:6]}", "passw0rd!2026x", "含弱口令词（l33t 也要还原）"),
        (f"{PREFIX}weak_kbd_{uuid.uuid4().hex[:6]}", "qwertyuiop12", "键盘顺序"),
    ]
    weak_results = []
    for username, password, label in weak_cases:
        status, body, _headers = call("POST", "/auth/register",
                                      body={"username": username, "password": password})
        exists = scalar("SELECT count(*) FROM users WHERE username = :u", u=username)
        weak_results.append((label, status, exists, str(body.get("detail") if isinstance(body, dict) else body)))
    check("三种弱口令注册全部 400，而且**一个账号都没建**（不占坑）",
          all(status == 400 and exists == 0 and detail for _label, status, exists, detail in weak_results),
          f"{[(label, status) for label, status, _e, _d in weak_results]}")

    status, _payload, _headers = call("GET", "/admin/audit?action=user.register_rejected&limit=50",
                                      admin_token)
    rejected = [row for row in _payload.get("data", [])
                if row.get("actor", "").startswith(f"{PREFIX}weak_")]
    check("弱口令注册被拒在审计里留痕（「注册被拒（弱口令）」，附中文原因）",
          status == 200 and len(rejected) >= len(weak_cases)
          and all(row["action_label"] == "注册被拒（弱口令）" and row["detail"].get("problems")
                  for row in rejected),
          f"{len(rejected)} 条，示例原因={rejected[0]['detail'].get('problems') if rejected else None}")

    # ---------------------------------------------------------- 两道闸的分工：限流 vs 账号锁定
    throttle_429 = None
    for _attempt in range(int(SETTINGS.login_max_attempts) + 1):
        status, body, headers = wrong_login(THROTTLE_USER)
        if status == 429:
            throttle_429 = (body, headers)
            break
    detail = throttle_429[0].get("detail") if throttle_429 else None
    blocked_rows = audit_entries("user.login_blocked", THROTTLE_USER, admin_token)
    check("换个不存在的用户名猛打：先 401，打满次数后变成限流 429（审计是 user.login_blocked）",
          throttle_429 is not None and blocked_rows
          and blocked_rows[-1]["action_label"] == "登录被限流",
          f"detail={detail} 审计条数={len(blocked_rows)}")
    check("限流的 429 与被锁的 429 文案一模一样（分不出账号存不存在）",
          detail is not None and RATE_LIMIT_MESSAGE.match(str(detail))
          and not user_row(THROTTLE_USER),
          f"detail={detail}，该用户名在库里无行={not user_row(THROTTLE_USER)}")

    # ---------------------------------------------------------- 收尾：清干净，演示账号没被碰过
    for user_id in (id_a, id_b):
        call("DELETE", f"/admin/users/{user_id}", admin_token)
    left = rows("SELECT id, username FROM users WHERE username LIKE :p", p=f"{PREFIX}%")
    demo = user_row(ADMIN[0])
    check("临时账号删干净，且演示账号的失败次数与锁定时间都没被动过（本脚本从不碰 demo）",
          not left and int(demo.get("failed_attempts") or 0) == 0 and demo.get("locked_until") is None,
          f"残留={left} demo failed_attempts={demo.get('failed_attempts')} locked_until={demo.get('locked_until')}")

    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、数据库容器是否在跑", flush=True)
        sys.exit(1)
