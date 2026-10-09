"""⑧ 季度/年度落库 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_rollups.py

验的是「季/年合计真的落库了，而且读数的人知道它是不是凑齐了」：
`POST /admin/rollups/rebuild` 之后 `GET /analysis/summary?period=<季>` 的 `source` 必须是
`stored`（不是现算）、金额等于三个月的月指标之和、比率是按相加后的金额重算的；年度缺月必须是
`complete=false` 且 `missing_months` 点名；重算某个月之后它所在的季/年跟着刷新；读一个没落库的
期间只会现算兜底、不会顺手写库；受限账号只看到自己部门的落库行，读管理端 403。
脚本自己建临时受限账号，跑完删掉。用 urllib + 空 ProxyHandler 绕开宿主代理。
"""
from __future__ import annotations

import json
import random
import string
import sys
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000/api/v1"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN = ("demo_analyst", "demo_password_2026")
PASSWORD = "RollupSmoke#2026"

# 演示数据是 2026-04 ~ 2026-10：有的季度三个月齐、年度缺 5 个月。
# 期间不写死：先读管理概览里的月指标清单，再挑「三个月齐的季度」与「缺月的年度」，
# 这样换了数据集（或重新导入过）脚本仍然说得清自己在验哪几期。
EMPTY_PERIOD = "2019-Q1"  # 没有任何月指标，只能现算兜底
DEPT = "018"

RESULTS: list[tuple[str, bool, str]] = []


def call(method: str, path: str, token: str | None = None, body: dict | None = None, raw: bool = False):
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
    if raw:
        return status, text
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


def make_scoped(admin: str, label: str, departments: list[str]) -> tuple[str, int, str]:
    """注册一个临时账号并把数据范围分给它（⑳ 起账号不能自己改，只能由管理员分配）。"""
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    username = f"rollup_{label}_{suffix}"
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    user_id = payload["id"]
    token = login(username, PASSWORD)
    status, payload = call("PATCH", f"/admin/users/{user_id}", token=admin,
                           body={"departments": departments})
    assert status == 200, f"分配部门失败：{status} {payload}"
    status, me = call("GET", "/auth/me", token=token)
    assert status == 200, f"读 /auth/me 失败：{status} {me}"
    return token, user_id, username


def plan_periods(months: list[str]) -> tuple[str, tuple[str, ...], str, tuple[str, ...], tuple[str, ...]]:
    """挑出「三个月齐的季度」与「缺月的年度」，返回 (季度, 该季月份, 年度, 已落月份, 缺的月份)。"""
    present = sorted({month for month in months if len(month) == 7 and month[4] == "-"})
    assert present, f"库里没有任何月指标，先跑导入：{months}"
    year = present[-1][:4]
    year_months = tuple(month for month in present if month.startswith(year))
    missing = tuple(f"{year}-{index:02d}" for index in range(1, 13) if f"{year}-{index:02d}" not in year_months)
    for quarter in range(4, 0, -1):
        span = tuple(f"{year}-{month:02d}" for month in range(quarter * 3 - 2, quarter * 3 + 1))
        if all(month in year_months for month in span):
            return f"{year}-Q{quarter}", span, year, year_months, missing
    raise AssertionError(f"{year} 年没有三个月齐的季度：{year_months}")


def summary(token: str, period: str) -> dict:
    status, payload = call("GET", f"/analysis/summary?period={urllib.parse.quote(period)}", token=token)
    assert status == 200, f"读 {period} 汇总失败：{status} {payload}"
    return payload


def rollups(token: str, **params) -> dict:
    query = "&".join(f"{key}={urllib.parse.quote(str(value))}" for key, value in params.items())
    status, payload = call("GET", f"/admin/rollups{'?' + query if query else ''}", token=token)
    assert status == 200, f"读 /admin/rollups 失败：{status} {payload}"
    return payload


def sweep(admin: str) -> None:
    """清掉上一次中途失败留下的临时账号（脚本崩了就来不及删自己建的那个）。"""
    status, payload = call("GET", "/admin/users?query=rollup_&limit=50", token=admin)
    rows = payload.get("data") if isinstance(payload, dict) else []
    leftovers = [row for row in (rows or []) if str(row.get("username", "")).startswith("rollup_")]
    for row in leftovers:
        call("DELETE", f"/admin/users/{row['id']}", token=admin)
    if leftovers:
        print(f"  清理上次残留的临时账号 {len(leftovers)} 个", flush=True)


