import logging
import os
import re
import secrets
from functools import lru_cache
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
    if host.startswith(("192.168.", "10.", "172.")):
        return True
    return host.endswith(".local")

def _valid_timezone(name: str) -> bool:
    """BUSINESS_TIMEZONE 体检（⑯）。

    这里直接用 `zoneinfo` 而不是 `backend.biztime.is_valid()`：biztime 要读配置，
    两边模块级互相 import 会成环。判定口径与 biztime 保持一致（只认 IANA 名字）。
    """
    candidate = (name or "").strip()
    if not candidate:
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(candidate)
    except Exception:
        return False
    return True


try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError:  # 允许仅安装 FastAPI 的骨架环境启动
    BaseSettings = None
    SettingsConfigDict = None


if BaseSettings is not None:

    class Settings(BaseSettings):
        app_env: str = "development"
        log_level: str = "INFO"
        # 日志格式：text = 人读的一行文本（默认，本地开发与几个真机脚本都在 grep 它）；
        # json = 一行一个 JSON，采集侧可直接索引字段（见 backend/logging_setup.py）。
        log_format: str = "text"
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
        # 登录失败锁定（账号级、落库）：连续失败到阈值才锁，锁定时长按 2 的幂增长并封顶。
        # 与上面的滑动窗口是两道闸——限流挡「打不中的海量尝试」，这里保证账号本身被锁上。
        login_lockout_threshold: int = 5
        login_lockout_base_seconds: int = 60
        login_lockout_max_seconds: int = 900
        login_failure_ttl_seconds: int = 900
        # 密码强度：注册时的长度下限（弱口令黑名单见 backend/passwords.py）。
        password_min_length: int = 10
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
        # Prometheus 指标端点（`/metrics`，文本格式，见 backend/metrics.py）：
        # METRICS_ENABLED=false 时直接 404；METRICS_TOKEN 非空时要求 `Authorization: Bearer <token>`
        # ——爬取侧通常没法走 JWT 登录，所以默认不鉴权，生产环境应在反代/内网层面限制来源。
        metrics_enabled: bool = True
        metrics_token: str = ""
        # 预警外发（⑭，见 backend/alerts_outbound.py 与 backend/models.py 的 AlertChannel）：
        # 单次 webhook 请求的超时——告警外发绝不能把重算请求拖住，超时就按这次失败处理，
        # 交给后台任务退避重试；一条投递单最多试几次也在这里。
        alert_webhook_timeout_seconds: float = 5.0
        alert_max_attempts: int = 3
        # 邮件通道的 SMTP 配置。留空 = 没配邮件（邮件投递**直接判失败**而不是重试用尽预算：
        # 配置错误重试多少次都一样，让它把重试次数烧完只会让人等更久）。
        alert_smtp_host: str = ""
        alert_smtp_port: int = 587
        alert_smtp_user: str = ""
        alert_smtp_password: str = ""
        alert_smtp_starttls: bool = True
        alert_mail_from: str = ""
        # 出口配额（每分钟、按账号）：导出一次能把整库拉走，问答一次要花模型的钱，
        # 两个都必须是"脚本打不动、人用不着"的量级——回归脚本一次要连问 28 个问题，
        # 所以默认给到 60，不是 10 那种会误伤自己的数字。
        export_max_per_minute: int = 30
        ai_max_per_minute: int = 60
        database_url: str = "postgresql+psycopg://finance:finance@localhost:5432/finance"
        deepseek_api_key: str = ""
        deepseek_base_url: str = "https://api.deepseek.com"
        deepseek_model: str = "deepseek-chat"
        # 让网关在流式回答的最后一个 chunk 里带上本次 token 用量（⑮ 的用量统计靠它）。
        # 默认开：不认这个参数的兼容网关会直接报错，backend/llm.py 会在"一个字都没吐出去"
        # 时自动不带该参数重试一次；想彻底不冒这个险就把它设成 false（用量退回按字数估算）。
        llm_include_usage: bool = True
        # 多轮上下文（⑰，见 backend/agent_sessions.py 的 history_for）：同一个会话里带最近的
        # 几轮问答给模型，才答得了「那上个月呢？」。两个都是成本旋钮——历史每轮都要重新计费，
        # 不设上限等于让会话越聊越贵。任一为 0 就关掉多轮（行为回到只带本轮证据）。
        agent_history_max_turns: int = 6
        agent_history_max_tokens: int = 1500
        # 业务时区（⑯，见 backend/biztime.py）：库里存 UTC，但「今天/本月」这类日历日口径
        # 必须有唯一答案。默认 Asia/Shanghai；写成不存在的时区名会进 config_problems
        # （STRICT_CONFIG=1 时直接拒绝启动），而不是悄悄退回默认值。
        business_timezone: str = "Asia/Shanghai"

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
            if not _valid_timezone(self.business_timezone):
                problems.append(
                    f"BUSINESS_TIMEZONE={self.business_timezone!r} 不是合法的 IANA 时区名"
                    "（例如 Asia/Shanghai、America/New_York）；Windows 上还需要 tzdata 包"
                )
            if self.agent_history_max_turns < 0 or self.agent_history_max_tokens < 0:
                problems.append(
                    f"AGENT_HISTORY_MAX_TURNS={self.agent_history_max_turns} / "
                    f"AGENT_HISTORY_MAX_TOKENS={self.agent_history_max_tokens} 不能是负数"
                    "（0 表示不带历史对话，只带本轮的财务证据）"
                )
            return problems

