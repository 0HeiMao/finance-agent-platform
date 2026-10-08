"""进程内后台任务：提交（幂等）、执行（可重试）、查询、进程重启后接续。

取代了 `celery_app.py` / `tasks.py` 那套**从未跑起来**的 Celery 脚手架（这个部署没有 Redis，
broker 和 worker 都不存在，任务提交不出去、也没人调用 `run_monthly_analysis`）。
现在只有一条真需要异步的路径——「撤销批次 + 重算所有受影响期间」，实现方式是：

1. `submit()` 先往 `background_jobs` 写一条 `queued` 记录并提交（任务提交本身是快操作，
   接口可以立刻返回 202 + 任务 id，不必等重算完）；
2. 由 FastAPI 的 `BackgroundTasks` 在响应发出后调用 `run_job()`（同步函数会进线程池，不阻塞事件循环），
   `run_job()` 自己开 Session：置 `running` → 调用处理函数 → 置 `succeeded` 并写 result，
   出异常则回滚后置 `failed`（或按重试预算放回 `queued`）并把异常文本截断存进 error；
3. 前端轮询 `GET /admin/jobs/{id}` 拿状态，失败时能看到原因而不是一个 500。

⑩ 补齐的两件以前缺的事：

* **幂等提交**：调用方给一个 `idempotency_key`（HTTP 头 `Idempotency-Key`，或者同一 `kind` + 同一份
  稳定 payload 的进行中任务），`submit()` 会把已有的那条读回来而不是再排一条 —— 用户双击「撤销」
  不会把同一批重算两遍。数据库上还有一条部分唯一索引兜住「同时查空、各自插一条」的竞态。
* **失败重试**：处理函数抛异常不再等于直接判死。`attempts < max_attempts` 就指数退避放回队列
  （`next_attempt_at` 记着下一次时间），用完预算才落 `failed`；人工重试走
  `POST /admin/jobs/{id}/retry`，它会给这条任务追加一份预算（编号继续涨，所以重跑仍然算「重跑」）。

   因为会重跑，**处理函数必须幂等**：同一份 payload 跑两遍不能多删一行，也不能把
   「上次已经做完了」当失败（`batch_ops.revert_batch(..., allow_missing=...)` 就是为这个加的）。

诚实的边界（README 也写了）：**单进程内存队列**，定时重试用的是进程内 `threading.Timer`，
没有分布式锁、也没有跨进程的重试队列；进程重启时未完成的任务由 `resume_interrupted_jobs()`
重新排队（处理函数幂等，所以重跑是安全的），已经用完预算的才标记失败——不这么做，任务会永远停在
`running`，界面上的进度条会一直转。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
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

# 重试预算与退避：第 1 次重试等 2 秒、第 2 次 4 秒……封顶 60 秒。
# 为什么要退避：失败多半是因为数据库忙/锁冲突，立刻重试只是再撞一次。
DEFAULT_MAX_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 60.0
# 去重时不该参与比对的 payload 字段：来源 IP 是每次请求都不同的环境信息，
# 同一个人双击「撤销」两次，payload 只差这个字段，拿它当指纹等于没有去重。
VOLATILE_PAYLOAD_KEYS = frozenset({"ip"})
# 去重时「这条任务还占着这个键」的状态：失败的不算（失败之后要允许重新提交同一个键）。
KEY_HOLDING_STATUSES = ACTIVE_STATUSES + ("succeeded",)
IDEMPOTENCY_KEY_MAX = 200


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
        # 重跑时「一行都没删」是正常结果：上一次尝试已经把行删掉了。第一次尝试则相反，
        # 空批次说明调用方点错了（或者有人抢先撤销了），必须报错。
        allow_missing=bool(context.get("is_retry")),
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
        "idempotency_key": job.idempotency_key,
        "attempts": attempts_of(job),
        "max_attempts": max_attempts_of(job),
        "next_attempt_at": job.next_attempt_at.isoformat() if job.next_attempt_at else None,
    }


def attempts_of(job: BackgroundJob) -> int:
    """已经试过几次。老记录可能是 NULL（加列之前建的），按 0 读。"""
    return int(job.attempts or 0)


def max_attempts_of(job: BackgroundJob) -> int:
    value = int(job.max_attempts or 0)
    return value if value > 0 else DEFAULT_MAX_ATTEMPTS


def can_retry(job: BackgroundJob) -> bool:
    """这条任务还有重试预算吗？（已经试过几次 < 预算）"""
    return attempts_of(job) < max_attempts_of(job)


def retry_delay(attempts: int) -> float:
    """第 `attempts` 次尝试失败后等多久再来（指数退避，封顶 60 秒）。"""
    steps = max(1, attempts) - 1
    return min(RETRY_BASE_SECONDS * (2**steps), RETRY_MAX_SECONDS)


def stable_payload(kind: str, payload: dict | None) -> tuple:
    """去重指纹：kind + 稳定的 payload 字段（忽略 `ip` 这类每次请求都不同的值）。"""
    items = tuple(
        sorted((key, str(value)) for key, value in (payload or {}).items()
               if key not in VOLATILE_PAYLOAD_KEYS)
    )
    return (kind, items)


def find_key_holder(db: Session, key: str) -> BackgroundJob | None:
    """按键找一条还占着这个键的任务（排队中/运行中/已成功）。"""
    if not key:
        return None
    return db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.idempotency_key == key, BackgroundJob.status.in_(KEY_HOLDING_STATUSES))
        .order_by(BackgroundJob.id.desc())
    ).first()


def find_active_duplicate(db: Session, kind: str, payload: dict | None) -> BackgroundJob | None:
    """找一条**正在处理同一件事**的任务（同 kind、同稳定 payload、还挂在排队中/运行中）。

    它管的是「用户在界面上连点两下」：第二次点击不该再排一条。已成功/已失败的不算重复 ——
    那两种情况下再提交一次是有意义的新请求（比如同一份文件重新导入后再撤销一次）。
    """
    fingerprint = stable_payload(kind, payload)
    rows = db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.kind == kind, BackgroundJob.status.in_(ACTIVE_STATUSES))
        .order_by(BackgroundJob.id.desc())
    ).all()
    return next((row for row in rows if stable_payload(kind, row.payload) == fingerprint), None)


def submit(
    db: Session,
    kind: str,
    payload: dict,
    *,
    actor: str | None = None,
    idempotency_key: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> tuple[BackgroundJob, bool]:
    """写一条待执行的任务记录。**不在这里执行**：执行由调用方交给 BackgroundTasks。

    返回 `(任务, 是否新建)`：去重命中时返回已有的那条并置 `False`，调用方据此把
    「已提交」和「复用了一条正在跑的任务」区分开来告诉用户。
    """
    if kind not in HANDLERS:
        # 提交侧就拦住：一个没有处理函数的任务排进去只会变成一条没人能修的 failed 记录
        raise ValueError(f"没有注册的后台任务类型：{kind}")
    key = (idempotency_key or "").strip() or None
    if key:
        existing = find_key_holder(db, key)
        if existing is not None:
            return existing, False
    else:
        duplicate = find_active_duplicate(db, kind, payload)
        if duplicate is not None:
            return duplicate, False

    job = BackgroundJob(
        kind=kind, status="queued", actor=actor or "", payload=payload or {},
        idempotency_key=key, attempts=0, max_attempts=max(1, int(max_attempts)),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # 两个请求同时查空、各自插一条 → 部分唯一索引拦下第二条。把它读回来当去重成功。
        db.rollback()
        existing = find_key_holder(db, key or "")
        if existing is not None:
            logger.info("后台任务去重（撞唯一索引）：键 %s 已有任务 #%s", key, existing.id)
            return existing, False
        raise
    db.refresh(job)
    return job, True


def _thread_timer(delay: float, job_id: int) -> None:
    """到点再跑一次（进程内定时器）。多副本部署要换成真正的外部队列，见 README。"""
    timer = threading.Timer(delay, run_job, args=(job_id,))
    timer.daemon = True
    timer.name = f"finance-job-retry-{job_id}"
    timer.start()


# 定时器可替换：测试里换成记录器，否则用例会真的睡上几秒、还会在断言之后开线程。
SCHEDULER: Callable[[float, int], None] = _thread_timer


def context_for(job: BackgroundJob) -> dict:
    """给处理函数的上下文。`is_retry` 是给它做幂等判断用的（第一次尝试 ≠ 重跑）。"""
    return {
        "actor": job.actor,
        "job_id": job.id,
        "attempt": attempts_of(job),
        "is_retry": attempts_of(job) > 1,
    }


def mark_failure(db: Session, job: BackgroundJob, exc: Exception, *, retry: bool, schedule: bool) -> str:
    """记下这次失败：还有预算就放回队列（返回 `"queued"`），否则判死（返回 `"failed"`）。"""
    attempts = attempts_of(job)
    job.error = f"第 {attempts} 次尝试失败：" + str(exc)[:480]
    if retry and can_retry(job):
        delay = retry_delay(attempts)
        job.status = "queued"
        job.next_attempt_at = datetime.now(UTC) + timedelta(seconds=delay)
        db.commit()
        logger.warning("后台任务 %s 第 %s 次尝试失败，%.1f 秒后重试：%s", job.id, attempts, delay, exc)
        if schedule:
            SCHEDULER(delay, job.id)
        return "queued"
    job.status = "failed"
    job.next_attempt_at = None
    job.finished_at = datetime.now(UTC)
    db.commit()
    return "failed"


def run_job(job_id: int, db: Session | None = None) -> None:
    """执行一条任务。异常不外抛，失败落在任务记录里（还可能按预算重排一次）。

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
        job.attempts = attempts_of(job) + 1
        job.started_at = job.started_at or datetime.now(UTC)
        job.next_attempt_at = None
        session.commit()

        function = HANDLERS.get(job.kind)
        if function is None:
            # 没有处理函数是配置问题，重试多少次都一样（除非有人发一次新版本）
            raise RuntimeError(f"没有注册的处理函数：{job.kind}")
        result = function(session, dict(job.payload or {}), context_for(job))

        job = session.get(BackgroundJob, job_id)
        job.status = "succeeded"
        job.result = result or {}
        job.error = None
        job.finished_at = datetime.now(UTC)
        session.commit()
    except Exception as exc:  # noqa: BLE001 —— 任务失败要落库，不能让后台线程静默炸掉
        logger.exception("后台任务 %s 失败", job_id)
        session.rollback()
        job = session.get(BackgroundJob, job_id)
        if job is not None:
            mark_failure(session, job, exc, retry=job.kind in HANDLERS, schedule=own_session)
    finally:
        if own_session:
            session.close()


