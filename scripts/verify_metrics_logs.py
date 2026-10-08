"""⑬ Prometheus 指标与结构化日志 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000，日志重定向到 output/_backend.log），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_metrics_logs.py

验的是「监控面板上那些数字真的来自这台机器的这台服务」，分四段：

1. **主实例（8000，文本日志）**：`GET /metrics` 是 Prometheus 文本格式且匿名可读；
   `finance_db_up`、按状态的 `finance_background_jobs`/`finance_alerts` 有值，并且
   **和另一条路读到的库状态对得上**（任务总数 = `/admin/jobs` 的 total，预警总数 =
   `/admin/overview` 的 `alert_rows`）—— 只验「字段存在」等于自证，对账才算验证。
2. **计数真的跟着流量走**：先连发三次 `/health`，`finance_http_requests_total` 里
   `route="/api/v1/health"` 的增量必须正好是 3；带 id 的路径两次不同 id 只长一条
   序列（`route=".../{row_id}"`，响应里不能出现 `/123` 字面量）；未匹配路径归 `unmatched`。
   顺带核对直方图的三条口径：桶单调不减、`le="+Inf"` 等于 `_count`、`_count` 与
   实际请求次数一致。
3. **契约不飘 + 压缩**：`/metrics` 不能出现在 `/openapi.json` 的 paths 里（它是给爬取侧
   的文本端点，混进接口契约会让契约比对多出一条永远在变的噪音）；它超过门槛，所以带
   `Accept-Encoding: gzip` 拿到的**网线字节**必须明显小于解压后的，且解压结果与
   `identity` 那份完全一致。另外请求 id 必须能在文本日志里按 `request_id=<id>` 找到
   （用户报障时「这一次」和「那一行」要能对上）。
4. **采样实例（另起两个短命 uvicorn）**：一个 `LOG_FORMAT=json` + `METRICS_TOKEN` +
   `SLOW_PATH_MS=1`，验「没带 token 401 / 带了 200」、日志**每一条**应用日志都是
   合法 JSON 且白名单字段平铺、慢接口告警同样是单行 JSON；另一个 `METRICS_ENABLED=false`
   验开关真的能关掉端点（404）。
   两个实例都往**文件**里写日志（这个沙箱里 python 用管道接子进程输出会被拒）。
   启动前若还有 `running` 的后台任务就先等一会儿 —— 新实例启动时会做「遗留任务重跑」，
   别把正在跑的任务顺带标失败。

脚本只读服务状态、不改数据（唯一写操作是第二个实例自己的日志文件），跑完把两个实例停掉。
"""
from __future__ import annotations

import gzip
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ROOT = "http://127.0.0.1:8000"
BASE = f"{ROOT}/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
LOG_PATH = PROJECT_ROOT / "output" / "_backend.log"

JSON_PORT = 8124
JSON_LOG = PROJECT_ROOT / "output" / "_metrics_json.log"
OFF_PORT = 8125
OFF_LOG = PROJECT_ROOT / "output" / "_metrics_off.log"
INSTANCE_TOKEN = "verify-metrics-9f3c"

