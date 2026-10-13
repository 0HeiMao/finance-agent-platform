"""管理员接口：用户、导入批次、知识库三块的查看与维护。

**权限与数据范围是两件事**（⑳ 起）：能不能管看账号上的 `users.is_admin` 标识，能看哪些数据看部门
列表（`backend/scope.py::scope_of`）。⑳ 之前两者是同一件事——「没有部门 = 不受部门限制 = 管理员」——
于是任何账号把自己的部门清空就等于升权，自助注册的新账号更是一注册就是管理员。现在
`is_admin()` 只读那一位标识，部门清空只表示「范围还没分配，什么都看不到」。
所有接口都经过 `admin_user` 依赖，普通账号拿到 403 而不是空数据，避免"看不到就等于没有"的误判。

三条刻意的安全约束：
- 不允许管理员改自己（部门或管理员标识）：改错了会立刻把自己锁在管理入口之外，属于自伤操作，
  要走就让**另一个**管理员来改。因此「最后一个管理员」不可能被自己弄丢，删除账号也另有一道守卫。
- 收回管理员标识前会数一遍管理员人数，最后一个不给收回（否则这个库再没人能管）。
- 删除导入批次时会**重算受影响期间**，否则会留下与交易明细对不上的历史指标（幽灵报表）。
  长任务版本走 `POST /admin/jobs/batch-revert`（后台执行 + 任务可查），同步版本保留给脚本调用。
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend import agent_sessions, alerts_outbound, audit, batch_ops, biztime, jobs, lockout, rollups
from backend.alert_rules import COMPARATORS, ensure_rules, rule_scope_label
from backend.auth import current_user, revoke_tokens, token_version_of
from backend.config import get_settings
from backend.db import get_db
from backend.knowledge_access import manageable_chunks
from backend.models import (
    AlertChannel,
    AlertDelivery,
    AlertRule,
    BackgroundJob,
    FinancialAlert,
    FinancialMetric,
    FinancialRollup,
    FinancialTransaction,
    User,
)
from backend.observability import snapshot
from backend.periods import GRAIN_LABELS, grain_of
from backend.rag.pgvector_store import DocumentChunk
from backend.scope import departments_of, is_admin, scope_of

logger = logging.getLogger("finance-api")

router = APIRouter(prefix="/api/v1/admin", tags=["admin"])

# 规则能引用的指标名与中文名（与 backend/alert_rules.py 的 metric_values 对应）
METRIC_LABELS = {
    "cost_ratio": "成本率（成本 ÷ 收入）",
    "net_profit_amount": "净利润金额",
    "gross_profit_amount": "毛利金额",
    "revenue_amount": "收入金额",
    "cost_amount": "成本金额",
    "expense_amount": "费用金额",
    "gross_profit_ratio": "毛利率",
    "net_profit_ratio": "净利率",
}

# 预警级别（与 FinancialAlert.level 的取值一致）
LEVELS = {"high": "高", "medium": "中", "low": "低"}


def admin_user(user: User = Depends(current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "需要管理员权限：当前账号没有管理员标识")
    return user


def _admin_count(db: Session, *, excluding: int | None = None) -> int:
    """还有几个管理员（`excluding` 用来把某一行先排除掉再数）。"""
    condition = select(func.count()).select_from(User).where(User.is_admin.is_(True))
    if excluding is not None:
        condition = condition.where(User.id != excluding)
    return db.scalar(condition) or 0


class DepartmentUpdate(BaseModel):
    # 部门（数据范围）：`departments` 优先；只给 `department` 时按「一个部门」处理（老前端兼容）；
    # 给空串/空数组 = **清空范围**（⑳ 起不再等于升级为管理员，那只表示「什么都看不到」）。
    # 两个都**没发**时范围一个字都不动（只授予/收回管理员标识时就是这种请求）。
    department: str = Field(default="", max_length=100)
    departments: list[str] | None = Field(default=None, max_length=20)
    # 管理员标识（⑳）：None = 本次不动它；true/false = 显式授予/收回。与部门各改各的。
    is_admin: bool | None = Field(default=None)


class BatchRevertRequest(BaseModel):
    batch: str = Field(min_length=1, max_length=200)


def _count(db: Session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return db.scalar(statement) or 0


def _blank_departments():
    """「不受部门限制」的行条件：多部门列（JSONB 数组）与老单值列都为空。

    `jsonb_array_length` 对 NULL / 非数组返回 NULL，coalesce 成 0 后与 0 比较，
    所以 `departments IS NULL`、`departments = []`、以及「两者都空」都能覆盖到。
    """
    return ((func.coalesce(func.jsonb_array_length(User.departments), 0) == 0)
            & (User.department.is_(None) | (func.trim(User.department) == "")))


def _periods_of(db: Session, column_date) -> list[str]:
    # 先取 DISTINCT 再排序：PostgreSQL 不允许 "SELECT DISTINCT x ... ORDER BY f(x)"
    # （ORDER BY 的表达式必须出现在选择列表里，函数式 distinct 也救不了）。
    period = func.to_char(column_date, "YYYY-MM").label("period")
    distinct_periods = select(period).distinct().subquery()
    rows = db.execute(
        select(distinct_periods.c.period).order_by(distinct_periods.c.period.desc())
    ).all()
    return [row[0] for row in rows]


def _batch_query():
    """批次聚合的「无 limit 版」查询，供计数与分页共用，避免两处条件写歪。"""
    return (
        select(
            FinancialTransaction.import_batch,
            func.count().label("rows"),
            func.min(FinancialTransaction.transaction_date),
            func.max(FinancialTransaction.transaction_date),
            func.min(FinancialTransaction.created_at),
            func.array_agg(func.distinct(
                func.to_char(FinancialTransaction.transaction_date, "YYYY-MM"))),
        )
        .where(FinancialTransaction.import_batch.is_not(None))
        .group_by(FinancialTransaction.import_batch)
        .order_by(func.count().desc(), FinancialTransaction.import_batch)
    )


def batch_count(db: Session) -> int:
    return db.scalar(
        select(func.count(func.distinct(FinancialTransaction.import_batch)))
        .where(FinancialTransaction.import_batch.is_not(None))
    ) or 0


def _batch_rows(db: Session, limit: int, row_offset: int = 0) -> list[dict]:
    rows = db.execute(_batch_query().limit(limit).offset(row_offset)).all()
    return [
        {
            "batch": row[0],
            "short": (row[0] or "")[:12],
            "rows": row[1],
            "first_date": row[2].strftime("%Y-%m-%d") if row[2] else None,
            "last_date": row[3].strftime("%Y-%m-%d") if row[3] else None,
            "imported_at": row[4].strftime("%Y-%m-%d %H:%M:%S") if row[4] else None,
            "periods": sorted(row[5] or []),
        }
        for row in rows
    ]


def _quality_checks(db: Session) -> list[dict]:
    """数据完整性体检：只做便宜的计数，但能抓到"指标与明细对不上"这类真问题。"""
    transaction_periods = set(_periods_of(db, FinancialTransaction.transaction_date))
    metric_periods = set(
        db.scalars(select(FinancialMetric.period)).all()
    )
    orphan_metrics = sorted(metric_periods - transaction_periods)
    missing_metrics = sorted(transaction_periods - metric_periods)
    blank_department = db.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(func.coalesce(FinancialTransaction.department, "") == "")) or 0
    zero_amount = db.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(
            FinancialTransaction.revenue_amount == 0,
            FinancialTransaction.cost_amount == 0,
            FinancialTransaction.expense_amount == 0,
        )) or 0
    batch_total = db.scalar(
        select(func.count(func.distinct(FinancialTransaction.import_batch)))) or 0
    no_batch = db.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch.is_(None))) or 0

    def check(name: str, value: int, hint: str) -> dict:
        return {"name": name, "value": value, "level": "warn" if value else "ok", "hint": hint}

    return [
        check("有明细但缺指标的期间", len(missing_metrics),
              "，".join(missing_metrics) or "指标与明细一一对应"),
        check("有指标但无明细的期间", len(orphan_metrics),
              "，".join(orphan_metrics) or "没有幽灵期间"),
        check("部门为空的数据行", blank_department,
              "部门为空只影响按部门归集，不影响总额"),
        check("三项金额全为 0 的数据行", zero_amount,
              "通常来自爬取源里的红字冲销或占位记录"),
        check("没有导入批次号的数据行", no_batch,
              f"当前共有 {batch_total} 个批次号"),
    ]


@router.get("/overview")
def overview(db: Session = Depends(get_db), me: User = Depends(admin_user)):
    users = db.scalars(select(User).order_by(User.id)).all()
    periods = list(db.scalars(
        select(FinancialMetric.period).distinct()
        .order_by(FinancialMetric.period.desc())).all())
    knowledge_sources = db.scalar(
        select(func.count(func.distinct(DocumentChunk.source)))) or 0
    batches = _batch_rows(db, 5)
    # 指标与预警只数「全公司口径」那一行（department IS NULL）：一次 calculate 会为每个部门
    # 各写一行派生数据，直接 _count 会把同一期的指标重复计入，管理页的规模数字看着像涨了 50 倍。
    company_metrics = _count(db, FinancialMetric, FinancialMetric.department.is_(None))
    company_alerts = _count(db, FinancialAlert, FinancialAlert.department.is_(None))
    all_metrics = _count(db, FinancialMetric)
    all_alerts = _count(db, FinancialAlert)
    return {
        "counts": {
            "users": len(users),
            "admins": sum(1 for user in users if is_admin(user)),
            "transactions": _count(db, FinancialTransaction),
            "metrics": company_metrics,
            "alerts": company_alerts,
            # 部门口径行的数量单独报，好让「全公司 7 期」和表里 50 行指标的区别一眼可见。
            "metric_rows": all_metrics,
            "alert_rows": all_alerts,
            "knowledge_chunks": _count(db, DocumentChunk),
            "knowledge_sources": knowledge_sources,
            "batches": db.scalar(
                select(func.count(func.distinct(FinancialTransaction.import_batch)))) or 0,
        },
        "periods": {"latest": periods[0] if periods else None, "all": periods},
        "recent_batches": batches,
        "quality": _quality_checks(db),
        # 季/年落库的覆盖面：管理页要能一眼看出「落了几期、哪几期缺月」，
        # 否则「年度合计」只有一个数字，看不出它是不是全年。
        "rollups": rollups.coverage(db),
        "observability": snapshot(),
        "me": {"id": me.id, "username": me.username, "department": me.department or ""},
    }


class RollupRebuildInput(BaseModel):
    """重算季/年落库的入参。两个字段都留空 = 按库里现有的月指标覆盖所有季与年。"""
    grains: list[str] | None = Field(default=None, max_length=2, description="quarter / year，默认两者都算")
    periods: list[str] | None = Field(default=None, max_length=200, description="只重算这几期，如 2026-Q3、2026")


@router.get("/rollups")
def list_rollups(grain: str | None = Query(None, pattern=r"^(quarter|year)$"),
                 db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """季度/年度落库明细 + 覆盖面（哪些期落了、哪几期缺月）。

    管理页拿它说明「年度合计是不是全年」：演示数据 2026-04~10，2026 年度缺 5 个月，
    `complete=false` 且 `missing_months` 点名，而不是把 7 个月假装成 12 个月。
    """
    statement = select(FinancialRollup).order_by(FinancialRollup.period, FinancialRollup.id)
    if grain:
        statement = statement.where(FinancialRollup.grain == grain)
    rows = db.scalars(statement).all()
    return {
        "data": [rollups.serialize(row) for row in rows],
        "coverage": rollups.coverage(db),
        "grains": [{"value": item, "label": GRAIN_LABELS[item]} for item in rollups.DEFAULT_GRAINS],
    }


@router.post("/rollups/rebuild")
def rebuild_rollups(payload: RollupRebuildInput, request: Request,
                    db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """重算季/年落库。

    两个用处：导入或重算某个月之后补齐它所在的季/年（正常路径由 `calculate()` 自动刷新，
    这里给「老库回填」和「落库数字被怀疑不对时的手工重算」兜底）。
    """
    grains = tuple(item for item in (payload.grains or list(rollups.DEFAULT_GRAINS))
                   if item in rollups.DEFAULT_GRAINS)
    if not grains:
        raise HTTPException(422, "grains 只能是 quarter、year")
    periods = list(payload.periods or []) or None
    if periods:
        bad = [item for item in periods if grain_of(item) not in grains]
        if bad:
            raise HTTPException(422, f"这些期间不是季/年口径，或与 grains 对不上：{'、'.join(bad)}")
    written = rollups.rollup(db, grains=grains, periods=periods)
    audit.record(db, me, "rollups.rebuild", target_type="rollups", target_id=",".join(periods or ()),
                 detail={"grains": list(grains), "rows": len(written),
                         "periods": periods or "全部季/年"}, request=request)
    db.commit()
    return {"rows": len(written), "grains": list(grains),
            "data": written, "coverage": rollups.coverage(db)}


@router.get("/users")
def list_users(
    query: str | None = Query(None, max_length=100),
    department: str | None = Query(None, max_length=100),
    unrestricted: bool | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    row_offset: int = Query(0, ge=0, alias="offset"),
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """用户列表（分页 + 筛选）。

    `total` 必须是**满足筛选条件的总行数**，不是当前这一页的条数——否则前端拿到 50 条会
    显示「共 50 人」，翻页也就无从谈起（这正是改造前的缺陷：`total = len(users)`，
    而 `limit` 一截断就撒谎）。筛选在数据库里做，分页才不会漏行。
    """
    condition = select(User)
    if query:
        condition = condition.where(User.username.ilike(f"%{query}%"))
    if department is not None:
        # 多部门账号在 `departments`（JSONB 数组）里，单值列只是第一个部门的回落：
        # 只比单值列会让「看部门 018 的账号」漏掉「018+097」这种账号。
        if department:
            condition = condition.where(
                (User.department == department) | User.departments.contains([department]))
        else:
            condition = condition.where(User.department.is_(None))
    if unrestricted is not None:
        # 「不受部门限制」= 部门列表为空（⑳ 起这只是「范围读全公司」，与管理员标识无关：
        # 没配部门的普通账号同样落在这一档，但他们什么都看不到）。用或上空白字符串/空数组
        # 是为了兼容历史上写成空串或空列表的行。
        blank = _blank_departments()
        condition = condition.where(blank if unrestricted else ~blank)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    users = db.scalars(
        condition.order_by(User.id).limit(limit).offset(row_offset)).all()
    # 筛选下拉的选项由后端给：前端自己拿一页数据去凑选项，翻页后就自相矛盾了。
    # 选项从账号记录里扫（而不是 distinct 单值列），这样「018+097」这种多部门账号的
    # 两个部门都会出现在下拉里；账号数量是「几个到几百个」，扫一遍比 lateral join 好懂。
    known: list[str] = []
    for row in db.scalars(select(User).order_by(User.id)).all():
        for name in departments_of(row):
            if name not in known:
                known.append(name)
    blank = _blank_departments()
    return {
        "data": [
            {
                "id": user.id,
                "username": user.username,
                "department": user.department or "",
                "departments": list(departments_of(user)),
                # 两件事分别给：能不能管（is_admin）与当前是不是全公司口径（unrestricted）。
                # 管理员被收窄到某部门时是第一栏 true、第二栏 false —— 前端「管理员」徽标读前者。
                "is_admin": is_admin(user),
                "unrestricted": scope_of(user) is None,
                "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else None,
                # 会话版本号：0 表示「从没退出/被踢过」，>0 表示历史上有过多少次作废动作
                "token_version": token_version_of(user),
                # 登录失败锁定（backend/lockout.py）：失败次数与「还剩几秒解锁」都要给出去，
                # 否则管理员只看到「登不上」，分不清是密码错还是被锁了。
                "failed_attempts": int(user.failed_attempts or 0),
                "locked_until": (user.locked_until.astimezone(UTC).isoformat()
                                 if user.locked_until else None),
                "locked": bool(lockout.locked_for(user)),
                "is_self": user.id == me.id,
            }
            for user in users
        ],
        "total": total,
        "offset": row_offset,
        "limit": limit,
        "departments": known,
        "unrestricted_count": db.scalar(
            select(func.count()).select_from(User).where(blank)) or 0,
        "me": {"id": me.id, "username": me.username},
    }


def _target_user(db: Session, user_id: int, me: User) -> User:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == me.id:
        raise HTTPException(400, "不能通过管理接口修改自己：数据范围与管理员标识要由另一个管理员改（⑳ 起没有自改入口）")
    return target


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: DepartmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """改一个账号的**数据范围**与**管理员标识**（两件事，可以只改其中一件）。

    自改被 `_target_user` 挡住 —— 那一条才是「不会把自己锁在门外」的主力：既然操作者也必须是
    管理员，接口上就永远至少剩下操作者本人这一位，所以「不能收回最后一个管理员」这条计数守卫
    在接口上只是兜底，真正会踩到的是没有「自己」这个概念的 `scripts/grant_admin.py --revoke`。
    两处改动分开留痕：只改范围就只记 `user.department_change`，只改标识就只记 `user.admin_change`；
    没发的字段一个字都不动（只授予标识的请求不该顺手清掉对方的部门）。
    """
    target = _target_user(db, user_id, me)
    previous_departments = "、".join(departments_of(target)) or "未分配"
    previous_admin = is_admin(target)
    if payload.is_admin is False and previous_admin and _admin_count(db, excluding=target.id) == 0:
        raise HTTPException(400, "不能收回最后一个管理员的管理员标识：收回后这个库再没有人能管")
    # 只改标识的请求（{"is_admin": true}）里没有部门字段，但 `department` 的默认值是空串 —— 直接照它
    # 执行就会把目标账号的部门一起清掉。所以只有客户端**真发了**某一个部门字段才动范围。
    department_fields_sent = bool({"departments", "department"} & payload.model_fields_set)
    if department_fields_sent:
        if payload.departments is not None:
            wanted = [item.strip() for item in payload.departments if (item or "").strip()]
        else:
            single = payload.department.strip()
            wanted = [single] if single else []
        target.departments = wanted or None
        # 单值列同步成第一个部门：老前端的「部门」列与审计文案读的都是它（范围只看 departments）。
        target.department = wanted[0] if wanted else None
    current_departments = "、".join(departments_of(target)) or "未分配"
    if department_fields_sent and current_departments != previous_departments:
        audit.record(db, me, "user.department_change", target_type="user", target_id=target.id,
                     detail={"username": target.username, "from": previous_departments,
                             "to": current_departments}, request=request)
    if payload.is_admin is not None and bool(payload.is_admin) != previous_admin:
        target.is_admin = bool(payload.is_admin)
        audit.record(db, me, "user.admin_change", target_type="user", target_id=target.id,
                     detail={"username": target.username,
                             "from": "管理员" if previous_admin else "普通账号",
                             "to": "管理员" if target.is_admin else "普通账号"},
                     request=request)
    db.commit()
    db.refresh(target)
    return {
        "id": target.id,
        "username": target.username,
        "department": target.department or "",
        "departments": list(departments_of(target)),
        "is_admin": is_admin(target),
        "unrestricted": scope_of(target) is None,
    }


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_user)):
    target = _target_user(db, user_id, me)
    # 删掉最后一个管理员 = 这个库再没人能管（不能改自己，所以这条只能在删「别人」时踩到）。
    if is_admin(target) and _admin_count(db, excluding=target.id) == 0:
        raise HTTPException(400, "不能删除最后一个管理员：删掉后这个库再没有人能管")
    username = target.username
    department = "、".join(departments_of(target))
    # 先记录再删：删掉以后 target.id 还在这个 Python 对象里，但写进 detail 的必须是快照
    audit.record(db, me, "user.delete", target_type="user", target_id=target.id,
                 detail={"username": username, "department": department or "未分配",
                         "is_admin": is_admin(target)}, request=request)
    db.delete(target)
    db.commit()
    return {"deleted": True, "username": username}


@router.post("/users/{user_id}/logout")
def force_logout_user(user_id: int, request: Request, db: Session = Depends(get_db),
                      me: User = Depends(admin_user)):
    """把某个账号**所有**已签发的令牌作废（会话版本号 +1），用于「人在别处、必须立刻踢掉」。

    自己也走 `_target_user` 的 400：管理员想让自己下线，用 `POST /auth/logout` 更合适
    （那个接口审计的是「本人退出」，这里审计的是「管理员踢人」，混在一起就查不清了）。
    """
    target = _target_user(db, user_id, me)
    previous = token_version_of(target)
    version = revoke_tokens(target)
    db.add(target)
    audit.record(db, me, "user.force_logout", target_type="user", target_id=target.id,
                 detail={"username": target.username, "from_version": previous, "to_version": version,
                         "effect": "该账号此前签发的所有令牌立即失效"}, request=request)
    db.commit()
    return {"id": target.id, "username": target.username, "token_version": version, "logged_out": True}


@router.post("/users/{user_id}/unlock")
def unlock_user(user_id: int, request: Request, db: Session = Depends(get_db),
                me: User = Depends(admin_user)):
    """提前解除某个账号的登录失败锁定（把失败次数与锁定时间清掉）。

    什么时候需要它：账号被爆破打锁了，本人即使记得密码也只能等到 `locked_until` 才登得上
    （`max_seconds` 默认封顶 900 秒，但值班的人往往希望立刻放行）。**不需要**它是「重置密码」
    ——这里一个字都不碰口令与会话版本：解锁不等于让人换密码，也不等于踢人下线。
    """
    target = _target_user(db, user_id, me)
    info = lockout.unlock(target)
    db.add(target)
    audit.record(db, me, "user.unlock", target_type="user", target_id=target.id,
                 detail={"username": target.username, "cleared_attempts": info["cleared_attempts"],
                         "was_locked_for": info["was_locked_for"],
                         "effect": "该账号可以立刻再次尝试登录"}, request=request)
    db.commit()
    return {"id": target.id, "username": target.username, "failed_attempts": 0,
            "locked_until": None, "locked": False, "cleared_attempts": info["cleared_attempts"]}


@router.get("/departments")
def list_departments(db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """部门权限总览：数据里有哪些部门、各自多少数据、哪些账号被限定在这个部门。

    部门**不是**一张可增删的表，而是从数据行里长出来的：财务明细的 `department` 列、
    指标/预警的部门口径行、知识库片段的 `metadata_json.department`。所以这里只做汇总与
    账号绑定，不提供「新建部门」——凭空建一个没有数据的部门没有任何意义。
    """
    users = db.scalars(select(User).order_by(User.id)).all()
    departments: dict[str, dict] = {}

    def slot(name: str) -> dict:
        return departments.setdefault(name, {
            "name": name, "label": f"部门 {name}",
            "transactions": 0, "revenue_amount": Decimal(0), "expense_amount": Decimal(0),
            "periods": [], "metric_rows": 0, "alert_rows": 0, "users": [],
        })

    for name, count, revenue, expense in db.execute(
        select(
            FinancialTransaction.department,
            func.count(),
            func.coalesce(func.sum(FinancialTransaction.revenue_amount), 0),
            func.coalesce(func.sum(FinancialTransaction.expense_amount), 0),
        )
        .where(func.coalesce(FinancialTransaction.department, "") != "")
        .group_by(FinancialTransaction.department)
    ).all():
        item = slot(name)
        item["transactions"] = int(count)
        item["revenue_amount"] = revenue
        item["expense_amount"] = expense

    for name, count in db.execute(
        select(FinancialMetric.department, func.count())
        .where(FinancialMetric.department.is_not(None))
        .group_by(FinancialMetric.department)
    ).all():
        slot(name)["metric_rows"] = int(count)

    for name, count in db.execute(
        select(FinancialAlert.department, func.count())
        .where(FinancialAlert.department.is_not(None))
        .group_by(FinancialAlert.department)
    ).all():
        slot(name)["alert_rows"] = int(count)

    for name in db.scalars(
        select(func.distinct(FinancialTransaction.department))
        .where(func.coalesce(FinancialTransaction.department, "") != "")
    ).all():
        item = slot(name)
        period = func.to_char(FinancialTransaction.transaction_date, "YYYY-MM").label("period")
        distinct_periods = (
            select(period).where(FinancialTransaction.department == name).distinct().subquery())
        item["periods"] = [row[0] for row in db.execute(
            select(distinct_periods.c.period).order_by(distinct_periods.c.period)).all()]

    unassigned = []
    for user in users:
        payload = {
            "id": user.id, "username": user.username, "department": user.department or "",
            "departments": list(departments_of(user)),
            "is_admin": is_admin(user), "unrestricted": scope_of(user) is None,
            "is_self": user.id == me.id,
        }
        if not payload["departments"]:
            # ⑳ 起「没有部门」这一档里有两类账号，都必须出现在这里，不能只收 unrestricted 的那种：
            # 管理员（没配部门 = 看全公司）与刚注册、还没分配范围（什么都看不到）的普通账号。
            # 否则新账号会在部门权限页里彻底消失，管理员根本找不到该给谁配范围。
            unassigned.append(payload)
        else:
            # 多部门账号会出现在它每一个部门下（每行都带着完整的部门列表，前端只展示一行归属）。
            for name in payload["departments"]:
                slot(name)["users"].append(payload)

    rows = []
    for item in departments.values():
        rows.append({
            **item,
            "expense_total": item["expense_amount"] or Decimal(0),
            "revenue_amount": f"{Decimal(item['revenue_amount'] or 0):.2f}",
            "expense_amount": f"{Decimal(item['expense_amount'] or 0):.2f}",
            "user_count": len(item["users"]),
        })
    # 有账号或有权重的排前面：部门列表有几十行，管理员真正要找的是「谁被限定了」。
    rows.sort(key=lambda item: (item["user_count"] == 0, -item["expense_total"], item["name"]))
    for item in rows:
        item.pop("expense_total", None)
    return {
        "departments": rows,
        # 键名保留 `unrestricted_users`（老前端与脚本读它），含义按 ⑳ 收窄成
        # 「不属于任何部门的账号」：里面有管理员，也有还没分配范围的普通账号。
        "unassigned_users": unassigned,
        "unrestricted_users": unassigned,
        "summary": {
            "departments_with_data": sum(1 for item in rows if item["transactions"]),
            "departments_with_users": sum(1 for item in rows if item["user_count"]),
            "unrestricted_users": sum(1 for item in unassigned if item["unrestricted"]),
            "unassigned_users": len(unassigned),
            "admin_users": sum(1 for item in users if is_admin(item)),
            "total_users": len(users),
        },
        "me": {"id": me.id, "username": me.username, "department": me.department or "",
               "departments": list(departments_of(me))},
    }


@router.get("/batches")
def list_batches(
    limit: int = Query(50, ge=1, le=200),
    row_offset: int = Query(0, ge=0, alias="offset"),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """批次列表（分页）。`total` 是**库里的批次总数**，不是本页条数。"""
    return {"data": _batch_rows(db, limit, row_offset),
            "total": batch_count(db), "offset": row_offset, "limit": limit}


@router.delete("/batches/{batch}")
def delete_batch(batch: str, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """同步撤销：先删该批次明细，再重算受影响期间（没有剩余明细的期间连指标一起清掉）。

    保留这个同步版本是为了脚本与外部调用方——它们的等待成本可控。
    界面走 `POST /admin/jobs/batch-revert`：期间一多，一个 HTTP 请求里重算完所有期间并不合适。
    """
    try:
        return batch_ops.revert_batch(db, batch, actor=me.username, ip=audit.client_ip(request))
    except batch_ops.BatchNotFound as exc:
        raise HTTPException(404, str(exc)) from exc


@router.post("/jobs/batch-revert", status_code=202)
def submit_batch_revert(
    payload: BatchRevertRequest,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """把「撤销批次 + 重算」排成后台任务，立刻返回 `202` 与任务记录。

    提交前只做一次便宜的 `count(*)` 校验：批次不存在就直接 404，不必为一个注定失败的任务
    建一条记录（那样界面上会长出一条没人能修的 failed，而用户其实只是点错了）。
    真正的删除与重算在 `jobs.run_job` 里执行，失败原因落在任务的 `error` 上。

    **幂等**（⑩）：请求头 `Idempotency-Key` 有值就用它去重；没有值时也会拦住「同一批次正在撤销中」
    的重复提交。去重命中时不新建任务，返回的还是那一条（`deduplicated: true`），
    所以用户双击「撤销」不会把同一批重算两遍。
    """
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if len(key) > jobs.IDEMPOTENCY_KEY_MAX:
        raise HTTPException(422, f"Idempotency-Key 太长（最多 {jobs.IDEMPOTENCY_KEY_MAX} 字符）")
    # 去重查在「批次还在不在」之前：同一个键的请求重放时，批次很可能已经被第一次执行删掉了，
    # 这时正确回答是「还是那条任务」，而不是 404 —— 否则客户端的重试永远拿不到它要的结果。
    if jobs.find_key_holder(db, key) is None and not batch_ops.batch_rows(db, payload.batch):
        raise HTTPException(404, "批次不存在或已被撤销")
    job, created = jobs.submit(
        db, "batch.revert", {"batch": payload.batch, "ip": audit.client_ip(request)},
        actor=me.username, idempotency_key=key or None,
    )
    if created:
        background.add_task(jobs.run_job, job.id)
    else:
        logger.info("后台任务去重：批次 %s 已有任务 #%s（状态 %s）", payload.batch, job.id, job.status)
    return {**jobs.serialize(job), "deduplicated": not created}


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(
    job_id: int,
    background: BackgroundTasks,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """人工重试一条失败的任务：放回队列、追加一份重试预算，立刻返回 `202`。

    只允许对 `failed` 的任务重试：对成功的任务「重试」意味着再删一次已经删掉的数据
    （处理函数是幂等的，所以只会是一条空结果，但把「撤销」按钮再按一次仍然是个坏主意），
    对还在排队/运行中的任务重试则会排出第二条一模一样的活儿，两者都直接 409。
    """
    job = db.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(404, "没有这条后台任务")
    if job.status != "failed":
        raise HTTPException(409, f"只有失败的任务才需要重试（当前状态：{jobs.STATUS_LABELS.get(job.status, job.status)}）")
    if job.kind not in jobs.HANDLERS:
        raise HTTPException(409, f"这个任务类型已经没有处理函数了，重试也不会成功：{job.kind}")
    jobs.prepare_retry(db, job)
    audit.record(db, me.username, "job.retry", target_type="job", target_id=str(job.id),
                 detail={"kind": job.kind, "previous_error": job.error,
                         "ip": audit.client_ip(request)})
    db.commit()
    background.add_task(jobs.run_job, job.id)
    return jobs.serialize(job)


@router.get("/jobs")
def list_jobs(
    status: str | None = Query(None, max_length=20),
    kind: str | None = Query(None, max_length=40),
    limit: int = Query(50, ge=1, le=200),
    row_offset: int = Query(0, ge=0, alias="offset"),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """后台任务列表（倒序）。`total` 是筛选后的总条数，不是本页条数。"""
    if status and status not in jobs.STATUSES:
        raise HTTPException(422, f"状态只能是 {'/'.join(jobs.STATUSES)}")
    rows, total = jobs.query(db, status=status, kind=kind, limit=limit, offset=row_offset)
    return {
        "data": [jobs.serialize(row) for row in rows],
        "total": total, "offset": row_offset, "limit": limit,
        "kinds": [{"value": name, "label": jobs.KIND_LABELS.get(name, name)} for name in sorted(jobs.HANDLERS)],
        "statuses": jobs.status_options(),
    }


@router.get("/jobs/{job_id}")
def get_job(job_id: int, db: Session = Depends(get_db), _: User = Depends(admin_user)):
    """查一条任务：前端轮询它拿进度，失败时读 `error` 而不是把 500 摊给用户。"""
    job = db.get(BackgroundJob, job_id)
    if job is None:
        raise HTTPException(404, "没有这条后台任务")
    return jobs.serialize(job)


@router.get("/agent-usage")
def agent_usage_summary(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """跨用户的 Agent 问答用量（⑮）。

    与 `/api/v1/ai/agent/usage` 分开是因为口径不同：那个只报本人，这个按用户拆开，给管理员看
    「谁在烧 token」。`estimated` 与 `api` 也分开计数 —— 估算出来的数字只能当参考，不是账单。
    """
    stats = agent_sessions.usage_stats(db, user_id=None, days=days)
    return {"data": stats, "series": agent_sessions.daily_series(stats),
            "status_labels": agent_sessions.STATUS_LABELS}


@router.get("/knowledge")
def list_knowledge(
    source: str | None = Query(None, max_length=500),
    document_type: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    row_offset: int = Query(0, ge=0, alias="offset"),
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """知识库来源聚合（分页 + 筛选）。

    一行 = 一个来源（同一份文件切出的多个片段合成一行），所以 `total` 是**满足筛选的来源个数**，
    `matched_chunks` 是这些来源的片段数；再给一个全局 `all_chunks`，让页面能显示「命中 3 个来源 / 共 5 个片段」。
    片段级过滤（来源名、文档类型）必须在聚合前做 where，否则分页会把别人的片段算进命中数。
    """
    clauses = [manageable_chunks(me)]
    if source:
        clauses.append(DocumentChunk.source.ilike(f"%{source}%"))
    if document_type:
        clauses.append(DocumentChunk.document_type == document_type)
    total = db.scalar(select(func.count(func.distinct(DocumentChunk.source))).where(*clauses)) or 0
    matched_chunks = db.scalar(
        select(func.count()).select_from(DocumentChunk).where(*clauses)) or 0
    rows = db.execute(
        select(
            DocumentChunk.source,
            func.count().label("chunks"),
            func.min(DocumentChunk.created_at),
            func.max(DocumentChunk.created_at),
            func.array_agg(func.distinct(DocumentChunk.document_type)),
            func.array_agg(func.distinct(DocumentChunk.metadata_json["embedding_model"].astext)),
            func.min(func.substr(DocumentChunk.content, 1, 120)),
        )
        .where(*clauses)
        .group_by(DocumentChunk.source)
        .order_by(func.count().desc(), DocumentChunk.source)
        .limit(limit)
        .offset(row_offset)
    ).all()
    data = [
        {
            "source": row[0],
            "chunks": row[1],
            "first_at": row[2].strftime("%Y-%m-%d %H:%M") if row[2] else None,
            "last_at": row[3].strftime("%Y-%m-%d %H:%M") if row[3] else None,
            "document_types": sorted(t for t in (row[4] or []) if t),
            "embedding_models": sorted(m for m in (row[5] or []) if m),
            "preview": (row[6] or "").replace("\n", " ")[:100],
        }
        for row in rows
    ]
    types = sorted(t for t in db.scalars(
        select(func.distinct(DocumentChunk.document_type)).where(manageable_chunks(me))).all() if t)
    return {"data": data, "total": total, "offset": row_offset, "limit": limit,
            "matched_chunks": matched_chunks, "all_chunks": db.scalar(
                select(func.count()).select_from(DocumentChunk).where(manageable_chunks(me))) or 0,
            "document_types": types}


def _stored_paths(db: Session, source: str, *clauses) -> set[str]:
    """某个来源的片段引用到的上传件路径（`/rag/documents/upload` 写进 metadata 的那个）。"""
    rows = db.execute(
        select(DocumentChunk.metadata_json["stored_path"].astext)
        .where(DocumentChunk.source == source, *clauses)
    ).all()
    return {row[0] for row in rows if row[0]}


def _remove_stored_file(stored: str) -> bool:
    """删掉一个已经没人引用的上传件，返回是否真的删了。

    **只删知识库上传目录里的文件**：`stored_path` 是库里的值，如果它被改坏、或者指向
    `data/raw` 里那些按仓库约定只读的原始数据，拿它直接 unlink 就是在删别人的数据。
    所以先 resolve 再判断是否落在 `rag_upload_dir` 之内，越界的一律不碰（只记日志）。
    """
    upload_dir = Path(get_settings().rag_upload_dir).resolve()
    target = Path(stored)
    try:
        resolved = target.resolve()
    except OSError:  # pragma: no cover - 路径非法（超长、非法字符）
        logger.warning("知识库上传件路径无法解析，跳过：%s", stored)
        return False
    if not resolved.is_relative_to(upload_dir):
        logger.warning("知识库上传件不在上传目录内，拒绝删除：%s", stored)
        return False
    if not resolved.is_file():
        return False
    resolved.unlink()
    return True


@router.delete("/knowledge")
def delete_knowledge(
    request: Request,
    source: str = Query(..., min_length=1, max_length=500),
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """按来源删除当前管理员可维护的知识库片段，不影响其他部门同名来源。

    片段删掉后，那次上传落盘的文档如果**没有别的来源再引用**，就成了谁也指不到的孤儿文件
    （路径只存在于刚被删掉的行里），所以这里顺手清理：先记下要删的片段引用了哪些路径，
    删除后再按路径数一遍引用数，归零才 unlink。
    """
    condition = manageable_chunks(me)
    candidates = _stored_paths(db, source, condition)
    deleted = db.execute(
        delete(DocumentChunk).where(DocumentChunk.source == source, condition)
    ).rowcount or 0
    if not deleted:
        db.rollback()
        raise HTTPException(404, "没有找到该来源的知识库片段")
    removed_files, kept_files = [], []
    for stored in sorted(candidates):
        still_used = db.scalar(
            select(func.count()).select_from(DocumentChunk)
            .where(DocumentChunk.metadata_json["stored_path"].astext == stored)
        ) or 0
        if still_used:
            kept_files.append(stored)
        elif _remove_stored_file(stored):
            removed_files.append(stored)
    audit.record(db, me, "knowledge.delete", target_type="knowledge", target_id=source,
                 detail={"source": source, "deleted_chunks": deleted,
                         "removed_files": len(removed_files), "kept_files": len(kept_files)},
                 request=request)
    db.commit()
    return {"source": source, "deleted_chunks": deleted, "removed_files": len(removed_files)}


# ------------------------------------------------------------------ 预警规则

class AlertRuleUpdate(BaseModel):
    """改**默认规则**（全公司 · 所有期间）的阈值/级别/开关/说明。

    只给阈值/级别/开关/说明——规则形状本身（比较哪个指标、怎么比）不给改，
    否则前端要长成一个表达式编辑器，而平台只有「某指标超过/低于某阈值」这一种真实需求。
    要按部门或期间换阈值，请用 `POST /admin/alert-rules` 新建一条分档规则。
    """
    threshold_value: Decimal | None = Field(None, description="阈值，允许负数")
    level: str | None = Field(None, pattern="^(high|medium|low)$")
    enabled: bool | None = None
    description: str | None = Field(None, max_length=200)


class AlertRuleCreate(BaseModel):
    """新建一条**分档**规则：按部门、按期间，或两者同时限定。

    必须至少限定一个维度。全公司全期间的那条每个 `alert_type` 只该有一条（默认规则），
    再建一条 scope 全空的规则会和它抢同一个类型，谁生效只看 id 大小，用户没法解释。
    """
    alert_type: str = Field(..., min_length=1, max_length=100)
    department: str = Field("", max_length=100)
    period: str = Field("", pattern=r"^$|^\d{4}-(0[1-9]|1[0-2])$")
    metric_name: str = Field(..., min_length=1, max_length=100)
    comparator: str = Field("gt", pattern="^(gt|lt)$")
    threshold_value: Decimal = Field(..., description="阈值，允许负数")
    level: str = Field("high", pattern="^(high|medium|low)$")
    require_metric: str = Field("", max_length=100)
    description: str = Field("", max_length=200)
    enabled: bool = True


class AlertRuleScope(BaseModel):
    """按 id 改一条规则（含分档维度）。空串 = 清空该维度，`None` = 不改。"""
    department: str | None = Field(None, max_length=100)
    period: str | None = None
    metric_name: str | None = Field(None, min_length=1, max_length=100)
    comparator: str | None = Field(None, pattern="^(gt|lt)$")
    threshold_value: Decimal | None = None
    level: str | None = Field(None, pattern="^(high|medium|low)$")
    require_metric: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=200)
    enabled: bool | None = None


# ------------------------------------------------------------------ 预警外发通道

class AlertChannelCreate(BaseModel):
    """新建一条外发通道。

    `secret` 是 webhook 的 HMAC-SHA256 密钥：**只写不读**，任何响应都不会把它回显出来，
    只告诉调用方「配了没有」——密钥一旦能被读回来，界面上别人截一张图就等于泄露了。
    """
    name: str = Field(..., min_length=1, max_length=100)
    kind: str = Field("webhook", pattern="^(webhook|email)$")
    target: str = Field(..., min_length=1, max_length=500,
                        description="webhook=接收地址 URL；email=逗号分隔的收件人")
    min_level: str = Field("low", pattern="^(high|medium|low)$",
                           description="只外发该级别及以上的预警")
    department: str = Field("", max_length=100, description="空 = 不限部门（含全公司口径）")
    enabled: bool = True
    secret: str = Field("", max_length=200)


class AlertChannelUpdate(BaseModel):
    """改一条通道：`None` = 不改该项；`department` / `secret` 给空串 = 清空。

    这里不用「空串即不改」：密钥本来就是可有可无的，管理员需要一条明确的「把密钥删掉」的路径。
    """
    name: str | None = Field(None, min_length=1, max_length=100)
    kind: str | None = Field(None, pattern="^(webhook|email)$")
    target: str | None = Field(None, min_length=1, max_length=500)
    min_level: str | None = Field(None, pattern="^(high|medium|low)$")
    department: str | None = Field(None, max_length=100)
    enabled: bool | None = None
    secret: str | None = Field(None, max_length=200)


class AlertNotifyRequest(BaseModel):
    """补发：把「待处理」的预警按当前通道配置补成投递单。period 留空 = 所有期间。"""
    period: str = Field("", pattern=r"^$|^\d{4}-(0[1-9]|1[0-2])$")


def _channel_or_404(db: Session, channel_id: int) -> AlertChannel:
    channel = db.get(AlertChannel, channel_id)
    if channel is None:
        raise HTTPException(404, "没有这条外发通道")
    return channel


def _clean_kind(value: str) -> str:
    if value not in alerts_outbound.KINDS:
        raise HTTPException(422, f"通道类型只能是 {'、'.join(alerts_outbound.KINDS)}")
    return value


def _clean_channel_level(value: str) -> str:
    if value not in alerts_outbound.LEVEL_ORDER:
        raise HTTPException(422, "最低级别只能是 high、medium 或 low")
    return value


def _clean_target(kind: str, target: str) -> str:
    """按通道类型校验目标。webhook 在这里就把「地址不能用」挡下来，别等到发的时候才报错。"""
    text = (target or "").strip()
    if kind == "webhook":
        try:
            return alerts_outbound.check_webhook_target(text)
        except alerts_outbound.ConfigurationError as exc:
            raise HTTPException(422, str(exc)) from exc
    if not text:
        raise HTTPException(422, "邮件通道至少要有一个收件人")
    addresses = [item.strip() for item in text.split(",") if item.strip()]
    # 只做「像不像一个邮箱」的形状检查：真的能不能收，只有发一次才知道（见 test 端点）。
    bad = [item for item in addresses
           if item.count("@") != 1 or item.startswith("@") or item.endswith("@")
           or any(char.isspace() for char in item)]
    if bad:
        raise HTTPException(422, f"这些收件人看着不像邮箱地址：{'、'.join(bad[:3])}")
    return ", ".join(addresses)


def _clean_channel_department(db: Session, value: str | None) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    known, _periods = _scope_options(db)
    if text not in known:
        raise HTTPException(422, f"没有「{text}」这个部门（按部门限定要写数据里存在的部门名）")
    return text


def _channel_snapshot(channel: AlertChannel) -> dict:
    """审计快照。**不含 secret**，只记「配没配」——密钥不该落进审计表。"""
    return {"name": channel.name, "kind": channel.kind, "target": channel.target,
            "min_level": channel.min_level, "department": channel.department,
            "enabled": channel.enabled, "has_secret": bool(channel.secret)}


def _clean_text(value: str | None) -> str | None:
    """空串与纯空白都算「不限定」：页面上把分档维度清空再保存，就是回到全域。"""
    text = (value or "").strip()
    return text or None


def _metric_or_422(name: str, field: str) -> str:
    if name not in METRIC_LABELS:
        raise HTTPException(422, f"{field} 只能是 {'、'.join(METRIC_LABELS)} 之一")
    return name


def _threshold_or_422(value: Decimal) -> Decimal:
    if abs(value) >= Decimal("10000000000000000"):
        raise HTTPException(422, "阈值超出可表示范围")
    return value


def _rule_json(rule: AlertRule) -> dict:
    return {
        "id": rule.id,
        "alert_type": rule.alert_type,
        "department": rule.department or "",
        "period": rule.period or "",
        # 分档规则是「覆盖」：同一 alert_type 一次重算只取最具体的一条，界面要能一眼看出谁赢
        "scope_label": rule_scope_label(rule),
        "is_default": not rule.department and not rule.period,
        "metric_name": rule.metric_name,
        "metric_label": METRIC_LABELS.get(rule.metric_name, rule.metric_name),
        "comparator": rule.comparator,
        "comparator_label": COMPARATORS.get(rule.comparator, rule.comparator),
        "threshold_value": _threshold_text(rule),
        "level": rule.level,
        "require_metric": rule.require_metric or "",
        "description": rule.description or "",
        "enabled": bool(rule.enabled),
        "updated_at": rule.updated_at.strftime("%Y-%m-%d %H:%M") if rule.updated_at else "",
    }


def _threshold_text(rule) -> str:
    """阈值统一按数据库精度（4 位小数）输出。

    不这么做的话同一件事会有三种写法：库里 `0.8000`、请求体里 `-1`、审计里 `-1`，
    页面上再显示一次 `0.8000`，对不上时没法判断到底哪个是生效值。
    """
    value = rule.threshold_value if rule.threshold_value is not None else Decimal("0")
    return str(Decimal(value).quantize(Decimal("0.0001")))


def _rule_snapshot(rule) -> dict:
    """规则的可编辑字段快照，供审计记 before/after。"""
    return {"department": rule.department or "", "period": rule.period or "",
            "metric_name": rule.metric_name, "comparator": rule.comparator,
            "threshold_value": _threshold_text(rule), "level": rule.level,
            "require_metric": rule.require_metric or "",
            "enabled": bool(rule.enabled), "description": rule.description or ""}


def _scope_options(db: Session) -> tuple[list[str], list[str]]:
    """分档可选的部门与期间：都从库里已有数据扫出来，不让用户手打。

    手打的部门名一旦有空格或错字，规则会永远匹配不上，而页面上看不出任何异常。
    """
    departments = sorted({(value or "").strip() for value in db.scalars(
        select(FinancialMetric.department).distinct()).all()} - {""})
    periods = sorted({value for value in db.scalars(
        select(FinancialMetric.period).distinct()).all() if value}, reverse=True)
    return departments, periods[:36]


@router.get("/alert-rules")
def list_alert_rules(db: Session = Depends(get_db), _: User = Depends(admin_user)):
    """预警规则一览。`ensure_rules` 会顺手补齐默认两条，所以老库第一次访问就能看到内容。"""
    rules = ensure_rules(db)
    db.commit()
    departments, periods = _scope_options(db)
    return {"data": [_rule_json(rule) for rule in rules],
            "comparators": COMPARATORS, "metrics": METRIC_LABELS,
            "levels": LEVELS, "departments": departments, "periods": periods}


@router.post("/alert-rules", status_code=201)
def create_alert_rule(payload: AlertRuleCreate, request: Request,
                      db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """新建一条分档规则。重算时同一 `alert_type` 取最具体的一条（部门+期间 > 单维 > 默认）。"""
    department = _clean_text(payload.department)
    period = _clean_text(payload.period)
    if not department and not period:
        raise HTTPException(422, "分档规则至少要限定部门或期间之一；全公司全期间的阈值请改默认规则")
    if period and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise HTTPException(422, "期间要写成 YYYY-MM")
    metric_name = _metric_or_422(payload.metric_name, "metric_name")
    require_metric = _clean_text(payload.require_metric)
    if require_metric:
        _metric_or_422(require_metric, "require_metric")
    threshold = _threshold_or_422(payload.threshold_value)
    ensure_rules(db)
    duplicate = db.scalars(select(AlertRule).where(
        AlertRule.alert_type == payload.alert_type,
        AlertRule.department.is_(None) if department is None else AlertRule.department == department,
        AlertRule.period.is_(None) if period is None else AlertRule.period == period)).first()
    if duplicate is not None:
        raise HTTPException(409, f"已有同样范围的「{payload.alert_type}」规则（{rule_scope_label(duplicate)}），"
                                 "请直接改那一条")
    rule = AlertRule(alert_type=payload.alert_type, department=department, period=period,
                     metric_name=metric_name, comparator=payload.comparator,
                     threshold_value=threshold, level=payload.level,
                     require_metric=require_metric, description=payload.description,
                     enabled=payload.enabled)
    db.add(rule)
    db.flush()
    audit.record(db, me, "alert_rule.create", target_type="alert_rule", target_id=str(rule.id),
                 detail={"alert_type": rule.alert_type, "scope": rule_scope_label(rule),
                         "after": _rule_snapshot(rule)}, request=request)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


@router.put("/alert-rules/id/{rule_id}")
def update_alert_rule_by_id(rule_id: int, payload: AlertRuleScope, request: Request,
                            db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """按 id 改一条规则（默认规则也能改，只是不能删）。改完立即对下一次重算生效。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(404, "没有这条预警规则")
    before = _rule_snapshot(rule)
    department = _clean_text(payload.department) if payload.department is not None else rule.department
    period = _clean_text(payload.period) if payload.period is not None else rule.period
    if period and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", period):
        raise HTTPException(422, "期间要写成 YYYY-MM")
    if payload.metric_name is not None:
        rule.metric_name = _metric_or_422(payload.metric_name, "metric_name")
    if payload.comparator is not None:
        rule.comparator = payload.comparator
    if payload.threshold_value is not None:
        rule.threshold_value = _threshold_or_422(payload.threshold_value)
    if payload.level is not None:
        rule.level = payload.level
    if payload.require_metric is not None:
        require_metric = _clean_text(payload.require_metric)
        if require_metric:
            _metric_or_422(require_metric, "require_metric")
        rule.require_metric = require_metric
    if payload.description is not None:
        rule.description = payload.description
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    if not department and not period:
        raise HTTPException(422, "分档规则至少要限定部门或期间之一；要回到全公司口径请新建默认规则")
    clash = db.scalars(select(AlertRule).where(
        AlertRule.id != rule.id, AlertRule.alert_type == rule.alert_type,
        AlertRule.department.is_(None) if department is None else AlertRule.department == department,
        AlertRule.period.is_(None) if period is None else AlertRule.period == period)).first()
    if clash is not None:
        raise HTTPException(409, f"已有同样范围的「{rule.alert_type}」规则（{rule_scope_label(clash)}）")
    rule.department, rule.period = department, period
    after = _rule_snapshot(rule)
    audit.record(db, me, "alert_rule.update", target_type="alert_rule", target_id=str(rule.id),
                 detail={"alert_type": rule.alert_type, "scope": rule_scope_label(rule),
                         "before": before, "after": after}, request=request)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


