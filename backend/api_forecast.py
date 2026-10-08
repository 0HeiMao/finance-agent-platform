from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.auth import current_user
from backend.db import get_db
from backend.models import FinancialMetric, User
from backend.scope import metric_totals, scope_metric, scope_of, scope_payload

router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])
def trend(values: list[float], periods: int):
    if not values: return [0.0] * periods
    delta = (values[-1] - values[0]) / max(len(values) - 1, 1)
    return [round(values[-1] + delta * (i + 1), 2) for i in range(periods)]
@router.get("/financial")
def forecast(periods: int = Query(3, ge=1, le=12), db: Session = Depends(get_db), user: User = Depends(current_user)):
    # 预测是「外推」，喂进去的历史序列一旦混入别的口径，趋势线会直接画错，
    # 所以这里和看板用同一个 scope；多部门账号的序列必须**一期一个点**（指标行按期间聚合，
    # 否则一个期间会出现两条部门行，趋势线会把两个部门当成前后两期）。
    scope = scope_of(user)
    rows = db.scalars(scope_metric(select(FinancialMetric), scope)
                      .order_by(FinancialMetric.period, FinancialMetric.id)).all()
    totals = metric_totals(rows)
    return {"method":"linear_trend","periods":periods,
            "revenue_amounts":trend([float(item.revenue_amount) for item in totals],periods),
            "net_profit_amounts":trend([float(item.net_profit_amount) for item in totals],periods),
            "needs_human_review":True,"scope":scope_payload(scope),
            "based_on_periods":[item.period for item in totals]}
