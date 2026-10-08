"""撤销导入批次（长任务，同步接口与后台任务共用同一份实现）。

抽出来的原因：这件事以前只存在于 `api_admin.delete_batch` 里，接口是同步的——
期间一多就是「一个 HTTP 请求里重算所有受影响期间」，请求超时与否全看数据量。
现在管理接口（脚本、API 调用）与后台任务都走这里，行为完全一致，不会出现两套撤销逻辑。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend import audit, locks
from backend.models import FinancialTransaction
from backend.transactions import recalculate_periods


class BatchNotFound(LookupError):
    """批次不存在或已被撤销。调用方决定是回 404 还是把后台任务标记成失败。"""


def batch_rows(db: Session, batch: str) -> int:
    """该批次的明细行数（提交任务前用它快速校验，避免为一个不存在的批次排一个任务）。"""
    return db.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch == batch)
    ) or 0


def revert_batch(
    db: Session,
    batch: str,
    *,
    actor: str | None = None,
    ip: str | None = None,
    job_id: int | None = None,
    allow_missing: bool = False,
) -> dict:
    """撤销一个批次：先删该批次明细，再重算受影响期间（没有剩余明细的期间连指标一起清掉）。

    删除与重算写在同一个事务里：撤销到一半就退出会留下「明细没了、指标还在」的幽灵报表，
    那比不撤销更糟。`audit.record` 也在这条事务里，所以业务回滚时审计不会留下一条假记录。

    `allow_missing`（⑩ 幂等重试）：重跑一个任务时「一行都没删」不是错误，而是**上一次尝试已经把
    行删掉了**的正常结局——进程可能正是在「删完还没写回任务状态」的那一刻挂的。这时返回
    `already_reverted=True` 的空结果，任务判成功。第一次尝试（含同步接口）保持严格：空批次就是
    调用方点错了，必须报 `BatchNotFound`，否则界面会把「批次本来就不存在」显示成撤销成功。
    """
    rows = db.execute(
        select(func.distinct(func.to_char(FinancialTransaction.transaction_date, "YYYY-MM")))
        .where(FinancialTransaction.import_batch == batch)
    ).all()
    affected = sorted(row[0] for row in rows)
    # 期间锁：删行**之前**取锁，删完才锁的话，另一个正在重算这一期的请求已经读到了旧的行集合。
    locks.lock_periods(db, locks.period_lock_set(affected))
    deleted = db.execute(
        delete(FinancialTransaction).where(FinancialTransaction.import_batch == batch)
    ).rowcount or 0
    if not deleted:
        if not allow_missing:
            db.rollback()
            raise BatchNotFound("批次不存在或已被撤销")
        # 已经撤销过了：不重算、不清指标（上一次尝试已经做完了），只留一条审计说明是重跑
        audit.record(db, actor, "batch.revert", target_type="batch", target_id=batch,
                     detail={"deleted_rows": 0, "periods": affected, "recalculated": [],
                             "cleared": [], "already_reverted": True, "ip": ip, "job": job_id})
        db.commit()
        return {
            "batch": batch,
            "deleted_rows": 0,
            "recalculated": [],
            "cleared": [],
            "already_reverted": True,
        }

    recalculated: list[str] = []
    cleared: list[str] = []
    refreshed = recalculate_periods(db, affected)
    recalculated.extend(refreshed["recalculated"])
    cleared.extend(refreshed["cleared"])

    audit.record(db, actor, "batch.revert", target_type="batch", target_id=batch,
                 detail={"deleted_rows": deleted, "periods": affected,
                         "recalculated": recalculated, "cleared": cleared,
                         "ip": ip, "job": job_id})
    db.commit()
    return {
        "batch": batch,
        "deleted_rows": deleted,
        "recalculated": recalculated,
        "cleared": cleared,
    }
