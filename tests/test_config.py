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

    stub = SimpleNamespace(validate=list, is_development=False, strict_config=True)
    monkeypatch.setattr(main, "get_settings", lambda: stub)
    main.check_startup_config()


# ---------------------------------------------------------------- 业务时区（⑯）

def _prod_settings(**overrides):
    base = {"app_env": "production", "auth_secret_key": "x" * 48, "cors_origins": "http://localhost:5173"}
    return Settings(**{**base, **overrides})


def test_business_timezone_defaults_to_shanghai_and_other_iana_names_are_clean():
    assert _prod_settings().business_timezone == "Asia/Shanghai"
    assert _prod_settings().validate() == []
    # 换时区不是错误：只要名字是 IANA 的
    assert _prod_settings(business_timezone="America/New_York").validate() == []
    assert _prod_settings(business_timezone="Asia/Shanghai").validate() == []


@pytest.mark.parametrize("value", ["Not/AZone", "UTC+8", ""])
def test_invalid_business_timezone_is_reported(value):
    """`UTC+8` 这种「看着像时区」的写法、以及空串（.env 里写了键没写值）都要在体检里点名。"""
    problems = _prod_settings(business_timezone=value).validate()
    assert any("BUSINESS_TIMEZONE" in problem for problem in problems), problems


def test_business_timezone_with_padding_is_accepted():
    """`.env` 里手写难免带空格，两处判定都先 strip 再校验，不该因为一个尾随空格拦住启动。"""
    from backend.config import _valid_timezone

    assert _valid_timezone(" Asia/Shanghai ") is True
    assert _prod_settings(business_timezone=" Asia/Shanghai ").validate() == []


def test_config_timezone_check_and_biztime_agree_on_valid_names():
    """两处各有一套判定（config 用 zoneinfo、biztime 再包一层），口径必须一致。"""
    from backend import biztime
    from backend.config import _valid_timezone

    for name in ("Asia/Shanghai", "America/New_York", "Europe/London", "Not/AZone", "UTC+8"):
        assert _valid_timezone(name) is biztime.is_valid(name), name
    # 唯一的有意分歧：空串在 config 里算「写错了」，在 biztime 里算「用默认值」（避免请求 500）
    assert _valid_timezone("") is False and biztime.is_valid("") is True


# ---------------------------------------------------------------- 多轮上下文（⑰）

def test_agent_history_budgets_default_and_accept_zero():
    assert _prod_settings().agent_history_max_turns == 6
    assert _prod_settings().agent_history_max_tokens == 1500
    assert _prod_settings().validate() == []
    # 0 是「关掉多轮」，不是写错：两个都设 0 也该是干净配置
    assert _prod_settings(agent_history_max_turns=0, agent_history_max_tokens=0).validate() == []


@pytest.mark.parametrize("overrides", [{"agent_history_max_turns": -1}, {"agent_history_max_tokens": -1}])
def test_negative_agent_history_budget_is_reported(overrides):
    """负数没有含义（0 已经是关掉），与其在运行期当 0 用，不如在启动体检里点名。"""
    problems = _prod_settings(**overrides).validate()
    assert any("AGENT_HISTORY" in problem for problem in problems), problems
