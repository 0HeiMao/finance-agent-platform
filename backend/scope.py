"""数据范围（部门权限）——把「这个账号能看到哪些数据」收敛到一处。

规则只有一条，财务数据与知识库共用：

- 账号的部门列表为空 = 不受部门限制（财务管理员），看到全公司口径；
- 有一个或多个部门 = 只看这些部门的明细，以及由这些明细重算出来的指标与预警。

**为什么是一个列表而不是单个部门**：集团里一个分析员常同时管两三个部门，
以前只能靠建三个账号或干脆放开成不受限（那等于把别的部门也看光）。多部门是
「并集」语义：金额相加、比率由相加后的金额重算（比率不能相加，0.4 + 0.5 没有意义）。

财务侧的落法：`financial_metrics` / `financial_alerts` 按 `department` 分行存储，
`department IS NULL` 是全公司汇总行，`department='097'` 是该部门的口径行。
**任何读这两张表的地方都必须过 `scope_metric` / `scope_alert`**，否则会把部门行与汇总行混在一起加总
（这类错误不会报错，只会让数字悄悄变大，所以过滤器必须和查询写在一起）。
多部门账号**看不到**全公司汇总行：汇总行里含别的部门，看到它就等于绕开范围。

明细侧的落法：`scope_transactions` 给 `financial_transactions` 加部门条件；部门为空的行不属于任何
部门，受限账号看不到——这正是管理概览里「部门为空的数据行」要被单独体检出来的原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select

from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction

# None = 不受部门限制（读全公司汇总行 / 全部明细）；非空元组 = 只能看这些部门。
Scope = tuple[str, ...] | None

_RATIO_SCALE = Decimal("0.0001")


def departments_of(user) -> tuple[str, ...]:
    """账号的部门列表（去重保序）。

    优先读 `User.departments`；为空时回落到老的单值列 `User.department`——
    多部门上线前建的账号与新库都不需要数据迁移，行为与改造前完全一致。
    """
    raw = getattr(user, "departments", None)
    items: list[str] = []
    if isinstance(raw, (list, tuple)):
        for value in raw:
            text_value = str(value or "").strip()
            if text_value and text_value not in items:
                items.append(text_value)
    if not items:
        legacy = (getattr(user, "department", None) or "").strip()
        if legacy:
            items.append(legacy)
    return tuple(items)


def scope_of(user) -> Scope:
    """当前账号的数据范围；None 表示不受部门限制（财务管理员）。"""
    items = departments_of(user)
    return items or None


def scope_of_departments(departments: list[str] | tuple[str, ...] | None) -> Scope:
    """把一串部门名规整成 scope（去掉空白与重复项）。"""
    items: list[str] = []
    for value in departments or ():
        text_value = str(value or "").strip()
        if text_value and text_value not in items:
            items.append(text_value)
    return tuple(items) or None


def scope_label(scope: Scope) -> str:
    if scope is None:
        return "全公司"
    if len(scope) == 1:
        return f"部门 {scope[0]}"
    head = "、".join(scope[:3])
    tail = "" if len(scope) <= 3 else f" 等 {len(scope)} 个部门"
    return f"部门 {head}{tail}"


def scope_slug(scope: Scope) -> str:
    """文件名里的口径片段：全部部门 -> all，单部门 -> 097，多部门 -> 018-097。"""
    return "all" if scope is None else "-".join(scope)


def scope_text(scope: Scope) -> str:
    """给人看/给审计详情存的口径文案：「全部部门」或「097」/「018、097」。"""
    return "全部部门" if scope is None else "、".join(scope)


def row_label(department: str | None) -> str:
    """单行的口径文案（导出明细/指标时用：行上永远只有一个部门或「全公司」）。

    和 `scope_label` 分开是因为两者的入参不是一个东西：`scope_label` 收的是账号的
    范围（可能是几个部门的元组），这里收的是数据行自带的单个部门。
    """
    return "全公司" if not (department or "").strip() else f"部门 {department}"


def in_scope(scope: Scope, department: str | None) -> bool:
    """某个部门口径是否落在本范围内（None 表示全公司汇总行）。"""
    if scope is None:
        return department is None
    return bool(department) and department in scope


def scope_metric(statement: Select, scope: Scope) -> Select:
    """把 FinancialMetric 查询收窄到该账号的口径。"""
    if scope is None:
        return statement.where(FinancialMetric.department.is_(None))
    return statement.where(FinancialMetric.department.in_(scope))


def scope_alert(statement: Select, scope: Scope) -> Select:
    """把 FinancialAlert 查询收窄到该账号的口径。"""
    if scope is None:
        return statement.where(FinancialAlert.department.is_(None))
    return statement.where(FinancialAlert.department.in_(scope))


def scope_transactions(statement: Select, scope: Scope) -> Select:
    """按部门收窄明细。scope 为 None 时不过滤（全公司口径）。"""
    if scope is None:
        return statement
    return statement.where(FinancialTransaction.department.in_(scope))


def scope_payload(scope: Scope, *, requested: str | None = None) -> dict:
    """给前端的范围说明，页面顶部据此提示「你看到的是本部门口径」。

    `requested` 是本次请求实际带上的口径（管理员切换视角时才有值），用来区分
    「账号本身不受限」与「不受限账号主动切到了某部门」——两者的提示文案不同。
    """
    departments = list(scope or ())
    payload = {
        "department": departments[0] if len(departments) == 1 else "",
        "departments": departments,
        "label": scope_label(scope),
        "unrestricted": scope is None,
        "note": _scope_note(scope),
    }
    if requested is not None:
        payload["requested"] = True
        payload["note"] = f"正在按「部门 {requested}」口径查看（该部门行由程序按明细重算），不是全公司口径。"
    return payload


def _scope_note(scope: Scope) -> str:
    if scope is None:
        return "当前账号不受部门限制，看板与分析为全公司口径。"
    if len(scope) == 1:
        return (f"当前账号只看到「部门 {scope[0]}」的明细与据此重算的指标、预警；"
                "部门为空的数据行归全公司口径，不在本范围。")
    return (f"当前账号看到「{scope_label(scope)}」这几本部门账的合计（金额相加、比率按合计重算），"
            "不含全公司汇总行，也不含其他部门。")


def resolve_requested_scope(user, requested: str | None) -> tuple[Scope, str | None]:
    """解析请求里的 `department` 参数，返回 (生效口径, 违规说明)。

    - 不受限账号（没有部门）：可以指定任意一个部门，实现「管理员按部门查看」；
      不带参数则是不受限（读全公司汇总行）。
    - 多部门账号：不带参数 = 看自己这几个部门的并集；带其中一个部门 = 收窄到那一个；
      带别的部门（自己的范围之外）**一律视为越权**并返回错误说明——
      不能让一个受限账号靠加参数把范围放大到别的部门或全公司。
    """
    own = scope_of(user)
    wanted = (requested or "").strip() or None
    if own is None:
        return ((wanted,) if wanted else None), None
    if wanted is None:
        return own, None
    if wanted in own:
        return (wanted,), None
    return own, f"当前账号的数据范围是「{scope_label(own)}」，不能查看「部门 {wanted}」"


@dataclass(frozen=True)
class MetricTotals:
    """一批 FinancialMetric 行按期间聚合后的结果，字段名与模型对齐，便于沿用既有渲染代码。

    多部门的意义就在这里：`revenue_amount` 等金额是**相加**的，而毛利率/净利率是
    「相加后的金额」算出来的——直接把两行的比率相加会得到 0.8 这种看着像数字、
    实际毫无意义的结论。
    """

    period: str
    departments: tuple[str, ...]
    department: str | None
    revenue_amount: Decimal
    cost_amount: Decimal
    expense_amount: Decimal
    gross_profit_amount: Decimal
    net_profit_amount: Decimal
    gross_profit_ratio: Decimal
    net_profit_ratio: Decimal


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if not denominator:
        return Decimal(0)
    return (numerator / denominator).quantize(_RATIO_SCALE)


def metric_totals(rows, *, period_order: list[str] | None = None) -> list[MetricTotals]:
    """把指标行按期间聚合成「一期一条」。

    - 单行期间直接沿用库里存的比率（重算逻辑与 `api_analysis.calculate` 一致，
      但保留下来的值一定与页面/导出已经展示过的那份相等，避免末位跳动）；
    - 多行期间（多部门账号）金额相加、比率用相加后的金额重算。
    """
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.period, []).append(row)
    order = period_order if period_order is not None else sorted(grouped)
    totals: list[MetricTotals] = []
    for period in order:
        bucket = grouped.get(period)
        if not bucket:
            continue
        revenue = sum((row.revenue_amount or Decimal(0) for row in bucket), Decimal(0))
        cost = sum((row.cost_amount or Decimal(0) for row in bucket), Decimal(0))
        expense = sum((row.expense_amount or Decimal(0) for row in bucket), Decimal(0))
        gross = sum((row.gross_profit_amount or Decimal(0) for row in bucket), Decimal(0))
        net = sum((row.net_profit_amount or Decimal(0) for row in bucket), Decimal(0))
        if len(bucket) == 1:
            gross_ratio = Decimal(bucket[0].gross_profit_ratio or 0)
            net_ratio = Decimal(bucket[0].net_profit_ratio or 0)
        else:
            gross_ratio = _ratio(gross, revenue)
            net_ratio = _ratio(net, revenue)
        names = tuple(sorted({str(row.department or "") for row in bucket}))
        totals.append(MetricTotals(
            period=period,
            departments=tuple(name for name in names if name),
            department=(bucket[0].department if len(bucket) == 1 else None),
            revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
            gross_profit_amount=gross, net_profit_amount=net,
            gross_profit_ratio=gross_ratio, net_profit_ratio=net_ratio))
    return totals
