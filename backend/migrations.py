"""轻量迁移：项目没有 Alembic，但结构变更必须能在**已有的库**上补上。

为什么需要它：`Base.metadata.create_all()` 只会建缺失的表，**不会**给已有表加列。
于是「模型加了字段、接口开始 SELECT 它、真库还是老结构」会变成运行时
`UndefinedColumn: column financial_alerts.review_status does not exist`——
而且是接口 500、回归脚本连带失败，排查起来容易误以为是业务代码的问题。

做法很土但有效：把「需要补的列」写在这里，逐条查 `information_schema` 再 `ALTER TABLE`，
幂等、可重复调用。表本身的创建仍然交给 `create_all()`。
索引是同一个坑的另一个面：`create_all()` 只在**建表**时建索引，所以「表已存在 + 模型新加索引」
同样要手写进 `INDEXES`，否则老库一直在顺序扫描而且不报错。

之所以不上 Alembic：这个项目的部署形态是「一个进程 + 一个库」，`ensure_schema()` 在启动时
自动补齐，多一套 `alembic upgrade head` 的运维动作反而多一个忘跑的环节；代价是
**任何加列都必须往 `COLUMNS` 里加一行**，否则老库升级会漏——忘了的表现是接口 500，
所以 `missing_columns()` 会在启动时把漏掉的列直接点名（见 `backend/main.py` 的启动钩子）；
索引漏了则交给 `missing_indexes()` 点名。

上面这些的前提是「**表本身先建出来**」，而表建不建得出来还取决于两件容易被忽略的事：
① 表得先被注册进 `Base.metadata`（靠导入，`models` 之外的模块里的表同样要显式导入）；
② 建 `document_chunks` 之前库里得有 `vector` 扩展。两件事在本地老库上都被初始化脚本
一次性做掉了，所以只有「从零建的库」（CI 的一次性容器、新机器）才会暴露——
`ensure_pgvector()` 就是为后者补的。
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

from backend.db import Base, engine as default_engine

logger = logging.getLogger("finance-api.migrations")

# 显式导入模型模块：`create_all()` 只会建「已注册进 metadata」的表，而注册靠导入。
# 少了这一行，在「只导入 migrations」的场景（一次性脚本、测试夹具）里新表会被静默漏掉——
# 实测 `AuditLog` 就是这么消失的：只 `from backend.migrations import ensure_schema` 时
# `audit_logs` 根本没建，接口再报 `relation "audit_logs" does not exist`。
from backend import models as _models  # noqa: F401

# 知识库表（`document_chunks`）不在 models.py 里，而在 RAG 模块里，所以是**同一个坑的第二例**：
# 不导入它，`ensure_schema()` 就不会建这张表，而「测试夹具 / 一次性脚本」走的正是
# 「只导入 migrations」这条路。CI 拿一个空库跑测试时，这里会变成
# `relation "document_chunks" does not exist` —— 本地库因为早就建好了，一直看不出来。
from backend.rag import pgvector_store as _pgvector_store  # noqa: F401

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
    # 会话版本号：老库补列时必须给 DEFAULT 0，否则已有的账号行是 NULL，
    # 「令牌里的 ver 与这一列比较」当场变成 null != 0 —— 所有人一起被踢下线。
    ("users", "token_version", "INTEGER DEFAULT 0 NOT NULL"),
    # 登录失败锁定（账号级）：同样必须带 DEFAULT 0 / NULL，否则老账号行是 NULL，
    # 「连续失败次数」一读就是 NULL，退避公式算不出该锁多久。
    ("users", "failed_attempts", "INTEGER DEFAULT 0 NOT NULL"),
    ("users", "locked_until", "TIMESTAMP WITH TIME ZONE"),
    ("users", "last_failed_at", "TIMESTAMP WITH TIME ZONE"),
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

# 需要**补上**的索引：(表名, 索引名, 列清单（DDL 片段）, 为什么要它)。
# 为什么单独列一份：`create_all()` 只在**建表**那一刻顺带建模型的索引。表已经存在时，
# 往 `models.py` 里新加一个 `Index(...)` 什么都不会发生——本地新库跑得好好的，老库照旧全表扫。
# 这和「加列」是同一个坑，所以同样按名字查 `pg_indexes` 再 `CREATE INDEX IF NOT EXISTS`。
# 加这三条不是推测：`missing_indexes()` 在真库上真报出了后两条（都是「先有表、后加 index=True 列」），
# 也就是说在补上之前，按状态筛预警、按冲销来源找原行都在顺序扫。建索引会短暂锁表（数据量小，够用）。
INDEXES: list[tuple[str, str, str, str]] = [
    ("financial_transactions", "ix_financial_transactions_department_date", "department, transaction_date",
     "受限账号的每条查询都按 department 过滤、再按交易日期圈期间，缺它就是顺序扫全表"),
    ("financial_transactions", "ix_financial_transactions_reversal_of_id", "reversal_of_id",
     "⑨ 加列时带的 index=True 没落到老库：冲销行找原行、明细列表标注「已冲销」都要走它"),
    ("financial_alerts", "ix_financial_alerts_review_status", "review_status",
     "⑥/⑨ 加列时带的 index=True 没落到老库：预警列表默认按 pending 过滤，缺索引只能顺序扫"),
]


def has_pgvector(engine: Engine | None = None) -> bool:
    """库里有 `vector` 扩展吗？`document_chunks.embedding` 是 `VECTOR(384)`，没有它这张表建不出来。"""
    target = engine if engine is not None else default_engine
    try:
        with target.connect() as connection:
            return bool(connection.scalar(
                text("SELECT 1 FROM pg_extension WHERE extname = 'vector'")))
    except SQLAlchemyError:
        return False


def ensure_pgvector(engine: Engine | None = None) -> bool:
    """保证库装着 `vector` 扩展（装好返回 True）。

    这一步原来只写在 `scripts/init_pgvector_db.py` 里，于是**一个从头建的库**（CI 的一次性
    容器、新同事的第一台机器）只要没手工跑过那个脚本，`create_all()` 就会炸在建
    `document_chunks` 的那一刻：`type "vector" does not exist`。本地库早就初始化过，
    所以这个洞只在空库上显形 —— CI 用的正好就是空库，一跑就撞上了。

    装不上（比如镜像不是 `pgvector/pgvector`）只告警、不抛：结构问题应该由启动日志点名，
    而不是让整个服务起不来 —— 与 `backend/main.py` 里「迁移失败只告警」的口径一致。
    """
    target = engine if engine is not None else default_engine
    try:
        with target.begin() as connection:
            connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except SQLAlchemyError as exc:
        logger.warning("pgvector 扩展不可用（%s）：知识库表 document_chunks 建不出来，"
                       "请用 pgvector/pgvector 镜像，或在库里手工执行 CREATE EXTENSION vector", exc)
        return False
    return True


def ensure_schema(engine: Engine | None = None) -> list[str]:
    """建缺失的表 + 补缺失的列 + 去掉过时的唯一约束/唯一索引，返回本次真正执行的 DDL 描述（空列表 = 结构已是最新）。"""
    target = engine if engine is not None else default_engine
    applied: list[str] = []
    # 先装扩展、再建表：`document_chunks.embedding` 是 `VECTOR(384)`，扩展不在时
    # `create_all()` 会直接抛 `type "vector" does not exist`（且只在空库上抛）。
    if not has_pgvector(target) and ensure_pgvector(target):
        applied.append("pgvector 扩展")
    Base.metadata.create_all(bind=target)
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
        for table, name, columns, _why in INDEXES:
            exists = connection.scalar(
                text("SELECT 1 FROM pg_indexes"
                     " WHERE schemaname = current_schema() AND tablename = :table AND indexname = :name"),
                {"table": table, "name": name})
            if exists:
                continue
            connection.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"))
            applied.append(f"{table}.{name}（索引）")
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


def missing_indexes(engine: Engine | None = None) -> list[str]:
    """对照模型与真库，列出「模型声明了、库里没有」的索引。

    和 `missing_columns()` 同一个道理，只是对象换成了索引：`create_all()` 只在**建表**那一刻
    顺带建索引，往模型里新加一个 `Index(...)` 而忘了写进 `INDEXES`，老库到上线那天还在顺序扫，
    而且**不会报错**——只能靠启动时点名 + 单测断言把它变成可见的。
    """
    target = engine if engine is not None else default_engine
    inspector = inspect(target)
    existing = set(inspector.get_table_names())
    gaps: list[str] = []
    for table in Base.metadata.sorted_tables:
        if table.name not in existing:
            gaps.append(f"{table.name}（整张表缺失）")
            continue
        names = {index["name"] for index in inspector.get_indexes(table.name)}
        gaps.extend(f"{table.name}.{index.name}（索引缺失）"
                    for index in sorted(table.indexes, key=lambda item: item.name)
                    if index.name not in names)
    return gaps
