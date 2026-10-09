"""⑳ 管理员标识与数据范围分开 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000；⑳ 的迁移会在启动时补列并按「原先不受限 = 管理员」回填），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_admin_identity.py

为什么必须有这个脚本：接口用例跑在测试库里、管理员还是用例自己造出来的，证明不了真机上这四件事 ——

1. **升级不会把演示账号关在门外**：迁移把「原先不受部门限制」的老账号回填成管理员，
   `demo_analyst` 升级当天照旧能进管理端；
2. **自助扩权这条路真的堵死了**：现场注册一个账号，四种写法（含 `{"departments": []}` 与
   `{"is_admin": true}`）全部 403、库里那一行一个字段都没变、审计里留下 `user.department_self_denied`；
3. **标识与范围是两件事**：只发 `{"is_admin": true}` 不许顺手清掉对方的部门；被收窄到某个部门的
   管理员照样进得了管理端（但只看得见那一本账）；收回标识也不动范围；
4. **未分配部门 ≠ 管理员**：新账号默认什么都看不到，把部门清空也升不了权（⑳ 修掉的正是这条）。

刻意不去登出演示账号，也不动它的部门与标识（只在最后确认它仍是管理员、`token_version` 没变）；
临时账号都叫 `verify_admin_*`，跑前跑后各清一次，可以反复跑。
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text  # noqa: E402

from backend.db import engine  # noqa: E402

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "verify-scope-2026"  # 不能带「admin」：密码强度校验把它当值得猜的片段
PREFIX = "verify_admin_"
GRANT_SCRIPT = ROOT / "scripts" / "grant_admin.py"
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


def login(username: str, password: str = PASSWORD) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def me(token: str) -> dict:
    status, payload = call("GET", "/auth/me", token=token)
    assert status == 200, f"读 /auth/me 失败：{status} {payload}"
    return payload


def register(label: str) -> tuple[int, str, str]:
    """注册一个临时账号，返回 (id, username, token)。"""
    username = f"{PREFIX}{label}_{uuid.uuid4().hex[:6]}"
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    return payload["id"], username, login(username)


def transactions(token: str, **params) -> tuple[int, dict]:
    query = urllib.parse.urlencode({"limit": 5, **params})
    return call("GET", f"/financial/transactions?{query}", token=token)


def grant_admin(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 —— 命令与参数都是本仓库内的常量
        [sys.executable, str(GRANT_SCRIPT), *args],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def sweep() -> None:
    """清掉上次崩溃留下的临时账号（直接删库行；它们留下的审计行属于历史，不动）。"""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE username LIKE :p"), {"p": f"{PREFIX}%"})


def main() -> int:  # noqa: PLR0915 —— 冒烟脚本就是把一条条现场检查排开
    sweep()
    admin_token = login(*ADMIN)
    admin_me = me(admin_token)

    # ---------------------------------------------------------- 升级当天：演示账号仍是管理员
    check("迁移补出了 users.is_admin 列",
          scalar("SELECT count(*) FROM information_schema.columns "
                 "WHERE table_name = 'users' AND column_name = 'is_admin'") == 1)
    check("回填生效：演示账号 is_admin = true（原先不受部门限制 = 管理员）",
          scalar("SELECT is_admin FROM users WHERE username = :u", u=ADMIN[0]) is True
          and scalar("SELECT count(*) FROM users WHERE is_admin") >= 1,
          f"库里管理员 {scalar('SELECT count(*) FROM users WHERE is_admin')} 个")
    check("GET /auth/me 带出 is_admin 与 unrestricted（两个字段不能互相推）",
          admin_me.get("is_admin") is True and admin_me.get("unrestricted") is True,
          f"is_admin={admin_me.get('is_admin')} unrestricted={admin_me.get('unrestricted')}")
    status, payload = call("GET", "/system/status", admin_token)
    check("System 设置页读到的 me.is_admin = true（页面据此显示是否管理员）",
          status == 200 and payload["me"].get("is_admin") is True
          and payload["me"].get("unrestricted") is True,
          f"status={status} me.is_admin={payload['me'].get('is_admin') if status == 200 else payload}")

    # ---------------------------------------------------------- 新账号：非管理员 + 什么都看不到
    user_id, username, token = register("plain")
    status, payload = call("POST", "/auth/register", body={"username": f"{PREFIX}probe_x", "password": PASSWORD})
    check("注册只回 id/username（不再顺带发令牌、也不回部门）",
          status == 200 and set(payload) == {"id", "username"}, f"status={status} keys={sorted(payload)}")
    call("DELETE", f"/admin/users/{payload['id']}", admin_token)  # 这个只为看响应结构，马上删掉

    plain_me = me(token)
    check("新注册账号默认不是管理员（注册即管理员是 ⑳ 之前的毛病）",
          plain_me["is_admin"] is False, f"is_admin={plain_me['is_admin']}")
    check("新注册账号默认没有数据范围：unrestricted = False 且部门为空（**不是**全公司）",
          plain_me["unrestricted"] is False and plain_me["departments"] == []
          and plain_me["department"] is None,
          f"unrestricted={plain_me['unrestricted']} departments={plain_me['departments']}")
    status, listed = transactions(token)
    check("未分配部门的新账号在明细里一行都看不到（total = 0）",
          status == 200 and listed["total"] == 0, f"status={status} total={listed.get('total')}")
    status, payload = call("GET", "/admin/users", token)
    check("未分配部门的新账号进不了管理端（403 且文案说清缺什么）",
          status == 403 and "管理员" in str(payload.get("detail")), f"status={status} {payload}")

    # ---------------------------------------------------------- 自助扩权：四种写法全拒
    attempts = [
        ("把部门填成 018", {"departments": ["018"]}),
        ("把部门清空", {"departments": []}),
        ("用单值字段填 018", {"department": "018"}),
        ("直接要管理员标识", {"is_admin": True}),
    ]
    for label, body in attempts:
        status, payload = call("PATCH", "/auth/me", token, body)
        check(f"自助扩权被拒：{label} → 403",
              status == 403 and "管理员" in str(payload.get("detail")), f"status={status} {payload}")

    after = me(token)
    check("被拒之后 /auth/me 三个字段一点没变",
          after["is_admin"] is False and after["unrestricted"] is False and after["departments"] == [],
          f"{after}")
    row = rows("SELECT is_admin, department, departments FROM users WHERE id = :i", i=user_id)[0]
    check("被拒之后库里那一行也没变（部门仍是空、标识仍是 false）",
          row[0] is False and not (row[1] or "") and not (row[2] or []), f"{row}")
    status, payload = call("GET", "/admin/audit?action=user.department_self_denied&limit=50", admin_token)
    denied = [item for item in payload.get("data", [])
              if str(item.get("target_id")) == str(user_id)]
    check("每次被拒都留痕：审计 user.department_self_denied 有 4 条，detail 记下想改成什么",
          status == 200 and len(denied) == 4 and all("attempted" in item.get("detail", {}) for item in denied),
          f"status={status} 条数={len(denied)}")

    # ---------------------------------------------------------- 管理员分配的是「范围」不是「权限」
    status, payload = call("PATCH", f"/admin/users/{user_id}", admin_token, {"departments": ["018"]})
    check("管理员把 018 分给这个账号：标识仍是 false、范围变成 018",
          status == 200 and payload.get("is_admin") is False and payload.get("unrestricted") is False
          and payload.get("departments") == ["018"] and payload.get("department") == "018",
          f"status={status} {payload}")
    status, listed = transactions(token)
    check("范围真的生效：该账号现在只看得到 018 的行（total > 0 且行部门全是 018）",
          status == 200 and listed["total"] > 0
          and {item["department"] for item in listed["data"]} == {"018"},
          f"status={status} total={listed.get('total')} 行部门={sorted({i['department'] for i in listed.get('data', [])})}")
    status, payload = call("GET", "/admin/users", token)
    check("有了范围也还是普通账号：管理端仍然 403（范围不是权限）",
          status == 403, f"status={status} {payload}")
    status, payload = call("GET", "/admin/audit?action=user.department_change&limit=50", admin_token)
    changed = [item for item in payload.get("data", []) if str(item.get("target_id")) == str(user_id)]
    check("分配范围留痕：user.department_change 记下 未分配 → 018",
          status == 200 and any(item.get("detail", {}).get("from") == "未分配"
                                and item["detail"].get("to") == "018" for item in changed),
          f"status={status} {changed[:2]}")

    # ---------------------------------------------------------- 只发 is_admin：不许顺手清掉部门
    status, payload = call("PATCH", f"/admin/users/{user_id}", admin_token, {"is_admin": True})
    check("只发 {\"is_admin\": true} 就能授予管理员",
          status == 200 and payload.get("is_admin") is True, f"status={status} {payload}")
    check("……而且部门一个字都没动（这条请求里根本没有部门字段）",
          payload.get("departments") == ["018"] and payload.get("department") == "018",
          f"departments={payload.get('departments')} department={payload.get('department')}")
    scoped_me = me(token)
    check("被收窄的管理员：is_admin = true 但 unrestricted = false（能管 + 只看 018）",
          scoped_me["is_admin"] is True and scoped_me["unrestricted"] is False
          and scoped_me["departments"] == ["018"], f"{scoped_me}")
    status, payload = call("GET", "/admin/users?limit=5", token)
    check("被收窄到某个部门的管理员照样进得了管理端（能不能管与数据范围是两件事）",
          status == 200 and "data" in payload, f"status={status}")
    status, payload = call("GET", "/admin/overview", token)
    check("管理概览也打得开（收窄只影响数据口径，不影响管理权）", status == 200, f"status={status}")
    status, listed = transactions(token)
    check("但它读到的仍是 018 那一本账（scope.label = 部门 018）",
          status == 200 and listed["scope"]["label"] == "部门 018"
          and {item["department"] for item in listed["data"]} == {"018"},
          f"status={status} scope={listed.get('scope', {}).get('label')}")
    status, payload = call("GET", "/financial/transactions?department=097&limit=5", token)
    check("收窄的管理员看不了别的部门（?department=097 → 403）",
          status == 403 and "不能查看" in str(payload.get("detail")), f"status={status} {payload}")
    status, payload = call("GET", "/admin/audit?action=user.admin_change&limit=50", admin_token)
    granted = [item for item in payload.get("data", []) if str(item.get("target_id")) == str(user_id)]
    check("授予标识留痕：user.admin_change 记下 普通账号 → 管理员",
          status == 200 and any(item.get("detail", {}).get("from") == "普通账号"
                                and item["detail"].get("to") == "管理员" for item in granted),
          f"status={status} {granted[:2]}")

    # ---------------------------------------------------------- 收回标识：同样不动范围
    status, payload = call("PATCH", f"/admin/users/{user_id}", admin_token, {"is_admin": False})
    check("收回管理员标识：is_admin = false，部门仍是 018",
          status == 200 and payload.get("is_admin") is False and payload.get("departments") == ["018"],
          f"status={status} {payload}")
    status, payload = call("GET", "/admin/users", token)
    check("收回后立刻进不了管理端（权限每次都从库里读，不用重新登录）",
          status == 403, f"status={status} {payload}")
    status, payload = call("GET", "/admin/audit?action=user.admin_change&limit=50", admin_token)
    revoked = [item for item in payload.get("data", []) if str(item.get("target_id")) == str(user_id)]
    check("收回也留痕：user.admin_change 记下 管理员 → 普通账号",
          status == 200 and any(item.get("detail", {}).get("from") == "管理员"
                                and item["detail"].get("to") == "普通账号" for item in revoked),
          f"status={status} {revoked[:2]}")

    # ---------------------------------------------------------- 未分配部门 ≠ 管理员（⑳ 的那条洞）
    empty_id, _empty_name, empty_token = register("empty")
    status, payload = call("PATCH", f"/admin/users/{empty_id}", admin_token, {"departments": []})
    check("管理员把某人的部门清空：unrestricted 仍是 false、标识仍是 false（清空不再等于升权）",
          status == 200 and payload.get("is_admin") is False and payload.get("unrestricted") is False,
          f"status={status} {payload}")
    status, payload = call("GET", "/admin/users", empty_token)
    check("……那个账号自己也升不了权：管理端 403", status == 403, f"status={status} {payload}")
    status, listed = transactions(empty_token)
    check("……而且它确实什么都看不到（total = 0）",
          status == 200 and listed["total"] == 0, f"status={status} total={listed.get('total')}")

    # ---------------------------------------------------------- 守卫与救援脚本
    status, payload = call("PATCH", f"/admin/users/{admin_me['id']}", admin_token, {"is_admin": False})
    check("管理员不能通过管理接口改自己（400，请找另一个管理员或用 grant_admin.py）",
          status == 400 and "自己" in str(payload.get("detail")), f"status={status} {payload}")

    done = grant_admin([username])
    check("python scripts/grant_admin.py <账号> 能把人提成管理员（没有管理接口时的救援路径）",
          done.returncode == 0 and scalar("SELECT is_admin FROM users WHERE id = :i", i=user_id) is True,
          f"exit={done.returncode} {(done.stdout or '').strip().splitlines()[-1:]}")
    done = grant_admin(["--revoke", username])
    check("grant_admin.py --revoke 也能收回（此刻演示账号还在，所以不是最后一个）",
          done.returncode == 0 and scalar("SELECT is_admin FROM users WHERE id = :i", i=user_id) is False,
          f"exit={done.returncode} {(done.stdout or '').strip().splitlines()[-1:]}")
    done = grant_admin([f"{PREFIX}没有这个人"])
    check("grant_admin.py 对不存在的账号 → 退出码 1（不会默默什么都不做）",
          done.returncode == 1 and "没有这个账号" in (done.stderr or ""),
          f"exit={done.returncode} {done.stderr.strip()}")

    for temp_id, _name, _token in ((user_id, username, token), (empty_id, "", empty_token)):
        call("DELETE", f"/admin/users/{temp_id}", admin_token)
    left = rows("SELECT id FROM users WHERE username LIKE :p", p=f"{PREFIX}%")
    demo_now = rows("SELECT is_admin, token_version FROM users WHERE username = :u", u=ADMIN[0])[0]
    check("临时账号清干净了（跑完脚本不该在库里留下东西）", not left, f"残留 {left}")

    if not scalar("SELECT count(*) FROM users WHERE username = :u AND is_admin", u=ADMIN[0]):
        grant_admin([ADMIN[0]])  # 有人把演示账号的标识收了，先补回来，后面才验得下去
    admins_before = [(int(row[0]), row[1])
                     for row in rows("SELECT id, username FROM users WHERE is_admin ORDER BY id")]
    others = [(uid, name) for uid, name in admins_before if name != ADMIN[0]]
    # 「最后一个管理员收不掉」这条守卫默认踩不到 —— 真机库里总是还有别的管理员。要确定性地走到它，
    # 就得先把其他管理员的标识临时摘掉（下面一定补回来），让演示账号真的成为唯一管理员。
    for uid, _name in others:
        call("PATCH", f"/admin/users/{uid}", admin_token, {"is_admin": False})
    stripped = [(uid, name) for uid, name in others
                if not rows("SELECT is_admin FROM users WHERE id = :i", i=uid)[0][0]]
    check(f"临时摘掉其他 {len(others)} 个管理员，让演示账号成为唯一管理员",
          len(stripped) == len(others)
          and scalar("SELECT count(*) FROM users WHERE is_admin") == 1,
          f"摘掉 {len(stripped)}/{len(others)} 个")
    done = grant_admin(["--revoke", ADMIN[0]])
    check("最后一个管理员收不掉：grant_admin.py --revoke demo_analyst 退出码 1 并说明原因",
          done.returncode == 1 and "最后一个管理员" in (done.stderr or ""),
          f"exit={done.returncode} {done.stderr.strip()}")
    for uid, _name in stripped:
        call("PATCH", f"/admin/users/{uid}", admin_token, {"is_admin": True})
    back = scalar("SELECT count(*) FROM users WHERE is_admin")
    check(f"其他 {len(stripped)} 个管理员的标识补回来了（和被摘掉之前一样多）",
          back == len(admins_before), f"{len(admins_before)} → {back}")
    check("演示账号仍是管理员、会话版本没被动过（本脚本不登出 demo）",
          rows("SELECT is_admin, token_version FROM users WHERE username = :u", u=ADMIN[0])[0][0] is True
          and int(demo_now[1]) == int(admin_me["token_version"]),
          f"版本 {admin_me['token_version']} → {demo_now[1]}")

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
