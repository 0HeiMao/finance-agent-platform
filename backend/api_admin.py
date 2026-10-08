"""管理员接口：用户、导入批次、知识库三块的查看与维护。

权限模型沿用项目既有语义：**`User.department` 为空 = 不受部门限制 = 财务管理员**
（与 `GET /auth/me` 的 `unrestricted`、README 的口径一致），因此不额外引入角色字段，
也就不需要迁移。所有接口都经过 `admin_user` 依赖，普通账号拿到 403 而不是空数据，
避免"看不到就等于没有"的误判。

两条刻意的安全约束：
- 不允许管理员改自己的部门/删自己的账号。改部门会立刻把自己锁在管理入口之外，属于自伤操作。
- 删除导入批次时会**重算受影响期间**，否则会留下与交易明细对不上的历史指标（幽灵报表）。
  长任务版本走 `POST /admin/jobs/batch-revert`（后台执行 + 任务可查），同步版本保留给脚本调用。
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend import audit, batch_ops, jobs
from backend.alert_rules import COMPARATORS, ensure_rules
from backend.api_analysis import calculate
from backend.auth import current_user
from backend.config import get_settings
from backend.db import get_db
from backend.models import AlertRule, BackgroundJob, FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.observability import snapshot
from backend.rag.pgvector_store import DocumentChunk

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


def is_admin(user: User) -> bool:
    return not (user.department or "").strip()


def admin_user(user: User = Depends(current_user)) -> User:
    if not is_admin(user):
        raise HTTPException(403, "需要管理员权限：当前账号已限定部门")
    return user


class DepartmentUpdate(BaseModel):
    # 空字符串等同于"清空部门"（升级为管理员），所以不做 min_length
    department: str = Field(default="", max_length=100)


class BatchRevertRequest(BaseModel):
    batch: str = Field(min_length=1, max_length=200)


def _count(db: Session, model, *criteria) -> int:
    statement = select(func.count()).select_from(model)
    if criteria:
        statement = statement.where(*criteria)
    return db.scalar(statement) or 0


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
        "observability": snapshot(),
        "me": {"id": me.id, "username": me.username, "department": me.department or ""},
    }


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
        condition = condition.where(User.department == (department or None))
    if unrestricted is not None:
        # 「不受部门限制」= department 为空，与 is_admin() 同一口径；
        # 用或上空白字符串是为了兼容历史上写成空串的行。
        blank = User.department.is_(None) | (func.trim(User.department) == "")
        condition = condition.where(blank if unrestricted else ~blank)
    total = db.scalar(select(func.count()).select_from(condition.subquery())) or 0
    users = db.scalars(
        condition.order_by(User.id).limit(limit).offset(row_offset)).all()
    # 筛选下拉的选项由后端给：前端自己拿一页数据去凑选项，翻页后就自相矛盾了
    blank = User.department.is_(None) | (func.trim(User.department) == "")
    departments = [row[0] for row in db.execute(
        select(func.distinct(User.department))
        .where(~blank)
        .order_by(User.department)).all() if row[0]]
    return {
        "data": [
            {
                "id": user.id,
                "username": user.username,
                "department": user.department or "",
                "unrestricted": is_admin(user),
                "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else None,
                "is_self": user.id == me.id,
            }
            for user in users
        ],
        "total": total,
        "offset": row_offset,
        "limit": limit,
        "departments": departments,
        "unrestricted_count": db.scalar(
            select(func.count()).select_from(User).where(blank)) or 0,
        "me": {"id": me.id, "username": me.username},
    }


def _target_user(db: Session, user_id: int, me: User) -> User:
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if target.id == me.id:
        raise HTTPException(400, "不能通过管理接口修改自己：请到 System 设置里改「我的部门」")
    return target


@router.patch("/users/{user_id}")
def update_user(
    user_id: int,
    payload: DepartmentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    me: User = Depends(admin_user),
):
    target = _target_user(db, user_id, me)
    previous = target.department
    target.department = payload.department.strip() or None
    audit.record(db, me, "user.department_change", target_type="user", target_id=target.id,
                 detail={"username": target.username, "from": previous or "不受限",
                         "to": target.department or "不受限"}, request=request)
    db.commit()
    db.refresh(target)
    return {
        "id": target.id,
        "username": target.username,
        "department": target.department or "",
        "unrestricted": is_admin(target),
    }


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, db: Session = Depends(get_db), me: User = Depends(admin_user)):
    target = _target_user(db, user_id, me)
    username = target.username
    department = target.department
    # 先记录再删：删掉以后 target.id 还在这个 Python 对象里，但写进 detail 的必须是快照
    audit.record(db, me, "user.delete", target_type="user", target_id=target.id,
                 detail={"username": username, "department": department or "不受限"}, request=request)
    db.delete(target)
    db.commit()
    return {"deleted": True, "username": username}


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

    unrestricted = []
    for user in users:
        payload = {
            "id": user.id, "username": user.username, "department": user.department or "",
            "unrestricted": is_admin(user), "is_self": user.id == me.id,
        }
        if payload["unrestricted"]:
            unrestricted.append(payload)
        else:
            slot(user.department)["users"].append(payload)

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
        "unrestricted_users": unrestricted,
        "summary": {
            "departments_with_data": sum(1 for item in rows if item["transactions"]),
            "departments_with_users": sum(1 for item in rows if item["user_count"]),
            "unrestricted_users": len(unrestricted),
            "total_users": len(users),
        },
        "me": {"id": me.id, "username": me.username, "department": me.department or ""},
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
    """
    if not batch_ops.batch_rows(db, payload.batch):
        raise HTTPException(404, "批次不存在或已被撤销")
    job = jobs.submit(db, "batch.revert", {"batch": payload.batch, "ip": audit.client_ip(request)},
                      actor=me.username)
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


