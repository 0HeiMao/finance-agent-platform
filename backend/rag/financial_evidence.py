"""把已导入的财务数据整理成确定性的证据块，供财务 Agent 作答。

设计约束：
- 这里只做「取数 + 按规则汇总」，不做任何猜测性计算，也不调用大模型。
- 大模型只负责解释这些证据；证据里没有的数字不允许出现。
- 每个来源都带上期间与口径，便于前端展示「依据」。
"""

from __future__ import annotations

from dataclasses import replace
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import periods as period_mod
from backend.agent_facts import fact, metric_facts
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction
from backend.rag.question_periods import parse
from backend.scope import Scope, metric_totals, scope_alert, scope_label, scope_metric, scope_transactions

CENT = Decimal("0.01")
MAX_TABLE_PERIODS = 24
MAX_DETAIL_ROWS = 6


def requested_periods(question: str) -> list[str]:
    """将明确期间或相对期间展开为月份；不明确的时间要求澄清。"""
    return list(parse(question).months)


def money(value: Decimal | float | int | None) -> str:
    return str(Decimal(value or 0).quantize(CENT, rounding=ROUND_HALF_UP))


def ratio(value: Decimal | float | int | None) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def available_periods(db: Session, scope: Scope = None) -> list[str]:
    return list(db.scalars(scope_metric(
        select(FinancialMetric.period).distinct(), scope).order_by(FinancialMetric.period)))


def pick_period(question: str, periods: list[str]) -> str | None:
    """仅在未指定月份时默认最新一期，指定但无数据时返回 None。"""
    if not periods:
        return None
    try:
        matched = requested_periods(question)
    except ValueError:
        return None
    for period in matched:
        if period in periods:
            return period
    return None if matched else periods[-1]


def _aggregate(db: Session, period: str, column, scope: Scope = None) -> list[tuple[str, Decimal, int]]:
    key = func.coalesce(column, "未标注")
    statement = (
        select(key, func.sum(FinancialTransaction.revenue_amount), func.sum(FinancialTransaction.expense_amount), func.count())
        # 区间比较（见 period_mod.period_range）：能吃到 (department, transaction_date) 索引，
        # 而且 23:59:59 这种带时间的行也会被算进本期，不再被 to_char 相等比较漏掉。
        .where(period_mod.period_range(FinancialTransaction.transaction_date, period))
        .group_by(key)
        .order_by(func.sum(FinancialTransaction.expense_amount).desc())
        .limit(MAX_DETAIL_ROWS)
    )
    return [(row[0], Decimal(row[1] or 0), Decimal(row[2] or 0), int(row[3] or 0))
            for row in db.execute(scope_transactions(statement, scope)).all()]