def prepare_retry(db: Session, job: BackgroundJob) -> BackgroundJob:
    """人工重试：放回队列，并**追加**一份重试预算。

    为什么不把 `attempts` 归零：处理函数靠 `is_retry`（= 已经试过 ≥ 2 次）判断「上次可能已经把活干完了」，
    归零会让重跑看起来像第一次尝试，于是 `batch_ops.revert_batch` 会因为「一行都没删」再次判失败 ——
    正好把重试要解决的问题留着。追加预算的做法让编号继续涨（界面显示「第 4/6 次」），
    语义也诚实：人点一次「重试」就是再给三次机会。

    `error` **保留**：重跑期间界面还能显示上一次为什么失败，成功了才会被 `run_job` 清掉。
    """
    job.status = "queued"
    job.max_attempts = attempts_of(job) + DEFAULT_MAX_ATTEMPTS
    job.next_attempt_at = None
    job.finished_at = None
    db.commit()
    db.refresh(job)
    return job


def resume_interrupted_jobs(db: Session, *, schedule: bool = True) -> dict:
    """启动时接续上次进程留下的 `queued`/`running` 任务。

    以前这里是 `reap_stale_jobs()`：一律标记成失败（因为任务不可重跑）。现在处理函数是幂等的，
    所以默认**重新排队重跑**，只有两种情况仍然判死：① 这个任务类型已经没有处理函数了；
    ② 已经用完重试预算。返回 `{"requeued": [...], "failed": [...], "total": n}`。
    """
    rows = db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.status.in_(ACTIVE_STATUSES))
        .order_by(BackgroundJob.id)
    ).all()
    requeued: list[int] = []
    failed: list[int] = []
    for job in rows:
        if job.kind not in HANDLERS:
            job.status = "failed"
            job.error = "进程重启，且这个任务类型已经没有处理函数"
            job.next_attempt_at = None
            job.finished_at = datetime.now(UTC)
            failed.append(job.id)
        elif not can_retry(job):
            job.status = "failed"
            job.error = f"进程重启，任务未完成（已用完 {max_attempts_of(job)} 次尝试）"
            job.next_attempt_at = None
            job.finished_at = datetime.now(UTC)
            failed.append(job.id)
        else:
            job.status = "queued"
            job.next_attempt_at = None
            job.started_at = None
            job.error = f"进程重启，任务已重新排队（下次是第 {attempts_of(job) + 1} 次尝试）"
            requeued.append(job.id)
    db.commit()
    if schedule:
        for job_id in requeued:
            # 不等：启动时排一个 0 秒的定时器，让 lifespan 先跑完（重算可能要几十秒）
            SCHEDULER(0.0, job_id)
    return {"requeued": requeued, "failed": failed, "total": len(rows)}


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
