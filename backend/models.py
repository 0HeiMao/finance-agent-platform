from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # 部门可见性（多部门）：`departments` 是权威值，填了部门列表 = 只看这些部门的明细与据此
    # 重算的指标、预警（几本部门账相加），知识库则是「这些部门 + 没标部门的公共资料」。
    # 由管理员在 /admin/users 维护，不采信请求头里的自述部门。
    # **留空不再等于不受限**（见下面的 is_admin）：留空只表示「还没分配范围」。
    departments: Mapped[list | None] = mapped_column(JSONB)
    # 单值列保留给老客户端与导出/审计里的展示（口径文案只有一行），语义 = 第一个部门。
    # 读权限**不要**用它，一律走 backend/scope.py 的 scope_of()（它会优先读 departments，
    # 列表为空时回落到这一列，所以多部门上线前建的账号行为不变）。
    department: Mapped[str | None] = mapped_column(String(100))
    # 管理员标识（⑳）：权限是「能不能管」、部门是「能看哪些数据」，两件事分开记。
    # ⑳ 之前 `is_admin()` 的定义是「没有部门限制」，于是任何账号把自己的部门清空就等于升权
    # （自助注册的新账号更是天生如此，一注册就是全库管理员）。老库按「原先不受部门限制 =
    # 管理员」回填，演示账号因此完全不变；此后新注册的账号默认 false。
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False,
                                           server_default="false")
    # 会话版本号：JWT 是无状态的，签发以后服务端收不回来（这正是它「不可撤销」的含义）。
    # 令牌里带上签发时的 `ver`，每个请求都和这一列比一次：不相等就是「这个令牌已经作废」。
    # 加一 = 该账号**所有**已发出的令牌立刻失效（退出登录、管理员强制下线都走它）。
    # 默认 0 而不是 1：上线前签发的令牌没有 `ver` 声明，按 0 处理，所以升级不会把所有人踢下线。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # 登录失败锁定（账号级、落库而不是放内存）：`failed_attempts` 是「静默期内连续失败了几次」，
    # `locked_until` 是锁到什么时候，`last_failed_at` 用来判断上一串失败是不是已经过去太久、
    # 该从 0 重新数。退避公式与阈值见 backend/lockout.py；只有登录、成功登录、管理员解锁会改它们。
    failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
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
    # 单行更正/冲销。冲销**不删原行**：新增一条金额取负的冲销行（合计自然抵消），原行记
    # reversed_at/reversed_by 并在界面上显示成「已冲销」——删掉原行的话，冲销前后看板上的
    # 明细条数与凭证都对不上，也没法解释「这笔钱去哪了」。corrections 是这一行被改过几次。
    reversal_of_id: Mapped[int | None] = mapped_column(Integer, index=True)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by: Mapped[str | None] = mapped_column(String(100))
    corrections: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 受限账号的**每条**查询都要按部门过滤，再按交易日期圈出期间（看板、汇总、指标重算、
    # 导出都长这样），所以复合索引首列放 department、次列放 transaction_date：
    # 单查部门能用（最左前缀），部门+期间范围也能用。只有 transaction_date 那一个单列索引时，
    # 「本部门 2026 年 1~7 月」只能先按日期捞全公司再逐行丢——行数一多就是全表扫。
    # 注意：`create_all()` 只在**建表**时建索引，老库要补索引得靠 migrations.INDEXES（见那里）。
    __table_args__ = (
        Index("ix_financial_transactions_department_date", "department", "transaction_date"),
    )

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

