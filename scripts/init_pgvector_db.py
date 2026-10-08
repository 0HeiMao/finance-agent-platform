"""初始化 PostgreSQL pgvector 扩展和业务表，并补齐历史库缺失的列。

项目没有引入 Alembic，但「已经建好的库」需要能平滑升级，所以把幂等的列补齐放在这里：
`Base.metadata.create_all` 只建**新表**，不会给已存在的表加列。
"""

import sys
from pathlib import Path

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:  # 支持 `python scripts/init_pgvector_db.py`
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import models  # noqa: E402,F401 - 注册用户和财务表模型
from backend.db import Base, engine  # noqa: E402
from backend.rag.pgvector_store import DocumentChunk  # noqa: E402,F401 - 注册向量表模型

# (表, 列, 类型定义)。只允许 ADD COLUMN IF NOT EXISTS 这类幂等语句。
COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # 部门可见性落地后新增：users.department
    ("users", "department", "VARCHAR(100)"),
    # 财务口径按部门拆分后新增：NULL = 全公司汇总行，有值 = 该部门口径行。
    # 已有历史数据补上列后 department 全是 NULL，正好就是「全公司」行，不需要回填；
    # 各期间的部门行由 scripts/migrate_department_scope.py 调 calculate() 重新生成。
    ("financial_metrics", "department", "VARCHAR(100)"),
    ("financial_alerts", "department", "VARCHAR(100)"),
)

# 列加完顺手补索引（CREATE INDEX IF NOT EXISTS 幂等）。
INDEX_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("ix_financial_metrics_department", "financial_metrics (department)"),
    ("ix_financial_alerts_department", "financial_alerts (department)"),
)

# (表, 列, 目标类型, 精度, 小数位)。只在一期里出现「收入极小、费用很大」的部门时，
# net_profit_ratio 会超过 Numeric(10,4) 的 6 位整数上限（PostgreSQL 直接 NumericValueOutOfRange），
# 所以比率列加宽到 (20,4)。用 numeric_precision/numeric_scale 判断，已经够宽就不再 ALTER。
COLUMN_TYPE_MIGRATIONS: tuple[tuple[str, str, str, int, int], ...] = (
    ("financial_metrics", "gross_profit_ratio", "NUMERIC(20, 4)", 20, 4),
    ("financial_metrics", "net_profit_ratio", "NUMERIC(20, 4)", 20, 4),
)


def _existing_columns(connection, table: str) -> set[str]:
    rows = connection.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": table},
    )
    return {row[0] for row in rows}


def _numeric_shape(connection, table: str, column: str) -> tuple[int | None, int | None]:
    row = connection.execute(
        text(
            "SELECT numeric_precision, numeric_scale FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    ).first()
    if row is None:
        return (None, None)
    return (row[0], row[1])


def apply_column_migrations(connection) -> list[str]:
    """返回本次实际执行的迁移描述，便于脚本输出日志。"""
    applied: list[str] = []
    for table, column, definition in COLUMN_MIGRATIONS:
        if column in _existing_columns(connection, table):
            continue
        connection.execute(text(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}'))
        applied.append(f"{table}.{column}")
    return applied


def apply_index_migrations(connection) -> list[str]:
    applied: list[str] = []
    for name, target in INDEX_MIGRATIONS:
        connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {target}"))
        applied.append(name)
    return applied


def apply_type_migrations(connection) -> list[str]:
    """加宽数值列。精度已经够宽就跳过，避免每次启动都重写整张表。"""
    applied: list[str] = []
    for table, column, definition, precision, scale in COLUMN_TYPE_MIGRATIONS:
        current = _numeric_shape(connection, table, column)
        if current == (precision, scale):
            continue
        connection.execute(text(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE {definition}"))
        applied.append(f"{table}.{column} -> {definition}")
    return applied


def main() -> None:
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        Base.metadata.create_all(connection)
        applied = apply_column_migrations(connection)
        widened = apply_type_migrations(connection)
        indexes = apply_index_migrations(connection)
    print("PostgreSQL + pgvector 初始化完成")
    if applied:
        print("已补齐列：" + "、".join(applied))
    else:
        print("表结构已是最新，无需补齐列")
    if widened:
        print("已加宽数值列：" + "、".join(widened))
    print(f"已确保索引：{'、'.join(indexes)}")


if __name__ == "__main__":
    main()
