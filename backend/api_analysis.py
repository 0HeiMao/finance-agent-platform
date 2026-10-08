from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import extract, func, select, delete
from sqlalchemy.orm import Session
from backend import audit, locks, rollups
from backend.auth import current_user
from backend.alert_rules import ensure_rules, evaluate, metric_values, resolve_rules
from backend.db import get_db
from backend.models import AlertRule, FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.periods import GRAIN_LABELS
from backend.scope import (Scope, in_scope, resolve_requested_scope, scope_alert,
                           scope_label, scope_metric, scope_of, scope_payload, scope_transactions)

router = APIRouter(prefix="/api/v1/analysis", tags=["analysis"])


def _scoped(user: User, requested: str | None) -> tuple[Scope, dict, str | None]:
    """解析部门参数，返回 (生效口径, 提示条载荷, 越权说明)。"""
    scope, violation = resolve_requested_scope(user, requested)
    # 只有「本身不受限的管理员主动切到某个部门」才需要在页面上说明这是切过来的视角。
    explicit = scope[0] if (scope and len(scope) == 1 and scope_of(user) is None) else None
    return scope, scope_payload(scope, requested=explicit), violation


# 处理闭环的状态机：pending（待处理）/ confirmed（已确认，确实异常）/ false_positive（误报）/
# resolved（已处理完，含补数与后续动作）。不做任意字符串——状态是前端按钮和统计口径的输入。
REVIEW_STATUSES = ("pending", "confirmed", "false_positive", "resolved")
STATUS_LABELS = {"pending": "待处理", "confirmed": "已确认", "false_positive": "误报", "resolved": "已处理"}


class ReviewInput(BaseModel):
    """处理一条预警的入参。`note` 在「误报」时必填（理由见端点里的判断）。"""
    status: str = Field(..., description="pending / confirmed / false_positive / resolved")
    note: str | None = Field(None, max_length=2000)


def _alert_json(row: FinancialAlert) -> dict:
    """预警的统一序列化：列表页、明细页、处理动作都返回同一形状。"""
    return {
        "id": row.id, "period": row.period, "type": row.alert_type, "description": row.description,
        "level": row.level, "metric": row.metric_name, "department": row.department or "",
        "current_value": str(row.current_value if row.current_value is not None else 0),
        "threshold_value": str(row.threshold_value if row.threshold_value is not None else 0),
        "status": row.review_status or "pending",
        "status_label": STATUS_LABELS.get(row.review_status or "pending", row.review_status),
        "note": row.review_note or "",
        "reviewed_by": row.reviewed_by or "",
        "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else "",
        "needs_human_review": bool(row.needs_human_review),
    }


def _find_alert(db: Session, alert_id: int, user: User) -> FinancialAlert:
    """取预警并做口径校验：受限账号只能动自己范围内的预警（越权返回 404 语义之外的 403）。"""
    row = db.get(FinancialAlert, alert_id)
    if row is None:
        raise HTTPException(404, "预警不存在")
    own = scope_of(user)
    if own and not in_scope(own, row.department or None):
        raise HTTPException(403, f"当前账号的数据范围是「{scope_label(own)}」，不能处理「{row.department or '全公司'}」的预警")
    return row



def _build(period: str, department: str | None, rows: list[FinancialTransaction],
           rules: list[AlertRule] | None = None):
    """按给定的一批明细构造一条指标行与它触发的预警（部门口径与全公司口径共用同一套规则）。

    预警由 `alert_rules` 表里的规则驱动（`backend/alert_rules.py`），不再是写死的两个 if。
    """
    revenue = sum((r.revenue_amount or 0 for r in rows), Decimal(0))
    cost = sum((r.cost_amount or 0 for r in rows), Decimal(0))
    expense = sum((r.expense_amount or 0 for r in rows), Decimal(0))
    gross = revenue - cost; net = gross - expense
    metric = FinancialMetric(
        period=period, department=department,
        revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
        gross_profit_amount=gross, net_profit_amount=net,
        gross_profit_ratio=(gross / revenue if revenue else 0),
        net_profit_ratio=(net / revenue if revenue else 0))
    alerts = []
    values = metric_values(metric)
    for rule in rules or []:
        current = evaluate(rule, values)
        if current is None:
            continue
        alerts.append(FinancialAlert(
            period=period, department=department, alert_type=rule.alert_type,
            description=rule.description or rule.alert_type, level=rule.level,
            metric_name=rule.metric_name, current_value=current,
            threshold_value=rule.threshold_value))
    return metric, alerts


