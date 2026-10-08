"""把已有的财务指标/预警改造成「全公司 + 逐部门」两套口径行。

背景：`financial_metrics` / `financial_alerts` 新增了 `department` 列，
NULL = 全公司汇总行（原有那 7 行正好就是这个语义，不需要改），有值 = 该部门口径行。
历史库里只有全公司行，受限账号登进去会看不到任何数据，所以要把每个已有期间重算一遍。

用法（工作目录为项目根目录）：

    .venv\\Scripts\\python.exe scripts\\migrate_department_scope.py --dry-run
    .venv\\Scripts\\python.exe scripts\\migrate_department_scope.py

脚本是幂等的：`calculate()` 对同一期间是先删该期间全部指标/预警再按口径重建，
重复执行结果一致（只是白算一遍），所以可以安全地重跑。

注意：`calculate()` 是**按明细重算**，不读旧的指标行。如果某个期间只有指标没有明细
（历史清洗残留），重算会把该期间的指标清空——这属于数据本身的问题，
脚本会在执行前把这些期间列出来并要求 --force 才会继续。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from sqlalchemy import delete, func, select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 支持 `python scripts/migrate_department_scope.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.api_analysis import calculate  # noqa: E402
from backend.db import SessionLocal  # noqa: E402
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction  # noqa: E402


def _periods(db, column, *, transaction: bool = False) -> list[str]:
    if transaction:
        statement = select(func.distinct(func.to_char(FinancialTransaction.transaction_date, "YYYY-MM")))
    else:
        statement = select(func.distinct(column))
    return sorted(x for x in db.scalars(statement).all() if x)


def _counts(db) -> dict[str, int]:
    return {
        "metric_rows": db.scalar(select(func.count()).select_from(FinancialMetric)) or 0,
        "metric_company": db.scalar(
            select(func.count()).select_from(FinancialMetric).where(FinancialMetric.department.is_(None))) or 0,
        "metric_department": db.scalar(
            select(func.count()).select_from(FinancialMetric).where(FinancialMetric.department.is_not(None))) or 0,
        "alert_rows": db.scalar(select(func.count()).select_from(FinancialAlert)) or 0,
        "alert_company": db.scalar(
            select(func.count()).select_from(FinancialAlert).where(FinancialAlert.department.is_(None))) or 0,
        "alert_department": db.scalar(
            select(func.count()).select_from(FinancialAlert).where(FinancialAlert.department.is_not(None))) or 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="重建财务指标/预警的部门口径行")
    parser.add_argument("--dry-run", action="store_true", help="只报告要处理哪些期间，不写库")
    parser.add_argument("--force", action="store_true", help="允许重算「只有指标没有明细」的期间（会清空该期间指标）")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        metric_periods = set(_periods(db, FinancialMetric.period))
        transaction_periods = set(_periods(db, None, transaction=True))
        if not metric_periods and not transaction_periods:
            print("库里没有任何财务期间，无需迁移。")
            return 0

        orphan = sorted(metric_periods - transaction_periods)
        targets = sorted(metric_periods | transaction_periods)
        before = _counts(db)
        print(f"待重算期间（{len(targets)} 个）：{'、'.join(targets)}")
        print("迁移前：" + "，".join(f"{key}={value}" for key, value in before.items()))
        if orphan:
            print(f"警告：{len(orphan)} 个期间只有指标没有明细（{'、'.join(orphan)}），"
                  "重算会把这些期间的指标清空。")
            if not args.force and not args.dry_run:
                return 2

        if args.dry_run:
            print("--dry-run：未写库。")
            return 0

        for period in targets:
            if period in transaction_periods:
                calculate(db, period, commit=False)
            else:
                # 只有指标的孤儿期间：留着会更糟（受限账号按部门读不到全公司行之外的东西），
                # 所以顺手清掉，让「有明细才有指标」这条不变量重新成立。
                db.execute(delete(FinancialMetric).where(FinancialMetric.period == period))
                db.execute(delete(FinancialAlert).where(FinancialAlert.period == period))
        db.commit()

        after = _counts(db)
        print("迁移后：" + "，".join(f"{key}={value}" for key, value in after.items()))
        print(f"部门口径指标行：{before['metric_department']} -> {after['metric_department']}；"
              f"部门口径预警行：{before['alert_department']} -> {after['alert_department']}")
        if after["metric_company"] == 0:
            print("警告：迁移后没有全公司口径指标行，请检查明细数据。")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
