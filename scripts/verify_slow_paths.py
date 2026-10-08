"""⑪ 按路径耗时阈值告警 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000，日志重定向到 output/_backend.log），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_slow_paths.py

验的是「单次请求真的超过阈值时，运维不看管理页也能知道」：
先记下慢路径的累计超阈值次数，真发一次秒级的 `POST /rag/chat`（要走检索 + 大模型），
再读 `GET /observability/metrics`，该路径的超阈值次数必须涨、`worst_ms` 必须不低于阈值、
`last_at` 要有值；`GET /admin/overview` 给管理页的同一份观测里也要有这条；
后端日志里必须落下一条 `慢接口告警`（否则「告警」只是内存里的数字，没人会被叫醒）。
接着立刻再发一次：次数继续涨，但同一条路径在告警窗口内只写一条日志、被压掉的次数
计进 `suppressed`（否则几十秒的 SSE 会把日志刷满）。
最后验「快的请求不会被记账」：连发两次 `/health`，第二次不能让该路径的超阈值次数变化。

脚本只读服务状态、不改数据；跑完把计数留给服务本身（内存指标，重启即清）。
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
QUESTION = "2026-07 的收入是多少？"
# 观测与日志里记的是**完整**路径（含 /api/v1），而 `call()` 的 BASE 已经带了 /api/v1。
SLOW_PATH = "/api/v1/ai/rag/chat"
SLOW_CALL = "/ai/rag/chat"
FAST_PATH = "/api/v1/health"
FAST_CALL = "/health"
LOG_PATH = Path(__file__).resolve().parent.parent / "output" / "_backend.log"
ALERT_TAG = "慢接口告警"

RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=180) as response:
            status, payload = response.status, response.read()
    except urllib.error.HTTPError as exc:
        status, payload = exc.code, exc.read()
    text = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except json.JSONDecodeError:
        return status, text


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  |  {detail}" if detail else ""), flush=True)


def login(username: str, password: str) -> str:
    status, payload = call("POST", "/auth/login", body={"username": username, "password": password})
    assert status == 200, f"登录 {username} 失败：{status} {payload}"
    return payload["access_token"]


def metrics(token: str) -> dict:
    status, payload = call("GET", "/observability/metrics", token)
    assert status == 200 and isinstance(payload, dict), f"读观测接口失败：{status} {payload}"
    return payload


def exceeded_of(snapshot: dict, path: str) -> int:
    for row in snapshot.get("slow_paths") or []:
        if row.get("path") == path:
            return int(row.get("exceeded") or 0)
    return 0


def log_text() -> str:
    """读后端日志。

    后端是用 `uvicorn ... | Tee-Object -FilePath output\\_backend.log` 起的，而 PowerShell 的
    Tee-Object 在这台机器上写成 **UTF-16LE**（`ff fe` 开头），所以按 utf-8 读会解码失败/读成乱码：
    先两种编码都试一遍，取真的读到中文的那份（否则「日志里有没有告警」会永远判成没有）。
    """
    if not LOG_PATH.exists():
        return ""
    raw = LOG_PATH.read_bytes()
    for encoding in ("utf-16", "utf-8-sig", "utf-8"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if "慢" in text or "告警" in text:
            return text
    return raw.decode("utf-8", "replace")


def alert_lines(text: str, path: str | None = None) -> list[str]:
    lines = [line for line in text.splitlines() if ALERT_TAG in line]
    if path:
        lines = [line for line in lines if f"path={path}" in line]
    return lines


def main() -> int:
    token = login(*ADMIN)

    # 冷启动的第一次健康检查本身可能超过阈值（要连数据库、加载配置），先热身再作基线。
    call("GET", FAST_CALL)
    time.sleep(0.2)

    status, health = call("GET", FAST_CALL)
    check("健康检查可用", status == 200 and isinstance(health, dict), f"status={status}")

    base = metrics(token)
    missing = [key for key in ("slow_paths", "slow_default_ms", "slow_overrides", "slow_log_seconds") if key not in base]
    check("观测接口带慢接口告警字段", not missing, f"缺 {missing}" if missing else f"默认阈值 {base['slow_default_ms']} ms")

    default_ms = int(base["slow_default_ms"] or 0)
    overrides = base["slow_overrides"] or {}
    limit = int(overrides.get(SLOW_PATH, default_ms))
    check("默认阈值是 1000ms 且按路径可覆盖", default_ms == 1000,
          f"默认 {default_ms} ms；覆盖 {overrides or '（无）'}；本路径阈值 {limit} ms")

    log = log_text()
    threshold_line = next((line.strip() for line in log.splitlines() if "慢接口告警阈值" in line), "")
    check("启动日志写明告警阈值", bool(threshold_line),
          threshold_line[:130] or "日志里读不到这行 → 后端多半还是旧代码，或日志没重定向到 output/_backend.log")

    base_slow = exceeded_of(base, SLOW_PATH)
    base_fast = exceeded_of(base, FAST_PATH)
    base_alerts = len(alert_lines(log, SLOW_PATH))

    started = time.monotonic()
    status, answer = call("POST", SLOW_CALL, token, {"question": QUESTION})
    elapsed_ms = round((time.monotonic() - started) * 1000)
    check("真慢请求成功（检索 + 大模型）", status == 200 and isinstance(answer, dict),
          f"status={status}, 客户端耗时 {elapsed_ms} ms")
    check("这次请求真的慢到该报警", elapsed_ms >= limit,
          f"耗时 {elapsed_ms} ms，阈值 {limit} ms" if elapsed_ms >= limit
          else f"只有 {elapsed_ms} ms，没到 {limit} ms（模型太快时这一项会失败，不算告警功能的问题）")

    time.sleep(1.2)  # 等 Tee-Object 把 stdout 刷进日志文件
    after = metrics(token)
    slow_rows = {row.get("path"): row for row in after.get("slow_paths") or []}
    row = slow_rows.get(SLOW_PATH)
    check("慢路径被记进观测", row is not None, f"slow_paths 里有 {list(slow_rows)}")
    if row:
        check("累计超阈值次数 +1 以上", int(row["exceeded"]) > base_slow,
              f"{base_slow} → {row['exceeded']}")
        check("该路径的阈值与最慢耗时如实记录",
              int(row["threshold_ms"]) == limit and float(row["worst_ms"]) >= limit >= 1 and float(row["last_ms"]) > 0,
              f"阈值 {row['threshold_ms']} ms · 最慢 {row['worst_ms']} ms · 最近 {row['last_ms']} ms")
        check("最近一次超阈值有时间戳", bool(row.get("last_at")), f"last_at={row.get('last_at')}")

    log = log_text()
    new_alerts = alert_lines(log, SLOW_PATH)[base_alerts:]
    check("后端日志真的写下告警", bool(new_alerts),
          new_alerts[0].strip()[:160] if new_alerts else f"{LOG_PATH.name} 里没有新的「{ALERT_TAG} path={SLOW_PATH}」")
    if new_alerts:
        check("告警里点名了路径/耗时/阈值",
              "duration_ms=" in new_alerts[0] and f"threshold_ms={limit}" in new_alerts[0],
              new_alerts[0].strip()[:160])

    # 立刻再来一次：同路径在告警窗口内只写一条日志，条数记进 suppressed。
    alerts_before = len(alert_lines(log, SLOW_PATH))
    status2, _ = call("POST", SLOW_CALL, token, {"question": "2026-07 的成本是多少？"})
    time.sleep(1.2)
    third = metrics(token)
    row2 = {row.get("path"): row for row in third.get("slow_paths") or []}.get(SLOW_PATH) or {}
    log = log_text()
    check("第二条慢请求也被记账", status2 == 200 and int(row2.get("exceeded") or 0) > int((row or {}).get("exceeded") or 0),
          f"exceeded {(row or {}).get('exceeded')} → {row2.get('exceeded')}")
    check("同路径告警窗口内不重复刷日志（计进 suppressed）",
          len(alert_lines(log, SLOW_PATH)) == alerts_before and int(row2.get("suppressed") or 0) >= 1,
          f"告警行数 {alerts_before} → {len(alert_lines(log, SLOW_PATH))}，suppressed={row2.get('suppressed')}")

    call("GET", FAST_CALL)
    time.sleep(0.2)
    final = metrics(token)
    check("快请求不会被记成慢接口", exceeded_of(final, FAST_PATH) == base_fast,
          f"{FAST_PATH} 超阈值次数 {base_fast} → {exceeded_of(final, FAST_PATH)}")

    status, overview = call("GET", "/admin/overview", token)
    obs = (overview or {}).get("observability") if isinstance(overview, dict) else None
    check("管理页拿到的同一份观测里有这条告警",
          status == 200 and isinstance(obs, dict)
          and any(item.get("path") == SLOW_PATH for item in obs.get("slow_paths") or []),
          f"status={status}")

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} 项通过", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - 脚本要一句话说清哪儿炸了
        print(f"\n异常终止：{type(exc).__name__}: {exc}", flush=True)
        print("检查后端是否在 127.0.0.1:8000、日志是否重定向到 output/_backend.log", flush=True)
        sys.exit(1)
