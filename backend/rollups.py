"""季度 / 年度落库：把月口径指标加总成汇总行，存进 `financial_rollups`。

为什么要有这个模块：季与年的数字以前**只在请求里现算**——看板按月口径求和、报告与导出各自
对一批明细或指标行再求一次和。同一件事多处现算，口径就会漂（少一个部门行、比率被加成）、
每请求一次还要把 7~12 个月的明细都读一遍。落库以后：合计是一次算好的、可对账、可重复读。

三条规矩：

1. **只有月口径指标参与求和**。`financial_metrics` 里全是月行，季/年行只存在这张表里，
   所以不存在「把季度合计再加成年度合计」这种重复计入。
2. **比率一律用相加后的金额重算**，不做比率的加法或平均（部门口径下 `收入=0.01` 的比率能到 10^6，
   平均出来毫无意义）。
3. **缺月必须说出来**：`months` 记真的被加进去的月份，`complete` 表示是否凑齐整个季度/年度。
   演示数据是 2026-04~10，2026 年度只有 7 个月——此时年度合计仍然落库（有用），
   但 `complete=false` 且 `missing_months` 点名缺哪几期，而不是假装它是全年。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend import locks
from backend import periods as period_mod
from backend.models import FinancialMetric, FinancialRollup
from backend.periods import MONTH, QUARTER, YEAR, months_of, period_label
from backend.scope import MetricTotals, Scope, metric_totals, scope_metric

RATIO_SCALE = Decimal("0.0001")

# 落库只做季与年；月口径在 financial_metrics 里，不进这张表。
DEFAULT_GRAINS = (QUARTER, YEAR)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if not denominator:
        return Decimal(0)
    return (numerator / denominator).quantize(RATIO_SCALE)


def monthly_metric_periods(db: Session) -> list[str]:
    """库里有指标的月份（升序）。`financial_metrics` 只存月口径，但仍按格式过一遍，
    免得历史脏数据（比如手工插的 "2026-Q1"）被当成一个月再去套季度。"""
    values = db.scalars(select(FinancialMetric.period).distinct()).all()
    return sorted({value for value in values if period_mod.is_month(value)})


def target_periods(months: list[str], *, grains: tuple[str, ...] = DEFAULT_GRAINS) -> list[str]:
    """这些月份所覆盖的季度/年度期间（升序、去重）。"""
    targets: set[str] = set()
    for month in months:
        if QUARTER in grains:
            targets.add(period_mod.quarter_of(month))
        if YEAR in grains:
            targets.add(period_mod.year_of(month))
    return sorted(targets, key=period_mod.sort_key)


def rollup(db: Session, *, grains: tuple[str, ...] = DEFAULT_GRAINS,
           periods: list[str] | None = None) -> list[dict]:
    """（重）算季/年落库行，返回写入的行。

    `periods` 给定时只刷新这几个期间（导入/重算一个月之后只需要刷新它所在的季与年），
    否则按库里现有的月指标覆盖所有季与年（回填老库、或管理页上的「重算落库」）。

    每个目标期间都是**先删后建**：某个季度被整批撤销、月指标都没了的时候，
    这里删完一条都不建，库里就不会留下一个凭空的季度合计。
    """
    months = monthly_metric_periods(db)
    if periods is None:
        targets = target_periods(months, grains=grains)
    else:
        targets = sorted({p for p in periods if period_mod.grain_of(p) in grains}, key=period_mod.sort_key)

    # 期间锁：落库行是「每个目标期间先删后建」，若与「重算某个月」并发，一个请求会把另一个
    # 刚写好的季度合计覆盖成少算一个月的版本（数字变小且没有任何报错）。锁在 targets 算出来
    # 之后、读指标之前取；顺序同样交给 lock_periods 排序，与 calculate() 侧保持一致。
    locks.lock_periods(db, targets)

    # 月指标一次读出来按期间分桶，避免每个目标期间各查一遍库。
    buckets: dict[str, list[FinancialMetric]] = {}
    for row in db.scalars(select(FinancialMetric)).all():
        buckets.setdefault(row.period, []).append(row)

    written: list[FinancialRollup] = []
    for period in targets:
        grain = period_mod.grain_of(period)
        if grain is None:
            continue
        covered = months_of(period)
        # 按 department 分组：同一口径（全公司 / 某个部门）在季内的几个月相加，
        # 不同口径不能混在一起，否则全公司行会把部门行的金额重复计入。
        grouped: dict[str | None, list[FinancialMetric]] = {}
        for month in covered:
            for row in buckets.get(month, []):
                grouped.setdefault(row.department, []).append(row)
        db.execute(delete(FinancialRollup).where(
            FinancialRollup.grain == grain, FinancialRollup.period == period))
        for department, rows in sorted(grouped.items(), key=lambda item: (item[0] is not None, item[0] or "")):
            present = sorted({row.period for row in rows})
            revenue = sum((row.revenue_amount or Decimal(0) for row in rows), Decimal(0))
            cost = sum((row.cost_amount or Decimal(0) for row in rows), Decimal(0))
            expense = sum((row.expense_amount or Decimal(0) for row in rows), Decimal(0))
            gross = sum((row.gross_profit_amount or Decimal(0) for row in rows), Decimal(0))
            net = sum((row.net_profit_amount or Decimal(0) for row in rows), Decimal(0))
            record = FinancialRollup(
                grain=grain, period=period, department=department,
                revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
                gross_profit_amount=gross, net_profit_amount=net,
                gross_profit_ratio=_ratio(gross, revenue), net_profit_ratio=_ratio(net, revenue),
                months=present, month_count=len(present), complete=len(present) == len(covered),
                computed_at=datetime.now(UTC))
            db.add(record)
            written.append(record)
    db.flush()
    return [serialize(row) for row in written]


def refresh_for_months(db: Session, months: list[str]) -> list[dict]:
    """重算/撤销某个月之后，只刷新它所在的那几个季与年（落库跟着月口径走）。"""
    return rollup(db, periods=target_periods(sorted(set(months))))


def scope_rollup(statement, scope: Scope):
    """按数据范围过滤落库行：不受限账号读全公司行，受限账号读自己那几本账。"""
    if scope is None:
        return statement.where(FinancialRollup.department.is_(None))
    return statement.where(FinancialRollup.department.in_(scope))


def stored_rows(db: Session, scope: Scope, *, grain: str | None = None,
                periods: list[str] | None = None) -> list[FinancialRollup]:
    statement = scope_rollup(select(FinancialRollup), scope)
    if grain:
        statement = statement.where(FinancialRollup.grain == grain)
    if periods:
        statement = statement.where(FinancialRollup.period.in_(periods))
    return list(db.scalars(statement.order_by(FinancialRollup.id)).all())


def totals(db: Session, period: str, scope: Scope) -> list[MetricTotals]:
    """按期间读落库合计（多部门账号的多行在这里按金额相加、比率重算）。

    复用 `scope.metric_totals`：落库行的字段形状与指标行一致，
    「单行沿用存储比率、多行重算」的规矩也就自动与月口径一致。
    """
    rows = stored_rows(db, scope, periods=[period])
    return metric_totals(rows, period_order=[period])


def _combine(month_totals: list[MetricTotals], period: str) -> list[MetricTotals]:
    """把逐月合计并成一个季/年合计：金额相加、比率按相加后的金额重算。

    为什么不直接把各月的比率平均：部门口径下 `收入=0.01` 时比率能到 10^6，
    平均这种数字没有意义；金额的加法才是口径一致的。
    """
    if not month_totals:
        return []
    revenue = sum((item.revenue_amount for item in month_totals), Decimal(0))
    cost = sum((item.cost_amount for item in month_totals), Decimal(0))
    expense = sum((item.expense_amount for item in month_totals), Decimal(0))
    gross = sum((item.gross_profit_amount for item in month_totals), Decimal(0))
    net = sum((item.net_profit_amount for item in month_totals), Decimal(0))
    names = tuple(sorted({name for item in month_totals for name in item.departments}))
    department = month_totals[0].department if len(month_totals) == 1 and department_single(month_totals) else None
    return [MetricTotals(
        period=period, departments=names, department=department,
        revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
        gross_profit_amount=gross, net_profit_amount=net,
        gross_profit_ratio=_ratio(gross, revenue), net_profit_ratio=_ratio(net, revenue))]


def department_single(items: list[MetricTotals]) -> bool:
    """参与合计的各月是否**只有一个部门**（多部门时合计行不再属于某一个部门）。"""
    names = {name for item in items for name in item.departments}
    return len(names) <= 1


def computed_totals(db: Session, period: str, scope: Scope) -> tuple[list[MetricTotals], list[str]]:
    """落库还没覆盖到这一期时，用月指标现算（算法与落库完全一致，只是不写库）。

    返回 (合计, 实际有数据的月份)。调用方要如实把 source 标成 computed——
    页面不能把现算的数字当成落库的数字。
    """
    covered = list(months_of(period))
    rows = list(db.scalars(scope_metric(
        select(FinancialMetric).where(FinancialMetric.period.in_(covered)), scope)).all())
    present = sorted({row.period for row in rows})
    monthly = metric_totals(rows, period_order=present)
    return _combine(monthly, period), present


def aggregate(db: Session, period: str, scope: Scope) -> dict:
    """读某一期的合计：月口径读指标表，季/年读落库（缺失时现算兜底）。

    返回值里 `source` 说明数字是哪来的：
    - `month`    —— 月口径，本来就在 `financial_metrics` 里；
    - `stored`   —— 读的是落库合计 `financial_rollups`；
    - `computed` —— 落库还没覆盖（老库刚升级、或这一期从没重算过），按同一算法现算；
    - `empty`    —— 这一期一条数据都没有。
    `complete`/`missing_months` 只对季/年有意义：演示数据是 2026-04~10，
    2026 年度一定缺 5 个月，必须点名，不能让页面把 7 个月当成全年。
    """
    grain = period_mod.grain_of(period)
    covered = list(months_of(period))
    label = period_mod.GRAIN_LABELS.get(grain or MONTH, "")
    if grain not in DEFAULT_GRAINS:
        rows = list(db.scalars(scope_metric(
            select(FinancialMetric).where(FinancialMetric.period == period), scope)).all())
        totals = metric_totals(rows, period_order=[period])
        return {"period": period, "grain": grain or MONTH, "grain_label": label,
                "period_label": period_label(period), "source": "month" if totals else "empty",
                "months": [period] if totals else [], "covered_months": [period],
                "missing_months": [], "complete": bool(totals), "totals": totals}
    stored = stored_rows(db, scope, periods=[period])
    if stored:
        present = sorted({month for row in stored for month in (row.months or [])})
        missing = [month for month in covered if month not in present]
        return {"period": period, "grain": grain, "grain_label": label,
                "period_label": period_label(period), "source": "stored",
                "months": present, "covered_months": covered, "missing_months": missing,
                "complete": bool(present) and not missing, "totals": metric_totals(stored, period_order=[period])}
    totals, present = computed_totals(db, period, scope)
    missing = [month for month in covered if month not in present]
    return {"period": period, "grain": grain, "grain_label": label,
            "period_label": period_label(period), "source": "computed" if totals else "empty",
            "months": present, "covered_months": covered, "missing_months": missing,
            "complete": bool(present) and not missing, "totals": totals}


def serialize(row: FinancialRollup) -> dict:
    covered = months_of(row.period)
    present = list(row.months or [])
    missing = [month for month in covered if month not in present]
    to_float = lambda value: float(value or 0)  # noqa: E731 - 一行 lambda 比六个函数调用直观
    return {
        "id": row.id,
        "grain": row.grain,
        "grain_label": period_mod.GRAIN_LABELS.get(row.grain, row.grain),
        "period": row.period,
        "period_label": period_label(row.period),
        "department": row.department or "",
        "department_label": row.department or "全公司",
        "months": present,
        "month_count": row.month_count or len(present),
        "covered_months": list(covered),
        "missing_months": missing,
        "complete": bool(row.complete),
        "note": (f"已覆盖全部 {len(covered)} 个月" if row.complete
                 else f"缺 {'、'.join(missing) or '（无）'}"),
        "revenue_amount": to_float(row.revenue_amount),
        "cost_amount": to_float(row.cost_amount),
        "expense_amount": to_float(row.expense_amount),
        "gross_profit_amount": to_float(row.gross_profit_amount),
        "net_profit_amount": to_float(row.net_profit_amount),
        "gross_profit_ratio": to_float(row.gross_profit_ratio),
        "net_profit_ratio": to_float(row.net_profit_ratio),
        "computed_at": row.computed_at.strftime("%Y-%m-%d %H:%M:%S") if row.computed_at else None,
    }


def coverage(db: Session) -> dict:
    """管理页用：落库覆盖面（按粒度计数 + 缺月的期间清单）。

    缺月清单按 (粒度, 期间) 去重、**全公司行排在同期的部门行前面**：同一个季度里全公司行齐全、
    某个部门只落了其中一个月是常事，清单里必须说清是哪一行缺月，否则页面上会看到
    「2026 年度缺 11 个月」而全公司那一行其实只缺 5 个月。
    """
    rows = db.scalars(select(FinancialRollup)).all()
    periods_by_grain: dict[str, set[str]] = {QUARTER: set(), YEAR: set()}
    for row in rows:
        periods_by_grain.setdefault(row.grain, set()).add(row.period)
    incomplete: list[dict] = []
    for row in rows:
        if row.complete:
            continue
        payload = serialize(row)
        incomplete.append({"grain": row.grain, "period": row.period,
                           "period_label": payload["period_label"],
                           "department": row.department or "",
                           "department_label": payload["department_label"],
                           "missing_months": payload["missing_months"]})
    incomplete.sort(key=lambda item: (item["grain"], item["period"], item["department"] != ""))
    company_rows = db.scalar(
        select(func.count()).select_from(FinancialRollup)
        .where(FinancialRollup.department.is_(None))) or 0
    return {
        "rows": len(rows),
        "company_rows": company_rows,
        "department_rows": len(rows) - company_rows,
        "quarters": sorted(periods_by_grain.get(QUARTER, set()), key=period_mod.sort_key),
        "years": sorted(periods_by_grain.get(YEAR, set()), key=period_mod.sort_key),
        "incomplete": sorted(incomplete, key=lambda item: (item["grain"], item["period"])),
        "grains": list(DEFAULT_GRAINS),
        "grain_labels": {grain: period_mod.GRAIN_LABELS[grain] for grain in DEFAULT_GRAINS},
    }
