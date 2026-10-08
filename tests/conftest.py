"""pytest 公共夹具。

设计要点（和 scripts/verify_*.py 保持一致，避免"自证式测试"）：
- 依赖真 PostgreSQL：每个用例开一个外层事务，用 `join_transaction_mode="create_savepoint"`
  让被测代码里的 commit 只提交到 savepoint，用例结束整体 rollback，库里不留痕。
- 需要数据库的用例在库不可用时 skip 并说明原因，纯逻辑用例照样能跑。
- httpx 客户端一律 `trust_env=False`：本机 NO_PROXY 里含 "[::1]"，httpx 解析会抛
  `InvalidURL: Invalid port: ':1]'`。
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.db import (  # noqa: E402
    SessionLocal,
    engine,
    get_db,  # noqa: E402
)
from backend.main import app  # noqa: E402


@pytest.fixture(scope="session")
def require_database() -> None:
    """库连不上就跳过依赖数据库的用例，而不是伪装通过。"""
    try:
        with engine.connect() as connection:
            connection.execute(text("select 1"))
    except Exception as exc:  # pragma: no cover - 环境相关
        pytest.skip(f"需要可用的 PostgreSQL：{exc}")


@pytest.fixture(scope="session", autouse=True)
def ensure_application_schema() -> None:
    """补齐新增的表与列（和后端启动时调用的是同一套 `backend/migrations.ensure_schema`）。

    `httpx.ASGITransport` 不会触发 FastAPI 的 startup 事件，所以新建的表（如 `audit_logs`）
    不会自动出现；缺了它用例会在 `relation "audit_logs" does not exist` 上失败，
    而那是"环境还没建表"，不是代码错。库不可用时这里静默跳过，由 `require_database` 负责 skip。
    """
    try:
        from backend.migrations import ensure_schema
        ensure_schema()
    except Exception:  # pragma: no cover - 环境相关
        pass


@pytest.fixture
def db_session(require_database):
    """外层事务 + savepoint：用例里的 commit 最终都会随外层 rollback 一起消失。"""
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def api_app(db_session):
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield app
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def anon_client(api_app):
    """未登录客户端。注意不要复用它做登录：httpx 会把 Authorization 头持久化在实例上。"""
    transport = httpx.ASGITransport(app=api_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver", trust_env=False) as client:
        yield client


@pytest.fixture
def credentials() -> dict:
    # 口令要过 backend/passwords.py 的强度规则（含 "password" 这种弱口令词会被拒），
    # 所以字面量里不能再出现它。
    return {"username": f"pytest_{uuid.uuid4().hex[:10]}", "password": "pytest-login-2026"}


@pytest.fixture
async def auth_headers(anon_client, credentials) -> dict:
    """注册并登录一个随机用户，返回可直接传给请求的 Bearer 头。"""
    response = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 200, response.text
    response = await anon_client.post("/api/v1/auth/login", json=credentials)
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
