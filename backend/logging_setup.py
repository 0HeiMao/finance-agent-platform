"""⑬ 结构化日志（`LOG_FORMAT=json`）：一行一条 JSON，字段能被日志系统直接索引。

默认仍是人读的文本格式（`%(asctime)s %(levelname)s %(message)s`）——本地开发和几个真机
脚本都在 grep 这个格式（`scripts/verify_security_headers.py` 找 `request_id=`、
`scripts/verify_slow_paths.py` 找 `慢接口告警`），一起改掉是没必要的风险。
采集侧要结构化就把 `LOG_FORMAT=json` 打开。

三个刻意的取舍：

- **只收白名单字段**。`extra=` 里塞什么都往 JSON 里吐，会把内网地址、令牌这类意外字段
  一起带出去；这里只收已知的请求上下文（`request_id`/`path`/`method`/`status`/…）。
- **不接管 uvicorn 自己的日志器**（`uvicorn.error`/`uvicorn.access` 有自己的 handler 且
  `propagate=False`），所以打开 JSON 后应用日志是 JSON、uvicorn 的启动行仍是文本。
  想让访问日志也只留一份，用 `--no-access-log` 关掉它 —— 应用在观测中间件里已经记过一条。
- **重复调用是幂等的、且不摘别人的 handler**：pytest 的 `caplog` 与 uvicorn 都往根日志器上
  挂 handler，摘掉它们会让「日志断言」在测试里静默失效。所以只认自己装的那一个（带标记）。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

TEXT_FORMAT = "%(asctime)s %(levelname)s %(message)s"
JSON_FORMAT = "json"

# `extra=` 里允许进 JSON 的字段（请求上下文，都是短标量）。
STRUCTURED_FIELDS: tuple[str, ...] = (
    "request_id", "path", "route", "method", "status", "duration_ms",
    "threshold_ms", "exceeded", "suppressed", "worst_ms",
)

_HANDLER_MARKER = "_finance_log_handler"


def normalise_format(value: str | None) -> str:
    """认 `json` / `structured` 两种写法（大小写无所谓），别的都当文本格式。"""
    return JSON_FORMAT if str(value or "").strip().lower() in {"json", "structured"} else "text"


class JsonFormatter(logging.Formatter):
    """一行一个 JSON 对象；`extra=` 里的白名单字段平铺在同一层（不嵌套，字段好索引）。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in STRUCTURED_FIELDS:
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        if record.exc_info:
            # 栈信息也塞进同一个 JSON 字段（换行会被 json 转义，不会把一行日志切成多行）。
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def build_formatter(fmt: str | None) -> logging.Formatter:
    return JsonFormatter() if normalise_format(fmt) == JSON_FORMAT else logging.Formatter(TEXT_FORMAT)


def resolve_level(value: str | None) -> int:
    """日志级别按名字解析；写错（或空）就回落到 INFO，而不是让服务起不来。"""
    level = logging.getLevelName(str(value or "INFO").strip().upper())
    return level if isinstance(level, int) else logging.INFO


def configure_logging(settings=None, *, stream=None) -> str:
    """装配根日志器的 handler，返回实际生效的格式（`json` / `text`）。

    只认自己装过的 handler（`_HANDLER_MARKER`）：pytest / uvicorn 挂上来的不动，
    所以这个函数在测试里被反复调用也不会把 `caplog` 弄哑。
    """
    if settings is None:
        from backend.config import get_settings

        settings = get_settings()
    fmt = normalise_format(getattr(settings, "log_format", None))
    formatter = build_formatter(fmt)
    root = logging.getLogger()
    ours = [handler for handler in root.handlers if getattr(handler, _HANDLER_MARKER, False)]
    if not ours:
        handler = logging.StreamHandler(stream)
        setattr(handler, _HANDLER_MARKER, True)
        handler.setFormatter(formatter)
        root.addHandler(handler)
    else:
        for handler in ours:
            handler.setFormatter(formatter)
    root.setLevel(resolve_level(getattr(settings, "log_level", None)))
    return fmt