def pick_row(payload: dict, *, period: str, department: str = "") -> dict | None:
    for row in payload.get("data", []):
        if row["period"] == period and row["department"] == department:
            return row
    return None


def main() -> int:
    admin = login(*ADMIN)
    sweep(admin)
    status, overview = call("GET", "/admin/overview", token=admin)
    assert status == 200, f"读 /admin/overview 失败：{status} {overview}"
    quarter, quarter_months, year, year_months, year_missing = plan_periods(overview["periods"]["all"])
    token_dep, user_id, username = make_scoped(admin, "dept", [DEPT])
    print(f"临时受限账号 {username}（id={user_id}，部门 {DEPT}）", flush=True)
    print(f"库里有 {len(year_months)} 个月的指标：{'、'.join(year_months)}", flush=True)
    print(f"验的季度 {quarter}（{'、'.join(quarter_months)}）、年度 {year}（缺 {len(year_missing)} 个月：{'、'.join(year_missing)}）\n", flush=True)

    payload = rollups(admin)
    # 顶层的 grains 是给下拉用的 [{value,label}]，覆盖块里的 grains 才是纯值列表
    check("GET /admin/rollups 返回粒度选项与覆盖块",
          payload["coverage"]["grains"] == ["quarter", "year"] and payload["coverage"]["grain_labels"] == {"quarter": "季", "year": "年"}
          and [item["value"] for item in payload["grains"]] == ["quarter", "year"],
          f"{payload['grains']} {payload['coverage']['grain_labels']}")

    status, payload = call("POST", "/admin/rollups/rebuild", token=admin, body={})
    rebuilt = payload.get("rows") if isinstance(payload, dict) else None
    check("POST /admin/rollups/rebuild 全量重算成功", status == 200 and isinstance(rebuilt, int) and rebuilt > 0,
          f"{status}, 落库 {rebuilt} 行")

    snapshot = rollups(admin)
    cover = snapshot["coverage"]
    check("覆盖清单列出已落的季度与年度",
          quarter in cover["quarters"] and year in cover["years"],
          f"quarters={cover['quarters']} years={cover['years']}")
    year_rows = [item for item in cover["incomplete"] if item["period"] == year and item["grain"] == "year"]
    company_rows = [item for item in year_rows if item["department"] == ""]
    check(f"{year} 年度的全公司行被点名为缺 {len(year_missing)} 个月",
          bool(company_rows) and tuple(company_rows[0]["missing_months"]) == year_missing,
          f"{company_rows[0] if company_rows else year_rows[:1]}")
    check("缺月清单里每一行都写明是全公司还是某个部门（并排在全公司行之后）",
          all("department_label" in item for item in cover["incomplete"])
          and [item["department"] for item in year_rows] == sorted([item["department"] for item in year_rows], key=lambda name: name != ""),
          f"{year} 年度 {len(year_rows)} 行：{[(item['department_label'], len(item['missing_months'])) for item in year_rows[:4]]}")
    check("落库行分全公司行与部门行",
          cover["company_rows"] > 0 and cover["department_rows"] > 0 and cover["rows"] == cover["company_rows"] + cover["department_rows"],
          f"rows={cover['rows']} 全公司={cover['company_rows']} 部门={cover['department_rows']}")

    board = summary(admin, quarter)
    check("季度汇总读的是落库行（source=stored）",
          board["source"] == "stored" and board["grain"] == "quarter" and board["complete"] is True,
          f"source={board['source']} grain={board['grain_label']} complete={board['complete']}")
    check("季度汇总写明覆盖月份",
          tuple(board["months"]) == quarter_months and tuple(board["covered_months"]) == quarter_months and board["missing_months"] == [],
          f"{board['months']} covered={board['covered_months']} missing={board['missing_months']}")

    month_boards = {month: summary(admin, month) for month in quarter_months}
    check("月汇总仍是月口径（source=month）",
          all(item["source"] == "month" for item in month_boards.values()),
          f"{ {month: item['source'] for month, item in month_boards.items()} }")
    total = round(sum(item["revenue_amount"] for item in month_boards.values()), 2)
    check("季度收入等于三个月的月指标之和",
          abs(board["revenue_amount"] - total) <= 0.05,
          f"季度 {board['revenue_amount']:.2f} vs 三月之和 {total:.2f}")
    expected_ratio = round(board["net_profit_amount"] / board["revenue_amount"], 4) if board["revenue_amount"] else 0
    check("季度净利率按相加后的金额重算，不是各月比率相加",
          bool(board["revenue_amount"]) and abs(board["net_profit_ratio"] - expected_ratio) < 1e-6,
          f"净利率 {board['net_profit_ratio']} vs 净利/收入 {expected_ratio}")

    year_board = summary(admin, year)
    check("年度汇总读落库且如实报告缺月",
          year_board["source"] == "stored" and year_board["grain"] == "year" and year_board["complete"] is False,
          f"source={year_board['source']} grain={year_board['grain_label']} complete={year_board['complete']}")
    check("年度汇总点名缺哪几个月、落了哪几个月",
          tuple(year_board["missing_months"]) == year_missing and tuple(year_board["months"]) == year_months and len(year_board["covered_months"]) == 12,
          f"缺 {year_board['missing_months']} 落 {len(year_board['months'])} 个月")

    quarters = rollups(admin, grain="quarter")
    check("按粒度筛选取到季度行", bool(quarters["data"]) and all(row["grain"] == "quarter" for row in quarters["data"]),
          f"{len(quarters['data'])} 行")
    dep_row = pick_row(snapshot, period=quarter, department=DEPT)
    check(f"落库里有部门 {DEPT} 的季度行", dep_row is not None and dep_row["revenue_amount"] > 0,
          f"{dep_row['revenue_amount'] if dep_row else None}")

    dep_board = summary(token_dep, quarter)
    check("受限账号读到的是自己部门的落库行",
          dep_board["source"] == "stored" and dep_board["departments"] == [DEPT] and dep_row is not None
          and abs(dep_board["revenue_amount"] - dep_row["revenue_amount"]) <= 0.05,
          f"受限 {dep_board['revenue_amount']:.2f} vs 落库部门行 {dep_row['revenue_amount'] if dep_row else None}")
    status, body = call("GET", "/admin/rollups", token=token_dep)
    check("受限账号读管理端落库清单 403", status == 403, f"{status} {str(body)[:60]}")

    status, body = call("POST", "/admin/rollups/rebuild", token=admin, body={"grains": ["month"]})
    check("grains 只能是 quarter/year → 422", status == 422, f"{status} {str(body)[:80]}")
    status, body = call("POST", "/admin/rollups/rebuild", token=admin, body={"periods": [quarter_months[0]]})
    check("月口径不能指定给落库重算 → 422", status == 422, f"{status} {str(body)[:80]}")
    status, body = call("POST", "/admin/rollups/rebuild", token=admin, body={"grains": ["year"]})
    check("只重算年度粒度", status == 200 and bool(body.get("data")) and all(row["grain"] == "year" for row in body.get("data", [])),
          f"{status}, {body.get('rows')} 行，粒度 {sorted({row['grain'] for row in body.get('data', [])})}")

    before = summary(admin, quarter)
    status, body = call("POST", f"/analysis/calculate?period={urllib.parse.quote(quarter_months[-1])}", token=admin)
    after = summary(admin, quarter)
    check("重算某个月后它所在的季跟着刷新且数字不变（幂等）",
          status == 200 and after["source"] == "stored" and tuple(after["months"]) == quarter_months
          and abs(after["revenue_amount"] - before["revenue_amount"]) <= 0.05,
          f"重算前后季度收入 {before['revenue_amount']:.2f} → {after['revenue_amount']:.2f}")

    rows_before = rollups(admin)["coverage"]["rows"]
    empty = summary(admin, EMPTY_PERIOD)
    rows_after = rollups(admin)["coverage"]["rows"]
    check("没落库的期间现算兜底（source=computed/empty）且读路径不写库",
          empty["source"] in {"computed", "empty"} and rows_after == rows_before,
          f"source={empty['source']} rows {rows_before} → {rows_after}")

    status, audit = call("GET", "/admin/audit?action=rollups.rebuild&limit=50", token=admin)
    rows = audit.get("data", []) if isinstance(audit, dict) else []
    check("审计留痕 rollups.rebuild 且带粒度与行数",
          status == 200 and bool(rows) and any("grains" in str(row.get("detail")) for row in rows),
          f"{len(rows)} 条：{str(rows[0].get('detail'))[:90] if rows else ''}")

    status, overview = call("GET", "/admin/overview", token=admin)
    check("管理概览带 rollups 覆盖块",
          status == 200 and isinstance(overview.get("rollups"), dict) and overview["rollups"]["rows"] == rows_after,
          f"{status}, rows={overview.get('rollups', {}).get('rows')}")

    status, _ = call("DELETE", f"/admin/users/{user_id}", token=admin)
    print(f"  清理临时账号 {username} → {status}", flush=True)

    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