def _review_state(db: Session, period: str) -> dict[tuple[str, str, str], dict]:
    """取该期间已有的处理状态，键为 (部门, 预警类型, 指标名)。

    重算是「先删后建」，不继承的话用户点过的「已处理/误报」和备注会在下一次导入或撤销批次后
    无声消失——这正是原来 `needs_human_review` 只写默认值的后果。
    """
    rows = db.scalars(select(FinancialAlert).where(FinancialAlert.period == period)).all()
    return {(row.department or "", row.alert_type, row.metric_name): {
        "review_status": row.review_status,
        "review_note": row.review_note,
        "reviewed_by": row.reviewed_by,
        "reviewed_at": row.reviewed_at,
        "needs_human_review": row.needs_human_review,
    } for row in rows if row.review_status != "pending" or row.review_note}


def calculate(db: Session, period: str, commit: bool = True):
    """重算某期间的指标与预警：**全公司一行 + 每个出现的部门各一行**。

    部门权限要求受限账号只看到本部门口径，而全公司口径不能靠累加部门行得到（部门为空的行不归
    任何部门），所以两种口径都落库。返回值仍是全公司那一条，保持既有调用方的行为不变。
    """
    year, month = map(int, period.split("-"))
    datetime(year, month, 1)
    # 期间锁：这一段是「读明细 → 删旧指标/预警 → 重写」的读改写。两个请求同时重算同一期时，
    # 彼此读不到对方还没提交的明细，于是各写一份少算了对方数据的结果，而且没有任何报错——
    # 数字就一直错下去，因为不会有谁再来重算这一期。锁在事务结束时自动释放。
    # 取锁范围含这个月所在的季与年：季/年落库行是同一次重算的一部分，不能被另一个月份的并发重算覆盖。
    locks.lock_periods(db, locks.period_lock_set([period]))
    db.flush()
    rules = ensure_rules(db)
    previous = _review_state(db, period)
    db.execute(delete(FinancialMetric).where(FinancialMetric.period == period))
    db.execute(delete(FinancialAlert).where(FinancialAlert.period == period))
    rows = db.scalars(select(FinancialTransaction).where(extract("year", FinancialTransaction.transaction_date) == year, extract("month", FinancialTransaction.transaction_date) == month)).all()
    departments = sorted({(row.department or "").strip() for row in rows} - {""})
    metric = None
    alerts: list[FinancialAlert] = []
    for department in [None, *departments]:
        subset = rows if department is None else [r for r in rows if (r.department or "").strip() == department]
        # 规则按 (期间, 部门) 分档：同一个 alert_type 只取最具体的一条（覆盖，不叠加）
        active = resolve_rules(rules, period=period, department=department)
        item, item_alerts = _build(period, department, subset, active)
        db.add(item)
        for alert in item_alerts:
            # 继承处理状态：口径 + 类型 + 指标名三者相同才算同一条预警
            keep = previous.get((department or "", alert.alert_type, alert.metric_name))
            if keep:
                for field, value in keep.items():
                    setattr(alert, field, value)
            else:
                alert.review_status = "pending"
                alert.needs_human_review = True
            alert.needs_human_review = alert.review_status == "pending"
        db.add_all(item_alerts)
        if department is None:
            metric, alerts = item, item_alerts
    # 季/年落库跟着月口径走：这个月的指标刚被删了重建，它所在的季与年必须一起重算，
    # 否则看板/报告读到的是上一版季度合计（数字对不上，但没有任何报错）。
    # 必须先 flush：SessionLocal 是 autoflush=False，刚 db.add 的指标行还只在内存里，
    # 落库那边读库会漏掉这个月（用例 test_calculate_refreshes_quarter_rollup... 抓到的）。
    db.flush()
    rollups.refresh_for_months(db, [period])
    if commit: db.commit()
    else: db.flush()
    db.refresh(metric)
    return metric, alerts

