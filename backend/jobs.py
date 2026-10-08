"""进程内后台任务：提交、执行、查询。

取代了 `celery_app.py` / `tasks.py` 那套**从未跑起来**的 Celery 脚手架（这个部署没有 Redis，
broker 和 worker 都不存在，任务提交不出去、也没人调用 `run_monthly_analysis`）。
现在只有一条真需要异步的路径——「撤销批次 + 重算所有受影响期间」，实现方式是：

1. `submit()` 先往 `background_jobs` 写一条 `queued` 记录并提交（任务提交本身是快操作，
   接口可以立刻返回 202 + 任务 id，不必等重算完）；
2. 由 FastAPI 的 `BackgroundTasks` 在响应发出后调用 `run_job()`（同步函数会进线程池，不阻塞事件循环），
   `run_job()` 自己开 Session：置 `running` → 调用处理函数 → 置 `succeeded` 并写 result，
   出异常则回滚后置 `failed` 并把异常文本截断存进 error；
3. 前端轮询 `GET /admin/jobs/{id}` 拿状态，失败时能看到原因而不是一个 500。

诚实的边界（README 也写了）：**单进程内存队列**，没有分布式锁，也没有重试。
进程重启时未完成的任务会被 `reap_stale_jobs()` 标记成失败——不这么做，任务会永远停在
`running`，界面上的进度条会一直转。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from backend import batch_ops
from backend.db import SessionLocal
from backend.models import BackgroundJob

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = ("queued", "running")
# 任务处理函数：`(db, payload, context) -> result`
Handler = Callable[[Session, dict, dict], dict]
HANDLERS: dict[str, Handler] = {}
# 前端展示用：kind → 中文名
KIND_LABELS = {
    "batch.revert": "撤销导入批次",
}


def handler(kind: str) -> Callable[[Handler], Handler]:
    """注册处理函数。unknown kind 在 `run_job` 里被标记为失败，而不是静默什么都不做。"""

    def decorate(function: Handler) -> Handler:
        HANDLERS[kind] = function
        return function

    return decorate


@handler("batch.revert")
def _revert_batch(db: Session, payload: dict, context: dict) -> dict:
    return batch_ops.revert_batch(
        db,
        payload["batch"],
        actor=context.get("actor"),
        ip=payload.get("ip"),
        job_id=context.get("job_id"),
    )


def serialize(job: BackgroundJob) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "kind_label": KIND_LABELS.get(job.kind, job.kind),
        "status": job.status,
        "actor": job.actor,
        "payload": job.payload or {},
        "result": job.result,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def submit(db: Session, kind: str, payload: dict, *, actor: str | None = None) -> BackgroundJob:
    """写一条待执行的任务记录。**不在这里执行**：执行由调用方交给 BackgroundTasks。"""
    if kind not in HANDLERS:
        # 提交侧就拦住：一个没有处理函数的任务排进去只会变成一条没人能修的 failed 记录
        raise ValueError(f"没有注册的后台任务类型：{kind}")
    job = BackgroundJob(kind=kind, status="queued", actor=actor or "", payload=payload or {})
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def run_job(job_id: int, db: Session | None = None) -> None:
    """执行一条任务。异常不外抛，失败落在任务记录里。

    `db` 只在测试或内嵌调用时传（复用调用方的事务）；生产路径由 `BackgroundTasks` 调用，
    不传 Session——响应发完之后请求级 Session 已经关了，任务必须自己开一个。
    """
    own_session = db is None
    session = SessionLocal() if own_session else db
    try:
        job = session.get(BackgroundJob, job_id)
        if job is None or job.status not in ACTIVE_STATUSES:
            return
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        session.commit()

        function = HANDLERS.get(job.kind)
        if function is None:
            raise RuntimeError(f"没有注册的处理函数：{job.kind}")
        result = function(session, dict(job.payload or {}), {"actor": job.actor, "job_id": job.id})

        job = session.get(BackgroundJob, job_id)
        job.status = "succeeded"
        job.result = result or {}
        job.error = None
        job.finished_at = datetime.now(timezone.utc)
        session.commit()
    except Exception as exc:  # noqa: BLE001 —— 任务失败要落库，不能让后台线程静默炸掉
        logger.exception("后台任务 %s 失败", job_id)
        session.rollback()
        job = session.get(BackgroundJob, job_id)
        if job is not None:
            job.status = "failed"
            # 只留前 500 字：异常文本可能很长，而这张表是给人看的
            job.error = str(exc)[:500]
            job.finished_at = datetime.now(timezone.utc)
            session.commit()
    finally:
        if own_session:
            session.close()


def reap_stale_jobs(db: Session) -> int:
    """把上次进程留下的 `queued`/`running` 任务标记为失败。启动时调用一次，返回处理条数。"""
    result = db.execute(
        update(BackgroundJob)
        .where(BackgroundJob.status.in_(ACTIVE_STATUSES))
        .values(status="failed", error="进程重启，任务未完成", finished_at=datetime.now(timezone.utc))
    )
    db.commit()
    return result.rowcount or 0


def query(db: Session, *, status: str | None = None, kind: str | None = None,
          limit: int = 50, offset: int = 0) -> tuple[list[BackgroundJob], int]:
    condition = select(BackgroundJob)
    counter = select(func.count()).select_from(BackgroundJob)
    if status:
        condition = condition.where(BackgroundJob.status == status)
        counter = counter.where(BackgroundJob.status == status)
    if kind:
        condition = condition.where(BackgroundJob.kind == kind)
        counter = counter.where(BackgroundJob.kind == kind)
    rows = db.scalars(
        condition.order_by(BackgroundJob.id.desc()).limit(limit).offset(offset)
    ).all()
    return list(rows), db.scalar(counter) or 0


STATUSES = ("queued", "running", "succeeded", "failed")
STATUS_LABELS = {"queued": "排队中", "running": "运行中", "succeeded": "已完成", "failed": "失败"}


def status_options() -> list[dict]:
    return [{"value": value, "label": STATUS_LABELS[value]} for value in STATUSES]
