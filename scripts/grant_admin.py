"""⑳ 管理员标识的救援/引导脚本：给某个账号打上（或摘掉）管理员标识。

为什么需要它：⑳ 起「有没有部门」不再等于「是不是管理员」，管理员标识是 `users.is_admin`
这一列，界面上由管理员在「用户管理」里勾选。可有两种情况谁也进不去那个界面：

1. **全新的库**：`backend/auth.py::bootstrap_admin()` 会让第一个注册的账号自动成为管理员，
   所以正常流程不需要这个脚本。但要是种子/演示流程先建了一个普通账号当「第一个」，那这个库
   就永远没有管理员了 —— 用这个脚本点一个出来。
2. **老库回填不合意**：升级时按「原先不受部门限制 = 管理员」回填（见 `backend/migrations.py`
   的 `BACKFILLS`）。要是老库里根本没有这样一个账号，也得靠这里救。

用法（在仓库根目录、激活的 venv 里跑，和 scripts/ 下别的脚本一样）：

    python scripts/grant_admin.py demo_analyst              # 授予
    python scripts/grant_admin.py demo_analyst --revoke     # 收回

脚本会先跑一次 `ensure_schema()`（和启动时同一套轻量迁移），所以在一个还没升过级的库上直接
用它也不会因为缺列而失败；找不到用户名时**不改任何东西**，只报错退出（退出码 1），
这样能在自动化里直接用。它不写审计：审计表记的是「谁动了什么」，而这里没有登录者，
`users` 行本身就是结果，改了哪一行会打印出来。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import func, select  # noqa: E402

from backend.db import SessionLocal  # noqa: E402
from backend.migrations import ensure_schema  # noqa: E402
from backend.models import User  # noqa: E402


def admin_count(db) -> int:
    return db.scalar(select(func.count()).select_from(User).where(User.is_admin.is_(True))) or 0


def main() -> int:
    parser = argparse.ArgumentParser(description="授予或收回某个账号的管理员标识（⑳）。")
    parser.add_argument("username", help="要改的账号用户名")
    parser.add_argument("--revoke", action="store_true",
                        help="收回管理员标识（默认是授予）")
    args = parser.parse_args()

    # 先跑迁移：在一个还没升过级（没有 is_admin 列）的库上也能直接用。
    applied = ensure_schema()
    if applied:
        print("迁移已应用：" + "、".join(applied))

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == args.username))
        if user is None:
            print(f"没有这个账号：{args.username}", file=sys.stderr)
            return 1

        wanted = not args.revoke
        before = bool(user.is_admin)
        if before == wanted:
            state = "是" if before else "不是"
            print(f"{user.username}（id={user.id}）本来就已经{state}管理员，未做改动。"
                  f"当前管理员共 {admin_count(db)} 个。")
            return 0

        if not wanted and admin_count(db) <= 1:
            # 和接口上的守卫同一个道理：把最后一个管理员摘掉 = 这个库再也没人能管。
            print(f"拒绝收回：{user.username} 是最后一个管理员，收回后这个库再没有人能管。",
                  file=sys.stderr)
            return 1

        user.is_admin = wanted
        db.commit()
        action = "已授予管理员标识" if wanted else "已收回管理员标识"
        print(f"{action}：{user.username}（id={user.id}）；当前管理员共 {admin_count(db)} 个。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