def _summary_lines(metrics: list) -> list[str]:
    shown = metrics[-MAX_TABLE_PERIODS:]
    lines = [
        f"期间口径说明：全部金额为「元」，比率保留 4 位小数；本轮选取 {len(metrics)} 期指标，下表展示其中 {len(shown)} 期，不代表数据库只有这些期间。",
        "",
        "| 期间 | 收入金额 | 成本金额 | 费用金额 | 收支差额(净额) | 收入成本率 | 收入净额率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for metric in shown:
        lines.append(
            f"| {metric.period} | {money(metric.revenue_amount)} | {money(metric.cost_amount)} | {money(metric.expense_amount)} "
            f"| {money(metric.net_profit_amount)} | {ratio(metric.gross_profit_ratio)} | {ratio(metric.net_profit_ratio)} |"
        )
    return lines


def _period_lines(db: Session, period: str, metric,
                  scope: Scope = None) -> tuple[list[str], list[str], list[dict]]:
    facts = []
    lines = [
        f"### 当前期间明细（{period}）",
        f"- 收入金额 {money(metric.revenue_amount)} 元，成本金额 {money(metric.cost_amount)} 元，费用金额 {money(metric.expense_amount)} 元，收支差额 {money(metric.net_profit_amount)} 元。",
        f"- 收入成本率 {ratio(metric.gross_profit_ratio)}，收入净额率 {ratio(metric.net_profit_ratio)}。",
        "",
        f"按部门汇总（{period}，按费用金额降序，最多 {MAX_DETAIL_ROWS} 行）：",
        "| 部门 | 收入金额 | 费用金额 | 笔数 |",
        "| --- | --- | --- | --- |",
    ]
    for index, (name, revenue, expense, count) in enumerate(_aggregate(db, period, FinancialTransaction.department, scope)):
        lines.append(f"| {name} | {money(revenue)} | {money(expense)} | {count} |")
        if index < 3:
            facts.append(fact(f"{period}:department{index}:expense", "department_expense", "部门费用", period, (name,), money(expense), dimension=f"部门 {name}"))
    lines += ["", f"按科目汇总（{period}，按费用金额降序，最多 {MAX_DETAIL_ROWS} 行）：", "| 科目名称 | 收入金额 | 费用金额 | 笔数 |", "| --- | --- | --- | --- |"]
    for index, (name, revenue, expense, count) in enumerate(_aggregate(db, period, FinancialTransaction.account_name, scope)):
        lines.append(f"| {name} | {money(revenue)} | {money(expense)} | {count} |")
        facts.append(fact(f"{period}:account{index}:expense", "account_expense", "科目费用", period, scope, money(expense), dimension=name))
    alerts = db.scalars(scope_alert(
        select(FinancialAlert).where(FinancialAlert.period.in_(period_mod.months_of(period))), scope)
        .order_by(FinancialAlert.id)).all()
    lines += ["", f"本期系统预警（{len(alerts)} 条）："]
    if alerts:
        for alert in alerts:
            lines.append(
                f"- [{alert.level}] {alert.alert_type}：{alert.description}；"
                f"指标 {alert.metric_name} 当前值 {money(alert.current_value)}，阈值 {money(alert.threshold_value)}，需人工复核 {alert.needs_human_review}。"
            )
    else:
        lines.append("- 无。")
    sources = [
        {"name": f"财务汇总表 {period}", "kind": "financial_metric"},
        {"name": f"财务预警 {period}", "kind": "financial_alert"},
        {"name": f"明细账 {period}", "kind": "financial_transaction"},
    ]
    return lines, sources, facts


def collect(db: Session, question: str, scope: Scope = None) -> dict[str, Any]:
    """返回 {'has_data', 'periods', 'context', 'sources'}，context 为空字符串表示没有可用财务数据。

    `scope` 是提问者的数据范围：None = 全公司口径；非空元组 = 只看这些部门（多部门账号是并集）。
    受限账号的 Agent 绝不能看到别人的数字，所以指标、明细、预警三处都要过 scope。
    """
    periods = available_periods(db, scope)
    try:
        selection = parse(question)
    except ValueError as exc:
        return {"has_data": False, "periods": periods, "period": None, "context": "", "sources": [],
                "unavailable_message": str(exc)}
    requested = list(selection.months)
    missing = [period for period in requested if period not in periods]
    if missing:
        # 比较缺一端也不能借用其他月份；交给接口直接提示，不让模型猜。
        message = f"当前可见数据中没有以下月份的数据：{'、'.join(missing)}。请补充数据或调整月份，不能用其他月份代替。"
        return {"has_data": False, "periods": periods, "period": None,
                "context": "", "sources": [], "missing_periods": missing,
                "unavailable_message": message}
    if not periods:
        return {"has_data": False, "periods": [], "context": "", "sources": []}
    selected = requested or periods
    rows = list(db.scalars(scope_metric(
        select(FinancialMetric).where(FinancialMetric.period.in_(selected)), scope)
        .order_by(FinancialMetric.period, FinancialMetric.id)))
    # 一期一行：多部门账号同期有多条部门行，直接取「最后一条」会静默丢掉另一本账，
    # 所以这里按期间聚合（金额相加、比率按合计重算），证据表里才是一个部门并集的数字。
    totals = {item.period: item for item in metric_totals(rows, period_order=selected)}
    ordered = [totals[period] for period in selected if period in totals]
    period = selection.groups[0] if selection.groups else periods[-1]
    scope_line = ("" if scope is None
                  else f"数据范围：本证据只包含「{scope_label(scope)}」的明细，不是全公司口径。")
    lines = ["【已导入财务数据（程序确定性计算，可直接引用）】"]
    if scope_line:
        lines.append(scope_line)
    if requested:
        lines.append(f"时间口径：{'、'.join(selection.groups)}；相对日期基准 {selection.reference_date}（{selection.timezone}）。")
    if not requested:
        lines.append(f"未指定明确月份，本轮明细默认使用最新一期 {period}。")
    lines += ["", "#### 各期间汇总", "", *_summary_lines(ordered)]
    sources: list[dict[str, Any]] = [{"name": f"财务汇总表 {'、'.join(selected)}", "kind": "financial_metric"}]
    facts = []
    for detail_period in selection.groups or ([period] if period else []):
        if period_mod.is_month(detail_period):
            detail_metric = totals[detail_period]
        else:
            # 季度/年度先合计金额再重算比率，不把月比率相加，也不交给模型求和。
            members = [replace(totals[month], period=detail_period) for month in period_mod.months_of(detail_period)]
            detail_metric = metric_totals(members, period_order=[detail_period])[0]
        detail_lines, detail_sources, detail_facts = _period_lines(db, detail_period, detail_metric, scope)
        facts.extend(metric_facts(detail_metric, scope))
        facts.extend(detail_facts)
        lines += ["", *detail_lines]
        sources = detail_sources + sources
    lines += [
        "",
        "使用口径提醒：这里只包含已导入指标，不包含原因证明。成本为0可能来自源材料缺少成本科目，"
        "不能仅凭0得出成本极低的结论。没有提供现金流时间轴，不得据此推断无现金收付。",
    ]
    return {"has_data": True, "periods": periods, "period": period, "context": "\n".join(lines), "sources": sources, "facts": facts}
