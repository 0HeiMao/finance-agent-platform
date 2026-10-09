"""端到端现场演示：注册 -> 登录 -> 预览 -> 导入 -> 指标 -> 预警 -> 看板 -> Agent 问答。

与 scripts/verify_crawled_import.py 的区别：本脚本**真正提交数据**（数据保留在库里，
便于打开前端逐页查看），因此每一步都打印真实响应，供人工目视核对。
清理方式见文件末尾提示。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from sqlalchemy import func, select  # noqa: E402

from backend.db import SessionLocal  # noqa: E402
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction  # noqa: E402

DEFAULT_CSV = PROJECT_ROOT / "data" / "processed" / "accounting_transactions_import.csv"
BASE = "http://127.0.0.1:8000"
DEMO_USER = "demo_analyst"
DEMO_PASSWORD = "demo_password_2026"


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def title(step: str, text: str) -> None:
    print()
    print("=" * 72)
    print(f"步骤 {step}　{text}")
    print("=" * 72)


def show(payload, indent: int = 2, limit: int = 40) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=indent, default=str)
    lines = text.splitlines()
    for line in lines[:limit]:
        print(line)
    if len(lines) > limit:
        print(f"  …（共 {len(lines)} 行，已截断）")


def money(value) -> str:
    return f"{float(value):,.2f}"


async def demo(csv_path: Path, keep: bool) -> int:
    _console()
    if not csv_path.exists():
        print(f"FAIL: 找不到导入文件 {csv_path}", file=sys.stderr)
        return 2

    content = csv_path.read_bytes()
    print(f"演示文件 : {csv_path.relative_to(PROJECT_ROOT)}（{len(content):,} 字节）")
    print(f"后端地址 : {BASE}")

    title("1", "注册演示账号（PBKDF2，600000 轮）")
    credentials = {"username": DEMO_USER, "password": DEMO_PASSWORD}
    # trust_env=False：本机环境里存在重复的 NO_PROXY（含 [::1] 形式），
    # httpx 解析系统代理变量时会把它当端口而崩溃，直连 127.0.0.1 不需要代理。
    async with httpx.AsyncClient(base_url=BASE, timeout=180.0, trust_env=False) as client:
        registered = await client.post("/api/v1/auth/register", json=credentials)
        print(f"POST /api/v1/auth/register -> HTTP {registered.status_code}")
        if registered.status_code == 409:
            print("  该账号已存在，直接用它继续演示（不影响后续步骤）")
        else:
            show(registered.json())

        # ⑳ 起「没有部门」不再等于管理员：只有**空库第一个**注册的账号才自动是管理员
        # （backend/auth.py 的 bootstrap_admin），别的情况下演示账号只是普通账号，后面几步的
        # 管理接口会 403。本脚本在本机跑，直接改库最省事 —— 接口每次都从库里重读账号，
        # 改完立刻生效。真机运维用它：python scripts/grant_admin.py demo_analyst
        from backend.models import User  # noqa: PLC0415  与顶部模型导入同一处

        with SessionLocal() as db:
            demo_user = db.scalar(select(User).where(User.username == DEMO_USER))
            if demo_user is None:
                print("  找不到演示账号，跳过管理员检查", file=sys.stderr)
            elif demo_user.is_admin:
                print(f"  {DEMO_USER} 已经是管理员")
            else:
                demo_user.is_admin = True
                db.commit()
                print(f"  已把 {DEMO_USER} 设为管理员（否则演示到管理接口那几步会 403）")

        title("2", "登录换取 Bearer Token（有效期 12 小时）")
        login = await client.post("/api/v1/auth/login", json=credentials)
        print(f"POST /api/v1/auth/login -> HTTP {login.status_code}")
        if login.status_code != 200:
            show(login.json())
            return 1
        token = login.json()["access_token"]
        print(f"  token_type : {login.json()['token_type']}")
        print(f"  token 前 40 位 : {token[:40]}…（长度 {len(token)}）")
        headers = {"Authorization": f"Bearer {token}"}

        title("3", "未带 Token 访问看板（证明鉴权生效）")
        async with httpx.AsyncClient(base_url=BASE, timeout=60.0, trust_env=False) as anon:
            denied = await anon.get("/api/v1/dashboard", params={"period": "year"})
        print(f"GET /api/v1/dashboard（无 Token）-> HTTP {denied.status_code}")
        show(denied.json())

        title("4", "预览校验：先看数据能不能进，不写库")
        preview = await client.post(
            "/api/v1/financial/preview",
            headers=headers,
            files={"file": (csv_path.name, content, "text/csv")},
        )
        print(f"POST /api/v1/financial/preview -> HTTP {preview.status_code}")
        body = preview.json()
        print(f"  总行数     : {body['total']}")
        print(f"  缺失字段   : {body['missing']}")
        print(f"  错误行数   : {body['error_count']}")
        print(f"  结论       : valid={body['valid']}")
        print("  列名       : " + "、".join(body["columns"]))
        print("  首行预览   :")
        show(body["preview"][:1], limit=30)

        title("5", "正式导入（真实提交，数据保留）")
        imported = await client.post(
            "/api/v1/financial/import",
            headers=headers,
            files={"file": (csv_path.name, content, "text/csv")},
        )
        print(f"POST /api/v1/financial/import -> HTTP {imported.status_code}")
        if imported.status_code == 409:
            with SessionLocal() as probe:
                existing_batch = probe.scalar(
                    select(FinancialTransaction.import_batch)
                    .group_by(FinancialTransaction.import_batch)
                    .order_by(func.count(FinancialTransaction.id).desc())
                    .limit(1)
                )
            print("  此文件之前已导入过（内容哈希去重），本次跳过导入，直接看已有数据")
            result = {"success": True, "imported": 0, "errors": [], "batch": existing_batch,
                      "calculated": [{"period": p} for p in
                                     ["2026-04", "2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]]}
        else:
            result = imported.json()
        print(f"  success    : {result.get('success')}")
        print(f"  导入行数   : {result.get('imported')}（重复导入时为 0，属预期）")
        print(f"  批次哈希   : {result.get('batch')}")
        print(f"  错误       : {result.get('errors')}")
        print(f"  自动计算的期间: {[x.get('period') for x in result.get('calculated') or []]}")

        title("6", "落库核对：直接查 PostgreSQL")
        with SessionLocal() as db:
            total = db.scalar(select(func.count(FinancialTransaction.id)))
            print(f"  financial_transactions 总行数 : {total}")
            period = func.to_char(FinancialTransaction.transaction_date, "YYYY-MM")
            by_month = db.execute(
                select(
                    period,
                    func.count(FinancialTransaction.id),
                    func.sum(FinancialTransaction.revenue_amount),
                    func.sum(FinancialTransaction.expense_amount),
                ).group_by(period).order_by(period)
            ).all()
            for row in by_month:
                print(f"  {row[0]}：{row[1]:>5} 行　收入 {money(row[2] or 0):>20}　费用 {money(row[3] or 0):>24}")
            print()
            print("  financial_metrics 各期间指标（只列全公司汇总行，部门口径行见「部门权限」页）：")
            print(f"  {'期间':<9}{'收入':>20}{'费用':>24}{'净利':>26}{'净利率':>15}")
            for metric in db.scalars(
                    select(FinancialMetric).where(FinancialMetric.department.is_(None))
                    .order_by(FinancialMetric.period)).all():
                print(f"  {metric.period:<9}{money(metric.revenue_amount):>20}"
                      f"{money(metric.expense_amount):>24}{money(metric.net_profit_amount):>26}"
                      f"{float(metric.net_profit_ratio):>15.4f}")
            print()
            print("  financial_alerts 预警（同上，只列全公司汇总行）：")
            for alert in db.scalars(
                    select(FinancialAlert).where(FinancialAlert.department.is_(None))
                    .order_by(FinancialAlert.period)).all():
                print(f"    [{alert.level}] {alert.period} {alert.alert_type} — {alert.description}")

        title("7", "分析接口：该期间汇总")
        summary = await client.get("/api/v1/analysis/summary", headers=headers, params={"period": "2026-07"})
        print(f"GET /api/v1/analysis/summary?period=2026-07 -> HTTP {summary.status_code}")
        show(summary.json())

        title("8", "预警接口 + 预警交易明细")
        alerts = await client.get("/api/v1/analysis/alerts", headers=headers, params={"period": "2026-07"})
        print(f"GET /api/v1/analysis/alerts?period=2026-07 -> HTTP {alerts.status_code}")
        alert_list = alerts.json()
        show(alert_list if len(json.dumps(alert_list, default=str)) < 900 else {"data": alert_list["data"][:1], "总数": len(alert_list["data"])})
        if alert_list.get("data"):
            alert_id = alert_list["data"][0]["id"]
            detail = await client.get(f"/api/v1/analysis/alerts/{alert_id}/transactions", headers=headers)
            print(f"\nGET /api/v1/analysis/alerts/{alert_id}/transactions -> HTTP {detail.status_code}")
            rows = detail.json()["data"]
            print(f"  明细行数 : {len(rows)}（该期间全部交易，上限 200）")
            print(f"  {'日期':<12}{'科目':<16}{'部门':<8}{'费用金额':>22}")
            for row in rows[:6]:
                print(f"  {row['date'][:10]:<12}{row['account']:<16}{row['department']:<8}{money(row['expense']):>22}")
            if len(rows) > 6:
                print(f"  …（还有 {len(rows) - 6} 行）")

        title("9", "看板接口：KPI / 趋势 / 费用构成 / 预警")
        dashboard = await client.get("/api/v1/dashboard", headers=headers, params={"period": "year"})
        print(f"GET /api/v1/dashboard?period=year -> HTTP {dashboard.status_code}")
        board = dashboard.json()
        print(f"  has_data : {board.get('has_data')}")
        print(f"  数据更新 : {board.get('updated_at')}")
        print("  KPI：")
        for item in board.get("kpi", []):
            print(f"    {item['label']:<20} {item['value']:>24}   变化 {item.get('change')}")
        print(f"  覆盖月份 : {board.get('months')}")
        print("  费用构成 :")
        for item in board.get("expenses", []):
            print(f"    {item['name']:<10} {money(item['value']):>26}")
        print(f"  预警摘要 : {board.get('alerts')}")

        title("10", "知识库问答：先入库口径文档，再让 Agent 基于证据回答")
        doc = PROJECT_ROOT / "knowledge-base" / "finance-metrics.md"
        print(f"上传口径文档 : {doc.relative_to(PROJECT_ROOT)}")
        upd = await client.post(
            "/api/v1/rag/documents/upload",
            headers=headers,
            files={"file": (doc.name, doc.read_bytes(), "text/markdown")},
        )
        print(f"POST /api/v1/rag/documents/upload -> HTTP {upd.status_code}")
        show(upd.json(), limit=12)

        question = "2026年7月的费用异常在哪里？成本率异常为什么没有触发？"
        probe = await client.post("/api/v1/ai/rag/chat", headers=headers,
                                  json={"question": question, "top_k": 5})
        print(f"\nPOST /api/v1/ai/rag/chat（非流式，便于查看检索来源）-> HTTP {probe.status_code}")
        probe_body = probe.json()
        print(f"  answer_status : {probe_body.get('answer_status')}")
        names = [s.get("source") if isinstance(s, dict) else str(s) for s in probe_body.get("sources") or []]
        print(f"  检索来源      : {names}")
        preview = (probe_body.get("answer") or "").strip().replace("\n", " ")
        print(f"  回答预览      : {preview[:160]}{'…' if len(preview) > 160 else ''}")

        print(f"\nPOST /api/v1/ai/agent/chat/stream　问题：{question}")
        print("  —— SSE 流式返回（打字机效果的数据源）——")
        answer = []
        sources: list = []
        async with client.stream(
            "POST", "/api/v1/ai/agent/chat/stream", headers=headers, json={"question": question}
        ) as stream:
            print(f"  HTTP {stream.status_code}")
            event = None
            async for line in stream.aiter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    raw = line[5:].strip()
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event == "delta":
                        answer.append(payload.get("text", ""))
                        print(payload.get("text", ""), end="", flush=True)
                    elif event == "done":
                        sources = payload.get("sources") or []
                    elif event == "error":
                        print(f"\n  [错误事件] {payload.get('message')}")
            print()
        print(f"  —— 回答长度 {len(''.join(answer))} 字，检索来源 {sources} ——")

        print()
        print("  边界说明：Agent 只检索知识库文档，**不会**读取已导入的交易数据；")
        print("            所以「基于已导入数据算一下」这类问题它会说证据不足，而不是去查库。")

        title("11", "重复导入同一文件必须被拒绝")
        again = await client.post(
            "/api/v1/financial/import",
            headers=headers,
            files={"file": (csv_path.name, content, "text/csv")},
        )
        print(f"POST /api/v1/financial/import（同一文件第二次）-> HTTP {again.status_code}")
        show(again.json())

    print()
    print("=" * 72)
    print("演示完成。数据已保留在数据库中，可以直接打开前端查看。")
    print("=" * 72)
    print(f"  登录账号 : {DEMO_USER}")
    print(f"  登录密码 : {DEMO_PASSWORD}")
    print("  登录地址 : http://localhost:5173/login")
    print("  可看页面 : /dashboard（看板）、/analysis/profit（利润分析）、"
          "/analysis/cost（成本费用）、/financial/import（导入）、预警明细")
    if not keep:
        print()
        print("  如需清空本次演示数据，在 psql 里执行：")
        print("    DELETE FROM financial_alerts; DELETE FROM financial_metrics;")
        print("    DELETE FROM financial_transactions;")
        print(f"    DELETE FROM users WHERE username = '{DEMO_USER}';")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="端到端现场演示")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    return asyncio.run(demo(Path(args.csv).resolve(), args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
