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
]


def ensure_schema(engine: Engine | None = None) -> list[str]:
    """建缺失的表 + 补缺失的列，返回本次真正执行的 DDL 描述（空列表 = 结构已是最新）。"""
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
