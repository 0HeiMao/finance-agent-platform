"""数据范围（部门权限）——把「这个账号能看到哪些数据」收敛到一处。

规则只有一条，财务数据与知识库共用：

- `User.department` 为空 = 不受部门限制（财务管理员），看到全公司口径；
- 有值 = 只看该部门的明细，以及由这些明细重算出来的指标与预警。

财务侧的落法：`financial_metrics` / `financial_alerts` 按 `department` 分行存储，
`department IS NULL` 是全公司汇总行，`department='097'` 是该部门的口径行。
**任何读这两张表的地方都必须过 `scope_metric` / `scope_alert`**，否则会把部门行与汇总行混在一起加总
（这类错误不会报错，只会让数字悄悄变大，所以过滤器必须和查询写在一起）。

明细侧的落法：`scope_transactions` 给 `financial_transactions` 加部门条件；部门为空的行不属于任何
部门，受限账号看不到——这正是管理概览里「部门为空的数据行」要被单独体检出来的原因。
"""

from __future__ import annotations

from sqlalchemy import Select

from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction


def scope_of(user) -> str | None:
    """当前账号的数据范围；None 表示不受部门限制（财务管理员）。"""
    return (getattr(user, "department", None) or "").strip() or None


def scope_label(department: str | None) -> str:
    return "全公司" if department is None else f"部门 {department}"


def scope_metric(statement: Select, department: str | None) -> Select:
    """把 FinancialMetric 查询收窄到该账号的口径。"""
    if department is None:
        return statement.where(FinancialMetric.department.is_(None))
    return statement.where(FinancialMetric.department == department)


def scope_alert(statement: Select, department: str | None) -> Select:
    """把 FinancialAlert 查询收窄到该账号的口径。"""
    if department is None:
        return statement.where(FinancialAlert.department.is_(None))
    return statement.where(FinancialAlert.department == department)


def scope_transactions(statement: Select, department: str | None) -> Select:
    """按部门收窄明细。department 为 None 时不过滤（全公司口径）。"""
    if department is None:
        return statement
    return statement.where(FinancialTransaction.department == department)


def scope_payload(department: str | None, *, requested: str | None = None) -> dict:
    """给前端的范围说明，页面顶部据此提示「你看到的是本部门口径」。

    `requested` 是本次请求实际带上的口径（管理员切换视角时才有值），用来区分
    「账号本身不受限」与「不受限账号主动切到了某部门」——两者的提示文案不同。
    """
    payload = {
        "department": department or "",
        "label": scope_label(department),
        "unrestricted": department is None,
        "note": ("当前账号不受部门限制，看板与分析为全公司口径。" if department is None
                 else f"当前账号只看到「部门 {department}」的明细与据此重算的指标、预警；"
                      "部门为空的数据行归全公司口径，不在本范围。"),
    }
    if requested is not None:
        payload["requested"] = True
        payload["note"] = f"正在按「部门 {requested}」口径查看（该部门行由程序按明细重算），不是全公司口径。"
    return payload


def resolve_requested_scope(user, requested: str | None) -> tuple[str | None, str | None]:
    """解析请求里的 `department` 参数，返回 (生效口径, 违规说明)。

    - 不受限账号（部门为空）：可以指定任意部门，实现「管理员按部门查看」；
    - 受限账号：只允许自己的部门（或省略），**其他任何值都视为越权**并返回错误说明，
      不能让一个受限账号靠加参数把范围放大到别的部门或全公司。
    """
    own = scope_of(user)
    wanted = (requested or "").strip() or None
    if own is None:
        return wanted, None
    if wanted is None or wanted == own:
        return own, None
    return own, f"当前账号的数据范围是「部门 {own}」，不能查看「{wanted}」"

