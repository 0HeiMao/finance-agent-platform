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

诚实的边界（README 也写了）：**单进程派发**，任务只在提交它的那个进程里执行（没有跨进程的
派发/轮询/重试队列），定时重试用的是进程内 `threading.Timer`；进程重启时未完成的任务由
`resume_interrupted_jobs()` 重新排队（处理函数幂等，所以重跑是安全的），已经用完预算的才
标记失败——不这么做，任务会永远停在 `running`，界面上的进度条会一直转。

⑱ 之前这里有个真问题：上面那套「谁先读到谁跑」的逻辑在多副本部署下会**重复执行**——
两个进程同时 `session.get()` 都看到 `queued`，各自置 `running` 跑一遍（同一批数据被撤销两次），
而每个进程启动时又都会把别人**正在跑**的任务当成「上次留下的」重新排队。现在两道护栏：

* **执行权**：`run_job()` 先抢一条会话级 advisory lock（`locks.job_lock_key`，钉在专用连接上，
  进程一死连接断开、锁自动释放），再走 `claim()` 的**条件** `UPDATE … WHERE status='queued'`
  把 `queued → running` 变成原子操作。抢不到锁或没抢到那一行的进程直接退出，什么都不做；
  `worker_id` 记下「这条 running 归哪个进程」，界面与排查用得上。
* **接续**：`resume_interrupted_jobs()` 先抢一把全局锁，抢不到的进程立刻返回
  `{"skipped": True, ...}`；对 `running` 行先**探测**它的执行权锁——抢得到说明那个进程已经死了
  （僵尸，重排重跑），抢不到说明真有人正在跑（原样不动，列进 `still_running`）。
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import batch_ops
from backend.db import SessionLocal, engine
from backend.locks import job_lock_key, release_session_lock, resume_lock_key, try_session_lock
from backend.models import BackgroundJob

logger = logging.getLogger(__name__)