RESULTS: list[tuple[str, bool, str]] = []
SKIPPED: list[str] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None,
         headers: dict | None = None, base: str = BASE):
    request = urllib.request.Request(f"{base}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with OPENER.open(request, timeout=60) as response:
            status, payload, response_headers = response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        status, payload, response_headers = exc.code, exc.read(), dict(exc.headers)
    except urllib.error.URLError:
        # 端口上根本没人监听（`port_free()` 就靠这个判断），不是「接口返回了错误」。
        return 0, "", {}
    text = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(text), lowercase_headers(response_headers)
    except json.JSONDecodeError:
        return status, text, lowercase_headers(response_headers)


def lowercase_headers(headers: dict) -> dict:
    """头名统一小写：`dict(response.headers)` 给的是服务端原始大小写（`content-type`），
    按 `Content-Type` 去查会查不到 —— 真机就是这么把「有 Content-Encoding」判成没有的。"""
    return {str(name).lower(): value for name, value in headers.items()}


def raw_get(path: str, headers: dict | None = None, base: str = ROOT):
    """拿**网线上的原始字节**：urllib 不会自动解压，正好用来看 gzip 到底压没压。"""
    request = urllib.request.Request(f"{base}{path}", method="GET")
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    with OPENER.open(request, timeout=60) as response:
        return response.status, lowercase_headers(dict(response.headers)), response.read()


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def skip(name: str, why: str) -> None:
    SKIPPED.append(name)
    print(f"SKIP  {name}  |  {why}", flush=True)


def login(username: str, password: str) -> str:
    status, payload, _ = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def log_text(path: Path) -> str:
    """读日志。**必须先看 BOM 再决定编码**：本仓库的日志有两种来源 ——
    `| Tee-Object` 写出来的是 UTF-16LE（带 `ff fe` BOM），脚本自己 `Popen(stdout=句柄)`
    写出来的是 UTF-8。而「任意偶数长度的 UTF-8 文件」用 `utf-16` 解码**不会报错**，
    只会解出一串乱码（真机踩过：JSON 采样实例的日志被判成「0 条 JSON」）。
    """
    if not path.exists():
        return ""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace")
    return raw.decode("utf-8-sig", "replace")


def _squash(text: str) -> str:
    """把空白全删掉：`| Tee-Object` 会按控制台宽度（120 列）把长行**硬折**进文件，
    `... status=200 durat` 换行 `ion_ms=1.23` 就是真的写在两行里 —— 折点可能正好落在
    `duration_ms=` 中间，所以比对前得把空白整体去掉（连折行处也不补空格）。"""
    return "".join(text.split())


def wait_for_sequence(path: Path, needle: str, tags: tuple[str, ...],
                      window: int = 400, seconds: float = 20.0) -> str:
    """等日志里出现 needle，且它**紧后面**（允许被硬折成多行）带齐所有片段。

    返回找到的那段原始文字；超时返回空串。片段按给定顺序匹配 —— 顺序本身也是信息。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        text = log_text(path)
        start = text.find(needle)
        if start >= 0:
            segment = text[start:start + window]
            squashed = _squash(segment)
            cursor = 0
            for tag in tags:
                found = squashed.find(_squash(tag), cursor)
                if found < 0:
                    break
                cursor = found + len(_squash(tag))
            else:
                return " ".join(segment.split())
        time.sleep(0.25)
    return ""


def sample_value(body: str, sample: str) -> float | None:
    """取某个 sample 行的值（如 `finance_db_up 1`）。"""
    for line in body.splitlines():
        if line.startswith(f"{sample} "):
            return float(line.split(" ", 1)[1])
    return None


def label_value(body: str, metric: str, labels: str) -> float | None:
    """取 `metric{labels} 值`，labels 是精确匹配的标签串（不含大括号）。"""
    return sample_value(body, f"{metric}{{{labels}}}")


def series_sum(body: str, metric: str) -> float:
    total = 0.0
    for line in body.splitlines():
        if line.startswith((f"{metric}{{", f"{metric} ")):
            total += float(line.split(" ")[-1])
    return total


def label_sum(body: str, metric: str, partial: str) -> float:
    """把标签只写一部分的所有序列加起来（状态码可能变，路由模板不会变）。"""
    total = 0.0
    for line in body.splitlines():
        if line.startswith(f"{metric}{{") and partial in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


def port_free(port: int) -> bool:
    status, _, _ = call("GET", "/api/health", base=f"http://127.0.0.1:{port}")
    return status != 200


def wait_ready(port: int, seconds: float = 90.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _, _ = call("GET", "/api/health", base=f"http://127.0.0.1:{port}")
        if status == 200:
            return True
        time.sleep(0.5)
    return False


def start_instance(port: int, log_path: Path, extra_env: dict[str, str]):
    """另起一个 uvicorn：日志重定向到**文件**（沙箱里 python 用管道接子进程输出会被拒）。"""
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONPATH": str(PROJECT_ROOT), **extra_env}
    handle = log_path.open("wb")
    process = subprocess.Popen(  # noqa: S603 —— 命令与参数都是本仓库内的常量
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--no-access-log"],
        cwd=PROJECT_ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT)
    return process, handle


def stop_instance(process, handle) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:  # pragma: no cover - 环境相关
            process.kill()
    handle.close()


def active_jobs(token: str) -> int:
    status, payload, _ = call("GET", "/admin/jobs?status=running&limit=5", token)
    if status != 200 or not isinstance(payload, dict):
        return 0
    return int(payload.get("total") or 0)


def main() -> int:  # noqa: C901 —— 真机冒烟脚本天然是一长串断言
    token = login(*ADMIN)

    # ------------------------------------------------------------------ 1. 文本格式与库状态对账
    status, body, headers = call("GET", "/metrics", base=ROOT)
    check("匿名 GET /metrics 返回 200", status == 200, f"status={status}")
    content_type = headers.get("content-type", "")
    check("Content-Type 是 Prometheus 文本格式",
          content_type.startswith("text/plain; version=0.0.4"), content_type)
    check("含 # HELP 与 # TYPE 头", "# HELP finance_http_requests_total" in body
          and "# TYPE finance_http_request_duration_seconds histogram" in body)
    check("所有指标名都带 finance_ 前缀（不和 go_/process_ 撞名）",
          all(line.split()[2].startswith("finance_") for line in body.splitlines()
              if line.startswith("# TYPE ")),
          f"{sum(1 for line in body.splitlines() if line.startswith('# TYPE '))} 个指标")
    info = re.search(r'finance_app_info\{version="([^"]*)",environment="([^"]*)"\} (\S+)', body)
    check("finance_app_info 带版本与环境且恒为 1",
          bool(info) and info.group(3) == "1", info.group(0) if info else "没找到")
    started = sample_value(body, "finance_process_start_time_seconds")
    check("进程启动时间戳是过去某个时刻",
          started is not None and 0 < started <= time.time(), f"值={started}")
    check("finance_db_up = 1（采集时库是连得上的）", sample_value(body, "finance_db_up") == 1)
    check("在途请求数在响应渲染时就是 1（这条请求自己）",
          sample_value(body, "finance_http_requests_in_flight") == 1)

    jobs_total = series_sum(body, "finance_background_jobs")
    _, jobs, _ = call("GET", "/admin/jobs?limit=1", token)
    check("后台任务总数与 /admin/jobs 对得上",
          jobs_total == float(jobs.get("total") or 0), f"指标={jobs_total} 接口={jobs.get('total')}")
    alerts_total = series_sum(body, "finance_alerts")
    _, overview, _ = call("GET", "/admin/overview", token)
    check("预警总数与 /admin/overview 的 alert_rows 对得上",
          alerts_total == float(overview["counts"]["alert_rows"]),
          f"指标={alerts_total} 接口={overview['counts']['alert_rows']}")

    # ------------------------------------------------------------------ 2. 计数跟着流量走
    route = 'method="GET",route="/api/v1/health",status="200"'
    before = label_value(body, "finance_http_requests_total", route) or 0.0
    for _ in range(3):
        call("GET", "/health")
    _, after_body, _ = call("GET", "/metrics", base=ROOT)
    after = label_value(after_body, "finance_http_requests_total", route) or 0.0
    check("三次 /health 让该路由的计数正好 +3", after - before == 3.0,
          f"{before} → {after}")

    id_route = 'route="/api/v1/financial/transactions/{row_id}"'
    await_growth = label_sum(after_body, "finance_http_requests_total", id_route)
    call("GET", "/financial/transactions/123")
    call("GET", "/financial/transactions/456")
    _, id_body, _ = call("GET", "/metrics", base=ROOT)
    await_now = label_sum(id_body, "finance_http_requests_total", id_route)
    check("两个不同的 id 只长一条路由序列（标签是模板不是原始路径）",
          await_now - await_growth == 2.0 and id_route in id_body,
          f"{await_growth} → {await_now}")
    check("序列里没有把 id 当标签（没有 /123 这样的字面量）", "/123" not in id_body)

    call("GET", "/no/such/path/xyz")
    _, miss_body, _ = call("GET", "/metrics", base=ROOT)
    check("未匹配的路径归 unmatched",
          'finance_http_requests_total{method="GET",route="unmatched",status="404"}' in miss_body)

    # 直方图三条口径：桶单调不减、+Inf 等于 _count、_count 与真实请求次数一致。
    buckets = [float(value) for _, value in
               re.findall(r'route="/api/v1/health",le="([^"]*)"\} (\S+)', after_body)]
    check("直方图桶单调不减", all(b <= a for b, a in zip(buckets, buckets[1:], strict=False)),
          f"桶={buckets[:4]}…")
    inf = re.search(r'finance_http_request_duration_seconds_bucket\{method="GET",'
                    r'route="/api/v1/health",le="\+Inf"\} (\S+)', after_body)
    count = re.search(r'finance_http_request_duration_seconds_count\{method="GET",'
                      r'route="/api/v1/health"\} (\S+)', after_body)
    check('le="+Inf" 的桶等于 _count', bool(inf and count) and inf.group(1) == count.group(1),
          f"inf={inf.group(1) if inf else None} count={count.group(1) if count else None}")
    check("_count 与实际请求次数一致（≥ 上面那三次）",
          bool(count) and float(count.group(1)) >= 3.0, f"count={count.group(1) if count else None}")

    # ------------------------------------------------------------------ 3. 契约不飘 + 压缩 + 请求 id
    _, schema, _ = call("GET", "/openapi.json", base=ROOT)
    check("/metrics 不进接口契约（include_in_schema=False）", "/metrics" not in schema["paths"],
          f"paths={len(schema['paths'])} 条")
    _, gz_headers, gz_bytes = raw_get("/metrics", {"Accept-Encoding": "gzip"})
    _, _, plain_bytes = raw_get("/metrics", {"Accept-Encoding": "identity"})
    encoding = gz_headers.get("content-encoding", "identity")
    unpacked = gzip.decompress(gz_bytes) if encoding == "gzip" else gz_bytes
    check("/metrics 走 gzip 且网线字节明显更小",
          encoding == "gzip" and len(gz_bytes) < len(unpacked),
          f"{len(unpacked)} → {len(gz_bytes)} 字节（{encoding}）")
    # 两次请求之间**只有 /metrics 自己那条序列会变**（访问 /metrics 也会被计数），
    # 所以比对时把这条序列摘掉，剩下的必须逐字节相同。
    def without_self(text: bytes) -> str:
        return "\n".join(row for row in text.decode("utf-8").splitlines()
                         if 'route="/metrics"' not in row)

    check("压过的解压回来与 identity 那份一致（摘掉 /metrics 自己那条自增序列）",
          without_self(unpacked) == without_self(plain_bytes),
          f"{len(unpacked)} vs {len(plain_bytes)} 字节")

    probe_id = f"verify-metrics-{uuid.uuid4().hex[:8]}"
    _, _, health_headers = call("GET", "/health", headers={"X-Request-ID": probe_id})
    echoed = next((value for name, value in health_headers.items()
                   if name.lower() == "x-request-id"), "")
    check("响应回显了客户端给的请求 id", echoed == probe_id, echoed or "没有这个头")
    segment = wait_for_sequence(LOG_PATH, f"request_id={probe_id}",
                                ("path=/api/v1/health", "method=GET", "status=200", "duration_ms="))
    check("文本日志里那一次请求同时带 path/method/status/duration_ms",
          bool(segment), segment[:130] if segment else f"{probe_id} 的日志段一直没出现")

    # ------------------------------------------------------------------ 4. 采样实例：json 日志 + token + 开关
    running = active_jobs(token)
    if running:
        deadline = time.time() + 30
        while running and time.time() < deadline:
            time.sleep(2)
            running = active_jobs(token)
    if running:
        skip("采样实例（json 日志 / METRICS_TOKEN / METRICS_ENABLED=false）",
             f"还有 {running} 个 running 的后台任务，新实例启动会把它们标失败")
    else:
        for name, port in (("JSON 采样实例", JSON_PORT), ("关闭开关实例", OFF_PORT)):
            assert port_free(port), f"{name} 要用的端口 {port} 已被占用"

        process, handle = start_instance(JSON_PORT, JSON_LOG, {
            "LOG_FORMAT": "json", "METRICS_TOKEN": INSTANCE_TOKEN, "SLOW_PATH_MS": "1"})
        try:
            ready = wait_ready(JSON_PORT)
            check("JSON 采样实例起来了", ready, f"127.0.0.1:{JSON_PORT}")
            if ready:
                status, payload, _ = call("GET", "/metrics", base=f"http://127.0.0.1:{JSON_PORT}")
                check("配了 METRICS_TOKEN 后不带 token 是 401", status == 401, f"status={status}")
                status, _, _ = call("GET", "/metrics", base=f"http://127.0.0.1:{JSON_PORT}",
                                    headers={"Authorization": f"Bearer {INSTANCE_TOKEN}"})
                check("带上正确的 Bearer Token 是 200", status == 200, f"status={status}")
                # 打两个请求：/health 走一次中间件，慢接口告警按 SLOW_PATH_MS=1 必然触发。
                call("GET", "/api/v1/health", base=f"http://127.0.0.1:{JSON_PORT}")
                call("GET", "/metrics", base=f"http://127.0.0.1:{JSON_PORT}",
                     headers={"Authorization": f"Bearer {INSTANCE_TOKEN}"})
                time.sleep(1.0)
                lines = [row for row in log_text(JSON_LOG).splitlines() if row.startswith("{")]
                parsed = []
                broken = ""
                for row in lines:
                    try:
                        parsed.append(json.loads(row))
                    except json.JSONDecodeError:
                        broken = row[:120]
                        break
                check("应用日志每一条都是合法的单行 JSON", bool(parsed) and not broken,
                      broken or f"{len(parsed)} 条")
                request_row = next((row for row in parsed
                                    if str(row.get("message", "")).startswith("request request_id=")
                                    and row.get("path") == "/api/v1/health"), None)
                check("请求日志的 JSON 里有 request_id/path/method/status/duration_ms",
                      bool(request_row) and all(key in request_row for key in
                                                ("request_id", "path", "method", "status", "duration_ms")),
                      json.dumps(request_row, ensure_ascii=False)[:120] if request_row else "没找到")
                slow_row = next((row for row in parsed
                                 if row.get("level") == "WARNING"
                                 and "慢接口告警 path=" in str(row.get("message"))), None)
                check("慢接口告警也是单行 JSON 且字段进了 JSON（全项目日志走同一个 formatter）",
                      bool(slow_row) and all(key in slow_row for key in
                                             ("path", "duration_ms", "threshold_ms", "exceeded")),
                      json.dumps(slow_row, ensure_ascii=False)[:120] if slow_row else "没找到 WARNING 行")
        finally:
            stop_instance(process, handle)

        process, handle = start_instance(OFF_PORT, OFF_LOG, {"METRICS_ENABLED": "false"})
        try:
            ready = wait_ready(OFF_PORT)
            check("关闭开关实例起来了", ready, f"127.0.0.1:{OFF_PORT}")
            if ready:
                status, payload, _ = call("GET", "/metrics", base=f"http://127.0.0.1:{OFF_PORT}")
                check("METRICS_ENABLED=false 时 /metrics 是 404",
                      status == 404 and "关闭" in str(payload), f"status={status} {payload}")
                status, _, _ = call("GET", "/api/v1/health", base=f"http://127.0.0.1:{OFF_PORT}")
                check("关掉指标不影响其它接口", status == 200, f"status={status}")
        finally:
            stop_instance(process, handle)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过" + (f"，跳过 {len(SKIPPED)} 项" if SKIPPED else ""))
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL {name}  {detail}")
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
