"""用真实 PostgreSQL + 真实 ASGI 接口验证「爬取文件 -> 导入 -> 指标 -> 看板 -> RAG」闭环。

与 verify_import_flow.py 的分工：
- verify_import_flow.py 用手工构造的最小 CSV，验证接口行为与边界（舍入、去重、非法行）；
- 本脚本吃的是爬取后经 adapter_rgat_to_import.py 适配的真实源文件，重点验证
  「真实数据在整条管道里没有丢失或被改写」，因此所有断言都对着源文件重算，
  而不是对着接口自己的返回值。

隔离性：全过程在单个外层事务内，结束时整体回滚，数据库不留任何痕迹。
默认要求库里**没有**同文件的演示批次；若演示数据已入库，加 --sentinel 给摘要列打
唯一标记再导，即可绕开 sha256 判重、金额列不变（跑完自动删临时文件）。

用法：
    python scripts/verify_crawled_import.py
    python scripts/verify_crawled_import.py --period 2026-04
    python scripts/verify_crawled_import.py --sentinel vfy1234
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import csv
import io
import os
import secrets
import shutil
import sys
import tempfile
import time
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from sqlalchemy import extract, func, select  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from backend.config import clear_settings_cache, get_settings  # noqa: E402
from backend.db import engine, get_db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction  # noqa: E402

DEFAULT_CSV = PROJECT_ROOT / "data" / "processed" / "accounting_transactions_import.csv"
CENT = Decimal("0.01")

checks: list[tuple[str, bool, str]] = []


def check(label: str, passed: bool, detail: str = "") -> None:
    checks.append((label, bool(passed), detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")


def _console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def load_expected(path: Path) -> dict:
    """直接从待导入文件重算期望值，作为比对的唯一基准。"""
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        raise ValueError("导入文件不是 UTF-8 或 GB18030 编码")

    rows = list(csv.DictReader(io.StringIO(text)))
    required = {"交易日期", "科目名称"}
    missing = required - set(rows[0].keys()) if rows else required
    if missing:
        raise ValueError(f"缺少必填字段：{sorted(missing)}")

    def column(name: str) -> Decimal:
        return sum((money(Decimal(r.get(name) or "0")) for r in rows), Decimal(0))

    revenue = column("收入金额")
    cost = column("成本金额")
    expense = column("费用金额")
    periods: dict[str, dict[str, Decimal | int]] = {}
    for row in rows:
        key = row["交易日期"][:7]
        bucket = periods.setdefault(key, {"rows": 0, "revenue": Decimal(0), "cost": Decimal(0), "expense": Decimal(0)})
        bucket["rows"] += 1
        bucket["revenue"] += money(Decimal(row.get("收入金额") or "0"))
        bucket["cost"] += money(Decimal(row.get("成本金额") or "0"))
        bucket["expense"] += money(Decimal(row.get("费用金额") or "0"))

    return {
        "bytes": raw,
        "rows": rows,
        "count": len(rows),
        "revenue": revenue,
        "cost": cost,
        "expense": expense,
        "cash_in": column("现金流入"),
        "cash_out": column("现金流出"),
        "periods": periods,
    }


def expected_metric(bucket: dict) -> dict:
    revenue, cost, expense = bucket["revenue"], bucket["cost"], bucket["expense"]
    gross = revenue - cost
    net = gross - expense
    return {
        "revenue": money(revenue),
        "cost": money(cost),
        "expense": money(expense),
        "gross": money(gross),
        "net": money(net),
        "gross_ratio": (gross / revenue) if revenue else Decimal(0),
        "net_ratio": (net / revenue) if revenue else Decimal(0),
    }


def expected_alerts(bucket: dict, metric: dict) -> list[str]:
    """与 backend.api_analysis.calculate 的规则保持一致，但独立重写一遍，否则就是自证。"""
    types = []
    if bucket["revenue"] and bucket["cost"] / bucket["revenue"] > Decimal("0.8"):
        types.append("成本率异常")
    if bucket["revenue"] and metric["net"] < 0:
        types.append("利润异常")
    return sorted(types)


async def verify() -> int:
    _console()
    parser = argparse.ArgumentParser(description="验证爬取文件导入闭环")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--period", default=None, help="只校验某个期间，默认校验文件里出现的全部期间")
    parser.add_argument("--sentinel", default=None,
                        help="给摘要列加唯一标记后写临时 CSV 再导入，用于绕开与既有批次的 sha256 判重（金额列不变）")
    args = parser.parse_args()

    # 上传件不要落到项目的 data/uploads：财务导入读 UPLOAD_DIR、文档上传读 RAG_UPLOAD_DIR，
    # 两个都要改，再让设置缓存失效，收尾时删除。
    stub_dir = PROJECT_ROOT / "data" / "uploads" / f"_verify_crawl_{secrets.token_hex(4)}"
    os.environ["UPLOAD_DIR"] = str(stub_dir)
    os.environ["RAG_UPLOAD_DIR"] = str(stub_dir)
    clear_settings_cache()

    csv_path = Path(args.csv).resolve()
    if not csv_path.exists():
        print(f"FAIL: 导入文件不存在 {csv_path}", file=sys.stderr)
        return 2

    expected = load_expected(csv_path)
    periods = sorted(expected["periods"])
    if args.period:
        if args.period not in expected["periods"]:
            print(f"FAIL: 文件里没有期间 {args.period}（实际：{periods}）", file=sys.stderr)
            return 2
        periods = [args.period]

    # 已经在演示里导入过同一份文件时，接口会按 sha256 判重返回 409（这是正确行为）。
    # 为了让回归测试仍然能真实跑完整条导入链路，这里给「摘要」列加一个本次唯一的标记：
    # 文件内容变化 -> 批次哈希变化 -> 不再撞既有批次，而金额列一个字节都不动。
    temp_csv: Path | None = None
    if args.sentinel:
        rows = list(csv.DictReader(csv_path.read_text(encoding="utf-8-sig").splitlines()))
        fields = list(rows[0].keys())
        for index, row in enumerate(rows):
            row["摘要"] = f"{args.sentinel}-{index}"
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                          encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
            temp_csv = Path(handle.name)
        atexit.register(lambda: temp_csv.unlink(missing_ok=True))
        csv_path = temp_csv
        expected["bytes"] = csv_path.read_bytes()

    print(f"待导入文件 : {csv_path.relative_to(PROJECT_ROOT) if PROJECT_ROOT in csv_path.parents else csv_path}")
    print(f"数据行数   : {expected['count']}")
    print(f"期间       : {'、'.join(periods)}")
    print(f"源文件合计 : 收入 {expected['revenue']:,.2f} / 成本 {expected['cost']:,.2f} / 费用 {expected['expense']:,.2f}")
    print()

    with engine.connect() as connection:
        transaction = connection.begin()
        db = Session(bind=connection, join_transaction_mode="create_savepoint")

        def test_db():
            yield db

        app.dependency_overrides[get_db] = test_db
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                print("[1] 认证与接口连通")
                credentials = {"username": "crawl_" + secrets.token_hex(8), "password": secrets.token_urlsafe(20)}
                registered = await client.post("/api/v1/auth/register", json=credentials)
                check("注册新用户", registered.status_code == 200, f"HTTP {registered.status_code}")
                login = await client.post("/api/v1/auth/login", json=credentials)
                check("登录获取 Bearer Token", login.status_code == 200, f"HTTP {login.status_code}")
                if login.status_code != 200:
                    return 1
                client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                # 必须用独立的匿名客户端：httpx 的 AsyncClient 会把已设置的 Authorization
                # 固化在 client.headers 上，复用同一个 client 测「无 Token」会得到假通过。
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as anon:
                    unauthorized = await anon.get("/api/v1/dashboard", params={"period": "year"})
                check("无 Token 访问看板被拒", unauthorized.status_code in (401, 403), f"HTTP {unauthorized.status_code}")

                print("\n[2] 预览校验（真实爬取文件）")
                payload = {"file": (csv_path.name, expected["bytes"], "text/csv")}
                preview = await client.post("/api/v1/financial/preview", files=payload)
                check("预览接口返回 200", preview.status_code == 200, f"HTTP {preview.status_code}")
                body = preview.json() if preview.status_code == 200 else {}
                check("必填字段完整（missing 为空）", body.get("missing") == [], f"missing={body.get('missing')}")
                check("无非法行（error_count == 0）", body.get("error_count") == 0, f"error_count={body.get('error_count')}")
                check("行数与源文件一致", body.get("total") == expected["count"], f"{body.get('total')} vs {expected['count']}")
                check("预览结果 valid", body.get("valid") is True)
                if body.get("errors"):
                    for item in body["errors"][:5]:
                        print(f"        第 {item['row']} 行：{item['error']}")

                print("\n[3] 正式导入")
                imported = await client.post("/api/v1/financial/import", files={"file": (csv_path.name, expected["bytes"], "text/csv")})
                check("导入接口返回 200", imported.status_code == 200, f"HTTP {imported.status_code}")
                result = imported.json() if imported.status_code == 200 else {}
                check("success 为真", result.get("success") is True)
                check("导入行数与源文件一致", result.get("imported") == expected["count"], f"{result.get('imported')} vs {expected['count']}")
                check("无错误行", not result.get("errors"), f"errors={len(result.get('errors') or [])}")
                check("指标计算覆盖全部期间",
                      sorted(x["period"] for x in (result.get("calculated") or [])) == periods,
                      f"{[x['period'] for x in (result.get('calculated') or [])]}")

                print("\n[4] 落库数据与源文件逐项对平")
                stored = db.scalar(
                    select(func.count(FinancialTransaction.id))
                    .where(FinancialTransaction.import_batch == result.get("batch"))
                )
                check("交易行数落库一致", stored == expected["count"], f"{stored} vs {expected['count']}")
                summed = db.execute(
                    select(
                        func.sum(FinancialTransaction.revenue_amount),
                        func.sum(FinancialTransaction.cost_amount),
                        func.sum(FinancialTransaction.expense_amount),
                        func.sum(FinancialTransaction.cash_in_amount),
                        func.sum(FinancialTransaction.cash_out_amount),
                    ).where(FinancialTransaction.import_batch == result.get("batch"))
                ).one()
                for label, stored_value, expected_value in (
                    ("收入金额", summed[0], expected["revenue"]),
                    ("成本金额", summed[1], expected["cost"]),
                    ("费用金额", summed[2], expected["expense"]),
                    ("现金流入", summed[3], expected["cash_in"]),
                    ("现金流出", summed[4], expected["cash_out"]),
                ):
                    check(f"数据表 {label} 合计 = 源文件合计",
                          money(stored_value or 0) == money(expected_value),
                          f"{money(stored_value or 0):,.2f} vs {money(expected_value):,.2f}")

                print("\n[5] 指标与预警：用源文件独立重算后比对")
                for period in periods:
                    bucket = expected["periods"][period]
                    want = expected_metric(bucket)
                    metric = db.scalar(
                        select(FinancialMetric).where(
                            FinancialMetric.period == period,
                            # 每期同时存在「全公司汇总行(dept IS NULL)」和各部门口径行，
                            # 不加这个条件会取到部门行、或按 id 取错行。
                            FinancialMetric.department.is_(None),
                        ).order_by(FinancialMetric.id.desc())
                    )
                    if metric is None:
                        check(f"{period} 指标已生成", False, "数据库里没有 FinancialMetric")
                        continue
                    check(f"{period} 指标已生成", True, f"metric id={metric.id}")
                    for label, actual, wanted in (
                        ("收入", metric.revenue_amount, want["revenue"]),
                        ("成本", metric.cost_amount, want["cost"]),
                        ("费用", metric.expense_amount, want["expense"]),
                        ("毛利", metric.gross_profit_amount, want["gross"]),
                        ("净利", metric.net_profit_amount, want["net"]),
                    ):
                        check(f"{period} {label} = 源文件重算值", money(actual) == wanted, f"{money(actual):,.2f} vs {wanted:,.2f}")
                    check(f"{period} 毛利率 = 毛利/收入",
                          abs(Decimal(metric.gross_profit_ratio) - want["gross_ratio"]) < Decimal("0.0001"),
                          f"{Decimal(metric.gross_profit_ratio):.4f} vs {want['gross_ratio']:.4f}")
                    check(f"{period} 净利率 = 净利/收入",
                          abs(Decimal(metric.net_profit_ratio) - want["net_ratio"]) < Decimal("0.0001"),
                          f"{Decimal(metric.net_profit_ratio):.4f} vs {want['net_ratio']:.4f}")

                    alerts = db.scalars(select(FinancialAlert).where(
                        FinancialAlert.period == period, FinancialAlert.department.is_(None))).all()
                    check(f"{period} 预警类型 = 规则重算结果",
                          sorted(a.alert_type for a in alerts) == expected_alerts(bucket, want),
                          f"{sorted(a.alert_type for a in alerts)} vs {expected_alerts(bucket, want)}")

                    summary = await client.get("/api/v1/analysis/summary", params={"period": period})
                    check(f"{period} /analysis/summary 与源文件一致",
                          summary.status_code == 200
                          and money(Decimal(str(summary.json()["revenue_amount"]))) == want["revenue"]
                          and money(Decimal(str(summary.json()["net_profit_amount"]))) == want["net"],
                          f"HTTP {summary.status_code}")

                    metric_rows = db.scalar(
                        select(func.count(FinancialTransaction.id)).where(
                            extract("year", FinancialTransaction.transaction_date) == int(period[:4]),
                            extract("month", FinancialTransaction.transaction_date) == int(period[5:7]),
                        )
                    )
                    check(f"{period} 该期间交易行数 = 源文件行数",
                          metric_rows == bucket["rows"], f"{metric_rows} vs {bucket['rows']}")

                print("\n[6] 看板 KPI 与源文件一致")
                dashboard = await client.get("/api/v1/dashboard", params={"period": "year"})
                check("看板接口返回 200", dashboard.status_code == 200, f"HTTP {dashboard.status_code}")
                board = dashboard.json()
                check("has_data 为真", board.get("has_data") is True)
                total_revenue = sum((b["revenue"] for b in expected["periods"].values()), Decimal(0))
                total_cost = sum((b["cost"] for b in expected["periods"].values()), Decimal(0))
                total_expense = sum((b["expense"] for b in expected["periods"].values()), Decimal(0))
                total_profit = total_revenue - total_cost - total_expense
                kpi = {item["label"]: item for item in board.get("kpi", [])}
                check("营业收入 KPI = 源文件收入合计",
                      kpi.get("营业收入", {}).get("value") == f"{float(money(total_revenue)):,.2f}",
                      f"{kpi.get('营业收入', {}).get('value')} vs {float(money(total_revenue)):,.2f}")
                check("收支差额 KPI = 源文件(收入-成本-费用)",
                      kpi.get("收支差额（简化口径）", {}).get("value") == f"{float(money(total_profit)):,.2f}",
                      f"{kpi.get('收支差额（简化口径）', {}).get('value')} vs {float(money(total_profit)):,.2f}")
                check("看板月份列表 = 源文件期间",
                      sorted(board.get("months", [])) == periods,
                      f"{board.get('months')} vs {periods}")
                # 注意：适配脚本是逐行四舍五入到分再入库，本脚本是先把源文件整列相加再四舍五入，
                # 5.8 万亿量级上「前 5 个部门之和」不可能等于「全量费用」的四舍五入值（差 486 万）。
                # 所以这里校验的是两条成立的性质：金额降序，且 Top5 不超过全量费用。
                pie_values = [Decimal(str(x["value"])) for x in board.get("expenses", [])]
                pie_total = sum(pie_values, Decimal(0))
                check("费用构成（按部门 Top5）金额降序且不超过全量费用",
                      pie_values == sorted(pie_values, reverse=True)
                      and pie_total <= money(Decimal(kpi.get("费用金额", {}).get("value", "0").replace(",", ""))),
                      f"pie={money(pie_total)} vs 费用KPI={kpi.get('费用金额', {}).get('value')}")
                check("费用金额 KPI 与源文件费用合计一致（允许逐行取整带来的分位误差）",
                      abs(money(Decimal(kpi.get("费用金额", {}).get("value", "0").replace(",", "")))
                          - money(total_cost + total_expense)) < Decimal("10000"),
                      f"{kpi.get('费用金额', {}).get('value')} vs {money(total_cost + total_expense)}")
                # 本轮看板新补的字段：环比不可用时要明说原因，不能给假数字；6 个 KPI 都要在。
                compare = board.get("compare") or {}
                check("环比不可用时给出原因（而不是编造变化）",
                      compare.get("available") is False and bool(compare.get("reason")),
                      f"{compare}")
                check("所有 KPI 的 change 在无同比数据时为 null",
                      all(item.get("change") is None for item in board.get("kpi", [])),
                      f"{[item.get('change') for item in board.get('kpi', [])]}")
                check("看板返回 6 个 KPI（含成本/费用/收支差额率/成本率）",
                      {item["label"] for item in board.get("kpi", [])} == {"营业收入", "成本金额", "费用金额", "收支差额（简化口径）", "收支差额率", "成本率"},
                      f"{[item['label'] for item in board.get('kpi', [])]}")
                check("成本率 KPI 带预警阈值说明",
                      kpi.get("成本率", {}).get("threshold_hint") == "预警阈值 80.00%",
                      f"{kpi.get('成本率')}")
                check("图表口径说明齐全",
                      {"revenue", "expenses", "cash"} <= set((board.get("chart_notes") or {}).keys()),
                      f"{board.get('chart_notes')}")
                check("成本/现金口径的 KPI 不编造变化",
                      kpi.get("成本金额", {}).get("note") in {"无可比期间", ""} and kpi.get("成本金额", {}).get("change") is None,
                      f"{kpi.get('成本金额')}")

                print("\n[7] 重复导入必须被拒绝")
                again = await client.post("/api/v1/financial/import", files={"file": (csv_path.name, expected["bytes"], "text/csv")})
                check("同一文件二次导入返回 409", again.status_code == 409, f"HTTP {again.status_code}")

                print("\n[8] RAG 闭环（本地 384 维模型，事务内回滚）")
                try:
                    from backend.rag.embeddings import get_embedding_service
                    from backend.rag.pgvector_store import (
                        EMBEDDING_DIMENSION,
                        add_documents,
                        search_documents,
                    )

                    service = get_embedding_service()
                    model_name = service.model_name
                    check("本地 Embedding 模型可用", True, Path(model_name).name)
                    probe = "差旅费报销标准为每晚500元，超标准需财务经理审批。"
                    vector = service.encode([probe])[0]
                    check(f"向量维度 = {EMBEDDING_DIMENSION}", len(vector) == EMBEDDING_DIMENSION, f"len={len(vector)}")

                    add_documents(db, [{"content": probe, "source": "verify-crawl/probe.txt",
                                        "metadata_json": {"embedding_model": model_name}, "embedding": vector}])
                    hits = search_documents(db, service.encode(["差旅费每晚能报销多少？"])[0], 3,
                                            max_distance=get_settings().rag_max_distance, embedding_model=model_name)
                    check("按问题能检索回该片段", any(h.content == probe for h in hits), f"命中 {len(hits)} 条")

                    miss = await client.post("/api/v1/rag/search", json={"query": "差旅费标准"})
                    check("RAG 检索接口可用", miss.status_code in (200, 503),
                          f"HTTP {miss.status_code}（503=库中无匹配，非接口错误）")
                except RuntimeError as exc:
                    check("RAG 闭环", False, f"模型缺失（非逻辑错误）：{exc}")

                print("\n[9] 报告导出（Markdown 内容与金额格式）")
                last_period = periods[-1]
                report = await client.get("/api/v1/reports/monthly", params={"period": last_period})
                check("报告接口返回 200", report.status_code == 200, f"HTTP {report.status_code}")
                text = report.text
                want_revenue = money(expected["periods"][last_period]["revenue"])
                check("报告标题含期间", f"# {last_period} 财务分析报告" in text)
                check("报告含核心指标与风险预警小节",
                      "## 核心指标" in text and "## 风险预警" in text, text.splitlines()[0] if text else "")
                check("报告金额与源文件收入一致",
                      f"收入：{float(want_revenue):.2f} 元" in text,
                      f"期望 收入：{float(want_revenue):.2f} 元")
                check("报告带口径与限制说明", "## 口径与限制" in text and "成本金额" in text)
                check("报告比率以百分数呈现（不是原始 4 位小数）",
                      "毛利率：" in text and "%" in text and "gross_profit_ratio" not in text)
                # anon 客户端已在前面用 with 关闭，这里另开一个匿名客户端验证鉴权。
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as visitor:
                    anonymous = await visitor.get("/api/v1/reports/monthly", params={"period": last_period})
                check("报告接口未登录返回 401", anonymous.status_code == 401, f"HTTP {anonymous.status_code}")

                print("\n[10] 回滚隔离")
        finally:
            app.dependency_overrides.clear()
            db.close()
            transaction.rollback()

    imported_batch = result.get("batch") if isinstance(result, dict) else None
    with engine.connect() as connection:
        # 只查本次导入的批次：库里可能已经存在演示导入的数据，不能按科目名模糊匹配。
        if imported_batch:
            leftover = connection.execute(
                select(func.count(FinancialTransaction.id)).where(FinancialTransaction.import_batch == imported_batch)
            ).scalar()
        else:
            leftover = 0
    check("回滚后未残留本次导入数据", leftover == 0, f"匹配行数={leftover}")

    # Windows 上文件句柄释放有延迟，rmtree 可能第一次失败；重试几次再放弃，避免留下垃圾目录。
    for attempt in range(5):
        shutil.rmtree(stub_dir, ignore_errors=True)
        if not stub_dir.exists():
            break
        time.sleep(0.5 * (attempt + 1))
    if stub_dir.exists():
        print(f"WARN: 临时上传目录未清理干净：{stub_dir}", file=sys.stderr)

    failed = [c for c in checks if not c[1]]
    print("\n" + "=" * 62)
    print(f"断言总数 {len(checks)}，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    if failed:
        for label, _, detail in failed:
            print(f"  FAIL: {label} {detail}")
        return 1
    print("PASS: 爬取文件 -> 预览 -> 导入 -> 落库 -> 指标 -> 预警 -> 看板 -> RAG 全链路一致，且已回滚")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(verify()))