else:
    import os

    class Settings:
        app_env = os.getenv("APP_ENV", "development")
        log_level = os.getenv("LOG_LEVEL", "INFO")
        log_format = os.getenv("LOG_FORMAT", "text")
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
        login_lockout_threshold = int(os.getenv("LOGIN_LOCKOUT_THRESHOLD", "5"))
        login_lockout_base_seconds = int(os.getenv("LOGIN_LOCKOUT_BASE_SECONDS", "60"))
        login_lockout_max_seconds = int(os.getenv("LOGIN_LOCKOUT_MAX_SECONDS", "900"))
        login_failure_ttl_seconds = int(os.getenv("LOGIN_FAILURE_TTL_SECONDS", "900"))
        password_min_length = int(os.getenv("PASSWORD_MIN_LENGTH", "10"))
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
        metrics_enabled = os.getenv("METRICS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        metrics_token = os.getenv("METRICS_TOKEN", "")
        alert_webhook_timeout_seconds = float(os.getenv("ALERT_WEBHOOK_TIMEOUT_SECONDS", "5"))
        alert_max_attempts = int(os.getenv("ALERT_MAX_ATTEMPTS", "3"))
        alert_smtp_host = os.getenv("ALERT_SMTP_HOST", "")
        alert_smtp_port = int(os.getenv("ALERT_SMTP_PORT", "587"))
        alert_smtp_user = os.getenv("ALERT_SMTP_USER", "")
        alert_smtp_password = os.getenv("ALERT_SMTP_PASSWORD", "")
        alert_smtp_starttls = os.getenv("ALERT_SMTP_STARTTLS", "true").strip().lower() in {"1", "true", "yes", "on"}
        alert_mail_from = os.getenv("ALERT_MAIL_FROM", "")
        export_max_per_minute = int(os.getenv("EXPORT_MAX_PER_MINUTE", "30"))
        ai_max_per_minute = int(os.getenv("AI_MAX_PER_MINUTE", "60"))
        database_url = os.getenv("DATABASE_URL", "postgresql+psycopg://finance:finance@localhost:5432/finance")
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "")
        deepseek_base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
        deepseek_model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
        llm_include_usage = os.getenv("LLM_INCLUDE_USAGE", "true").strip().lower() in {"1", "true", "yes", "on"}
        agent_history_max_turns = int(os.getenv("AGENT_HISTORY_MAX_TURNS", "6"))
        agent_history_max_tokens = int(os.getenv("AGENT_HISTORY_MAX_TOKENS", "1500"))
        business_timezone = os.getenv("BUSINESS_TIMEZONE", "Asia/Shanghai")

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
            if not _valid_timezone(self.business_timezone):
                problems.append(f"BUSINESS_TIMEZONE={self.business_timezone!r} 不是合法的 IANA 时区名")
            if self.agent_history_max_turns < 0 or self.agent_history_max_tokens < 0:
                problems.append(
                    f"AGENT_HISTORY_MAX_TURNS={self.agent_history_max_turns} / "
                    f"AGENT_HISTORY_MAX_TOKENS={self.agent_history_max_tokens} 不能是负数"
                )
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

