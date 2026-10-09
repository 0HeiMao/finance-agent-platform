"""数据范围（部门权限）——把「这个账号能看到哪些数据」收敛到一处。

规则只有两条，财务数据与知识库共用：

- **能不能管**看账号上的管理员标识（`is_admin`），与数据范围无关；
- **能看哪些数据**看部门列表：有一个或多个部门 = 只看这些部门的明细，以及由这些明细重算出来的
  指标与预警；一个部门都没有的普通账号**什么都看不到**（不是「不受限」）。

⑳ 之前第二条只有「空 = 不受限」一种解释，权限也搭在上面（`is_admin()` = 没有部门限制）——
结果是任何账号把自己的部门清空就等于升权，自助注册的新账号更是一注册就是全库管理员。
现在「没配部门」对普通账号只表示「范围还没分配」，管理员标识由 `users.is_admin` 单独记。

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


def is_admin(user) -> bool:
    """账号能不能管（显式的管理员标识），与「能看哪些数据」是两件事。

    ⑳ 之前这里是 `not departments_of(user)`：权限搭在部门列表上，于是

    - 任何登录账号 `PATCH /auth/me {"departments": []}` 就能把自己升成管理员；
    - `POST /auth/register` 建的账号不带部门，**一注册就是**管理员（可读全库、可管用户）。

    现在只读 `User.is_admin` 这一位；老库按「原先不受部门限制 = 管理员」回填
    （见 `backend/migrations.py` 的 BACKFILLS），所以升级当天演示账号照旧是管理员。
    `getattr(..., False)` 兜底：单元测试与一次性脚本里会拿 SimpleNamespace 之类的假对象进来。
    """
    return bool(getattr(user, "is_admin", False))


def scope_of(user) -> Scope:
    """当前账号的数据范围。

    - 管理员**且**没配部门 → None（全公司口径；要看某个部门时用 `?department=` 收窄这一次请求）；
    - 配了部门（管理员也可以被收窄） → 那几本部门账，管理权不受影响；
    - 非管理员且没配部门 → 空元组：范围还没分配，**什么都看不到**（不是不受限）。
    """
    items = departments_of(user)
    return items or (None if is_admin(user) else ())


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
    if not scope:
        # 空范围不是「全公司」也不是「部门 」——文案里必须一眼看出是「还没分配」，
        # 否则受限账号会把「什么都看不到」误读成「数据没了」。
        return "未分配部门"
    if len(scope) == 1:
        return f"部门 {scope[0]}"
    head = "、".join(scope[:3])
    tail = "" if len(scope) <= 3 else f" 等 {len(scope)} 个部门"
    return f"部门 {head}{tail}"


def scope_slug(scope: Scope) -> str:
    """文件名里的口径片段：全部部门 -> all，单部门 -> 097，多部门 -> 018-097。

    空范围给 `none` 而不是空串：导出文件名是 `...-{scope_slug}.csv`，空串会多出一个
    连字符（`transactions-2026-07-.csv`），也让人分不清「没范围」和「模板拼错了」。
    """
    if scope is None:
        return "all"
    return "-".join(scope) or "none"


def scope_text(scope: Scope) -> str:
    """给人看/给审计详情存的口径文案：「全部部门」、「097」/「018、097」或「未分配部门」。

    空范围刻意**不**返回空串：审计详情里的 `scope` 字段空着，事后没人分得清是「全公司」
    还是「这个账号压根没范围」——而这两件事在 ⑳ 之后是不同的东西。
    """
    if scope is None:
        return "全部部门"
    return "、".join(scope) or scope_label(scope)


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
    if not scope:
        return ("当前账号还没有分配部门，看不到任何财务数据；知识库只看到没标部门的公共资料 —— "
                "数据范围由管理员在管理端的用户管理里分配。")
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