# 谁在跑：主机名 + 进程号 + 一小段随机串。多副本部署时同一个 pid 会重复出现，所以随机串不是装饰。
WORKER_ID = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
ACTIVE_STATUSES = ("queued", "running")
# 任务处理函数：`(db, payload, context) -> result`
Handler = Callable[[Session, dict, dict], dict]
HANDLERS: dict[str, Handler] = {}
# 前端展示用：kind → 中文名
KIND_LABELS = {
    "batch.revert": "撤销导入批次",
    "alert.dispatch": "外发预警通知",
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
    result = batch_ops.revert_batch(
        db,
        payload["batch"],
        actor=context.get("actor"),
        ip=payload.get("ip"),
        job_id=context.get("job_id"),
        # 重跑时「一行都没删」是正常结果：上一次尝试已经把行删掉了。第一次尝试则相反，
        # 空批次说明调用方点错了（或者有人抢先撤销了），必须报错。
        allow_missing=bool(context.get("is_retry")),
    )
    # 撤销会重算受影响期间 → 期间里的预警是**新行**，该发的通知要在这里补排一次。
    # 函数内导入：`alerts_outbound` 反过来要用本模块的 submit/SCHEDULER，
    # 而本模块的 `batch_ops` → `transactions` 也在链条上，模块级导入会成环。
    from backend import alerts_outbound  # noqa: PLC0415

    # 这里不 commit：投递单本来就该和「撤销+重算」是同一个事务，而 jobs.submit() 内部会提交
    # （即投递单与任务记录一起落盘），随后 run_job 还会再提交一次任务状态，没有副作用。
    alerts_outbound.schedule(db, periods=result.get("recalculated"), actor=context.get("actor"))
    return result


@handler("alert.dispatch")
def _dispatch_alerts(db: Session, payload: dict, context: dict) -> dict:
    """预警外发（⑭）：把一批投递单发到成功或判失败为止（实现在 backend/alerts_outbound.py）。

    注册放在本模块、实现放在那边，是因为 `HANDLERS` 是这里的全局表（`submit` / `run_job` 都读它），
    而 alerts_outbound 反过来要用 `submit` / `SCHEDULER`——模块级互相导入会成环，
    于是这里只做注册与转交，导入推迟到调用时。
    """
    from backend import alerts_outbound  # noqa: PLC0415

    return alerts_outbound.dispatch(db, payload, context)


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
        # ⑱ 谁正在跑它（`status=running` 时才有值）。多副本部署下这是排查「任务卡在运行中」的
        # 第一手线索：记下来的那个进程就是该查的那个。
        "worker_id": job.worker_id,
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
    # ⑱ 不管哪种结局，这条任务都不再「归某个进程」了：放回队列是等下一次执行，
    # 判死则永远不会有执行者。留着旧的 worker_id 只会让界面显示一个早已跑完的进程。
    job.worker_id = None
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


def claim(db: Session, job_id: int) -> BackgroundJob | None:
    """把一条 `queued` 任务**原子地**变成「本进程正在跑」，抢不到返回 `None`。

    为什么必须是条件 UPDATE，而不是「先 `session.get()` 看一眼、再改字段提交」：后者在多副本下
    两个进程会同时读到 `queued`，于是各自把同一批明细撤销一遍。`WHERE status='queued'` 让输的
    一方 `rowcount == 0` —— Postgres 在 READ COMMITTED 下会等赢家提交，再按**新值**重判条件，
    所以它看到的是 `running` 而不是自己以为的 `queued`，安静退出（这正是「条件 UPDATE 当锁用」）。
    """
    result = db.execute(
        update(BackgroundJob)
        .where(BackgroundJob.id == job_id, BackgroundJob.status == "queued")
        .values(
            status="running",
            worker_id=WORKER_ID,
            attempts=func.coalesce(BackgroundJob.attempts, 0) + 1,
            started_at=func.coalesce(BackgroundJob.started_at, datetime.now(UTC)),
            next_attempt_at=None,
        )
        # 用 `coalesce(attempts, 0) + 1` 这种 SQL 表达式时 ORM 的「就地求值」同步策略没法跟进，
        # 直接关掉它、回头 populate_existing 重新读一遍，免得拿到旧值。
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        db.rollback()
        return None
    db.commit()
    return db.get(BackgroundJob, job_id, populate_existing=True)


@contextmanager
def job_claim_lock(job_id: int) -> Iterator[bool]:
    """整段执行期间独占这条任务。拿不到 → 已经有别的进程在跑它。

    锁借一条**专用连接**（不是调用方 Session 的连接：Session 一提交就可能把连接还回池子，
    而池化连接是带着锁还回去的，解锁就落到别的连接上了），退出前显式解锁再关连接。
    连接一断锁就没了，所以它同时是「这条 `running` 是死是活」的判据：
    `resume_interrupted_jobs()` 抢得到 = 那个进程已经不在了。
    """
    key = job_lock_key(job_id)
    connection = engine.connect()
    try:
        if not try_session_lock(connection, key):
            yield False
            return
        try:
            yield True
        finally:
            # 解锁失败就是锁泄漏（这条任务会永远抢不动）：必须留下痕迹，不能静默
            if not release_session_lock(connection, key):
                logger.warning("后台任务 %s 的执行权锁没能释放（key=%s）", job_id, key)
    finally:
        connection.close()


def _execute(session: Session, job_id: int, *, schedule: bool) -> None:
    """抢下这条任务并跑一遍处理函数。异常不外抛，落在任务记录里。"""
    job = claim(session, job_id)
    if job is None:
        # 不是 `queued` 了：已经被别人抢走、已经跑完、或者被人工重试/接续改成了别的状态。
        # 以前这里只判「在不在 ACTIVE_STATUSES 里」，于是 `running` 会被**再跑一遍**。
        logger.info("后台任务 %s 当前不是排队中，跳过本次执行", job_id)
        return
    try:
        function = HANDLERS.get(job.kind)
        if function is None:
            # 没有处理函数是配置问题，重试多少次都一样（除非有人发一次新版本）
            raise RuntimeError(f"没有注册的处理函数：{job.kind}")
        result = function(session, dict(job.payload or {}), context_for(job))

        job = session.get(BackgroundJob, job_id, populate_existing=True)
        job.status = "succeeded"
        job.result = result or {}
        job.error = None
        job.finished_at = datetime.now(UTC)
        job.worker_id = None
        session.commit()
    except Exception as exc:  # noqa: BLE001 —— 任务失败要落库，不能让后台线程静默炸掉
        logger.exception("后台任务 %s 失败", job_id)
        session.rollback()
        job = session.get(BackgroundJob, job_id)
        if job is not None:
            mark_failure(session, job, exc, retry=job.kind in HANDLERS, schedule=schedule)


def run_job(job_id: int, db: Session | None = None) -> None:
    """执行一条任务。异常不外抛，失败落在任务记录里（还可能按预算重排一次）。

    `db` 只在测试或内嵌调用时传（复用调用方的事务）—— 那种情况下**不抢跨进程执行权**：
    要抢就得借一条专用连接，而测试传进来的 Session 本身跑在一个外层事务里，锁不该凭空多出来。
    同进程内不会有两个执行者（派发在提交它的那个进程里），所以跳过锁是安全的；
    真正的多副本互斥由不传 `db` 的生产路径（`BackgroundTasks` / `threading.Timer`）负责。
    """
    if db is not None:
        _execute(db, job_id, schedule=False)
        return
    with job_claim_lock(job_id) as locked:
        if not locked:
            logger.info("后台任务 %s 已有其它进程在跑，本进程跳过", job_id)
            return
        session = SessionLocal()
        try:
            _execute(session, job_id, schedule=True)
        finally:
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
    job.worker_id = None  # ⑱ 放回队列：这条任务又不归任何进程了
    db.commit()
    db.refresh(job)
    return job


def is_job_running(job_id: int) -> bool:
    """这条任务**真的**有人在跑吗？抢一下它的执行权锁就知道。

    抢得到 = 没有进程持有它（多半是上次进程死在这儿了），于是这是个僵尸，该重排；
    顺手**立刻把锁放掉** —— 不放的话重排后接手执行的那个进程反而抢不到了
    （advisory lock 在同一会话里是重入计数的，`run_job` 拿到的是「第二次」而不是「第一次」）。
    """
    key = job_lock_key(job_id)
    with engine.connect() as connection:
        if not try_session_lock(connection, key):
            return True
        if not release_session_lock(connection, key):
            logger.warning("探测后台任务 %s 的执行权锁后没能释放（key=%s）", job_id, key)
        return False


def resume_interrupted_jobs(db: Session, *, schedule: bool = True) -> dict:
    """启动时接续上次进程留下的 `queued`/`running` 任务。

    以前这里是 `reap_stale_jobs()`：一律标记成失败（因为任务不可重跑）。现在处理函数是幂等的，
    所以默认**重新排队重跑**，只有两种情况仍然判死：① 这个任务类型已经没有处理函数了；
    ② 已经用完重试预算。

    ⑱ 的两道护栏都在这上面：
    * **一份活只让一个进程干**：多副本同时启动时每个进程都会跑这个函数。先抢全局锁
      （`locks.resume_lock_key`），抢不到的直接返回 `{"skipped": True, ...}` 且**一个字都不改**——
      否则 N 个进程把同一批任务各重排一次、各排一个定时器，同一份撤销被跑 N 遍。
    * **不打断还在跑的任务**：`running` 行不都是遗骸。抢得到它的执行权锁 = 那个进程已经死了
      （锁随连接断开自动释放）→ 僵尸，重排重跑；抢不到 = 真有人在跑 → 原样不动，
      列进 `still_running` 报出来。以前这里一律重排，于是第二个副本一启动就把别人正在跑的活
      又排了一遍（同一份数据被撤销两次）。

    返回 `{"requeued": [...], "failed": [...], "still_running": [...], "skipped": bool, "total": n}`。
    """
    key = resume_lock_key()
    with engine.connect() as connection:
        if not try_session_lock(connection, key):
            logger.info("另一个进程正在接续后台任务，本进程跳过")
            return {"requeued": [], "failed": [], "still_running": [], "skipped": True, "total": 0}
        try:
            return _resume_locked(db, schedule=schedule)
        finally:
            if not release_session_lock(connection, key):
                logger.warning("启动接续的全局锁没能释放（key=%s）", key)


def _resume_locked(db: Session, *, schedule: bool) -> dict:
    """`resume_interrupted_jobs()` 的主体：已经持有全局锁，可以放手改这些行了。"""
    rows = db.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.status.in_(ACTIVE_STATUSES))
        .order_by(BackgroundJob.id)
    ).all()
    requeued: list[int] = []
    failed: list[int] = []
    still_running: list[int] = []
    for job in rows:
        if job.status == "running" and is_job_running(job.id):
            # 真有进程在跑它：这是它的活，不是遗骸
            still_running.append(job.id)
            continue
        if job.kind not in HANDLERS:
            job.status = "failed"
            job.worker_id = None
            job.error = "进程重启，且这个任务类型已经没有处理函数"
            job.next_attempt_at = None
            job.finished_at = datetime.now(UTC)
            failed.append(job.id)
        elif not can_retry(job):
            job.status = "failed"
            job.worker_id = None
            job.error = f"进程重启，任务未完成（已用完 {max_attempts_of(job)} 次尝试）"
            job.next_attempt_at = None
            job.finished_at = datetime.now(UTC)
            failed.append(job.id)
        else:
            job.status = "queued"
            job.worker_id = None
            job.next_attempt_at = None
            job.started_at = None
            job.error = f"进程重启，任务已重新排队（下次是第 {attempts_of(job) + 1} 次尝试）"
            requeued.append(job.id)
    db.commit()
    if schedule:
        for job_id in requeued:
            # 不等：启动时排一个 0 秒的定时器，让 lifespan 先跑完（重算可能要几十秒）
            SCHEDULER(0.0, job_id)
    return {
        "requeued": requeued,
        "failed": failed,
        "still_running": still_running,
        "skipped": False,
        "total": len(rows),
    }


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
