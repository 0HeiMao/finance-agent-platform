from functools import lru_cache
import logging
import os
import re
import secrets
from pathlib import Path

# 本机（以及不少 Windows 开发机）会把 NO_PROXY 写成含 IPv6 字面量的形式，例如
# "...,::1,[::1]"。httpx 解析 NO_PROXY 时会把 "[::1]" 的端口部分读成 ":1]"，直接抛
# httpx.InvalidURL: Invalid port: ':1]'。这会让 openai SDK、requests 等一切尊重代理环境
# 变量的库在**构造客户端时**就崩溃——表现为 DeepSeek 永远“不可用”，而 curl 直连却正常。
# 这里只清洗这一种非法条目，其余代理配置保持不动。
_INVALID_NO_PROXY_ENTRY = "[::1]"

# 仓库里携带的占位密钥。任何非 development 环境都必须换掉，见 Settings.validate()。
DEFAULT_AUTH_SECRET_KEY = "change-this-secret-in-env"

_PROJECT_ROOT = Path(__file__).resolve().parents[1]

logger = logging.getLogger("finance.config")


def env_file_path() -> Path:
    """定位 .env：优先当前工作目录（uvicorn/脚本都在项目根跑），否则回落到项目根。"""
    local = Path(".env")
    return local if local.exists() else _PROJECT_ROOT / ".env"


def write_env_value(path: Path, key: str, value: str) -> None:
    """就地更新 .env 里的一个键，保留其余行与注释；键不存在则追加到末尾。"""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=", re.IGNORECASE)
    replaced = False
    output: list[str] = []
    for line in lines:
        if pattern.match(line):
            output.append(f"{key}={value}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"{key}={value}")
    path.write_text("\n".join(output).rstrip("\n") + "\n", encoding="utf-8")


def provision_auth_secret(settings) -> bool:
    """密钥是空值或仓库占位值时，生成随机串并写回 .env；返回是否发生了改动。

    `change-this-secret-in-env` 在仓库里是公开的字符串，谁都能拿它签出一个合法的 JWT 冒充
    任意用户——只在日志里警告一句，等于让「开发环境」长期跑在可伪造令牌的配置上。
    所以首次启动直接把占位值换成真随机串：进程内立即生效，同时落盘供下次启动复用。
    写盘失败也不阻断启动（内存里那份随机密钥仍然有效），只是重启后会再换一次。
    """
    current = (settings.auth_secret_key or "").strip()
    if current and current != DEFAULT_AUTH_SECRET_KEY:
        return False
    secret = secrets.token_urlsafe(48)
    settings.auth_secret_key = secret
    path = env_file_path()
    try:
        write_env_value(path, "AUTH_SECRET_KEY", secret)
        logger.warning(
            "AUTH_SECRET_KEY 是仓库占位值，已生成随机密钥并写入 %s；"
            "在此之前签发的登录令牌全部失效，请重新登录。", path)
    except OSError as exc:
        logger.error(
            "AUTH_SECRET_KEY 是仓库占位值，且无法写入 %s（%s）；"
            "本次进程使用内存中的随机密钥，重启后会再次更换。", path, exc)
    return True


def sanitize_proxy_env() -> None:
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        kept = [item for item in raw.split(",") if item.strip() and item.strip() != _INVALID_NO_PROXY_ENTRY]
        cleaned = ",".join(kept)
        if cleaned != raw:
            os.environ[name] = cleaned


def _parse_slow_path_overrides(raw: str) -> dict[str, int]:
    """把 `SLOW_PATH_OVERRIDES` 解析成 `{路径: 毫秒}`，坏条目跳过（见 Settings.slow_path_thresholds）。"""
    result: dict[str, int] = {}
    for item in (raw or "").split(","):
        text = item.strip()
        if not text or "=" not in text:
            continue
        path, _, value_text = text.rpartition("=")
        path = path.strip()
        try:
            value = int(value_text.strip())
        except ValueError:
            continue
        if path and value > 0:
            result[path] = value
    return result


def _origin_host(origin: str) -> str:
    """从 CORS 来源里取出主机名，取不到就返回空串。"""
    text = origin.strip()
    if not text or text == "*":
        return ""
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0]
    if text.startswith("["):  # IPv6 字面量
        return text[1 : text.find("]")] if "]" in text else text
    return text.split(":", 1)[0].lower()


def _is_local_host(host: str) -> bool:
    """本机/内网地址视为开发环境，不参与生产配置校验。"""
    if host in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
        return True
    if host.startswith("192.168.") or host.startswith("10.") or host.startswith("172."):
        return True
    return host.endswith(".local")

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # 允许仅安装 FastAPI 的骨架环境启动
    BaseSettings = None
    SettingsConfigDict = None


