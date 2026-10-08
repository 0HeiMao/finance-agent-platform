"""明细单行更正与冲销：导入出错时的两条窄修法。

为什么需要它：导入是财务数据唯一的写入入口，过去导错一个数字只能「整批撤销再重导」——
批次里其它九千行明明是好的。这里补上两条最小路径：

- **更正**（`PATCH`）：就地改字段，`corrections` 加一，审计里留 before/after 的差值。
- **冲销**（`POST /reverse`）：**不删原行**，新增一条金额取负的冲销行，原行标记
  `reversed_at`/`reversed_by`。删行会让「冲销前后明细条数与原始凭证」对不上，也没法回答
  「这笔钱去哪了」；负数行参与合计，数学上自然抵消，所以指标仍然等于真实账。

两条路径都遵守数据范围：受限账号只能动自己部门的行，`department` 为空的全公司行只有
不受部门限制的账号能动（与 `/analysis/alerts/{id}/transactions` 的越权口径一致）。

改完必须重算受影响期间并刷新季/年落库——否则看板上还是旧指标，而明细已经变了，
这种「数字对不上」是最难查的一类问题（也是 `backend/batch_ops.py` 里已经踩过的坑）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from backend import audit, locks, rollups
from backend import periods as period_mod
from backend.api_analysis import calculate
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction
from backend.scope import Scope, scope_transactions


class CorrectionError(ValueError):
    """更正请求本身有问题（接口回 422，消息面向使用者）。"""


class RowNotFound(LookupError):
    """这一行不存在（接口回 404）。"""


class RowForbidden(PermissionError):
    """这一行不在账号的数据范围内（接口回 403）。"""


class AlreadyReversed(ValueError):
    """已经冲销过、或这行本身就是冲销行（接口回 409）。"""


# (字段名, 中文标签)：金额列可以改，但要按导入时同一套规则校验（去千分位、两位小数、上限）。
AMOUNT_FIELDS: tuple[tuple[str, str], ...] = (
    ("revenue_amount", "收入金额"),
    ("cost_amount", "成本金额"),
    ("expense_amount", "费用金额"),
    ("cash_in_amount", "现金流入"),
    ("cash_out_amount", "现金流出"),
)
TEXT_FIELDS: tuple[tuple[str, str], ...] = (
    ("account_code", "科目编码"),
    ("account_name", "科目名称"),
    ("department", "部门"),
    ("description", "摘要"),
)
TEXT_LIMITS = {"account_code": 50, "account_name": 200, "department": 100, "description": 2000}
AMOUNT_LIMIT = Decimal("10000000000000000")


def parse_amount(raw: object, label: str) -> Decimal:
    """与导入同一套金额规则：去千分位、四舍五入到分、拒绝非有限数、拒绝超大数。"""
    text = str(raw if raw is not None else "").strip().replace(",", "").replace("，", "") or "0"
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise CorrectionError(f"{label}不是有效金额：{text}") from exc
    if not value.is_finite():
        raise CorrectionError(f"{label}不是有效金额：{text}")
    value = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(value) >= AMOUNT_LIMIT:
        raise CorrectionError(f"{label}超出范围")
    return value


def parse_date(raw: object) -> datetime:
    """只接受不带时区的 ISO 日期或日期时间（与导入的「交易日期不能包含时区」一致）。"""
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise CorrectionError("交易日期不能为空")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CorrectionError(f"交易日期无法解析：{text}") from exc
    if value.tzinfo is not None:
        raise CorrectionError("交易日期不能包含时区")
    return value


def parse_text(raw: object, field: str) -> str:
    text = "" if raw is None else str(raw).strip()
    limit = TEXT_LIMITS[field]
    if len(text) > limit:
        raise CorrectionError(f"{dict(TEXT_FIELDS)[field]}最长 {limit} 字")
    if field == "account_name" and not text:
        raise CorrectionError("科目名称不能为空")
    return text


def row_in_scope(scope: Scope, row: FinancialTransaction) -> bool:
    """这一行是否落在账号的数据范围内。

    受限账号看不含部门的全公司行（那些行不属于任何部门，只有不受限账号能动），
    与 `scope_transactions` 的语义保持一致——否则「看得到」与「改得动」会两套口径。
    """
    department = (row.department or "").strip()
    if scope is None:
        return True
    return bool(department) and department in scope


def snapshot(row: FinancialTransaction) -> dict:
    """审计用的行快照（只记会变的字段，金额转成字符串避免浮点噪声）。"""
    data = {
        "transaction_date": row.transaction_date.strftime("%Y-%m-%d"),
        "import_batch": row.import_batch,
    }
    for field, _label in TEXT_FIELDS:
        data[field] = getattr(row, field) or ""
    for field, _label in AMOUNT_FIELDS:
        data[field] = f"{getattr(row, field):.2f}"
    return data


def apply_changes(row: FinancialTransaction, payload: dict) -> tuple[dict, dict]:
    """把请求里出现过的字段写进行对象，返回 (改动前, 改动后) 两个只含变动字段的快照。"""
    changes = {key: value for key, value in payload.items() if value is not None}
    if not changes:
        raise CorrectionError("没有要更正的字段")
    unknown = set(changes) - {name for name, _ in AMOUNT_FIELDS} - {name for name, _ in TEXT_FIELDS} - {"transaction_date"}
    if unknown:
        raise CorrectionError(f"不认识的字段：{', '.join(sorted(unknown))}")

    before: dict = {}
    after: dict = {}
    if "transaction_date" in changes:
        before["transaction_date"] = row.transaction_date.strftime("%Y-%m-%d")
        row.transaction_date = parse_date(changes["transaction_date"])
        after["transaction_date"] = row.transaction_date.strftime("%Y-%m-%d")
    for field, label in TEXT_FIELDS:
        if field not in changes:
            continue
        before[field] = getattr(row, field) or ""
        setattr(row, field, parse_text(changes[field], field))
        after[field] = getattr(row, field) or ""
        _ = label
    for field, label in AMOUNT_FIELDS:
        if field not in changes:
            continue
        before[field] = f"{getattr(row, field):.2f}"
        setattr(row, field, parse_amount(changes[field], label))
        after[field] = f"{getattr(row, field):.2f}"
    if before == after:
        raise CorrectionError("改动的值与原值一致，没有需要保存的内容")
    return before, after


def period_of(row: FinancialTransaction) -> str:
    return row.transaction_date.strftime("%Y-%m")


def recalculate_periods(db: Session, periods: set[str] | list[str]) -> dict:
    """重算受影响的期间；期间里已经没有明细了就连指标与预警一起清掉。

    这段逻辑与 `backend/batch_ops.revert_batch` 里的是同一件事（「某期的行集合变了」），
    所以抽在这里由两边共用：更正可能把一行从 A 月挪到 B 月、冲销会新增负数行，
    不留神就会留下「明细没了、指标还在」的幽灵报表。
    """
    recalculated: list[str] = []
    cleared: list[str] = []
    # 期间锁：下面「这一期还剩几行 → 有则重算、无则清掉指标」是一段读改写。不加锁的话，
    # 并发的两个请求可能各自数出「还剩 0 行」或各自读到对方的旧数据，写出错误的指标/落库行。
    # 锁按期间名排序后一次取全（循环里逐个取会让两个多期请求各持一半、互等对方而 deadlock）。
    locks.lock_periods(db, locks.period_lock_set(periods))
    # SessionLocal 是 autoflush=False：调用方刚改过的字段还在内存里（更正日期就是这种），
    # 不先落盘，下面的「这一期还剩几行」会数出错的口径（把有数据的一期当成空的清掉）。
    db.flush()
    for period in sorted(set(periods)):
        remaining = db.scalar(
            select(func.count()).select_from(FinancialTransaction).where(
                # 用区间而不是 to_char(...)=period：见 period_mod.period_range 的说明
                # （这个函数名与入参同名，所以这里显式走模块名，避免遮蔽自己）。
                period_mod.period_range(FinancialTransaction.transaction_date, period))
        ) or 0
        if remaining:
            calculate(db, period, commit=False)
            recalculated.append(period)
        else:
            db.execute(
                FinancialMetric.__table__.delete().where(FinancialMetric.period == period))
            db.execute(
                FinancialAlert.__table__.delete().where(FinancialAlert.period == period))
            rollups.refresh_for_months(db, [period])
            cleared.append(period)
    return {"recalculated": recalculated, "cleared": cleared}


def get_row(db: Session, row_id: int) -> FinancialTransaction:
    row = db.get(FinancialTransaction, row_id)
    if row is None:
        raise RowNotFound("明细不存在")
    return row


def serialize(row: FinancialTransaction) -> dict:
    data = {
        "id": row.id,
        "transaction_date": row.transaction_date.strftime("%Y-%m-%d"),
        "period": period_of(row),
        "import_batch": row.import_batch,
        "reversed": row.reversed_at is not None,
        "reversed_at": row.reversed_at.strftime("%Y-%m-%d %H:%M:%S") if row.reversed_at else "",
        "reversed_by": row.reversed_by or "",
        "reversal_of_id": row.reversal_of_id,
        "is_reversal": row.reversal_of_id is not None,
        "corrections": row.corrections or 0,
    }
    for field, _label in TEXT_FIELDS:
        data[field] = getattr(row, field) or ""
    for field, _label in AMOUNT_FIELDS:
        data[field] = float(getattr(row, field) or 0)
    return data


def list_rows(db: Session, scope: Scope, *, period: str | None = None, department: str | None = None,
              batch: str | None = None, query: str | None = None,
              limit: int = 50, offset: int = 0) -> tuple[list[FinancialTransaction], int]:
    """范围 + 筛选后的明细分页。`total` 是**筛选后**的总数（与 /admin/* 的分页口径一致）。"""
    statement = scope_transactions(select(FinancialTransaction), scope)
    if period:
        statement = statement.where(
            period_mod.period_range(FinancialTransaction.transaction_date, period))
    if department:
        statement = statement.where(FinancialTransaction.department == department)
    if batch:
        statement = statement.where(FinancialTransaction.import_batch == batch)
    if query:
        like = f"%{query}%"
        statement = statement.where(or_(FinancialTransaction.account_name.ilike(like),
                                        FinancialTransaction.account_code.ilike(like),
                                        FinancialTransaction.description.ilike(like)))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.scalars(
        statement.order_by(FinancialTransaction.transaction_date.desc(),
                           FinancialTransaction.id.desc()).limit(limit).offset(offset)).all()
    return list(rows), int(total)


def periods_in(db: Session, scope: Scope) -> list[str]:
    """筛选下拉用的期间清单（按范围收敛，部门账号看不到别人的期间数）。"""
    values = db.scalars(
        scope_transactions(select(func.distinct(func.to_char(
            FinancialTransaction.transaction_date, "YYYY-MM"))), scope)).all()
    return sorted((value for value in values if value), reverse=True)


def correct(db: Session, row_id: int, payload: dict, *, scope: Scope, actor: str,
            request: Request | None = None) -> dict:
    """更正一行：校验 → 就地把字段改掉 → 重算受影响期间 → 审计 → 提交。"""
    row = get_row(db, row_id)
    if not row_in_scope(scope, row):
        raise RowForbidden("该明细不在当前账号的数据范围内")
    if row.reversed_at is not None:
        raise AlreadyReversed("这一行已经冲销，冲销后再改原行会让账更乱；请先撤销冲销（如果有）或重新导入")
    # 旧期间必须在改字段之前取：日期被改到别的月份时，它才是「这一行原来算在哪一期」。
    old_period = period_of(row)
    # 期间锁要在**改行之前**取，而且要把新旧两期**一次性**锁全：锁的意义是「这一期的行集合
    # 不再变化」，改完才锁的话另一个正在重算这一期的请求仍会读到改到一半的行集合；
    # 分两次锁（先锁旧期、改完再锁新期）则可能与「反向搬运」的另一个请求互相等成 deadlock。
    targets = {old_period}
    if payload.get("transaction_date") is not None:
        targets.add(parse_date(payload["transaction_date"]).strftime("%Y-%m"))
    locks.lock_periods(db, locks.period_lock_set(targets))
    before, after = apply_changes(row, payload)
    row.corrections = (row.corrections or 0) + 1
    new_period = period_of(row)
    # 日期可能被改到别的月份，所以两个期间都要重算（旧期间可能因此空了）。
    refreshed = recalculate_periods(db, {old_period, new_period})
    audit.record(db, actor, "transaction.update", target_type="transaction", target_id=str(row.id),
                 detail={"before": before, "after": after, "periods": sorted({old_period, new_period})},
                 request=request)
    db.commit()
    return {"data": serialize(row), **refreshed}


def reverse(db: Session, row_id: int, *, scope: Scope, actor: str, reason: str | None = None,
            request: Request | None = None) -> dict:
    """冲销一行：新增一条负数行并把原行标记成已冲销（不删原行）。"""
    row = get_row(db, row_id)
    if not row_in_scope(scope, row):
        raise RowForbidden("该明细不在当前账号的数据范围内")
    if row.reversal_of_id is not None:
        raise AlreadyReversed("这一行本身就是冲销行，不能再次冲销")
    if row.reversed_at is not None:
        raise AlreadyReversed("这一行已经冲销过")

    now = datetime.now(UTC)
    # 冲销行与原行在同一个月，锁这一期就够；同样要在**改行/加行之前**取锁。
    locks.lock_periods(db, locks.period_lock_set([period_of(row)]))
    reversal = FinancialTransaction(
        transaction_date=row.transaction_date,
        account_code=row.account_code,
        account_name=row.account_name,
        department=row.department,
        description=f"冲销：{row.description or row.account_name}",
        revenue_amount=-row.revenue_amount,
        cost_amount=-row.cost_amount,
        expense_amount=-row.expense_amount,
        cash_in_amount=-row.cash_in_amount,
        cash_out_amount=-row.cash_out_amount,
        # 冲销行留在同一个批次里：批次号是文件内容哈希，造一个假批次会让「撤销批次」
        # 只收回一半的行；同一个批次 → 撤销时原行与冲销行一起消失，净额仍然为 0。
        import_batch=row.import_batch,
        reversal_of_id=row.id,
    )
    db.add(reversal)
    row.reversed_at = now
    row.reversed_by = actor
    period = period_of(row)
    refreshed = recalculate_periods(db, {period})
    # 冲销行的 id 要 flush 之后才有；先落盘再记一条审计，避免同一次冲销留两条记录。
    db.flush()
    audit.record(db, actor, "transaction.reverse", target_type="transaction", target_id=str(row.id),
                 detail={"period": period, "reversal_id": reversal.id, "reason": reason or "",
                         "revenue_amount": f"{row.revenue_amount:.2f}"}, request=request)
    db.commit()
    return {"original": serialize(row), "reversal": serialize(reversal), **refreshed}
