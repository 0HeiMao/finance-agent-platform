"""⑯ 业务时区显式声明 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_timezone.py

为什么这些只能在真后端上验：

- **「今天」是不是业务时区的今天**，取决于进程里那份配置（`BUSINESS_TIMEZONE`）和跑起来的是哪份代码。
  单测只能证明函数算得对；这里要证明**正在服务的那个进程**报出来的业务日、业务时刻、各接口的时间戳
  口径一致，而且和脚本自己按 IANA 规则独立算出来的一致。
- **审计导出的日期窗口**要连着真库：北京时间凌晨那条记录，UTC 看还是前一天——只有真库 + 真接口
  才能验出「窗口按业务日历日切，而不是按 UTC 切」。
- 顺手能抓**进程没重启**：老的进程里没有 `business_timezone` 这个字段，这里第一组检查就会红，
  而单测和 `check_all.py` 都不会知道线上跑的是旧代码。

不改任何数据（只登录、读接口、导一次审计 CSV）。用 urllib + 空 ProxyHandler 绕开宿主代理。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
EXPECTED_NAME = "Asia/Shanghai"
EXPECTED_LABEL = "Asia/Shanghai (UTC+08:00)"
EXPECTED_OFFSET = "UTC+08:00"
TOLERANCE_SECONDS = 300  # 接口的「现在」与本机时钟的允许偏差

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  {'PASS' if ok else 'FAIL'} {name}" + (f"  {detail}" if detail else ""), flush=True)


def call(method: str, path: str, token: str | None = None, body: dict | None = None):
    request = urllib.request.Request(f"{BASE}{path}", method=method)
    if body is not None:
        request.data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with OPENER.open(request, timeout=60) as response:
            status, payload = response.status, response.read()
            headers = dict(response.headers)
    except urllib.error.HTTPError as exc:
        status, payload, headers = exc.code, exc.read(), dict(exc.headers)
    text = payload.decode("utf-8", "replace")
    try:
        return status, json.loads(text), headers
    except json.JSONDecodeError:
        return status, text, headers


def login() -> str:
    status, payload, _ = call("POST", "/auth/login", body={"username": ADMIN[0], "password": ADMIN[1]})
    assert status == 200, f"登录失败：HTTP {status} {payload}"
    return payload["access_token"]


def local_business_day() -> str:
    """脚本这一侧独立算一遍业务日（不 import 后端代码）：用 IANA 规则自己转。"""
    return datetime.now(UTC).astimezone(ZoneInfo(EXPECTED_NAME)).date().isoformat()


def as_offset(text: str) -> timedelta | None:
    """`2026-10-09T00:26:56+08:00` 的偏移；naive 串返回 None。"""
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    return moment.utcoffset()


def header(headers: dict, name: str) -> str | None:
    """响应头大小写不敏感地取值：uvicorn 发出去的是小写键名，不能按 `Content-Type` 硬取。"""
    wanted = name.lower()
    for key, value in headers.items():
        if key.lower() == wanted:
            return value
    return None


def main() -> int:
    print(f"⑯ 业务时区真机冒烟 {datetime.now(UTC).isoformat(timespec='seconds')}", flush=True)
    token = login()
    check("登录 demo 账号", bool(token))

    # ---------------------------------------------------------- 运行态上报
    status, status_payload, _ = call("GET", "/system/status", token=token)
    app = status_payload.get("app", {}) if isinstance(status_payload, dict) else {}
    info = app.get("business_timezone")
    check("GET /system/status 上报 app.business_timezone（没有它说明后端进程还是旧代码，要重启）",
          status == 200 and isinstance(info, dict),
          f"HTTP {status} keys={sorted(app)}")
    if not isinstance(info, dict):
        return finish()
    check("字段齐全：name / label / offset / now / today",
          set(info) == {"name", "label", "offset", "now", "today"}, f"{info}")
    check(f"默认时区是 {EXPECTED_NAME}，偏移写着 {EXPECTED_OFFSET}",
          info.get("name") == EXPECTED_NAME and info.get("offset") == EXPECTED_OFFSET
          and info.get("label") == EXPECTED_LABEL, f"{info.get('label')}")
    check("`now` 是带偏移的 ISO 串（不是 naive 的服务器本机时间）",
          as_offset(info.get("now", "")) == timedelta(hours=8), f"now={info.get('now')}")
    check("`today` 就是 `now` 的日期部分", info.get("today") == (info.get("now") or "")[:10],
          f"today={info.get('today')} now={info.get('now')}")

    # 「现在」得真的是现在：接口与本机时钟（同一时区）差得不多
    try:
        reported = datetime.fromisoformat(info["now"])
    except (KeyError, ValueError):
        reported = None
    drift = abs((reported - datetime.now(UTC)).total_seconds()) if reported else 1e9
    check(f"上报的业务时刻与本机时钟相差 < {TOLERANCE_SECONDS}s（证明不是写死的常量）",
          drift < TOLERANCE_SECONDS, f"偏差 {drift:.1f}s")

    # ---------------------------------------------------------- 业务日 vs UTC 日
    business_day = local_business_day()
    utc_day = datetime.now(UTC).date().isoformat()
    check("接口报的业务日 = 脚本按 IANA 规则独立算出的业务日",
          info.get("today") == business_day, f"接口 {info.get('today')} / 脚本 {business_day}")
    if business_day != utc_day:
        check(f"业务日与 UTC 日确实不同（业务 {business_day} vs UTC {utc_day}）——"
              "这正是「看板里的『今天』」必须声明时区的原因",
              info.get("today") != utc_day, f"{info.get('today')} vs {utc_day}")
        # 同一个瞬间，三个时区是三个不同的日历日：说明决定权在配置，不在机器
        moment = datetime.now(UTC)
        new_york = moment.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        check("同一瞬间在 UTC / Asia/Shanghai / America/New_York 下可以落在三个不同的日历日",
              len({utc_day, business_day, new_york}) >= 2,
              f"UTC {utc_day} / 上海 {business_day} / 纽约 {new_york}")
    else:
        check("（本次运行时业务日与 UTC 日相同，跳过「两者不同」的对照——"
              "UTC+8 凌晨再跑一次就能看到差异）", True, f"{business_day}")

    # ---------------------------------------------------------- 各接口的时间戳口径
    status, dashboard, _ = call("GET", "/dashboard", token=token)
    updated = dashboard.get("updated_at") if isinstance(dashboard, dict) else None
    check("GET /dashboard 的 updated_at 带业务时区偏移",
          status == 200 and as_offset(updated or "") == timedelta(hours=8), f"{updated}")
    if updated:
        gap = abs((datetime.fromisoformat(updated) - datetime.now(UTC)).total_seconds())
        check("updated_at 就是刚才那一刻（不是启动时间）", gap < 600, f"差 {gap:.0f}s")

    status, scope, _ = call("GET", "/export/scope", token=token)
    check("GET /export/scope 明说导出用的是哪个时区的日历日",
          status == 200 and scope.get("timezone") == EXPECTED_LABEL
          and as_offset(scope.get("generated_at", "")) == timedelta(hours=8),
          f"HTTP {status} timezone={scope.get('timezone') if isinstance(scope, dict) else scope}")

    # ---------------------------------------------------------- 审计窗口按业务日切
    today = info["today"]
    status, audit, _ = call(
        "GET", f"/admin/audit?action=user.login&since={today}&until={today}&limit=200", token=token)
    rows = audit.get("data", []) if isinstance(audit, dict) else []
    mine = [row for row in rows if row.get("actor") == ADMIN[0]]
    check("审计按「业务今天」筛选：这次登录本身就在窗口里",
          status == 200 and bool(mine), f"HTTP {status} 命中 {len(rows)} 条")
    # 审计行的时刻**故意保持 UTC**（导出的 CSV 表头就写着「时间(UTC)」，便于对数），
    # ⑯ 改的是筛选用哪个月历日，不是把行内时刻换成业务时区——这里把这条口径也钉住：
    # 它必须是带偏移的显式 UTC，不能是 naive 的「服务器本机时间」。
    row = mine[0] if mine else {}
    stamp = row.get("at")
    business_stamp_day = (
        datetime.fromisoformat(stamp).astimezone(ZoneInfo(EXPECTED_NAME)).date().isoformat()
        if stamp else None)
    check("审计行的时间戳是带偏移的显式 UTC（不是 naive 的服务器本机时间）",
          as_offset(stamp or "") == timedelta(0), f"at={stamp}")
    check("这一条按业务时区算确实落在业务今天——这正是它该出现在今天窗口里的原因",
          business_stamp_day == today, f"at={stamp} → 业务日 {business_stamp_day} / {today}")

    # 昨天（业务日）那一天的窗口里，不该再出现刚才这条。这是全套检查里最硬的一条：
    # 北京时间凌晨这条记录按 UTC 算还停在昨天，只有窗口真的按业务日历日切才会「今天有、昨天没有」。
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
    status, older, _ = call(
        "GET", f"/admin/audit?action=user.login&since={yesterday}&until={yesterday}&limit=200", token=token)
    older_rows = older.get("data", []) if isinstance(older, dict) else []
    check("「业务昨天」的窗口里没有今天这条（窗口真的按业务日切，不是把整表都返回）",
          status == 200 and (stamp or "")[:10] == utc_day
          and not any(item.get("id") == row.get("id") for item in older_rows),
          f"yesterday={yesterday} at={stamp}（UTC 面看是昨天）")

    status, _, headers = call(
        "GET", f"/admin/audit/export?since={today}&until={today}&format=csv", token=token)
    content_type = header(headers, "content-type")
    check("审计 CSV 导出用业务日窗口也能出文件（并回报条数，导出本身留痕）",
          status == 200 and "csv" in (content_type or "").lower()
          and header(headers, "x-export-rows") is not None,
          f"HTTP {status} {content_type} rows={header(headers, 'x-export-rows')}")

    # ---------------------------------------------------------- 用量窗口含今天
    status, usage, _ = call("GET", "/ai/agent/usage?days=1", token=token)
    data = usage.get("data", {}) if isinstance(usage, dict) else {}
    series = usage.get("series") or [] if isinstance(usage, dict) else []
    check("GET /ai/agent/usage 的窗口起点是业务日 00:00",
          status == 200 and (data.get("window_start") or "")[11:] == "00:00:00+08:00",
          f"window_start={data.get('window_start')}")
    check("用量曲线的最后一天就是业务今天（含今天的口径）",
          bool(series) and series[-1].get("date") == today,
          f"最后一天={(series or [{}])[-1].get('date')} / 业务今天={today}")

    status, admin_usage, _ = call("GET", "/admin/agent-usage?days=1", token=token)
    admin_series = admin_usage.get("series") or [] if isinstance(admin_usage, dict) else []
    check("管理员用量口径一致（最后一天也是业务今天）",
          status == 200 and bool(admin_series) and admin_series[-1].get("date") == today,
          f"HTTP {status} 最后一天={(admin_series or [{}])[-1].get('date')}")

    # ---------------------------------------------------------- 本地判定与接口一致
    sys.path.insert(0, str(PROJECT_ROOT))
    from backend import biztime  # noqa: E402
    from backend.config import _valid_timezone  # noqa: E402

    check("本地 biztime 的时区名/文案与真机上报一致",
          biztime.current_name() == info["name"] and biztime.label() == info["label"],
          f"{biztime.label()}")
    check("非法时区名会被拒（本机口径）：UTC+8 / Not/AZone / 空串",
          not biztime.is_valid("UTC+8") and not biztime.is_valid("Not/AZone")
          and _valid_timezone("UTC+8") is False and _valid_timezone("") is False)
    try:
        biztime.zone("Not/AZone")
        raised = False
    except biztime.TimezoneConfigError:
        raised = True
    check("取一个非法时区会抛 TimezoneConfigError（配置错误不静默退默认）", raised)
    # 注意：两个 tzinfo 相同实例的 aware datetime 相减走「墙上时间」（PEP 495），
    # 直接相减会得到 24 小时；必须先换算到 UTC 才看得出夏令时那天只有 23 小时。
    dst_start, dst_end = biztime.day_bounds(date(2026, 3, 8), name="America/New_York")
    length = dst_end.astimezone(UTC) - dst_start.astimezone(UTC)
    check("America/New_York 那天不是 24 小时（夏令时边界不硬加一天）",
          length == timedelta(hours=23), f"{length}")

    return finish()


def finish() -> int:
    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