if BaseSettings is not None:

    class Settings(BaseSettings):
        app_env: str = "development"
        log_level: str = "INFO"
        api_prefix: str = "/api/v1"
        cors_origins: str = "http://localhost:5173"
        model_provider: str = ""
        model_name: str = ""
        # 通用大模型配置。.env 里写的是 DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL，
        # 而 rag/answer.py 读的是 model_api_key / model_base_url；两个都留空会让问答
        # 链路永远判定“未配置大模型”，只把检索原文贴出来，DeepSeek 根本不会被调用。
        # 这里不改读取行为（pydantic-settings 2.15 下用 validation_alias 反而读不到
        # .env 的同名字段，已实测），而是在 effective_* 属性里做回退。
        model_api_key: str = ""
        model_base_url: str = ""
        auth_secret_key: str = DEFAULT_AUTH_SECRET_KEY
        # JWT 有效期改成可配置：原来硬编码 12 小时，改口径必须动代码。
        access_token_expire_minutes: int = 12 * 60
        # 登录限流（进程内滑动窗口）：同一用户名+来源在窗口内超过次数就 429。
        login_max_attempts: int = 10
        login_window_seconds: int = 300
        # 生产环境启动时校验配置（占位密钥、CORS 通配）；development 只记日志不拦。
        strict_config: bool = False
        embedding_model: str = ""
        rerank_model: str = ""
        # 本地 384 维 embedding 对中文的余弦距离普遍落在 0.33~0.99，旧代码硬编码的
        # 0.45 会把“净利润怎么算”这类真正相关的片段也丢掉，这里放宽为可配置阈值。
        rag_max_distance: float = 0.8
        # 宽松阈值会拉进噪声，因此先多召回候选、再由 rerank_model 重排压缩到 top_k。
        rag_candidate_k: int = 20
        # data/raw 按仓库约定只读（原始文件不得覆盖删除），上传落盘一律另放目录。
        upload_dir: str = "data/uploads"
        rag_upload_dir: str = "data/uploads/documents"
        # 审计日志的保留天数：超过这个天数才算过期，只有 POST /admin/audit/purge 会删（并留痕）。
        audit_retention_days: int = 180
        # 慢接口告警：单次请求超过阈值就记一条 WARNING，并按路径限流（见 backend/observability.py）。
        # 默认 1000ms：本地演示库上的正常接口都在 100ms 内，而 Agent 问答/报告导出本来就要几秒，
        # 所以那几条重接口用 SLOW_PATH_OVERRIDES 单独放宽，而不是把全局阈值调到没人会超。
        slow_path_ms: int = 1000
        slow_path_overrides: str = ""
        slow_path_log_seconds: int = 60
        # 出口配额（每分钟、按账号）：导出一次能把整库拉走，问答一次要花模型的钱，
        # 两个都必须是"脚本打不动、人用不着"的量级——回归脚本一次要连问 28 个问题，
        # 所以默认给到 60，不是 10 那种会误伤自己的数字。
        export_max_per_minute: int = 30
        ai_max_per_minute: int = 60
        database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"
        deepseek_api_key: str = ""
        deepseek_base_url: str = "https://api.deepseek.com"
        deepseek_model: str = "deepseek-chat"

        model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

        @property
        def cors_origin_list(self) -> list[str]:
            return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

        @property
        def effective_api_key(self) -> str:
            return self.model_api_key or self.deepseek_api_key

        @property
        def effective_base_url(self) -> str:
            return self.model_base_url or self.deepseek_base_url

        @property
        def slow_path_thresholds(self) -> dict[str, int]:
            """按路径覆盖慢接口阈值，写法 `/api/v1/ai/agent/chat=5000,/api/v1/rag/search=2000`。

            解析失败的条目直接跳过而不是抛异常：这是个「调优用的旋钮」，写错一个逗号不该让服务起不来。
            """
            return _parse_slow_path_overrides(self.slow_path_overrides)

        @property
        def is_development(self) -> bool:
            return self.app_env.strip().lower() in {"development", "dev", "local", "test"}

        def validate(self) -> list[str]:
            """返回启动阻断项（不含仅提醒项）。development 下由调用方决定是否忽略。"""
            problems: list[str] = []
            if not self.auth_secret_key or self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY:
                problems.append(
                    "AUTH_SECRET_KEY 仍是仓库里的占位值，任何人都能伪造登录令牌；请在 .env 里换成"
                    '长随机串（python -c "import secrets;print(secrets.token_urlsafe(48))"）'
                )
            if "*" in self.cors_origin_list:
                problems.append(
                    "CORS_ORIGINS 含通配 '*'：带 Bearer Token 的接口会向任意站点开放，请明确列出前端域名"
                )
            else:
                for origin in self.cors_origin_list:
                    host = _origin_host(origin)
                    if host and not _is_local_host(host):
                        problems.append(f"CORS_ORIGINS 里的 {origin} 不是本机地址，请确认这是预期的对外来源")
            return problems

