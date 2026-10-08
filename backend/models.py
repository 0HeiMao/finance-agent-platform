from datetime import datetime
from decimal import Decimal
from sqlalchemy import DateTime, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from backend.db import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 部门可见性（多部门）：`departments` 是权威值，留空 = 不受部门限制（财务管理员）；
    # 填了部门列表 = 只看这些部门的明细与据此重算的指标、预警（几本部门账相加），
    # 知识库则是「这些部门 + 没标部门的公共资料」。由管理员在 /admin/users 维护，
    # 不采信请求头里的自述部门。
    departments: Mapped[list | None] = mapped_column(JSONB)
    # 单值列保留给老客户端与导出/审计里的展示（口径文案只有一行），语义 = 第一个部门。
    # 读权限**不要**用它，一律走 backend/scope.py 的 scope_of()（它会优先读 departments，
    # 列表为空时回落到这一列，所以多部门上线前建的账号行为不变）。
    department: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class FinancialTransaction(Base):
    __tablename__ = "financial_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    transaction_date: Mapped[datetime] = mapped_column(DateTime, index=True)
    account_code: Mapped[str | None] = mapped_column(String(50))
    account_name: Mapped[str] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text)
    revenue_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    expense_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    cash_in_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    cash_out_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    import_batch: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class FinancialMetric(Base):
    __tablename__ = "financial_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    # 指标按口径分行：NULL = 全公司汇总行；有值 = 该部门的口径行（只看本部门的账号读这一行）。
    # 读这张表的地方必须过 backend/scope.py 的 scope_metric，否则会把部门行和汇总行一起加总。
    department: Mapped[str | None] = mapped_column(String(100), index=True)
    revenue_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    expense_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    gross_profit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    net_profit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    # 比率列用 Numeric(20, 4) 而不是 (10, 4)：部门口径下「收入极小、费用很大」的分母会让比率
    # 冲到 10^6 以上（实测 2026-05 某部门 revenue=0.01 级别），(10,4) 只留 6 位整数会直接
    # NumericValueOutOfRange 让整次重算失败。比率仍是四位小数，只是整数位留足。
    gross_profit_ratio: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    net_profit_ratio: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class FinancialAlert(Base):
    __tablename__ = "financial_alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(20), index=True)
    # 与 FinancialMetric 同一口径：NULL = 全公司预警，有值 = 该部门口径预警。
    department: Mapped[str | None] = mapped_column(String(100), index=True)
    alert_type: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String(20))
    metric_name: Mapped[str] = mapped_column(String(100))
    current_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    # 处理闭环：重算某期间时这两组字段会被**按口径+类型继承**过来（见 api_analysis.calculate），
    # 否则用户点过「已处理」的结论会在下一次导入/撤销后被无声抹掉。
    review_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    review_note: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(100))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 与 review_status 冗余，但报告/导出/PowerBI 老接的是这一列，保持「待处理 = True」的语义。
    needs_human_review: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class AlertRule(Base):
    """预警规则：阈值与开关放在库里而不是常量里，改完立即对下一次重算生效。

    只支持「按某个指标名与阈值比较」这一种形态（`comparator` 目前只有 `gt`），
    因为平台里两条规则都是这个形状；写成通用规则引擎反而没法验证。
    """
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    metric_name: Mapped[str] = mapped_column(String(100))
    comparator: Mapped[str] = mapped_column(String(10), default="gt")
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    level: Mapped[str] = mapped_column(String(20), default="high")
    # 该指标为 0（或不存在）时这条规则不适用：收入为 0 时「成本占收入比」没有意义。
    require_metric: Mapped[str | None] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())

class AuditLog(Base):
    """审计日志：谁在什么时候对系统做了写操作。

    记的是结构化事实（动作、对象、范围、条数），**不记**密码、令牌、文件内容和明细金额；
    `backend/audit.py` 的 `_scrub` 会再兜一层，把这些键和超长字符串丢掉。
    撤销批次、删账号、删知识库、改预警规则这些操作在此之前没有任何留痕。
    """
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    actor: Mapped[str] = mapped_column(String(100), index=True)
    # 动作发生时机器的部门归属（账号部门可能事后被改，所以快照一份）
    actor_department: Mapped[str | None] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60), index=True)
    target_type: Mapped[str] = mapped_column(String(40), default="")
    target_id: Mapped[str | None] = mapped_column(String(120))
    detail: Mapped[dict] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(String(60))


class BackgroundJob(Base):
    """后台任务：把「撤销批次 + 重算所有受影响期间」这类长活儿移出请求。

    为什么不是 Celery：这个部署没有 Redis（`redis` 包本身都没装），装了 broker 也没有 worker 可跑，
    `backend/celery_app.py` 与 `backend/tasks.py` 是**从未被执行过**的脚手架——任务提交不出去，
    `run_monthly_analysis` 也没人调用。真需要异步的只有「撤销批次」这一条（期间多、每期全量重算），
    于是用「进程内线程 + 这张表」落地：任务状态可查询、失败有记录，不再假装有一个消息队列。

    代价写在 README 里：单进程内存队列，进程重启时未完成的任务由 `jobs.reap_stale_jobs()` 标记失败，
    多副本部署需要换成真正的外部队列。
    """
    __tablename__ = "background_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    # queued / running / succeeded / failed
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    actor: Mapped[str] = mapped_column(String(100), default="")
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
