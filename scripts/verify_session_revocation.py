"""③ 会话版本号 + 强制下线 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_session_revocation.py

为什么必须有这个脚本：接口用例只能证明「`/auth/logout` 把 `users.token_version` 加了 1」，
证明不了「**已经发出去的那份令牌**真的会被拒」。这里每一步都用真 HTTP + 真库：

- 拿到的令牌再发一次请求 → 200；退出登录之后拿同一份令牌再发 → 401 且理由说得清；
- 手搓一份**没有 `ver` 声明**的令牌（等于升级前签发的老令牌）：版本为 0 时能用，
  被作废之后立刻不能用（这就是「升级不会把所有人踢下线，但下线一定立刻生效」）；
- 管理员强制下线别人的账号时，目标掉线而管理员自己不受影响；
- 审计里留痕，且**不许**把令牌相关字段写进日志（`audit._scrub` 那条「含 token 的键整条丢掉」）。

刻意不去登出演示账号 `demo_analyst`：本脚本只让两个临时账号（`verify_session_a_*` /
`verify_session_b_*`）来回下线，结尾把它们删掉，所以演示库的会话版本不会被动过——
最后一项检查就是拿这个当断言。脚本开头也会清掉上次崩溃留下的临时账号，可以反复跑。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from jose import jwt as jose_jwt  # noqa: E402
from sqlalchemy import select, text  # noqa: E402

from backend.auth import ALGORITHM, SECRET_KEY, token_for, token_version_of  # noqa: E402
from backend.db import SessionLocal, engine  # noqa: E402
from backend.models import User  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "verify-session-2026"
PREFIX = "verify_session_"
RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=60) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    raw = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def scalar(sql: str, **params):
    with engine.connect() as conn:
        return conn.execute(text(sql), params).scalar()


def rows(sql: str, **params) -> list[tuple]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def login(username: str, password: str = PASSWORD) -> tuple[str, int]:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"], payload["token_version"]


def register(username: str) -> int:
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    return payload["id"]


def claims(token: str) -> dict:
    return jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def legacy_token(user_id: int, username: str) -> str:
    """老令牌：声明里只有 sub/username/exp —— 没有 `ver`（本功能上线前签发的就是长这样）。"""
    expires = datetime.now(timezone.utc) + timedelta(minutes=30)
    return jose_jwt.encode({"sub": str(user_id), "username": username, "exp": expires},
                           SECRET_KEY, algorithm=ALGORITHM)


def me(token: str):
    return call("GET", "/auth/me", token)


def sweep() -> None:
    """清掉上次崩溃留下的临时账号（直接删库行；它们留下的审计行属于历史，不动）。"""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{PREFIX}%"})


def main() -> int:
    sweep()
    admin_token, demo_version = login(*ADMIN)
    status, admin_me = me(admin_token)
    assert status == 200, admin_me

    # ---------------------------------------------------------- 结构闸门（真库 + 真模块）
    column = rows("SELECT data_type, is_nullable, column_default FROM information_schema.columns "
                  "WHERE table_name = 'users' AND column_name = 'token_version'")
    check("真库的 users.token_version 存在、NOT NULL 且默认 0（老行不会因为补列当场全员下线）",
          column and column[0][0] == "integer" and column[0][1] == "NO" and str(column[0][2]) == "0",
          f"{column}")

    # ---------------------------------------------------------- 临时账号 A：自己退出登录
    name_a = f"{PREFIX}a_{uuid.uuid4().hex[:8]}"
    id_a = register(name_a)
    with SessionLocal() as session:
        user_a = session.scalar(select(User).where(User.id == id_a))
        minted = token_for(user_a)
        version_a = token_version_of(user_a)
    check("服务端签发的令牌里带着会话版本号 ver，且等于库里那行",
          claims(minted).get("ver") == version_a == 0,
          f"ver={claims(minted).get('ver')}，库里的 token_version={version_a}")

    legacy_a = legacy_token(id_a, name_a)
    status, _ = me(legacy_a)
    check("版本为 0 时，没有 ver 声明的老令牌仍然可用（升级不踢人）",
          status == 200, f"GET /auth/me → {status}")

    token_a1, version_a1 = login(name_a)
    token_a2, _version_a2 = login(name_a)
    status, payload = me(token_a1)
    check("登录响应带 token_version，且 /auth/me 与库里一致",
          version_a1 == 0 and status == 200 and payload["token_version"] == 0,
          f"登录返回 {version_a1}，/auth/me 返回 {payload.get('token_version')}")

    status, body = call("POST", "/auth/logout", token_a1)
    check("退出登录返回 200 并把会话版本 +1",
          status == 200 and body.get("token_version") == 1 and body.get("logged_out") is True,
          f"status={status} body={body}")

    for label, stale in (("第一份", token_a1), ("同账号另一台设备的第二份", token_a2)):
        status, payload = me(stale)
        check(f"退出登录后，{label}旧令牌立刻 401 且理由明确",
              status == 401 and "已在别处退出" in str(payload.get("detail")),
              f"status={status} detail={payload.get('detail') if isinstance(payload, dict) else payload}")

    status, payload = me(legacy_a)
    check("没有 ver 的老令牌也被一起作废（版本已经不是 0 了）",
          status == 401 and "已在别处退出" in str(payload.get("detail")), f"status={status}")

    fresh_a, version_fresh = login(name_a)
    status, payload = me(fresh_a)
    check("重新登录拿到新版本（ver=1）的令牌，可以正常用",
          version_fresh == 1 and status == 200 and payload["token_version"] == 1,
          f"ver={version_fresh}，/auth/me → {status}")

    status, payload = call("GET", "/admin/audit?action=user.logout", fresh_a)
    entry = next((row for row in payload.get("data", []) if row.get("target_id") == str(id_a)), None)
    check("审计里留下 user.logout（中文标签 + 会话版本号）",
          status == 200 and entry is not None and entry["action_label"] == "退出登录"
          and entry["detail"].get("session_version") == 1,
          f"status={status} entry={entry}")

    check("审计 detail 里没有任何含 token 的键（避免把令牌写进日志）",
          entry is not None and not any("token" in key.lower() for key in entry["detail"]),
          f"detail 键：{sorted((entry or {}).get('detail', {}))}")

    # ---------------------------------------------------------- 临时账号 B：被管理员强制下线
    name_b = f"{PREFIX}b_{uuid.uuid4().hex[:8]}"
    id_b = register(name_b)
    token_b, version_b = login(name_b)
    status, _ = me(token_b)
    check("被踢之前，账号 B 的令牌可用（会话版本 0）",
          status == 200 and version_b == 0, f"status={status} ver={version_b}")

    status, body = call("POST", f"/admin/users/{id_b}/logout", admin_token)
    check("管理员可以把别人强制下线（会话版本 0 → 1）",
          status == 200 and body.get("token_version") == 1 and body.get("username") == name_b,
          f"status={status} body={body}")

    status_b, payload_b = me(token_b)
    status_admin, _ = me(admin_token)
    check("被踢账号的令牌立刻失效，而管理员自己的令牌不受影响",
          status_b == 401 and "已在别处退出" in str(payload_b.get("detail")) and status_admin == 200,
          f"B → {status_b}，管理员 → {status_admin}")

    again_b, again_version = login(name_b)
    status, _ = me(again_b)
    check("被踢账号重新登录就能继续用（被踢 ≠ 被停用）",
          status == 200 and again_version == 1, f"status={status} ver={again_version}")

    status, payload = call("GET", f"/admin/users?query={name_b}", admin_token)
    listed = (payload.get("data") or [{}])[0] if status == 200 else {}
    check("用户列表带出 token_version：管理页据此显示「已作废 N 次」",
          status == 200 and listed.get("token_version") == 1 and listed.get("username") == name_b,
          f"status={status} listed={listed}")

    status, payload = call("GET", "/admin/audit?action=user.force_logout", admin_token)
    kicked = next((row for row in payload.get("data", []) if row.get("target_id") == str(id_b)), None)
    check("强制下线也留痕：actor 是管理员、target 是被踢账号、detail 记下新版本",
          status == 200 and kicked is not None and kicked["actor"] == ADMIN[0]
          and kicked["action_label"] == "强制下线" and kicked["detail"].get("to_version") == 1,
          f"status={status} entry={kicked}")

    status, payload = call("GET", "/system/status", again_b)
    check("System 设置页读到的 me.token_version 与真库一致（页面显示的会话版本）",
          status == 200 and payload["me"]["token_version"] == 1,
          f"status={status} me={payload.get('me') if isinstance(payload, dict) else payload}")

    # ---------------------------------------------------------- 边界：自己踢自己 / 不存在 / 非管理员
    status, payload = call("POST", f"/admin/users/{admin_me['id']}/logout", admin_token)
    check("管理员踢自己走 /admin 接口会被拒（400，请用 /auth/logout）",
          status == 400 and "自己" in str(payload.get("detail")), f"status={status} {payload}")

    status, payload = call("POST", "/admin/users/99999999/logout", admin_token)
    check("对不存在的账号强制下线 → 404", status == 404, f"status={status} {payload}")

    # 把 B 收窄成部门账号：无部门在这个项目里就等于管理员，不收窄就测不出 403
    status, payload = call("PATCH", "/auth/me", again_b, {"departments": ["018"]})
    assert status == 200 and payload["unrestricted"] is False, payload
    status, payload = call("POST", f"/admin/users/{admin_me['id']}/logout", again_b)
    check("部门受限账号不能强制下线别人（403）",
          status == 403 and "管理员" in str(payload.get("detail")), f"status={status} {payload}")

    # ---------------------------------------------------------- 收尾：清干净
    for user_id, name in ((id_a, name_a), (id_b, name_b)):
        call("DELETE", f"/admin/users/{user_id}", admin_token)
    left = rows("SELECT id, username FROM users WHERE username LIKE :p", p=f"{PREFIX}%")
    demo_now = scalar("SELECT token_version FROM users WHERE username = :u", u=ADMIN[0])
    check("临时账号删干净，且演示账号的会话版本没被动过（本脚本不登出 demo）",
          not left and int(demo_now) == int(demo_version),
          f"残留 {left}，demo 版本 {demo_version} → {demo_now}")

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
