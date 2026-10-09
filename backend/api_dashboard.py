"""看板概览接口。

从 `backend/main.py` 里搬出来的：这个接口连带它的一串 `Decimal` 汇总助手占了 200 多行，
混在应用装配（中间件、路由注册、启动校验）里，看 `main.py` 时很难分清"这是框架装配"
还是"这是业务"。现在与其它业务路由一样是独立的 `APIRouter`，`main.py` 只负责 include。

路径同时挂 `/api/dashboard` 与 `/api/v1/dashboard`：前者是历史路径，前端与脚本还在用，
直接去掉会打出 404，所以保留两个装饰器（与 `main.py` 里的 `/api/chat` 同一处理方式）。
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from backend import biztime
from backend.auth import current_user
from backend.db import get_db
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.scope import (
    Scope,
    resolve_requested_scope,
    scope_alert,
    scope_label,
    scope_metric,
    scope_of,
    scope_payload,
    scope_transactions,
)

router = APIRouter(tags=["dashboard"])

# 金额一律用字符串输出：float 会在 5.8 万亿这种量级丢分位，看板和源文件就对不上了。
_PERCENT = Decimal("0.01")


@router.get("/api/dashboard")
@router.get("/api/v1/dashboard")
def dashboard(period: str = "year", department: str | None = Query(None, max_length=100),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    if period not in {"month", "quarter", "year"}:
        raise HTTPException(422, "不支持的期间")

    # 管理员可以按任意部门查看（限制部门账号只能看自己范围内的，越权直接 403）。
    requested, violation = resolve_requested_scope(user, department)
    if violation:
        raise HTTPException(403, violation)
    # 只有「不受限账号主动切了视角」才需要在提示条里说明，受限账号本来就是本部门口径。
    explicit = requested[0] if (requested and len(requested) == 1 and scope_of(user) is None) else None
    scope = requested

    # 数据期间不一定与自然日期对齐（演示数据是 2026-04~2026-10），取库里最新一期所在的年份，
    # 这样"本月/本季/本年"指向的是有数据的期间，而不是空响应的当前自然月。
    # 用 scope_metric 取，受限账号拿到的是自己部门的期间，不会跟着别人的数据走。
    latest_metric = db.scalars(scope_metric(select(FinancialMetric), scope).order_by(desc(FinancialMetric.period))).first()
    # 没有指标可依据时，退回「业务时区里的今天」（⑯）：原来用 `datetime.now()` 读的是服务器
    # 本机时区，容器里通常是 UTC，UTC+8 的早上 8 点前「今天」会少一天。
    today = biztime.now()
    year = int(latest_metric.period.split("-")[0]) if latest_metric and "-" in latest_metric.period else today.year
    if period == "month":
        start_month = 12 if latest_metric and latest_metric.period.endswith("-12") else (int(latest_metric.period.split("-")[1]) if latest_metric else today.month)
        months = [f"{year}-{start_month:02d}"]
        period_label = "本月"
    elif period == "quarter":
        month = int(latest_metric.period.split("-")[1]) if latest_metric else today.month
        first = (month - 1) // 3 * 3 + 1
        months = [f"{year}-{m:02d}" for m in range(first, first + 3)]
        period_label = "本季"
    else:
        months = [f"{year}-{m:02d}" for m in range(1, 13)]
        period_label = "本年"

    start = datetime(year, 1, 1)
    end = datetime(year, 12, 31, 23, 59, 59)
    rows = db.scalars(
        scope_transactions(select(FinancialTransaction), scope)
        .where(FinancialTransaction.transaction_date >= start, FinancialTransaction.transaction_date <= end)
        .order_by(FinancialTransaction.transaction_date)
    ).all()
    # 只保留所选期间的交易
    rows = [row for row in rows if row.transaction_date.strftime("%Y-%m") in months]
    if not rows:
        return {
            "period": period, "period_label": period_label, "months": months, "has_data": False,
            "scope": scope_payload(scope, requested=explicit),
            "message": f"{scope_label(scope)}在 {year} 年 {months[0][5:]}~{months[-1][5:]} 期间没有找到财务数据，请先导入。",
            "kpi": [], "revenue": [], "expenses": [], "cash": {"x": [], "inflow": [], "outflow": []},
            "alerts": [],
        }

    totals = _sum_metrics(rows)
    revenue = totals["revenue"]
    cost = totals["cost"]
    expense = totals["expense"]
    profit = totals["profit"]
    net_ratio = (profit / revenue) if revenue else Decimal(0)
    cost_ratio = (cost / revenue) if revenue else Decimal(0)

    # 环比：与紧邻的上一期比。上一期必须有完整数据，否则不给数字（前端会整行不显示）。
    previous_months = _previous_months(months, period)
    previous_totals, previous_complete = _totals_by_months(db, previous_months, scope)
    compare = {
        "type": "环比", "label": f"{previous_months[0]}~{previous_months[-1]}",
        "available": previous_complete,
        "reason": "" if previous_complete else f"缺少 {previous_months[0]}~{previous_months[-1]} 的完整数据，无法计算环比",
    }

    monthly: dict[str, Decimal] = {}
    by_department: dict[str, Decimal] = {}
    for row in rows:
        key = row.transaction_date.strftime("%Y-%m")
        monthly[key] = monthly.get(key, Decimal(0)) + (row.revenue_amount or Decimal(0))
        name = (row.department or "未标注部门").strip() or "未标注部门"
        by_department[name] = by_department.get(name, Decimal(0)) + (row.expense_amount or Decimal(0))
    top_departments = sorted(by_department.items(), key=lambda item: item[1], reverse=True)[:5]

    compare_label = compare["label"] if previous_complete else ""
    previous = previous_totals if previous_complete else None

    def previous_value(key: str, scale: Decimal = Decimal(1)):
        if not previous:
            return None
        return (previous.get(key) or Decimal(0)) * scale

    return {
        "period": period,
        "period_label": period_label,
        "has_data": True,
        "updated_at": biztime.now().isoformat(timespec="seconds"),
        "range": {"year": year, "months": months, "label": f"{months[0]}~{months[-1]}"},
        "compare": compare,
        "scope": scope_payload(scope, requested=explicit),
        "source_note": "政府会计总账公开数据（加拿大 Receiver General），口径见 data/processed/README.md",
        "kpi": [
            _kpi("营业收入", revenue, "元", "↗", None, previous_value("revenue"), compare_label),
            _kpi("成本金额", cost, "元", "▣", None, previous_value("cost"), compare_label),
            _kpi("费用金额", expense, "元", "⇄", None, previous_value("expense"), compare_label),
            _kpi("收支差额（简化口径）", profit, "元", "◈", None, previous_value("profit"), compare_label),
            _kpi("收支差额率", net_ratio * 100, "%", "%", None, previous_value("net_ratio", Decimal(100)), compare_label),
            _kpi("成本率", cost_ratio * 100, "%", "％", Decimal("0.8") * 100,
                 previous_value("cost_ratio", Decimal(100)), compare_label,
                 threshold_hint=f"预警阈值 {Decimal('0.8') * 100:.2f}%"),
        ],
        "chart_notes": {
            "revenue": f"按交易日期逐月汇总（{'、'.join(m[5:] for m in months)}），单位为元",
            "expenses": "按支出金额最高的前 5 个部门汇总；源数据无成本科目，成本列恒为 0",
            "cash": "源数据没有银行流水时间轴，现金流入/流出按 0 处理，此处仅显示未分类净额",
            "cash_note": "现金流入/流出口径不可用",
        },
        "months": list(monthly),
        # 金额一律用字符串输出：float 会在 5.8 万亿这种量级丢分位，看板和源文件就对不上了
        "revenue": [str(_money(value)) for value in monthly.values()],
        "expenses": [
            {"name": f"{name}（部门）", "value": str(_money(value)),
             "ratio": str((value / expense * 100).quantize(_PERCENT)) if expense else "0.00"}
            for name, value in top_departments
        ],
        "cash": {
            "x": ["现金收支（未分类）"],
            "inflow": [str(_money(sum((row.cash_in_amount or Decimal(0) for row in rows), Decimal(0))))],
            "outflow": [str(_money(-sum((row.cash_out_amount or Decimal(0) for row in rows), Decimal(0))))],
        },
        "alerts": [
            {"id": a.id, "type": a.alert_type, "desc": a.description, "level": a.level,
             "period": a.period, "department": a.department or "",
             "time": a.created_at.isoformat() if a.created_at else ""}
            for a in db.scalars(
                scope_alert(select(FinancialAlert), scope)
                .where(FinancialAlert.period.in_(months)).order_by(desc(FinancialAlert.period))
            ).all()
        ],
    }


def _money(value: Decimal) -> Decimal:
    """金额统一四舍五入到分。"""
    return Decimal(value or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _sum_metrics(rows) -> dict[str, Decimal]:
    """对一批交易做确定性汇总，全部走 Decimal，不用 float。"""
    revenue = sum((row.revenue_amount or Decimal(0) for row in rows), Decimal(0))
    cost = sum((row.cost_amount or Decimal(0) for row in rows), Decimal(0))
    expense = sum((row.expense_amount or Decimal(0) for row in rows), Decimal(0))
    return {"revenue": revenue, "cost": cost, "expense": expense, "profit": revenue - cost - expense}


def _previous_months(months: list[str], period: str) -> list[str]:
    """上一期的期间列表。month -> 上一个月；quarter -> 上个季度；year -> 上一年的同月。"""
    first = months[0]
    year, month = int(first[:4]), int(first[5:7])
    if period == "month":
        span, step = 1, -1
    elif period == "quarter":
        span, step = 3, -3
    else:
        span, step = 12, -12
    total_start = year * 12 + (month - 1) + step
    return [f"{(total_start + offset) // 12}-{(total_start + offset) % 12 + 1:02d}" for offset in range(span)]


def _totals_by_months(db: Session, months: list[str], scope: Scope = None) -> tuple[dict[str, Decimal] | None, bool]:
    """上一期的汇总。任一月份缺该口径的 FinancialMetric 就认为数据不完整，返回 (None, False)——
    宁可说"算不了环比"，也不能拿半个月的数据去比。

    多部门账号同期有多行（一本部门账一行），所以这里除了「每月都有数据」还要求
    「每月的行数等于范围内的部门数」：某个部门少一期就说明这一期不可比。
    """
    rows = db.scalars(scope_metric(select(FinancialMetric).where(FinancialMetric.period.in_(months)), scope)).all()
    expected = 1 if scope is None else len(scope)  # 全公司一行；多部门一本账一行；空范围 0 行
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.period] = counts.get(row.period, 0) + 1
    if len(counts) != len(months) or any(counts.get(month, 0) != expected for month in months):
        return None, False
    revenue = sum((row.revenue_amount or Decimal(0) for row in rows), Decimal(0))
    cost = sum((row.cost_amount or Decimal(0) for row in rows), Decimal(0))
    expense = sum((row.expense_amount or Decimal(0) for row in rows), Decimal(0))
    net = sum((row.net_profit_amount or Decimal(0) for row in rows), Decimal(0))
    return {
        "revenue": revenue, "cost": cost, "expense": expense, "profit": net,
        "net_ratio": (net / revenue) if revenue else Decimal(0),
        "cost_ratio": (cost / revenue) if revenue else Decimal(0),
    }, True


def _kpi(label: str, value: Decimal, unit: str, icon: str, threshold: Decimal | None,
         previous: Decimal | None = None, compare_label: str = "", threshold_hint: str = "") -> dict:
    """把一个指标格式化成看板卡片。previous 为 None 时不编造 change，改为给 note。"""
    item = {
        "label": label,
        "value": f"{float(value):,.2f}",
        "unit": unit,
        "icon": icon,
        "change": None,
        "dir": "flat",
    }
    if threshold_hint:
        item["threshold_hint"] = threshold_hint
    if previous is None:
        item["note"] = "无可比期间" if not compare_label else f"缺少 {compare_label} 数据"
        return item
    if previous == 0:
        item["note"] = f"{compare_label}为 0，无法计算环比"
        return item
    percent = (value - previous) / abs(previous) * 100
    item["change"] = f"{abs(percent):.2f}%"
    item["dir"] = "up" if percent > 0 else ("down" if percent < 0 else "flat")
    item["compare_value"] = f"{float(previous):,.2f}"
    return item
