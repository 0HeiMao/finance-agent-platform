from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session
from backend.auth import current_user
from backend.db import get_db
from backend.models import FinancialMetric, User
from backend.scope import scope_metric, scope_of, scope_payload
router = APIRouter(prefix="/api/v1/forecast", tags=["forecast"])
def trend(values: list[float], periods: int):
    if not values: return [0.0] * periods
    delta = (values[-1] - values[0]) / max(len(values) - 1, 1)
    return [round(values[-1] + delta * (i + 1), 2) for i in range(periods)]
@router.get("/financial")
def forecast(periods: int = Query(3, ge=1, le=12), db: Session = Depends(get_db), user: User = Depends(current_user)):
    # 预测是「外推」，喂进去的历史序列一旦混入别的口径，趋势线会直接画错，
    # 所以这里和看板用同一个 scope：受限账号只按本部门的指标序列外推。
    department = scope_of(user)
    rows = db.scalars(scope_metric(select(FinancialMetric), department).order_by(FinancialMetric.period)).all()
    return {"method":"linear_trend","periods":periods,"revenue_amounts":trend([float(x.revenue_amount) for x in rows],periods),"net_profit_amounts":trend([float(x.net_profit_amount) for x in rows],periods),"needs_human_review":True,"scope":scope_payload(department),"based_on_periods":[x.period for x in rows]}