@router.get("/knowledge")
def list_knowledge(
    source: str | None = Query(None, max_length=500),
    document_type: str | None = Query(None, max_length=100),
    limit: int = Query(50, ge=1, le=500),
    row_offset: int = Query(0, ge=0, alias="offset"),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """知识库来源聚合（分页 + 筛选）。

    一行 = 一个来源（同一份文件切出的多个片段合成一行），所以 `total` 是**满足筛选的来源个数**，
    `matched_chunks` 是这些来源的片段数；再给一个全局 `all_chunks`，让页面能显示「命中 3 个来源 / 共 5 个片段」。
    片段级过滤（来源名、文档类型）必须在聚合前做 where，否则分页会把别人的片段算进命中数。
    """
    clauses = []
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
        select(func.distinct(DocumentChunk.document_type))).all() if t)
    return {"data": data, "total": total, "offset": row_offset, "limit": limit,
            "matched_chunks": matched_chunks, "all_chunks": _count(db, DocumentChunk),
            "document_types": types}


def _stored_paths(db: Session, source: str) -> set[str]:
    """某个来源的片段引用到的上传件路径（`/rag/documents/upload` 写进 metadata 的那个）。"""
    rows = db.execute(
        select(DocumentChunk.metadata_json["stored_path"].astext)
        .where(DocumentChunk.source == source)
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
    """按来源整份删除知识库片段（管理员操作，不受部门限制）。

    片段删掉后，那次上传落盘的文档如果**没有别的来源再引用**，就成了谁也指不到的孤儿文件
    （路径只存在于刚被删掉的行里），所以这里顺手清理：先记下要删的片段引用了哪些路径，
    删除后再按路径数一遍引用数，归零才 unlink。
    """
    candidates = _stored_paths(db, source)
    deleted = db.execute(
        delete(DocumentChunk).where(DocumentChunk.source == source)
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
    """改一条预警规则。只给阈值/级别/开关/说明——规则形状本身（比较哪个指标、怎么比）不给改，
    否则前端要长成一个表达式编辑器，而平台只有「某指标超过/低于某阈值」这一种真实需求。"""
    threshold_value: Decimal | None = Field(None, description="阈值，允许负数")
    level: str | None = Field(None, pattern="^(high|medium|low)$")
    enabled: bool | None = None
    description: str | None = Field(None, max_length=200)


def _rule_json(rule: AlertRule) -> dict:
    return {
        "alert_type": rule.alert_type,
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
    return {"threshold_value": _threshold_text(rule), "level": rule.level,
            "enabled": bool(rule.enabled), "description": rule.description or ""}


@router.get("/alert-rules")
def list_alert_rules(db: Session = Depends(get_db), _: User = Depends(admin_user)):
    """预警规则一览。`ensure_rules` 会顺手补齐默认两条，所以老库第一次访问就能看到内容。"""
    rules = ensure_rules(db)
    db.commit()
    return {"data": [_rule_json(rule) for rule in rules],
            "comparators": COMPARATORS, "metrics": METRIC_LABELS}


@router.put("/alert-rules/{alert_type}")
def update_alert_rule(alert_type: str, payload: AlertRuleUpdate, request: Request,
                      db: Session = Depends(get_db), me: User = Depends(admin_user)):
    """改阈值/开关。改完**立即对下一次重算生效**（不缓存规则），已有预警不会被追溯改写——
    历史预警是当时口径下的判断，追改会让报告与当时的结论对不上。"""
    rules = {rule.alert_type: rule for rule in ensure_rules(db)}
    rule = rules.get(alert_type)
    if rule is None:
        raise HTTPException(404, "没有这条预警规则")
    before = _rule_snapshot(rule)
    if payload.threshold_value is not None:
        if abs(payload.threshold_value) >= Decimal("10000000000000000"):
            raise HTTPException(422, "阈值超出可表示范围")
        rule.threshold_value = payload.threshold_value
    if payload.level is not None:
        rule.level = payload.level
    if payload.enabled is not None:
        rule.enabled = payload.enabled
    if payload.description is not None:
        rule.description = payload.description
    after = _rule_snapshot(rule)
    # 阈值与开关直接决定「会不会报出来」，改动的旧值/新值都要留痕
    audit.record(db, me, "alert_rule.update", target_type="alert_rule", target_id=alert_type,
                 detail={"alert_type": alert_type, "before": before, "after": after}, request=request)
    db.commit()
    db.refresh(rule)
    return _rule_json(rule)


# ------------------------------------------------------------------ 审计日志

@router.get("/audit")
def list_audit(
    action: str | None = Query(None, max_length=60),
    actor: str | None = Query(None, max_length=100),
    target_type: str | None = Query(None, max_length=40),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(admin_user),
):
    """审计日志：谁在什么时候改了什么。

    只读接口，不提供删除与导出——审计日志能被管理员随手清掉就失去意义了（要归档请直接读库）。
    """
    rows, total = audit.query(db, action=action, actor=actor, target_type=target_type,
                              limit=limit, offset=offset)
    return {
        "data": [audit.serialize(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
        "actions": audit.action_options(),
        "actors": audit.known_actors(db),
    }
