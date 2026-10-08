"""⑦ 预警规则按部门/期间分档 —— 真机冒烟。

跑法：先起后端（127.0.0.1:8000），再
    .\\.venv\\Scripts\\python.exe scripts\\verify_alert_rule_tiers.py

验的是「同一个 alert_type 有多条规则时，按 (期间, 部门) 取最具体的一条、命中即覆盖」这件事
在真库里真的成立：期间档能盖住默认规则、部门+期间档能盖住期间档、删掉分档规则后回落默认规则，
而且只影响它限定的那个期间/部门。脚本自己建临时受限账号与临时规则，跑完清理并把重算过的期间
复原成默认口径。用 urllib + 空 ProxyHandler 绕开宿主代理。
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
PASSWORD = "TierSmoke#2026"
MARK = "⑦冒烟"

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


def make_scoped(label: str, departments: list[str]) -> tuple[str, int, str]:
    suffix = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    username = f"tier_{label}_{suffix}"
    status, payload = call("POST", "/auth/register", body={"username": username, "password": PASSWORD})
    assert status == 200, f"注册 {username} 失败：{status} {payload}"
    token = login(username, PASSWORD)
    status, payload = call("PATCH", "/auth/me", token=token, body={"departments": departments})
    assert status == 200, f"绑定部门失败：{status} {payload}"
    return token, payload_id(username, token), username


def payload_id(_username: str, token: str) -> int:
    status, payload = call("GET", "/auth/me", token=token)
    assert status == 200, f"读 /auth/me 失败：{status} {payload}"
    return payload["id"]


def alerts(token: str, period: str) -> list[dict]:
    status, payload = call("GET", f"/analysis/alerts?period={urllib.parse.quote(period)}", token=token)
    assert status == 200, f"取 {period} 预警失败：{status} {payload}"
    return payload["data"]


def profit(items: list[dict]) -> tuple[str, str, str] | None:
    for item in items:
        if item["type"] == "利润异常":
            return item["threshold_value"], item["level"], item["description"]
    return None


def recompute(token: str, period: str) -> None:
    status, payload = call("POST", f"/analysis/calculate?period={urllib.parse.quote(period)}", token=token)
    assert status == 200, f"重算 {period} 失败：{status} {payload}"


def rule_body(**overrides) -> dict:
    body = {"alert_type": "利润异常", "metric_name": "net_profit_amount", "comparator": "lt",
            "threshold_value": 0, "level": "high", "description": f"{MARK}-临时"}
    body.update(overrides)
    return body


def main() -> int:
    admin = login(*ADMIN)
    status, catalog = call("GET", "/admin/alert-rules", token=admin)
    assert status == 200, f"读规则失败：{status} {catalog}"
    defaults = [row for row in catalog["data"] if row["is_default"]]
    check("GET /admin/alert-rules 带分档元数据与选项",
          status == 200 and len(defaults) >= 1 and bool(catalog["departments"]) and bool(catalog["periods"]),
          f"默认规则 {len(defaults)} 条、部门选项 {catalog['departments']}、期间选项 {len(catalog['periods'])} 个")
    check("默认规则 scope_label = 全公司 · 所有期间",
          defaults[0]["scope_label"] == "全公司 · 所有期间", defaults[0]["scope_label"])

    token_a, id_a, user_a = make_scoped("a", ["018"])
    token_b, id_b, user_b = make_scoped("b", ["097"])
    check("临时受限账号绑定成功（018 / 097）", True, f"{user_a} / {user_b}")

    # 找一个 018 与 097 都会按默认规则报「利润异常」的期间，再找一个当对照期间
    candidates = sorted(catalog["periods"], reverse=True)
    touched: list[str] = []
    tier_period = control_period = None
    for period in candidates[:4]:
        recompute(admin, period)
        touched.append(period)
        a_has = profit(alerts(token_a, period)) is not None
        b_has = profit(alerts(token_b, period)) is not None
        if a_has and b_has:
            if tier_period is None:
                tier_period = period
            elif control_period is None:
                control_period = period
                break
    check("找到一个 018 与 097 都按默认规则报利润异常的期间", tier_period is not None,
          f"分档期间 {tier_period}、对照期间 {control_period}（已重算 {touched}）")
    if tier_period is None:
        return finish()

    # 期间档：该期间下全公司口径永远不触发利润异常
    status, r2 = call("POST", "/admin/alert-rules", token=admin, body=rule_body(
        period=tier_period, threshold_value=-1e15, level="low", description=f"{MARK}-期间档"))
    check("POST 期间档规则 201 且 scope_label 正确",
          status == 201 and r2.get("scope_label") == f"全公司 · {tier_period}",
          f"{status} {r2.get('scope_label') if isinstance(r2, dict) else r2}")
    status, r1 = call("POST", "/admin/alert-rules", token=admin, body=rule_body(
        department="018", period=tier_period, threshold_value=5e5, level="medium",
        description=f"{MARK}-部门加期间档"))
    check("POST 部门+期间档规则 201 且 scope_label 正确",
          status == 201 and r1.get("scope_label") == f"018 · {tier_period}",
          f"{status} {r1.get('scope_label') if isinstance(r1, dict) else r1}")
    check("分档规则在列表里 is_default=False",
          not r1["is_default"] and not r2["is_default"] and r1["id"] != r2["id"], f"id {r1['id']} / {r2['id']}")

    status, payload = call("POST", "/admin/alert-rules", token=admin, body=rule_body(
        period=tier_period, threshold_value=-1e15))
    check("同 (类型,部门,期间) 重复建规则 → 409", status == 409, f"{status} {payload}")
    status, payload = call("POST", "/admin/alert-rules", token=admin, body=rule_body(description=f"{MARK}-两个维度都空"))
    check("部门与期间都空 → 422", status == 422, f"{status} {str(payload)[:90]}")
    status, payload = call("POST", "/admin/alert-rules", token=admin, body=rule_body(period="2026-13"))
    check("非法期间 → 422", status == 422, f"{status} {str(payload)[:90]}")
    status, payload = call("POST", "/admin/alert-rules", token=admin, body=rule_body(period=tier_period, metric_name="nope"))
    check("非法指标 → 422", status == 422, f"{status} {str(payload)[:90]}")
    status, payload = call("PUT", "/admin/alert-rules/id/99999999", token=admin, body={"threshold_value": 1})
    check("改不存在的规则 → 404", status == 404, f"{status} {str(payload)[:90]}")
    status, payload = call("PUT", f"/admin/alert-rules/id/{r2['id']}", token=admin, body={"department": "", "period": ""})
    check("分档规则被改成两个维度都空 → 422", status == 422, f"{status} {str(payload)[:90]}")
    status, payload = call("PUT", f"/admin/alert-rules/id/{r2['id']}", token=admin, body={"department": "018", "period": tier_period})
    check("把分档规则改成与另一条同范围 → 409", status == 409, f"{status} {str(payload)[:90]}")
    status, payload = call("DELETE", f"/admin/alert-rules/id/{defaults[0]['id']}", token=admin)
    check("默认规则不允许删除 → 400", status == 400, f"{status} {str(payload)[:90]}")

    # 三条口径在同一期间上应当给出三种不同结果
    recompute(admin, tier_period)
    company = profit(alerts(admin, tier_period))
    dept_a = profit(alerts(token_a, tier_period))
    dept_b = profit(alerts(token_b, tier_period))
    check("期间档盖住默认规则：全公司口径不再报利润异常", company is None, f"{company}")
    check("部门+期间档更具体：018 按 500000.00 报、级别 medium",
          dept_a is not None and dept_a[0] == "500000.00" and dept_a[1] == "medium" and MARK in dept_a[2],
          f"{dept_a}")
    check("兄弟部门 097 被期间档挡住（不报利润异常）", dept_b is None, f"{dept_b}")
    if control_period:
        control = (profit(alerts(admin, control_period)), profit(alerts(token_a, control_period)),
                   profit(alerts(token_b, control_period)))
        check("分档只影响它限定的期间：对照期间三口径仍按默认规则报",
              all(item is not None for item in control), f"{control}")

    status, payload = call("PUT", f"/admin/alert-rules/id/{r1['id']}", token=admin,
                           body={"threshold_value": 0, "level": "high"})
    check("PUT 按 id 改分档规则 200", status == 200, f"{status} {str(payload)[:90]}")
    recompute(admin, tier_period)
    dept_a = profit(alerts(token_a, tier_period))
    check("改阈值后 018 跟着变（0.00 / high）且仍不受期间档影响",
          dept_a is not None and dept_a[0] == "0.00" and dept_a[1] == "high", f"{dept_a}")

    status, payload = call("DELETE", f"/admin/alert-rules/id/{r1['id']}", token=admin)
    check("DELETE 部门档规则 200", status == 200, f"{status} {str(payload)[:90]}")
    recompute(admin, tier_period)
    dept_a = profit(alerts(token_a, tier_period))
    check("删掉部门档后 018 回落到期间档（不再报）", dept_a is None, f"{dept_a}")

    call("PUT", f"/admin/alert-rules/id/{r2['id']}", token=admin, body={"threshold_value": 0, "level": "high"})
    recompute(admin, tier_period)
    company = profit(alerts(admin, tier_period))
    dept_b = profit(alerts(token_b, tier_period))
    check("改期间档阈值后全公司与 097 都恢复报利润异常（0.00）",
          company is not None and company[0] == "0.00" and dept_b is not None and dept_b[0] == "0.00",
          f"{company} / {dept_b}")

    status, payload = call("DELETE", f"/admin/alert-rules/id/{r2['id']}", token=admin)
    check("DELETE 期间档规则 200", status == 200, f"{status} {str(payload)[:90]}")
    recompute(admin, tier_period)
    after = (profit(alerts(admin, tier_period)), profit(alerts(token_a, tier_period)), profit(alerts(token_b, tier_period)))
    check("删掉全部分档规则后三口径回到默认规则", all(item is not None and item[0] == "0.00" for item in after), f"{after}")

    for action, label in (("alert_rule.create", "新增"), ("alert_rule.update", "修改"), ("alert_rule.delete", "删除")):
        status, payload = call("GET", f"/admin/audit?action={action}&limit=100", token=admin)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        marked = [row for row in rows if MARK in str(row.get("detail"))]
        check(f"审计留痕 {action}（{label}分档规则）", status == 200 and bool(rows) and bool(marked),
              f"{len(rows)} 条，其中带冒烟标记 {len(marked)} 条")

    return finish(touched, admin, tier_period, (id_a, id_b, user_a, user_b))


def finish(touched: list[str] = (), admin: str = "", tier_period: str | None = None,
           accounts: tuple = ()) -> int:
    if admin and tier_period:
        for period in touched:
            try:
                recompute(admin, period)
            except Exception as exc:  # noqa: BLE001 - 清理阶段的失败只报告
                print(f"  清理重算 {period} 失败：{exc}")
        status, payload = call("GET", "/admin/alert-rules", token=admin)
        rows = payload.get("data", []) if isinstance(payload, dict) else []
        leftovers = [row for row in rows if MARK in (row.get("description") or "")]
        for row in leftovers:
            call("DELETE", f"/admin/alert-rules/id/{row['id']}", token=admin)
        check("临时分档规则清理干净", not leftovers, f"删除 {len(leftovers)} 条")
    for user_id, username in ((accounts[0] if accounts else None, accounts[2] if accounts else None),
                              (accounts[1] if accounts else None, accounts[3] if accounts else None)):
        if user_id:
            status, _ = call("DELETE", f"/admin/users/{user_id}", token=admin)
            print(f"  清理临时账号 {username} → {status}", flush=True)

    failed = [item for item in RESULTS if not item[1]]
    print(f"\n共 {len(RESULTS)} 项，PASS {len(RESULTS) - len(failed)}，FAIL {len(failed)}", flush=True)
    for name, _, detail in failed:
        print(f"  FAIL {name}  {detail}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
