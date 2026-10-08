"""启动配置校验：占位密钥必须在非 development 环境拦住启动，开发环境只告警。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

pytest.importorskip("pydantic_settings")

from backend.config import DEFAULT_AUTH_SECRET_KEY, Settings, _is_local_host, _origin_host  # noqa: E402


def test_placeholder_secret_is_reported():
    settings = Settings(app_env="production", auth_secret_key=DEFAULT_AUTH_SECRET_KEY,
                        cors_origins="https://finance.example.com")
    problems = settings.validate()
    assert any("AUTH_SECRET_KEY" in problem for problem in problems)


def test_strong_secret_with_local_cors_is_clean():
    settings = Settings(app_env="production", auth_secret_key="x" * 48,
                        cors_origins="http://localhost:5173")
    assert settings.validate() == []


def test_wildcard_cors_is_reported():
    settings = Settings(app_env="production", auth_secret_key="x" * 48, cors_origins="*")
    problems = settings.validate()
    assert any("CORS" in problem for problem in problems)


def test_remote_cors_origin_is_reported():
    settings = Settings(app_env="production", auth_secret_key="x" * 48,
                        cors_origins="https://finance.example.com")
    problems = settings.validate()
    assert any("finance.example.com" in problem for problem in problems)


@pytest.mark.parametrize("origin,expected", [
    ("http://localhost:5173", "localhost"),
    ("https://localhost", "localhost"),
    ("http://127.0.0.1:8000", "127.0.0.1"),
    ("http://[::1]:5173", "::1"),
    ("finance.example.com", "finance.example.com"),
    ("*", ""),
])
def test_origin_host_parsing(origin, expected):
    assert _origin_host(origin) == expected


@pytest.mark.parametrize("host,expected", [
    ("localhost", True),
    ("127.0.0.1", True),
    ("192.168.1.10", True),
    ("10.0.0.5", True),
    ("172.16.0.9", True),
    ("finance.example.com", False),
])
def test_local_host_detection(host, expected):
    assert _is_local_host(host) is expected


def test_startup_check_only_warns_in_development(monkeypatch):
    from backend import main

    stub = SimpleNamespace(validate=lambda: ["AUTH_SECRET_KEY 仍是占位值"], is_development=True, strict_config=False)
    monkeypatch.setattr(main, "get_settings", lambda: stub)
    main.check_startup_config()  # 不应抛异常

    stub.strict_config = True
    with pytest.raises(RuntimeError):
        main.check_startup_config()


def test_startup_check_raises_outside_development(monkeypatch):
    from backend import main

    stub = SimpleNamespace(validate=lambda: ["AUTH_SECRET_KEY 仍是占位值"], is_development=False, strict_config=False)
    monkeypatch.setattr(main, "get_settings", lambda: stub)
    with pytest.raises(RuntimeError, match="AUTH_SECRET_KEY"):
        main.check_startup_config()


def test_startup_check_noop_when_config_is_clean(monkeypatch):
    from backend import main

    stub = SimpleNamespace(validate=lambda: [], is_development=False, strict_config=True)
    monkeypatch.setattr(main, "get_settings", lambda: stub)
    main.check_startup_config()
