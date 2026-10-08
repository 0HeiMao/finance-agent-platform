"""⑫ 安全响应头 / GZip / X-Request-ID。

三件事放在一个模块里，因为它们都是「每个响应都得带上」的东西，而且互相还有依赖：
请求 id 先要有，安全响应头的日志与 500 响应才能把用户看到的那串 id 对上。

几个刻意的取舍：

- **CSP 分两套**。API 响应不需要加载任何资源，所以给最严的 `default-src 'none'`；
  但 `/docs`（Swagger UI）与 `/redoc` 自己是页面、要从 CDN 取 JS/CSS，
  给它 `default-src 'none'` 等于把文档页打坏，所以那边只留 `frame-ancestors 'none'`。
- **HSTS 只在 HTTPS 下发**。开发环境是 http://127.0.0.1，发一次 `Strict-Transport-Security`
  浏览器就把这个主机记一辈子，本地联调直接变成「必须 https」。
- **请求 id 不信任客户端**。换行符会把日志切成两行（日志注入），超长会把日志字段撑爆，
  所以只接受 1–128 位的 `[A-Za-z0-9._-]`，不合规就当没带、自己生成一个。
- **GZip 不重复压缩已经是 zip 的格式**。docx/xlsx/pptx 本身就是 zip，
  再压一遍只烧 CPU 不省字节；SSE 更不能压 —— 压了就要攒够一块才吐，流式就没了。
"""

from __future__ import annotations

import re
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import DEFAULT_EXCLUDED_CONTENT_TYPES

REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_MAX_LENGTH = 128
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,%d}$" % REQUEST_ID_MAX_LENGTH)

# API 响应里没有任何要加载的资源，所以给最严的一条；`frame-ancestors` 顺带把点击劫持也堵了。
API_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
# 文档页要从 CDN 取资源，只保留不许被套 iframe 这一条。
DOCS_CONTENT_SECURITY_POLICY = "frame-ancestors 'none'"

STATIC_SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=()"),
)

# HSTS 的 max-age 按一年给，并带上子域：反代后面这个头是唯一能防「http 明文回退」的东西。
HSTS_VALUE = "max-age=31536000; includeSubDomains"

GZIP_MINIMUM_SIZE = 1024
GZIP_COMPRESS_LEVEL = 6

# Office 文档（docx/xlsx/pptx）与 pdf 已经是压缩过的容器，压它只是白烧 CPU。
ALREADY_COMPRESSED_TYPES: tuple[str, ...] = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/pdf",
    "application/vnd.oasis.opendocument.spreadsheet",
)

# Starlette 默认的排除表里已经有 text/event-stream、image/*、application/zip 等，这里只做叠加。
GZIP_EXCLUDED_CONTENT_TYPES: tuple[str, ...] = DEFAULT_EXCLUDED_CONTENT_TYPES + ALREADY_COMPRESSED_TYPES


def request_id_from(value: str | None) -> str:
    """客户端带了合规的 id 就用它的（便于把前端日志与后端日志对上），否则自己生成一个。"""
    if value and REQUEST_ID_PATTERN.match(value):
        return value
    return uuid4().hex


def is_docs_path(path: str) -> bool:
    """Swagger UI / ReDoc / OpenAPI 描述文件：它们是给人看的页面，不吃严格 CSP。"""
    return path in ("/docs", "/redoc", "/openapi.json") or path.startswith(("/docs/", "/redoc/"))


def security_headers(path: str, *, https: bool) -> dict[str, str]:
    """按路径给出这一条响应该带的头。纯函数，方便单测把每条分支都走一遍。"""
    headers = dict(STATIC_SECURITY_HEADERS)
    headers["Content-Security-Policy"] = (
        DOCS_CONTENT_SECURITY_POLICY if is_docs_path(path) else API_CONTENT_SECURITY_POLICY
    )
    if https:
        headers["Strict-Transport-Security"] = HSTS_VALUE
    return headers


def request_is_https(scheme: str, forwarded_proto: str | None) -> bool:
    """反代后面 `request.url.scheme` 是 http，得看 nginx 传来的 `X-Forwarded-Proto`。"""
    if scheme == "https":
        return True
    if forwarded_proto is None:
        return False
    return forwarded_proto.split(",")[0].strip().lower() == "https"


def apply_security_headers(response, path: str, *, https: bool, request_id: str) -> None:
    """给一条响应补上安全头与请求 id（中间件与 500 处理器共用同一份口径）。

    500 走的是 Starlette 最外层的 `ServerErrorMiddleware`，它在所有用户中间件之外，
    所以那条响应不会经过 `RequestContextMiddleware` —— 只能由处理器自己补。
    """
    for name, value in security_headers(path, https=https).items():
        response.headers.setdefault(name, value)
    # 请求 id 一律回我们自己认定的那一个：客户端给的是「建议」，不是「事实」。
    response.headers[REQUEST_ID_HEADER] = request_id


class RequestContextMiddleware(BaseHTTPMiddleware):
    """最外层：先定请求 id，再统一加安全响应头（404/429/校验失败这些也会经过这里）。

    放在最外层是有意为之 —— 中间件链上最后 `add_middleware` 的那个在最外面，
    所以哪怕是内层中间件直接返回的 404/429，也会被补上这些头。
    唯一的例外是未处理异常（500）：那条响应由 `main.py` 的处理器自己补头。
    """

    async def dispatch(self, request, call_next):
        request_id = request_id_from(request.headers.get(REQUEST_ID_HEADER))
        request.state.request_id = request_id
        response = await call_next(request)
        https = request_is_https(request.url.scheme, request.headers.get("X-Forwarded-Proto"))
        apply_security_headers(response, request.url.path, https=https, request_id=request_id)
        return response


def request_id_of(request) -> str:
    """日志与 500 响应取请求 id 的唯一入口（拿不到就写 `-`，绝不抛异常）。"""
    return getattr(request.state, "request_id", None) or "-"