@router.delete("/alert-rules/id/{rule_id}")
def delete_alert_rule(rule_id: int, request: Request,
                      db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """删一条分档规则。**默认规则只能停用不能删**：删了那个 alert_type 就彻底不报了，
    而「不报」和「停用」在页面上看起来一样、后果差很远（前者连恢复的入口都没有）。"""
    rule = db.get(AlertRule, rule_id)
    if rule is None:
        raise HTTPException(404, "没有这条预警规则")
    if not rule.department and not rule.period:
        raise HTTPException(400, "全公司口径的默认规则不能删除，请改用停用（enabled=false）")
    snapshot = _rule_snapshot(rule)
    scope = rule_scope_label(rule)
    alert_type = rule.alert_type
    audit.record(db, me, "alert_rule.delete", target_type="alert_rule", target_id=str(rule_id),
                 detail={"alert_type": alert_type, "scope": scope, "before": snapshot}, request=request)
    db.delete(rule)
    db.commit()
    return {"id": rule_id, "alert_type": alert_type, "scope": scope, "deleted": True}


@router.put("/alert-rules/{alert_type}")
def update_alert_rule(alert_type: str, payload: AlertRuleUpdate, request: Request,
                      db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """改**默认规则**（全公司 · 所有期间）的阈值/开关。改完**立即对下一次重算生效**（不缓存规则），
    已有预警不会被追溯改写——历史预警是当时口径下的判断，追改会让报告与当时的结论对不上。"""
    rules = [rule for rule in ensure_rules(db) if not rule.department and not rule.period]
    rule = next((item for item in rules if item.alert_type == alert_type), None)
    if rule is None:
        raise HTTPException(404, "没有这条预警规则")
    before = _rule_snapshot(rule)
    if payload.threshold_value is not None:
        rule.threshold_value = _threshold_or_422(payload.threshold_value)
    if payload.level is not None:
        rule.level = payload.level
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    if payload.description is not None:
        rule.description = payload.description
    after = _rule_snapshot(rule)
    # 阈值与开关直接决定「会不会报出来」，改动的旧值/新值都要留痕
    audit.record(db, me, "alert_rule.update", target_type="alert_rule", target_id=alert_type,
                 detail={"alert_type": alert_type, "scope": rule_scope_label(rule),
                         "before": before, "after": after}, request=request)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


# ------------------------------------------------------------------ 预警外发通道

@router.get("/alert-channels")
def list_alert_channels(db: Session = Depends(get_db), _: User = Depends(admin_user)):
    """外发通道一览 + 每条通道的投递台账计数（待发送/已送达/已失败）。"""
    channels = db.scalars(select(AlertChannel).order_by(AlertChannel.id)).all()
    counts = alerts_outbound.delivery_counts(db)
    return {"data": [alerts_outbound.channel_json(channel, counts.get(channel.id))
                     for channel in channels],
            "kinds": alerts_outbound.KINDS, "min_levels": alerts_outbound.MIN_LEVELS,
            "levels": LEVELS, "departments": _scope_options(db)[0],
            "stats": alerts_outbound.stats(db)}


@router.post("/alert-channels", status_code=201)
def create_alert_channel(payload: AlertChannelCreate, request: Request,
                         db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """新建一条外发通道。**新建不会自动补发旧预警**——补发要显式点 `POST /admin/alerts/notify`，
    否则「加了个通道」这一个动作会静默地把历史预警一股脑轰出去。"""
    name = (payload.name or "").strip()
    if not name:
        raise HTTPException(422, "通道名不能为空")
    duplicate = db.scalars(select(AlertChannel).where(AlertChannel.name == name)).first()
    if duplicate is not None:
        raise HTTPException(409, f"已经有一条叫「{name}」的通道了")
    kind = _clean_kind(payload.kind)
    channel = AlertChannel(name=name, kind=kind, target=_clean_target(kind, payload.target),
                           min_level=_clean_channel_level(payload.min_level),
                           department=_clean_channel_department(db, payload.department),
                           enabled=payload.enabled, secret=_clean_text(payload.secret))
    db.add(channel)
    db.flush()
    audit.record(db, me, "alert_channel.create", target_type="alert_channel",
                 target_id=str(channel.id), detail=_channel_snapshot(channel), request=request)
    db.commit()
    db.refresh(channel)
    return alerts_outbound.channel_json(channel,
                                        alerts_outbound.delivery_counts(db).get(channel.id))


@router.put("/alert-channels/{channel_id}")
def update_alert_channel(channel_id: int, payload: AlertChannelUpdate, request: Request,
                         db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """改一条通道。改完**立即对下一次外发生效**（发送时现读通道，不缓存）。

    改了 `kind` 会按新类型重校 `target`：webhook 的地址和邮件地址不是一回事，
    留着旧值只会在第一次投递时才炸。
    """
    channel = _channel_or_404(db, channel_id)
    before = _channel_snapshot(channel)
    if payload.name is not None:
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, "通道名不能为空")
        clash = db.scalars(select(AlertChannel).where(
            AlertChannel.id != channel.id, AlertChannel.name == name)).first()
        if clash is not None:
            raise HTTPException(409, f"已经有一条叫「{name}」的通道了")
        channel.name = name
    kind = _clean_kind(payload.kind) if payload.kind is not None else channel.kind
    target = payload.target if payload.target is not None else channel.target
    channel.target = _clean_target(kind, target)
    channel.kind = kind
    if payload.min_level is not None:
        channel.min_level = _clean_channel_level(payload.min_level)
    if payload.department is not None:
        channel.department = _clean_channel_department(db, payload.department)
    if payload.enabled is not None:
        channel.enabled = payload.enabled
    if payload.secret is not None:
        # 空串 = 把密钥删掉（webhook 退回不带签名的裸 POST）。
        channel.secret = _clean_text(payload.secret)
    after = _channel_snapshot(channel)
    audit.record(db, me, "alert_channel.update", target_type="alert_channel",
                 target_id=str(channel.id), detail={"before": before, "after": after},
                 request=request)
    db.commit()
    db.refresh(channel)
    return alerts_outbound.channel_json(channel,
                                        alerts_outbound.delivery_counts(db).get(channel.id))


@router.delete("/alert-channels/{channel_id}")
def delete_alert_channel(channel_id: int, request: Request,
                         db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """删一条通道。**已有的投递台账不删**（历史记录留痕），只是 `channel_id` 变成空——
    所以台账里另外存了通道名快照，删完之后那些记录仍然能看出「当年发给了谁」。"""
    channel = _channel_or_404(db, channel_id)
    snapshot = _channel_snapshot(channel)
    audit.record(db, me, "alert_channel.delete", target_type="alert_channel",
                 target_id=str(channel_id), detail=snapshot, request=request)
    db.delete(channel)
    db.commit()
    return {"id": channel_id, "name": snapshot["name"], "deleted": True}


@router.post("/alert-channels/{channel_id}/test")
def test_alert_channel(channel_id: int, request: Request,
                       db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """同步发一条**测试通知**（不进投递台账：它不是某条预警的投递，混进台账会让
    「待发送 N 条」永远对不上真实预警数）。配置错在这里就能看见原因。"""
    channel = _channel_or_404(db, channel_id)
    result = alerts_outbound.test_channel(db, channel)
    audit.record(db, me, "alert_channel.test", target_type="alert_channel",
                 target_id=str(channel.id),
                 detail={"name": channel.name, "kind": channel.kind, "ok": result["ok"],
                         "status": result["status"], "detail": result["detail"][:200]},
                 request=request)
    db.commit()
    db.refresh(channel)
    return {**result, "channel": alerts_outbound.channel_json(
        channel, alerts_outbound.delivery_counts(db).get(channel.id))}


@router.get("/alert-deliveries")
def list_alert_deliveries(status: str | None = Query(None), channel_id: int | None = Query(None),
                          period: str | None = Query(None), level: str | None = Query(None),
                          limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
                          db: Session = Depends(get_db), _: User = Depends(admin_user)):
    """投递台账：每一条「某条预警 × 某个通道」的发送记录与它的失败原因。"""
    if status is not None and status not in alerts_outbound.STATUSES:
        raise HTTPException(422, f"状态只能是 {'、'.join(alerts_outbound.STATUSES)}")
    if level is not None and level not in alerts_outbound.LEVEL_ORDER:
        raise HTTPException(422, "级别只能是 high、medium 或 low")
    rows, total = alerts_outbound.query(db, status=status, channel_id=channel_id, period=period,
                                        level=level, limit=limit, offset=offset)
    return {"data": [alerts_outbound.serialize(row) for row in rows], "total": total,
            "limit": limit, "offset": offset, "status_labels": alerts_outbound.STATUS_LABELS,
            "levels": LEVELS, "stats": alerts_outbound.stats(db)}


@router.post("/alert-deliveries/{delivery_id}/retry")
def retry_alert_delivery(delivery_id: int, request: Request,
                         db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """人工重试一条失败的投递。会把 `attempts` 归零再排队：
    投递单的重试次数只用来决定「这一轮还要不要再试」，管理员判断通道已经修好了，
    就不该被上一次失败留下的次数挡住（与任务表保留尝试编号的口径不同）。"""
    delivery = db.get(AlertDelivery, delivery_id)
    if delivery is None:
        raise HTTPException(404, "没有这条投递记录")
    alerts_outbound.retry_delivery(db, delivery)
    outbound = alerts_outbound.schedule(db, actor=me.username)
    audit.record(db, me, "alert_delivery.retry", target_type="alert_delivery",
                 target_id=str(delivery.id),
                 detail={"channel": delivery.channel_name, "period": delivery.period,
                         "alert_type": delivery.alert_type, "level": delivery.level,
                         "job": (outbound["job"] or {}).get("id")},
                 request=request)
    db.commit()
    db.refresh(delivery)
    return {"data": alerts_outbound.serialize(delivery), "job": outbound["job"],
            "queued": outbound["deliveries"], "deduplicated": outbound["deduplicated"]}


@router.post("/alerts/notify")
def notify_alerts(payload: AlertNotifyRequest, request: Request,
                  db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """补发：把**待处理**的预警按当前通道配置补成投递单，并把所有待发送的投递排进队列。

    新建/修好通道之后点这一下。可以反复点：已存在的「预警 × 通道」组合不会再建一条
    （去重键是内容而不是行 id，见 alerts_outbound.alert_key），排队中的也不会重复排
    （`jobs` 按 payload 去重）。
    """
    period = _clean_text(payload.period)
    created = alerts_outbound.plan(db, period=period)
    # 先把投递单落库再排任务：后台任务读的是数据库，而不是这个还没提交的事务。
    db.commit()
    outbound = alerts_outbound.schedule(db, actor=me.username)
    audit.record(db, me, "alert.notify", target_type="alert", target_id=period or "all",
                 detail={"period": period or "all", "created": created,
                         "queued": outbound["deliveries"],
                         "job": (outbound["job"] or {}).get("id")},
                 request=request)
    db.commit()
    return {"period": period, "created": created, "queued": outbound["deliveries"],
            "job": outbound["job"], "deduplicated": outbound["deduplicated"]}


# ------------------------------------------------------------------ 审计日志

# 导出一次最多给这么多行：审计是留档用的，几万行的 CSV 在浏览器里拼字符串一样会卡死，
# 需要更大范围就按时间/动作分段导出（分页与筛选都在导出前生效）。
AUDIT_EXPORT_LIMIT = 5000


class AuditPurgeRequest(BaseModel):
    """按保留期清理审计。`dry_run` 默认 True：先看会删多少，再决定删不删。"""

    keep_days: int | None = Field(default=None, ge=1, le=3650)
    dry_run: bool = True


def _audit_window(since: date | None, until: date | None) -> tuple[datetime | None, datetime | None]:
    """把页面上的日期（含首含尾）转成时间窗：`since` 当天 00:00 起、`until` 次日 00:00 止。

    页面上的日期是**业务时区**的日历日（⑯ `backend/biztime.py`），库里存的是带时区的 UTC 时刻。
    这里原来用 `datetime.combine(...).astimezone()`——那读的是「服务器本机时区」，于是同一份审计
    在 UTC 容器与 UTC+8 的机器上查出来的范围不一样（UTC+8 的机器上表现为「今天早上 8 点前的记录
    查不到」）。现在按业务时区取边界，口径与页面、报告、用量窗口一致。
    """
    start = biztime.day_bounds(since)[0] if since else None
    end = biztime.day_bounds(until + timedelta(days=1))[0] if until else None
    return start, end


def _audit_filters(action: str | None, actor: str | None, target_type: str | None,
                   ip: str | None, since: date | None, until: date | None) -> dict:
    start, end = _audit_window(since, until)
    return {"action": action, "actor": actor, "target_type": target_type, "ip": ip,
            "since": start, "until": end}


@router.get("/audit")
def list_audit(
    action: str | None = Query(None, max_length=60),
    actor: str | None = Query(None, max_length=100),
    target_type: str | None = Query(None, max_length=40),
    ip: str | None = Query(None, max_length=60),
    since: date | None = Query(None),
    until: date | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """审计日志：谁在什么时候改了什么。

    **这个接口只读**：没有 PUT/DELETE，唯一的删除路径是 `POST /admin/audit/purge`（按保留期清理，
    而且那次清理自己也会留一条 `audit.purge`）——能被管理员随手清空的审计等于没有审计。
    归档走 `GET /admin/audit/export`（CSV），筛选与分页在导出前同样生效。
    """
    rows, total = audit.query(db, limit=limit, offset=offset,
                              **_audit_filters(action, actor, target_type, ip, since, until))
    return {
        "data": [audit.serialize(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": audit.action_options(),
        "actors": audit.known_actors(db),
        "ips": audit.known_ips(db),
        "retention_days": get_settings().audit_retention_days,
        "export_limit": AUDIT_EXPORT_LIMIT,
    }


@router.get("/audit/export")
def export_audit(
    request: Request,
    action: str | None = Query(None, max_length=60),
    actor: str | None = Query(None, max_length=100),
    target_type: str | None = Query(None, max_length=40),
    ip: str | None = Query(None, max_length=60),
    since: date | None = Query(None),
    until: date | None = Query(None),
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """把（筛选后的）审计导出成 CSV。

    两个细节是真踩过的：正文开头写 UTF-8 BOM，否则 Excel 打开中文列名是乱码；
    导出这件事本身也要留痕（`audit.export`，detail 里记条数与筛选条件），
    「谁把审计捞走了」在合规口径下和「谁改了数据」一样重要。
    """
    filters = _audit_filters(action, actor, target_type, ip, since, until)
    rows, total = audit.query(db, limit=AUDIT_EXPORT_LIMIT, offset=0, **filters)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(audit.CSV_HEADER)
    writer.writerows(audit.csv_record(row) for row in rows)
    audit.record(db, me, "audit.export", target_type="audit",
                 detail={"rows": len(rows), "total": total, "truncated": total > len(rows),
                         "action": action or "", "actor": actor or "", "target_type": target_type or "",
                         "ip": ip or "", "since": str(since or ""), "until": str(until or "")},
                 request=request)
    db.commit()
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"audit-{stamp}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Export-Rows": str(len(rows)),
                 "X-Export-Truncated": "true" if total > len(rows) else "false"},
    )


@router.post("/audit/purge")
def purge_audit(
    payload: AuditPurgeRequest,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    """按保留期清理审计日志（默认 `dry_run=True`，只报数不删）。

    刻意不做成「清空审计」：只能按天数往前删，且至少保留 1 天，删完自己写一条 `audit.purge`
    记录（含截止时间与删除条数）——清理这个动作本身必须可查，否则它就是审计的漏洞。
    """
    days = payload.keep_days or get_settings().audit_retention_days
    cutoff = audit.retention_cutoff(days)
    deleted = audit.purge(db, cutoff=cutoff, dry_run=payload.dry_run)
    if not payload.dry_run:
        audit.record(db, me, "audit.purge", target_type="audit",
                     detail={"keep_days": days, "before": cutoff.isoformat(), "deleted": deleted},
                     request=request)
    db.commit()
    remaining, total = audit.query(db, limit=1, offset=0)
    return {"dry_run": payload.dry_run, "keep_days": days, "before": cutoff.isoformat(),
            "deleted": deleted, "total": total, "sample_id": remaining[0].id if remaining else None}
