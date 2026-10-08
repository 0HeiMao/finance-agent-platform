"""预警规则：阈值、开关与比较方向放在库里（`alert_rules` 表），改完对下一次重算立即生效。

为什么要搬进库里：原来两个阈值是 `api_analysis.py` 里的常量（成本率 0.8、净利润 < 0），
财务口径按季度调整时只能改代码重启。搬进表以后管理页可以自己改，并且**改阈值本身也有痕迹**
（`updated_at` + 审计日志）。

规则形态刻意保持得很窄：`metric_name` 取一个指标值与 `threshold_value` 比较
（`comparator` 目前是 `gt` / `lt`），`require_metric` 指定「哪个指标为 0 时这条规则不适用」
（两条现成规则都要求收入非 0——收入为 0 时「成本占收入比」没有意义）。
写成通用表达式引擎反而没有可验证的边界。
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.db import Base
from backend.models import AlertRule, FinancialMetric

# 与改造前的常量语义逐字对齐：成本率 > 0.8、净利润 < 0（且都要有收入）。
# 对齐很重要——scripts/verify_crawled_import.py 会独立复算一遍期望预警，
# 语义只要漂移一点，那个 122 项断言的脚本就会红。
DEFAULT_RULES: list[dict] = [
    {"alert_type": "成本率异常", "metric_name": "cost_ratio", "comparator": "gt",
     "threshold_value": Decimal("0.8"), "level": "high", "require_metric": "revenue_amount",
     "description": "成本占收入超过 80%"},
    {"alert_type": "利润异常", "metric_name": "net_profit_amount", "comparator": "lt",
     "threshold_value": Decimal("0"), "level": "high", "require_metric": "revenue_amount",
     "description": "本期净利润为负"},
]

COMPARATORS = {"gt": "大于", "lt": "小于"}


def ensure_rules(db: Session) -> list[AlertRule]:
    """建表（老库没有这张表）并补齐默认规则，返回全部规则。

    建表放在这里而不是只放在 `scripts/init_pgvector_db.py`：老库升级时不会有人再跑一次初始化脚本，
    而管理页和重算都会调到这里，顺手建表最省事（`checkfirst=True` 是幂等的）。

    补默认规则时看的是「**全公司全期间**的那条在不在」，不是「这个类型有没有规则」：
    分档规则（部门 018 的成本率 0.85）不能顶替默认规则，否则那个类型一旦分档，
    其它部门/期间就没有兜底规则了。
    """
    Base.metadata.create_all(bind=db.get_bind(), tables=[AlertRule.__table__], checkfirst=True)
    existing = {(rule.alert_type, rule.department or "", rule.period or "")
                for rule in db.scalars(select(AlertRule)).all()}
    added = False
    for item in DEFAULT_RULES:
        if (item["alert_type"], "", "") in existing:
            continue
        db.add(AlertRule(**item))
        added = True
    if added:
        db.flush()
    return db.scalars(select(AlertRule).order_by(AlertRule.alert_type, AlertRule.id)).all()


def rule_scope_label(rule: AlertRule) -> str:
    """规则的适用范围文案：「全公司 · 所有期间」「部门 018 · 2026-10」。"""
    department = rule.department or "全公司"
    period = rule.period or "所有期间"
    return f"{department} · {period}"


def scope_rank(rule: AlertRule, *, period: str, department: str | None) -> int | None:
    """规则对 (期间, 部门) 的匹配程度：2 = 两个维度都限定且命中，1 = 只限定一个维度，0 = 都不限定。

    不匹配返回 None。空 `department` 的规则对任何部门都适用（包括全公司那一行）。
    """
    if rule.department and rule.department != (department or ""):
        return None
    if rule.period and rule.period != period:
        return None
    return (1 if rule.department else 0) + (1 if rule.period else 0)


def resolve_rules(rules: list[AlertRule], *, period: str, department: str | None) -> list[AlertRule]:
    """挑出这次重算真正生效的规则：**同一个 alert_type 只取最具体的一条**。

    分档是「覆盖」而不是「叠加」——叠加的话同一个部门会同时收到「成本率 > 0.8」和
    「成本率 > 0.85」两条同类型预警，处理完一条还剩一条，用户没法判断哪条才算数。
    同级（都只限定了部门，或都只限定了期间）按 id 小的胜出，保证同样的库每次算出同样的结果。
    """
    best: dict[str, tuple[int, int, AlertRule]] = {}
    for rule in rules:
        rank = scope_rank(rule, period=period, department=department)
        if rank is None:
            continue
        key = (-rank, rule.id or 0)
        current = best.get(rule.alert_type)
        if current is None or key < current[:2]:
            best[rule.alert_type] = (key[0], key[1], rule)
    chosen = sorted(best.values(), key=lambda item: (item[2].alert_type, item[1]))
    return [item[2] for item in chosen]


def metric_values(metric: FinancialMetric) -> dict[str, Decimal]:
    """规则能引用的指标集合：直接从指标行取，保证与报告/看板是同一套数字。"""
    return {
        "revenue_amount": metric.revenue_amount or Decimal(0),
        "cost_amount": metric.cost_amount or Decimal(0),
        "expense_amount": metric.expense_amount or Decimal(0),
        "gross_profit_amount": metric.gross_profit_amount or Decimal(0),
        "net_profit_amount": metric.net_profit_amount or Decimal(0),
        "gross_profit_ratio": metric.gross_profit_ratio or Decimal(0),
        "net_profit_ratio": metric.net_profit_ratio or Decimal(0),
        "cost_ratio": ((metric.cost_amount or Decimal(0)) / metric.revenue_amount
                       if metric.revenue_amount else Decimal(0)),
    }


def evaluate(rule: AlertRule, values: dict[str, Decimal]) -> Decimal | None:
    """命中就返回该规则的当前值，否则 None。`require_metric` 为 0 时视为规则不适用。"""
    if not rule.enabled:
        return None
    if rule.require_metric and not values.get(rule.require_metric):
        return None
    value = values.get(rule.metric_name)
    if value is None:
        return None
    if rule.comparator == "gt" and value > rule.threshold_value:
        return value
    if rule.comparator == "lt" and value < rule.threshold_value:
        return value
    return None