class FinancialRollup(Base):
    """季度 / 年度的落库合计行：把若干个月的月口径指标加总后**存起来**。

    为什么单开一张表，而不是给 `financial_metrics` 加一个 `grain` 列：
    那张表的每一行都被预警重算、环比、预测趋势、报告、导出、完整性体检等十几处按「月口径」读，
    塞进季/年行以后「这一行到底是不是月行」就变成每个读点都得记得判断的隐式约定——
    漏一处就出假数（预测会把 "2026" 当成第 13 个月画进趋势线，导出会把季度合计当成一个月）。
    单开一张表 + 明写 `months`/`complete`，既有月口径读点一行不用改，落的是什么也一眼可查。

    `months` 记的是**真的被加进去的月份**（按这个口径有指标行的月份），`complete` 表示这几期
    是否凑齐了整个季度/年度：演示数据是 2026-04~10，2026 年度只有 7 个月，缺 5 个月就要说出来，
    否则「年度合计」看起来像全年的。
    """

    __tablename__ = "financial_rollups"
    id: Mapped[int] = mapped_column(primary_key=True)
    # month 不会出现在这张表里（月口径在 financial_metrics），只接受 quarter | year
    grain: Mapped[str] = mapped_column(String(7), index=True)
    # "2026-Q3" / "2026"，与 periods.py 的规范写法一致
    period: Mapped[str] = mapped_column(String(20), index=True)
    # 与指标表同一套口径语义：NULL = 全公司汇总行，有值 = 该部门口径行
    department: Mapped[str | None] = mapped_column(String(100), index=True)
    revenue_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    cost_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    expense_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    gross_profit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    net_profit_amount: Mapped[Decimal] = mapped_column(Numeric(18, 2), default=0)
    # 比率列与指标表同样用 Numeric(20, 4)：部门口径下收入极小、费用很大时比率能冲到 10^6 以上
    gross_profit_ratio: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    net_profit_ratio: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    # 真的被求和的月份列表，如 ["2026-07","2026-08","2026-09"]
    months: Mapped[list | None] = mapped_column(JSONB)
    month_count: Mapped[int] = mapped_column(default=0)
    complete: Mapped[bool] = mapped_column(default=False, index=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

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

    规则可以**按部门/期间分档**：`department` / `period` 为空表示「所有部门 / 所有期间」。
    同一个 `alert_type` 在一次重算里只取最具体的一条（期间+部门 > 单维 > 全公司全期间），
    所以 `alert_type` 不再唯一——默认那两条就是 scope 全空的规则。
    """
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    alert_type: Mapped[str] = mapped_column(String(100), index=True)
    metric_name: Mapped[str] = mapped_column(String(100))
    comparator: Mapped[str] = mapped_column(String(10), default="gt")
    threshold_value: Mapped[Decimal] = mapped_column(Numeric(20, 4), default=0)
    level: Mapped[str] = mapped_column(String(20), default="high")
    # 分档维度：空 = 不限定（全公司 / 全期间）
    department: Mapped[str | None] = mapped_column(String(100))
    period: Mapped[str | None] = mapped_column(String(7))
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

    代价写在 README 里：跨进程的**派发**仍然没有（任务只在提交它的那个进程里执行），
    多副本部署需要换成真正的外部队列。⑱ 起「抢占」与「重启接续」本身是跨进程安全的：
    执行前要先抢一条会话级 advisory lock（`backend/locks.py` 的 `job_lock_key`）+ 条件更新
    `status='queued'`，启动接续也先抢一把全局锁、并且只重排那些**执行权锁没人持有**的
    `running` 行（真有人正在跑的不动），见 `jobs.run_job()` / `jobs.resume_interrupted_jobs()`。
    进程重启时还挂在 `queued`/`running` 的记录会被**重新排队重跑**
    （所以处理函数必须幂等：同一份 payload 跑两遍不能多删一行、也不能报「已经被撤销了」），
    已经用完重试预算的才标记为失败。
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
    # ⑩ 幂等：调用方给的去重键（HTTP 头 `Idempotency-Key`，没有就留空）。
    # 唯一性是**部分**的（见 __table_args__）：同一个键同时只能有一条「排队中/运行中/成功过」的任务，
    # 但失败过的不算 —— 失败之后本来就该允许拿同一个键重新提交。
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    # 重试：`attempts` 是已经试过几次（`run_job` 一进来就加一），`max_attempts` 是预算，
    # `next_attempt_at` 是下一次重试的时间（只有 status=queued 且它非空时才是「等重试」）。
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3, server_default="3")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ⑱ 谁正在跑这条任务（`jobs.WORKER_ID`：主机名-pid-随机串）。只在 `status=running` 时有意义，
    # 跑完/判失败/放回队列都会清空 —— 它不是「最后谁跑过」的审计，而是「现在这条 running 归谁」，
    # 多副本下配合 `locks.job_lock_key` 就能解释「任务卡在运行中」到底是哪个进程不在了。
    worker_id: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        # 「先查再插」在并发下会漏：两个请求同时查空、各自插一条。这条部分唯一索引把它变成
        # 数据库层面的保证，撞上时 `jobs.submit()` 把已有那条读回来，接口照样返回同一个任务 id。
        # 谓词里没有 failed —— 失败的任务不占键，人工重试与重新提交都不该被一条旧记录挡住。
        Index(
            "uq_background_jobs_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'succeeded')"),
        ),
    )


class AlertChannel(Base):
    """预警外发通道（⑭）：一条「预警生成后往哪里发」的配置。

    为什么要有通道表而不是几个常量：外发目标（企业微信/钉钉机器人、内部告警网关、邮件组）
    是**部署环境的信息**，写在代码里等于每次换环境都要改代码；而且「发什么级别、发哪个部门」
    本来就是业务规则，得让管理员在界面上调。

    `secret` 只用于 webhook 的 HMAC-SHA256 签名（收到方据此确认这条通知真的来自本平台），
    **任何接口都不会把它读回去**（列表只给 `has_secret`）——签名密钥泄露就等于任何人都能伪造通知。
    邮件通道不用它，SMTP 口令在 .env 里（见 backend/config.py）。
    """
    __tablename__ = "alert_channels"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    # webhook / email（取值见 backend/alerts_outbound.py 的 KINDS）
    kind: Mapped[str] = mapped_column(String(20), default="webhook")
    # webhook 是 URL，email 是收件人（逗号分隔）
    target: Mapped[str] = mapped_column(String(500))
    # 只发这个级别及以上：low < medium < high（LEVEL_ORDER）。默认 low = 全都发。
    min_level: Mapped[str] = mapped_column(String(20), default="low")
    # 空 = 不限部门（含全公司口径的预警）；有值 = 只发这个部门口径的预警
    department: Mapped[str | None] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(default=True)
    secret: Mapped[str | None] = mapped_column(String(200))
    # 最近一次投递结果缓存（列表页直接显示，不必每次去扫 台账）
    last_result: Mapped[str | None] = mapped_column(String(20))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class AlertDelivery(Base):
    """投递单（⑭）：一条「某条预警送往某个通道」的持久记录，也是重试的单位。

    三个刻意的选择：

    1. **按逻辑键去重，不按预警行 id。** 重算是「先删旧行、再建新行」（见 api_analysis.calculate），
       行 id 每轮都变；拿行 id 去重等于每重算一次就把同一批通知再轰一遍。
       所以唯一键是 `(alert_key, channel_id)`，`alert_key` = 期间|部门|类型|指标名。
    2. **自带 payload 快照。** 投递失败要重试，而重试时那条预警很可能已经被下一次重算删掉了；
       没有快照就只能发一条「预警不存在」，或者更糟——发出被改过的内容。
    3. **失败重试复用后台任务（backend/jobs.py）**，不自己写定时器：退避、预算、人工重试、
       进程重启接续都是现成的，投递单只需要说清「这条还没发成功」。
    """
    __tablename__ = "alert_deliveries"
    id: Mapped[int] = mapped_column(primary_key=True)
    # f"{period}|{department or ''}|{alert_type}|{metric_name}"，见 alerts_outbound.alert_key()
    alert_key: Mapped[str] = mapped_column(String(300), index=True)
    # 只作追溯：预警行会被重算删掉，所以这不是外键，也不保证还能查到
    alert_id: Mapped[int | None] = mapped_column(Integer)
    # 通道被删除时置空（ondelete=SET NULL）而不是级联删投递单：台账是「发过什么」的证据
    channel_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("alert_channels.id", ondelete="SET NULL"), index=True)
    # 快照：通道改名/改地址之后，台账里仍能看到当时是发给谁、什么类型的通道
    channel_name: Mapped[str] = mapped_column(String(100))
    channel_kind: Mapped[str] = mapped_column(String(20))
    period: Mapped[str] = mapped_column(String(20), index=True)
    department: Mapped[str | None] = mapped_column(String(100))
    alert_type: Mapped[str] = mapped_column(String(100))
    metric_name: Mapped[str] = mapped_column(String(100))
    level: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    # pending / sent / failed（取值见 alerts_outbound.STATUSES）
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    response_status: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())
    __table_args__ = (
        # 「同一条预警 + 同一个通道」只发一次：并发重算/补发时靠数据库拦，而不是靠先查后插。
        Index("uq_alert_deliveries_key_channel", "alert_key", "channel_id", unique=True),
    )


class AgentSession(Base):
    """Agent 会话（⑮）：一次「和财务助手聊下去」的容器。

    为什么要落库而不是只放在浏览器里：会话是**证据链**的一部分——一条结论是从哪些检索片段、
    哪个期间的财务数据推出来的，出了问题要能回看；token 用量也得有个按人/按天的落点，
    否则「这个月问答花了多少」只能靠日志。前端只拿会话 id，正文都在库里。

    `title` 由第一条提问派生（见 backend/agent_sessions.py 的 derive_title），
    空标题的会话在列表里显示成「未命名会话」。
    """
    __tablename__ = "agent_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    # 用户被删时连同会话一起删（ondelete=CASCADE）：这是个人问答记录，不是审计证据
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(120), default="")
    # 本会话问答时生效的数据范围（受限账号的部门口径，逗号分隔）；用于列表里解释"为什么只看到这些"
    scope: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(),
                                                 onupdate=func.now())


class AgentMessage(Base):
    """Agent 会话里的一条消息（⑮）：提问、回答、以及回答的用量与出处。

    三次刻意的选择：

    1. **先落"占位行"再流式。** 流式回答要能中途停止、还可能要跨进程重连，
       没有落库的行就只能靠内存里的字符串；所以回答一开始就写一行 `status="streaming"`，
       结束时才把正文、来源、token 写回去。
    2. **`status` 覆盖失败与中断。** 生成到一半被用户停掉是 `stopped`（有正文，是有效结果），
       进程崩了是 `interrupted`（读取时把超时的 streaming 归一成它），模型不可用是 `error`——
       这三种在界面上是完全不同的提示，不能都塞进"空回答"。
    3. **token 记来源。** `usage_source="api"` 是模型报的真数，`"estimated"` 是按字数估的；
       把两者混在一起报账，等于把估算当账单。
    """
    __tablename__ = "agent_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("agent_sessions.id", ondelete="CASCADE"), index=True)
    # 冗余一份用户：按人统计用量时不必回连会话表
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # user / assistant（取值见 backend/agent_sessions.py 的 ROLES）
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text, default="")
    # streaming / ok / stopped / error / interrupted（取值见 agent_sessions.STATUSES）
    status: Mapped[str] = mapped_column(String(20), default="ok", index=True)
    # 出错时的原话：状态只说明"失败了"，回看时得知道是模型不可用还是证据阶段就炸了
    error: Mapped[str | None] = mapped_column(Text)
    # 检索片段（[{type, name, ...}]），与 done 事件里的 sources 同源
    sources: Mapped[list] = mapped_column(JSONB, default=list)
    period: Mapped[str | None] = mapped_column(String(20))
    knowledge_chunks: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    scope: Mapped[str | None] = mapped_column(String(100))
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # api / estimated（哪个都拿不到时是 None）
    usage_source: Mapped[str | None] = mapped_column(String(20))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
