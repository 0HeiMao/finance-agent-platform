"""真机验证「安全响应头 + GZip + X-Request-ID」（⑫）：走真实 HTTP 打 127.0.0.1:8000。

为什么不能只在用例里验证：这三样东西的价值全在「**每一条**响应上」——包括 404、SSE、
文件下载、以及最外层 `ServerErrorMiddleware` 直接吐出来的 500。单测能证明函数返回什么，
证明不了真机上每条响应确实带上了、也证明不了压缩是真的发生在网线上。

    .\.venv\Scripts\python.exe scripts\verify_security_headers.py

覆盖 29 项：
  静态安全头与两套 CSP（API 严格 / 文档页宽松）→ 请求 id 的生成、回传、超长与非法值被替换 →
  **日志里能 grep 到响应里那个 id**（用户报 500 时唯一的对账线索）→
  HSTS 只在 `X-Forwarded-Proto: https` 时下发（本机 http 下发会把浏览器锁死）→
  GZip 的四个分支（大 JSON 压且解压后一致、小响应不压、CSV 压、xlsx 不压）→
  SSE 没被压、仍按 chunked 传、带不带 gzip 收到的字节一致 →
  500 也带请求 id 与安全头。

最后一项（500）走的是**同一份 `backend/main.py`、同一套中间件**，只是不经过 uvicorn：
真机没法在不制造故障的前提下触发未处理异常，而为了这一项去临时改库（比如把表改名）
风险太大 —— 脚本一旦中途死掉，库就留在坏状态。所以这里用 `ASGITransport` 直接调应用对象，
并额外用真机日志确认 500 会写的那一行里带着同一个 id。
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402

# 期望值一律**写死在脚本里**，不从 backend.http_headers 里 import ——
# 否则常量被改错时「脚本跟着一起错」，就成了自证式测试。
EXPECTED_STATIC = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
API_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
DOCS_CSP = "frame-ancestors 'none'"
HSTS_VALUE = "max-age=31536000; includeSubDomains"
GZIP_MINIMUM_SIZE = 1024
HEX32 = re.compile(r"^[0-9a-f]{32}$")

GZIP_ACCEPT = {"Accept-Encoding": "gzip"}
IDENTITY = {"Accept-Encoding": "identity"}
# 每次跑换一个 id：日志里可能还留着上一次跑的同一个 id，那样「日志能对账」就成了假绿。
PROBE_ID = f"verify-headers-{uuid4().hex[:8]}"
LONG_ID = "a" * 129
BAD_ID = "id;with;semicolon"
OFFICE_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

RESULTS: list[tuple[bool, str, str]] = []


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def check(name: str, ok, detail: str = "") -> bool:
    RESULTS.append((bool(ok), name, str(detail)))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)
    return bool(ok)


def static_problems(response: httpx.Response) -> str:
    """返回值为空串表示四个静态头 + 严格 CSP 都在。"""
    problems = [f"{name}={response.headers.get(name)!r}（期望 {value!r}）"
                for name, value in EXPECTED_STATIC.items() if response.headers.get(name) != value]
    if response.headers.get("Content-Security-Policy") != API_CSP:
        problems.append(f"CSP={response.headers.get('Content-Security-Policy')!r}")
    return "；".join(problems)


def decode_log(raw: bytes) -> str:
    """把日志字节解成文本，兼顾 PowerShell 写的 UTF-16。

    这个坑踩过一次：`uvicorn ... | Tee-Object -FilePath output\\_backend.log` 在本机
    （Windows PowerShell 5.1）写出的是 **UTF-16LE 带 BOM**，只按 utf-8 读会得到
    「每个 ASCII 字符后面跟一个 \\x00」的乱码 —— 于是 `request_id=` 一个都匹配不上，
    而 PowerShell 自己的 Get-Content/Select-String 读同一个文件却完全正常，
    很容易误判成「后端没写日志」。所以这里按字节读，先认 BOM，再按 NUL 密度判断。
    """
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    if raw.count(b"\x00") > len(raw) // 8:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8", errors="replace")


def log_tail(path: Path, limit: int = 4000) -> str:
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return "\n".join(decode_log(raw).splitlines()[-limit:])


def last_request_line(path: Path) -> str:
    """日志里最后一条 request_id= 行，用来在失败时说明「日志里到底有什么」。"""
    lines = [line for line in log_tail(path).splitlines() if "request_id=" in line]
    return lines[-1].strip() if lines else "(日志里一条 request_id= 都没有)"


def wait_in_log(path: Path, needle: str, seconds: float = 20.0) -> float:
    """等日志出现 needle，返回等了多少秒；超时返回 -1。

    日志是另一个进程写的（uvicorn 的 stdout 经 Tee-Object 落盘），落盘有延迟：
    实测正常在 1 秒内出现，慢的时候几秒（PowerShell 的管道缓冲），所以给足 20 秒。
    （读不出来还有第二个坑：Tee-Object 在 Windows PowerShell 5.1 上写的是 UTF-16，
    见 `decode_log()` —— 那不是“没写日志”，是读法不对。）
    """
    started = time.monotonic()
    deadline = started + seconds
    while time.monotonic() < deadline:
        if needle in log_tail(path):
            return time.monotonic() - started
        time.sleep(0.3)
    return -1.0 if needle not in log_tail(path) else time.monotonic() - started


def raw_length(client: httpx.Client, method: str, url: str, *, headers: dict, **kwargs) -> int:
    """数网线上真实的字节数：`content` 是解压后的，`iter_raw()` 才是收下来的。"""
    with client.stream(method, url, headers=headers, **kwargs) as response:
        response.raise_for_status()
        return sum(len(chunk) for chunk in response.iter_raw())


def run(args) -> int:
    _console()
    log_path = Path(args.log)
    base = args.base_url.rstrip("/")

    with httpx.Client(base_url=base, timeout=120.0, trust_env=False) as client:
        # ---- 静态头 + 请求 id：正常响应、404、以及「日志对得上」 --------------------
        health = client.get("/api/v1/health")
        check("健康检查可用", health.status_code == 200, f"HTTP {health.status_code}")
        problems = static_problems(health)
        check("正常响应的静态安全头与严格 CSP", not problems, problems or "4 个头 + CSP 齐全")

        generated = health.headers.get("X-Request-ID", "")
        check("没带 id 时自动生成 32 位十六进制", bool(HEX32.match(generated)), generated or "(缺失)")
        check("本机 http 不下发 HSTS", "Strict-Transport-Security" not in health.headers,
              "下发一次浏览器就把这个主机记成必须 https")
        check("原有观测中间件没被挤掉", bool(health.headers.get("X-Response-Time-Ms")),
              f"X-Response-Time-Ms={health.headers.get('X-Response-Time-Ms')}")

        missing = client.get("/api/v1/definitely-not-here")
        problems = static_problems(missing)
        check("404 也带齐安全头与请求 id",
              missing.status_code == 404 and not problems and bool(HEX32.match(missing.headers.get("X-Request-ID", ""))),
              f"HTTP {missing.status_code}；{problems or '头齐全'}")

        echoed = client.get("/api/v1/health", headers={"X-Request-ID": PROBE_ID})
        check("合规的客户端 id 原样回传", echoed.headers.get("X-Request-ID") == PROBE_ID,
              f"回来的是 {echoed.headers.get('X-Request-ID')!r}")
        waited = wait_in_log(log_path, f"request_id={PROBE_ID}")
        check("后端日志里能 grep 到响应里的那个 id",
              waited >= 0,
              f"等了 {waited:.1f} 秒" if waited >= 0
              else f"{log_path.name} 里没等到 request_id={PROBE_ID}；最后一条是：{last_request_line(log_path)}")

        too_long = client.get("/api/v1/health", headers={"X-Request-ID": LONG_ID})
        check("超长 id 被换成自己生成的",
              too_long.headers.get("X-Request-ID") != LONG_ID
              and bool(HEX32.match(too_long.headers.get("X-Request-ID", ""))),
              f"129 位 → {too_long.headers.get('X-Request-ID')}")
        illegal = client.get("/api/v1/health", headers={"X-Request-ID": BAD_ID})
        check("带分号的 id 被换成自己生成的",
              illegal.headers.get("X-Request-ID") != BAD_ID
              and bool(HEX32.match(illegal.headers.get("X-Request-ID", ""))),
              f"{BAD_ID!r} → {illegal.headers.get('X-Request-ID')}")

        # ---- CSP 分两套 ----------------------------------------------------------
        spec = client.get("/openapi.json", headers=GZIP_ACCEPT)
        check("接口描述文件走文档那套 CSP",
              spec.headers.get("Content-Security-Policy") == DOCS_CSP,
              spec.headers.get("Content-Security-Policy", "(缺失)"))
        for path in ("/docs", "/redoc"):
            page = client.get(path)
            check(f"{path} 打得开且不被严格 CSP 打坏",
                  page.status_code == 200 and page.headers.get("Content-Security-Policy") == DOCS_CSP,
                  f"HTTP {page.status_code}；CSP={page.headers.get('Content-Security-Policy')}")

        # ---- GZip 的四个分支 -----------------------------------------------------
        compressed = int(spec.headers.get("Content-Length", "0") or 0)
        check("几十 KB 的 JSON 被压了",
              spec.headers.get("Content-Encoding") == "gzip" and compressed < len(spec.content),
              f"网线 {compressed} 字节 → 解压后 {len(spec.content)} 字节")
        check("压缩响应带 Vary: Accept-Encoding",
              "accept-encoding" in (spec.headers.get("Vary", "").lower()),
              f"Vary={spec.headers.get('Vary')!r}（不然中间缓存会把压缩体发给不收 gzip 的客户端）")

        plain = client.get("/openapi.json", headers=IDENTITY)
        check("解压后与不压缩的那份完全一致",
              spec.json() == plain.json(),
              f"端点 {len(spec.json().get('paths', {}))} 个")

        small = client.get("/api/v1/health", headers=GZIP_ACCEPT)
        check("小响应不压（39 字节压了只多头部开销）",
              "Content-Encoding" not in small.headers,
              f"阈值 {GZIP_MINIMUM_SIZE} 字节，实测响应 {len(small.content)} 字节")

        # ---- 文件下载：该压的压，已经是压缩包的别压 -------------------------------
        login = client.post("/api/v1/auth/login",
                            json={"username": args.username, "password": args.password})
        check("登录仍然可用（⑫ 没把接口弄坏）", login.status_code == 200, f"HTTP {login.status_code}")
        auth = {"Authorization": "Bearer " + login.json()["access_token"]}

        csv_wire = raw_length(client, "GET", "/api/v1/export/transactions",
                              headers={**auth, **GZIP_ACCEPT}, params={"format": "csv"})
        csv_plain = raw_length(client, "GET", "/api/v1/export/transactions",
                               headers={**auth, **IDENTITY}, params={"format": "csv"})
        check("CSV 导出（明细）被压了",
              csv_wire < csv_plain // 2,
              f"{csv_plain} 字节 → 网线 {csv_wire} 字节（省 {100 - csv_wire * 100 // max(csv_plain, 1)}%）")

        xlsx = client.get("/api/v1/export/transactions", headers={**auth, **GZIP_ACCEPT},
                          params={"format": "xlsx"})
        check("xlsx 导出不压（它本身就是 zip）",
              xlsx.status_code == 200 and xlsx.headers.get("Content-Type", "").startswith(OFFICE_XLSX)
              and "Content-Encoding" not in xlsx.headers,
              f"{xlsx.headers.get('Content-Type', '')[:60]}… {len(xlsx.content)} 字节，"
              f"Content-Encoding={xlsx.headers.get('Content-Encoding')!r}")

        # ---- SSE：既不能压，也必须还按块传 ---------------------------------------
        # 这里**不**断言「首块早于末块」：本项目的中间件栈里有 BaseHTTPMiddleware，
        # 它会把流式响应攒到最后一次下发；而且在 Windows 上，流式生成器里的 asyncio.sleep
        # 从第二发起会瞬间返回（实测第 1 发 64 ms、第 2 发起 2.5 ms）。分块时间戳在这个环境里
        # 根本区分不出「在流」和「攒完再吐」，所以只断言能证明的事：没被压、按 chunked 传、
        # 带 gzip 与不带 gzip 收到的字节一模一样。
        sse_headers: dict[str, str] = {}
        raw = bytearray()
        chunks = 0
        with client.stream("POST", "/api/v1/chat/stream", json={"message": "SSE 压缩检查"},
                           headers=GZIP_ACCEPT) as stream:
            sse_headers = {name.lower(): value for name, value in stream.headers.items()}
            for chunk in stream.iter_raw():
                chunks += 1
                raw.extend(chunk)
        check("SSE 没被 gzip（压了就得攒够一块才吐）",
              "content-encoding" not in sse_headers,
              f"Content-Type={sse_headers.get('content-type')}；"
              f"Content-Encoding={sse_headers.get('content-encoding')!r}")
        check("SSE 是按块传的（没有 Content-Length，走 chunked）",
              chunks > 1 and sse_headers.get("content-length") is None,
              f"{chunks} 块、Transfer-Encoding={sse_headers.get('transfer-encoding')!r}")

        identity_raw = bytearray()
        with client.stream("POST", "/api/v1/chat/stream", json={"message": "SSE 压缩检查"},
                           headers=IDENTITY) as plain_stream:
            for chunk in plain_stream.iter_raw():
                identity_raw.extend(chunk)
        data_lines = raw.decode("utf-8", "replace").count("data: ")
        check("SSE 带不带 gzip 拿到的字节完全一样",
              raw == identity_raw and data_lines >= 2,
              f"{data_lines} 个事件、{len(raw)} 字节；"
              f"Cache-Control={sse_headers.get('cache-control')!r}")

        # ---- 反代场景：HSTS 只在 https 时下发 ------------------------------------
        forwarded = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https"})
        check("X-Forwarded-Proto: https 时下发 HSTS",
              forwarded.headers.get("Strict-Transport-Security") == HSTS_VALUE,
              forwarded.headers.get("Strict-Transport-Security", "(缺失)"))
        chained = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "https, http"})
        check("多段链路（https, http）也认 https",
              chained.headers.get("Strict-Transport-Security") == HSTS_VALUE,
              chained.headers.get("Strict-Transport-Security", "(缺失)"))
        downgrade = client.get("/api/v1/health", headers={"X-Forwarded-Proto": "http"})
        check("明说 http 时不下发 HSTS",
              "Strict-Transport-Security" not in downgrade.headers,
              "反代上一段是明文时不能把浏览器锁成 https")

    # ---- 500：同一份应用对象、同一套中间件，只差 uvicorn 那一层 -------------------
    code = check_unhandled_error()
    return code


def check_unhandled_error() -> int:
    """未处理异常的 500 也带请求 id 与安全头（真机无法在不制造故障的前提下触发）。

    必须走异步客户端：httpx 0.28 的 `ASGITransport` 只继承 `AsyncBaseTransport`，
    没有 `__enter__`，套在同步 `Client` 里会直接 `AttributeError`。
    """
    import asyncio

    from backend.main import app

    async def _boom():
        raise RuntimeError("verify_security_headers 故意炸")

    async def _call() -> httpx.Response:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     trust_env=False) as client:
            return await client.get("/api/v1/__verify_boom", headers={"X-Request-ID": PROBE_ID})

    app.add_api_route("/api/v1/__verify_boom", _boom)
    route = app.router.routes[-1]
    try:
        response = asyncio.run(_call())
    finally:
        app.router.routes.remove(route)

    body = response.json() if response.headers.get("Content-Type", "").startswith("application/json") else {}
    problems = static_problems(response)
    check("未处理异常返回 500 且带请求 id",
          response.status_code == 500
          and body.get("request_id") == PROBE_ID
          and response.headers.get("X-Request-ID") == PROBE_ID,
          f"HTTP {response.status_code}；body request_id={body.get('request_id')!r}")
    check("500 也带齐安全头（它绕过所有中间件，是处理器自己补的）",
          not problems,
          problems or "4 个头 + CSP 齐全")
    check("500 只吐一句人话，不回栈信息",
          body.get("detail") == "服务器内部错误" and "Traceback" not in response.text,
          f"detail={body.get('detail')!r}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真机验证安全响应头 / GZip / X-Request-ID（⑫）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default="demo_analyst")
    parser.add_argument("--password", default="demo_password_2026")
    parser.add_argument("--log", default=str(PROJECT_ROOT / "output" / "_backend.log"),
                        help="后端日志，用来确认响应里的请求 id 落在日志里")
    args = parser.parse_args()

    started = time.monotonic()
    code = run(args)
    if code:
        return code
    passed = sum(1 for ok, _name, _detail in RESULTS if ok)
    failed = [name for ok, name, _detail in RESULTS if not ok]
    if failed:
        print("失败项：" + "；".join(failed))
    print(f"\n{passed}/{len(RESULTS)} 项通过")
    print(f"总用时 {time.monotonic() - started:.1f} 秒")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 —— 冒烟脚本要把原因打人话
        print(f"\n异常终止：{type(exc).__name__}: {exc}")
        print("检查后端是否在 127.0.0.1:8000、前端 dev 5173 与 finance-postgres 容器是否在跑")
        sys.exit(1)
