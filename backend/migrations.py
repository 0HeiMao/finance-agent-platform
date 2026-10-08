"""轻量迁移：项目没有 Alembic，但结构变更必须能在**已有的库**上补上。

为什么需要它：`Base.metadata.create_all()` 只会建缺失的表，**不会**给已有表加列。
于是「模型加了字段、接口开始 SELECT 它、真库还是老结构」会变成运行时
`UndefinedColumn: column financial_alerts.review_status does not exist`——
而且是接口 500、回归脚本连带失败，排查起来容易误以为是业务代码的问题。

做法很土但有效：把「需要补的列」写在这里，逐条查 `information_schema` 再 `ALTER TABLE`，
幂等、可重复调用。表本身的创建仍然交给 `create_all()`。
之所以不上 Alembic：这个项目的部署形态是「一个进程 + 一个库」，`ensure_schema()` 在启动时
自动补齐，多一套 `alembic upgrade head` 的运维动作反而多一个忘跑的环节；代价是
**任何加列都必须往 `COLUMNS` 里加一行**，否则老库升级会漏——忘了的表现是接口 500，
所以 `missing_columns()` 会在启动时把漏掉的列直接点名（见 `backend/main.py` 的启动钩子）。
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from backend.db import Base, engine as default_engine

# 显式导入模型模块：`create_all()` 只会建「已注册进 metadata」的表，而注册靠导入。
# 少了这一行，在「只导入 migrations」的场景（一次性脚本、测试夹具）里新表会被静默漏掉——
# 实测 `AuditLog` 就是这么消失的：只 `from backend.migrations import ensure_schema` 时
# `audit_logs` 根本没建，接口再报 `relation "audit_logs" does not exist`。
from backend import models as _models  # noqa: F401

# (表名, 列名, DDL 片段)。DDL 里不要写 SQL 关键字大小写混用以外的花活，便于跨库阅读。
COLUMNS: list[tuple[str, str, str]] = [
    # 部门口径（部门行与全公司汇总行共存）
    ("financial_metrics", "department", "VARCHAR(100)"),
    ("financial_alerts", "department", "VARCHAR(100)"),
    # 预警处理闭环
    ("financial_alerts", "review_status", "VARCHAR(20) DEFAULT 'pending' NOT NULL"),
    ("financial_alerts", "review_note", "TEXT"),
    ("financial_alerts", "reviewed_by", "VARCHAR(100)"),
    ("financial_alerts", "reviewed_at", "TIMESTAMP WITH TIME ZONE"),
    # 预警规则可配置
    ("alert_rules", "require_metric", "VARCHAR(100)"),
    # 预警规则分档：空表示「所有部门 / 所有期间」
    ("alert_rules", "department", "VARCHAR(100)"),
    ("alert_rules", "period", "VARCHAR(7)"),
    # 多部门账号：权威值是一个 JSON 数组，老的单值列 department 作为回落与展示保留。
    # 已有账号不需要数据迁移：departments 为 NULL 时 scope_of() 会自动回落到 department。
    ("users", "departments", "JSONB"),
    # 单行更正/冲销：冲销用「负数冲销行 + 原行标记」而不是删行，所以要记清冲的是哪一行。
    ("financial_transactions", "reversal_of_id", "INTEGER"),
    ("financial_transactions", "reversed_at", "TIMESTAMP WITH TIME ZONE"),
    ("financial_transactions", "reversed_by", "VARCHAR(100)"),
    ("financial_transactions", "corrections", "INTEGER DEFAULT 0 NOT NULL"),
]

# 需要**去掉**的约束：(表名, 约束名, 为什么要去掉)。
# Postgres 给列级 `unique=True` 自动起的名字就是 `<表>_<列>_key`，所以这里按名字精确删——
# 不用通配符，也不会误伤别的约束。删约束的 DDL 同样只在「确实存在」时执行。
DROPPED_CONSTRAINTS: list[tuple[str, str, str]] = [
    ("alert_rules", "alert_rules_alert_type_key",
     "alert_type 不再唯一：同一类型可以按部门/期间分档成多条规则，"
     "留着唯一约束会让第二条规则插入时报 UniqueViolation"),
]

# 需要**从唯一索引换成普通索引**的索引：(表名, 索引名, 列名, 为什么要换)。
# `unique=True, index=True` 在 Postgres 里生成的是 `CREATE UNIQUE INDEX ix_<表>_<列>`——
# 它是索引而不是约束，既不进 `pg_constraint`（上面那段删不到），也不受表约束影响，
# 所以分档规则第二条会报 `duplicate key value violates unique constraint "ix_alert_rules_alert_type"`。
# 处理方式：确认它确实是 UNIQUE 就删掉再按模型现在的定义（`index=True`、不唯一）建回来。
PLAIN_INDEXES: list[tuple[str, str, str, str]] = [
    ("alert_rules", "ix_alert_rules_alert_type", "alert_type",
     "alert_type 要能一个类型多条（按部门/期间分档），索引保留做前缀查询即可，唯一性必须去掉"),
]


def ensure_schema(engine: Engine | None = None) -> list[str]:
    """建缺失的表 + 补缺失的列 + 去掉过时的唯一约束/唯一索引，返回本次真正执行的 DDL 描述（空列表 = 结构已是最新）。"""
    target = engine if engine is not None else default_engine
    Base.metadata.create_all(bind=target)
    applied: list[str] = []
    with target.begin() as connection:
        for table, column, ddl in COLUMNS:
            exists = connection.scalar(
                text("SELECT 1 FROM information_schema.columns"
                     " WHERE table_name = :table AND column_name = :column"),
                {"table": table, "column": column})
            if exists:
                continue
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            applied.append(f"{table}.{column}")
        for table, name, _why in DROPPED_CONSTRAINTS:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_constraint WHERE conname = :name"
                     " AND conrelid = CAST(:table AS regclass)"),
                {"name": name, "table": table})
            if not exists:
                continue
            connection.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {name}"))
            applied.append(f"{table}.-{name}")
        for table, name, column, _why in PLAIN_INDEXES:
            definition = connection.scalar(
                text("SELECT indexdef FROM pg_indexes"
                     " WHERE schemaname = current_schema() AND tablename = :table AND indexname = :name"),
                {"table": table, "name": name})
            if definition is None or "UNIQUE" not in definition.upper():
                continue
            connection.execute(text(f"DROP INDEX {name}"))
            connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({column})"))
            applied.append(f"{table}.{name}→非唯一")
    return applied


def missing_columns(engine: Engine | None = None) -> list[str]:
    """对照模型与真库，列出「模型里有、库里没有」的列（以及整张缺失的表）。

    给 `COLUMNS` 兜底：`create_all()` 不会给**已存在**的表补列，所以「模型加了字段、
    忘了写进 `COLUMNS`」只在第一次 SELECT 该列时炸成 500（就是当初
    `financial_alerts.review_status` / `audit_logs` 的翻车方式）。启动时查一次就能在日志里点名。
    """
    target = engine if engine is not None else default_engine
    inspector = inspect(target)
    existing = set(inspector.get_table_names())
    gaps: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            gaps.append(f"{table.name}（整张表缺失）")
            continue
        columns = {column["name"] for column in inspector.get_columns(table.name)}
        gaps.extend(f"{table.name}.{column.name}" for column in table.columns if column.name not in columns)
    return gaps
