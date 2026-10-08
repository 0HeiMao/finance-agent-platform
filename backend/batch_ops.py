"""撤销导入批次（长任务，同步接口与后台任务共用同一份实现）。

抽出来的原因：这件事以前只存在于 `api_admin.delete_batch` 里，接口是同步的——
期间一多就是「一个 HTTP 请求里重算所有受影响期间」，请求超时与否全看数据量。
现在管理接口（脚本、API 调用）与后台任务都走这里，行为完全一致，不会出现两套撤销逻辑。
"""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend import audit, rollups
from backend.api_analysis import calculate
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction


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
) -> dict:
    """撤销一个批次：先删该批次明细，再重算受影响期间（没有剩余明细的期间连指标一起清掉）。

    删除与重算写在同一个事务里：撤销到一半就退出会留下「明细没了、指标还在」的幽灵报表，
    那比不撤销更糟。`audit.record` 也在这条事务里，所以业务回滚时审计不会留下一条假记录。
    """
    rows = db.execute(
        select(func.distinct(func.to_char(FinancialTransaction.transaction_date, "YYYY-MM")))
        .where(FinancialTransaction.import_batch == batch)
    ).all()
    affected = sorted(row[0] for row in rows)
    deleted = db.execute(
        delete(FinancialTransaction).where(FinancialTransaction.import_batch == batch)
    ).rowcount or 0
    if not deleted:
        db.rollback()
        raise BatchNotFound("批次不存在或已被撤销")

    recalculated: list[str] = []
    cleared: list[str] = []
    for period in affected:
        remaining = db.scalar(
            select(func.count()).select_from(FinancialTransaction).where(
                func.to_char(FinancialTransaction.transaction_date, "YYYY-MM") == period)
        ) or 0
        if remaining:
            calculate(db, period, commit=False)
            recalculated.append(period)
        else:
            db.execute(delete(FinancialMetric).where(FinancialMetric.period == period))
            db.execute(delete(FinancialAlert).where(FinancialAlert.period == period))
            # 这一期已经没有任何明细/指标了，季/年落库行也必须跟着消失：
            # 否则看板上那个季度还挂着一个凭空的合计（数字来自已经被撤销的数据）。
            rollups.refresh_for_months(db, [period])
            cleared.append(period)

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