@router.post("/calculate")
def calculate_metrics(request: Request, period: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
                      db: Session = Depends(get_db), user: User = Depends(current_user)):
    metric, alerts = calculate(db, period, commit=False)
    # 重算会删掉并重建该期间的指标与预警（处理状态按口径继承），属于写操作，留痕
    audit.record(db, user, "metrics.calculate", target_type="period", target_id=period,
                 detail={"period": period, "alerts": len(alerts)}, request=request)
    db.commit()
    return {"period": period, "revenue_amount": float(metric.revenue_amount), "cost_amount": float(metric.cost_amount), "expense_amount": float(metric.expense_amount), "gross_profit_amount": float(metric.gross_profit_amount), "net_profit_amount": float(metric.net_profit_amount), "gross_profit_ratio": float(metric.gross_profit_ratio), "net_profit_ratio": float(metric.net_profit_ratio), "alerts": len(alerts)}

@router.get("/alerts")
def alerts(period: str, department: str | None = Query(None, max_length=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope, payload, violation = _scoped(user, department)
    if violation: raise HTTPException(403, violation)
    rows = db.scalars(scope_alert(select(FinancialAlert).where(FinancialAlert.period == period), scope)).all()
    return {"scope": payload, "data": [_alert_json(x) for x in rows]}

@router.get("/alerts/all")
def all_alerts(period: str | None = None, status: str | None = Query(None, max_length=20),
               limit: int = Query(50, ge=1, le=500), row_offset: int = Query(0, ge=0, alias="offset"),
               department: str | None = Query(None, max_length=100),
               db: Session = Depends(get_db), user: User = Depends(current_user)):
    """跨期间预警列表，供看板「查看全部」与预警监控页使用。period 为空则返回全部期间。

    带 `total` 是为了分页：`total` 是不受 limit 影响的满足条件行数，前端据此翻页。
    `status` 也要在**这里**过滤（而不是前端拿一页数据再筛）：筛选必须发生在分页之前，
    否则「只看误报」只能看到恰好落在当前这一页里的误报，第 2 页的误报会被静默漏掉。
    """
    scope, payload, violation = _scoped(user, department)
    if violation: raise HTTPException(403, violation)
    if status and status not in REVIEW_STATUSES:
        raise HTTPException(422, f"状态只能是 {'/'.join(REVIEW_STATUSES)}")
    condition = scope_alert(select(FinancialAlert), scope)
    if period:
        condition = condition.where(FinancialAlert.period == period)
    if status:
        condition = condition.where(FinancialAlert.review_status == status)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    statement = condition.order_by(FinancialAlert.period.desc(), FinancialAlert.id.desc()).limit(limit).offset(row_offset)
    rows = db.scalars(statement).all()
    periods = [p for p in db.scalars(scope_metric(select(FinancialMetric.period), scope).order_by(FinancialMetric.period.desc())).all()]
    return {
        "scope": payload,
        "data": [_alert_json(x) for x in rows],
        "periods": periods,
        "total": total,
        "offset": row_offset,
        "limit": limit,
    }

@router.get("/alerts/stats")
def alert_stats(period: str | None = None, department: str | None = Query(None, max_length=100),
                db: Session = Depends(get_db), user: User = Depends(current_user)):
    """按处理状态统计预警条数，供列表页顶部与看板显示「还剩几条没处理」。"""
    scope, payload, violation = _scoped(user, department)
    if violation: raise HTTPException(403, violation)
    condition = scope_alert(select(FinancialAlert.review_status, func.count()), scope)
    if period:
        condition = condition.where(FinancialAlert.period == period)
    counts = {status: 0 for status in REVIEW_STATUSES}
    for status, total in db.execute(condition.group_by(FinancialAlert.review_status)).all():
        counts[status or "pending"] = int(total)
    counts["total"] = sum(counts[status] for status in REVIEW_STATUSES)
    return {"scope": payload, "period": period or "", "counts": counts,
            "labels": STATUS_LABELS, "statuses": list(REVIEW_STATUSES)}


@router.patch("/alerts/{alert_id}")
def review_alert(alert_id: int, payload: ReviewInput, request: Request,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    """处理一条预警：确认 / 误报 / 已处理 / 退回待处理，可带备注。

    这是原来完全缺失的环节——`needs_human_review` 建行时写 True，之后没有任何接口能改它，
    预警列表读起来永远像「没人看过」。状态会由 `calculate()` 继承，重算不会抹掉处理结果。
    """
    if payload.status not in REVIEW_STATUSES:
        raise HTTPException(422, f"状态只能是 {'、'.join(REVIEW_STATUSES)} 之一")
    row = _find_alert(db, alert_id, user)
    note = (payload.note or "").strip()
    if payload.status == "false_positive" and not note:
        # 判成误报却不写理由，等于把「为什么不是问题」这件事丢了，后续没人能复核
        raise HTTPException(422, "标记为误报时必须填写原因")
    previous_status = row.review_status
    row.review_status = payload.status
    row.review_note = note or None
    row.needs_human_review = payload.status == "pending"
    if payload.status == "pending":
        row.reviewed_by = None
        row.reviewed_at = None
    else:
        row.reviewed_by = user.username
        row.reviewed_at = datetime.now(timezone.utc)
    # 「这条预警被谁判成了误报」是事后复盘的核心事实，必须留痕（备注截断到 200 字以内）
    audit.record(db, user, "alert.review", target_type="alert", target_id=row.id,
                 detail={"period": row.period, "alert_type": row.alert_type, "department": row.department or "全公司",
                         "from": previous_status, "to": payload.status, "note": note},
                 request=request)
    db.commit()
    db.refresh(row)
    return _alert_json(row)


def _department_items(db: Session, names: set[str]) -> list[dict]:
    """把部门名整理成下拉选项：名称 + 展示标签 + 明细行数（数量帮用户判断哪个部门有数据）。

    **只统计传入的部门**：受限账号的名字集合里只有自己那一个，如果这里顺手把全库部门
    都 group by 出来，下拉里就会泄露别的部门名（这正是要防的事情）。
    """
    keys = sorted({name.strip() for name in names if name and name.strip()})
    if not keys:
        return []
    counts = {
        (name or "").strip(): int(total)
        for name, total in db.execute(
            select(FinancialTransaction.department, func.count())
            .where(FinancialTransaction.department.in_(keys))
            .group_by(FinancialTransaction.department)
        ).all()
    }
    return [{"name": key, "label": f"部门 {key}", "transactions": counts.get(key, 0)} for key in keys]


@router.get("/departments")
def departments(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """可切换的数据口径列表（看板/分析页的「查看口径」下拉）。

    不受限账号拿到全部部门，受限账号只拿到自己那几个——避免受限账号从下拉里
    看到别的部门名字，也避免它误以为能切过去（后端另有 403 兜底）。

    `switchable` 表示「这个账号能不能自己换口径」：不受限账号当然可以；**绑定多个部门的账号
    也可以**——它能在自己那几个部门之间收窄（`resolve_requested_scope` 允许，越界仍然 403）。
    绑定单个部门的账号没什么可换的，前端就不显示下拉，只显示当前口径。
    """
    own = scope_of(user)
    if own:
        names = set(own)
    else:
        found = {(row or "").strip() for row in db.scalars(select(FinancialTransaction.department).distinct()).all()}
        found |= {(row or "").strip() for row in db.scalars(select(FinancialMetric.department).distinct()).all()}
        found |= {(row or "").strip() for row in db.scalars(select(FinancialAlert.department).distinct()).all()}
        names = {name for name in found if name}
    return {"scope": scope_payload(own), "departments": _department_items(db, names),
            "switchable": own is None or len(own) > 1}


@router.get("/alerts/{alert_id}/transactions")
def alert_transactions(alert_id: int, limit: int = Query(200, ge=1, le=2000), row_offset: int = Query(0, ge=0, alias="offset"),
                       db: Session = Depends(get_db), user: User = Depends(current_user)):
    scope = scope_of(user)
    alert = db.get(FinancialAlert, alert_id)
    if not alert: return {"data": [], "message": "预警不存在"}
    # 预警本身也带口径：受限账号拿到全公司（或别的部门）预警的 id 时要挡住，否则等于绕开范围看明细。
    if not in_scope(scope, alert.department or None):
        return {"data": [], "message": "该预警不在当前账号的数据范围内", "scope": scope_payload(scope)}
    # 明细按「这条预警自己的部门」过滤，而不是按账号范围：多部门账号点开部门 A 的预警时，
    # 不该把部门 B 的同期明细混进来。
    when = alert.department or None
    year, month = map(int, alert.period.split("-"))
    condition = scope_transactions(select(FinancialTransaction).where(extract("year", FinancialTransaction.transaction_date) == year, extract("month", FinancialTransaction.transaction_date) == month), (when,) if when else None)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    rows = db.scalars(condition.order_by(FinancialTransaction.transaction_date.desc(), FinancialTransaction.id.desc()).limit(limit).offset(row_offset)).all()
    return {"alert": _alert_json(alert), "scope": scope_payload(scope), "total": total, "offset": row_offset, "limit": limit, "data": [{"id": x.id, "date": x.transaction_date.strftime("%Y-%m-%d"), "account": x.account_name, "department": x.department, "revenue": float(x.revenue_amount or 0), "cost": float(x.cost_amount or 0), "expense": float(x.expense_amount or 0), "description": x.description} for x in rows]}

@router.get("/summary")
def summary(period: str, department: str | None = Query(None, max_length=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    """某一期的合计。

    月口径直接读 `financial_metrics`（那张表本来就是月行）；季/年读 `financial_rollups` 的落库合计，
    落库还没覆盖这一期时按同一算法现算兜底。`source` 如实说明数字是哪来的
    （`month`/`stored`/`computed`/`empty`），页面才不会把现算的数当成落库的数；
    `complete`/`missing_months` 对季/年说明「这几期是不是凑齐了」——演示数据只有 2026-04~10，
    2026 年度缺 5 个月，年度合计不是全年。
    """
    scope, payload, violation = _scoped(user, department)
    if violation: raise HTTPException(403, violation)
    board = rollups.aggregate(db, period, scope)
    grain = board["grain"]
    body = {
        "has_data": bool(board["totals"]),
        "period": period,
        "grain": grain,
        "grain_label": GRAIN_LABELS.get(grain, ""),
        "period_label": board["period_label"],
        "source": board["source"],
        "months": board["months"],
        "covered_months": board["covered_months"],
        "missing_months": board["missing_months"],
        "complete": board["complete"],
        "scope": payload,
    }
    if not board["totals"]:
        return {**body, "revenue_amount": 0, "cost_amount": 0, "net_profit_amount": 0, "net_profit_ratio": 0,
                "department": "", "departments": []}
    metric = board["totals"][0]
    return {**body, "revenue_amount": float(metric.revenue_amount), "cost_amount": float(metric.cost_amount), "net_profit_amount": float(metric.net_profit_amount), "net_profit_ratio": float(metric.net_profit_ratio), "department": metric.department or "", "departments": list(metric.departments)}
