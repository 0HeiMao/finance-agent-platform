"""Canonical field bindings rendered by the server rather than paraphrased by a model."""
from decimal import Decimal

from backend.scope import scope_label


def fact(key, metric, label, period, scope, value, unit="元", dimension=""):
    return {"fact_id": key, "metric": metric, "label": label, "period": period,
            "scope": sorted(set(scope)) if scope is not None else None,
            "scope_label": scope_label(scope), "value": str(value), "unit": unit, "dimension": dimension}


def metric_facts(metric, scope):
    fields = [("revenue", "收入", metric.revenue_amount, "元"),
              ("cost", "成本", metric.cost_amount, "元"),
              ("expense", "费用", metric.expense_amount, "元"),
              ("net_amount", "收支差额（简化口径）", metric.net_profit_amount, "元"),
              ("cost_ratio", "成本率", metric.gross_profit_ratio, "比率"),
              ("net_ratio", "收支差额率", metric.net_profit_ratio, "比率")]
    return [fact(f"{metric.period}:{key}", key, label, metric.period, scope,
                 Decimal(value or 0).quantize(Decimal('0.0001') if unit == '比率' else Decimal('0.01')), unit)
            for key, label, value, unit in fields]


def render_bound_claim(claim, ledger):
    refs = claim.get("fact_refs")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 12:
        raise ValueError("财务结论必须提供 fact_refs，绑定具体字段、期间、部门、数值和单位。")
    rendered = []
    for ref in refs:
        if not isinstance(ref, dict) or ref.get("evidence_id") not in claim["evidence_ids"]:
            raise ValueError("字段引用必须属于本条结论引用的证据。")
        item = ledger[ref["evidence_id"]]
        if item["tool"] not in ("query_metrics", "compare_expenses"):
            raise ValueError("财务字段必须来自财务工具。")
        matches = [entry for entry in item["result"].get("facts", []) if entry["fact_id"] == ref.get("fact_id")]
        if len(matches) != 1:
            raise ValueError("字段编号不存在或不唯一。")
        expected = matches[0]
        for key in ("metric", "period", "scope", "value", "unit"):
            # Exact strings avoid implicit unit conversion, rounding, or swapped periods/scopes.
            if key not in ref or ref[key] != expected[key]:
                raise ValueError(f"字段绑定的 {key} 与本轮证据不一致，禁止混用指标、期间、部门或单位。")
        dimension = f" · {expected['dimension']}" if expected.get('dimension') else ''
        rendered.append(f"{expected['period']} · {expected['scope_label']}{dimension}：{expected['label']} {expected['value']} {expected['unit']}。")
    # Deliberately do not display claim.text: matching references cannot prove that prose agrees.
    return "\n".join(rendered)