else:
    import os

    class Settings:
        app_env = os.getenv("APP_ENV", "development")
        log_level = os.getenv("LOG_LEVEL", "INFO")
        api_prefix = "/api/v1"
        cors_origins = os.getenv("CORS_ORIGINS", "http://localhost:5173")
        model_provider = os.getenv("MODEL_PROVIDER", "")
        model_name = os.getenv("MODEL_NAME", "")
        model_api_key = os.getenv("MODEL_API_KEY", "")
        model_base_url = os.getenv("MODEL_BASE_URL", "")
        auth_secret_key = os.getenv("AUTH_SECRET_KEY", DEFAULT_AUTH_SECRET_KEY)
        access_token_expire_minutes = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", str(12 * 60)))
        login_max_attempts = int(os.getenv("LOGIN_MAX_ATTEMPTS", "10"))
        login_window_seconds = int(os.getenv("LOGIN_WINDOW_SECONDS", "300"))
        strict_config = os.getenv("STRICT_CONFIG", "").strip().lower() in {"1", "true", "yes", "on"}
        embedding_model = os.getenv("EMBEDDING_MODEL", "")
        rerank_model = os.getenv("RERANK_MODEL", "")
        rag_max_distance = float(os.getenv("RAG_MAX_DISTANCE", "0.8"))
        rag_candidate_k = int(os.getenv("RAG_CANDIDATE_K", "20"))
        upload_dir = os.getenv("UPLOAD_DIR", "data/uploads")
        rag_upload_dir = os.getenv("RAG_UPLOAD_DIR", "data/uploads/documents")
        audit_retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "180"))
        slow_path_ms = int(os.getenv("SLOW_PATH_MS", "1000"))
        slow_path_overrides = os.getenv("SLOW_PATH_OVERRIDES", "")
        slow_path_log_seconds = int(os.getenv("SLOW_PATH_LOG_SECONDS", "60"))
        export_max_per_minute = int(os.getenv("EXPORT_MAX_PER_MINUTE", "30"))
        ai_max_per_minute = int(os.getenv("AI_MAX_PER_MINUTE", "60"))
        database_url = os.getenv("DATABASE_URL", "postgresql+psycopg://finance:finance@localhost:5432/finance")
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

        @property
        def cors_origin_list(self) -> list[str]:
            return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

        @property
        def effective_api_key(self) -> str:
            return self.model_api_key or self.deepseek_api_key

        @property
        def effective_base_url(self) -> str:
            return self.model_base_url or self.deepseek_base_url

        @property
        def slow_path_thresholds(self) -> dict[str, int]:
            return _parse_slow_path_overrides(self.slow_path_overrides)

        @property
        def is_development(self) -> bool:
            return self.app_env.strip().lower() in {"development", "dev", "local", "test"}

        def validate(self) -> list[str]:
            problems: list[str] = []
            if not self.auth_secret_key or self.auth_secret_key == DEFAULT_AUTH_SECRET_KEY:
                problems.append("AUTH_SECRET_KEY 仍是仓库里的占位值，必须换成随机串")
            if "*" in self.cors_origin_list:
                problems.append("CORS_ORIGINS 含通配 '*'")
            return problems

@lru_cache
def _settings() -> Settings:
    # 读取 .env 之前先修好代理环境变量，避免 openai/httpx 之后构造客户端时崩溃。
    sanitize_proxy_env()
    settings = Settings()
    # 占位密钥换成随机串——必须在 auth.py 读取 SECRET_KEY 之前完成，因此放在这里而不是启动事件里。
    provision_auth_secret(settings)
    return settings


def get_settings() -> Settings:
    return _settings()


def clear_settings_cache() -> None:
    """让下一次 get_settings() 重新读取 .env 与进程环境变量。

    只有测试/验证脚本需要它：这些脚本必须在导入 backend 各模块之后才能改写
    RAG_UPLOAD_DIR 之类的环境变量，而 lru_cache 会让早先读到默认值的实例一直生效。
    """
    _settings.cache_clear()

