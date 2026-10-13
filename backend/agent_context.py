"""Server-owned alert context and bounded, user-visible evidence snapshots."""
from fastapi import HTTPException

from backend.models import FinancialAlert
from backend.scope import in_scope, scope_label, scope_of


def alert_context(db, user, alert_id):
    alert = db.get(FinancialAlert, alert_id)
    if alert is None:
        raise HTTPException(404, "预警不存在或已被重新计算，请刷新预警列表")
    if not in_scope(scope_of(user), alert.department or None):
        raise HTTPException(403, "该预警不在当前账号的数据范围内")
    scope = (alert.department,) if alert.department else None
    return {
        "id": alert.id, "period": alert.period, "department": alert.department or "",
        "scope_label": scope_label(scope), "type": alert.alert_type,
        "description": alert.description or "", "metric": alert.metric_name,
        "current_value": str(alert.current_value) if alert.current_value is not None else "未提供",
        "threshold_value": str(alert.threshold_value) if alert.threshold_value is not None else "未提供",
        "question": f"请分析 {alert.period} 的{alert.alert_type}，核对指标和适用制度，区分已证实事实与待核实原因。",
    }, scope


def evidence_snapshot(tool, result, arguments, scope):
    """Only explicit display fields; never persist raw model messages or arbitrary result keys."""
    if not result.get("evidence_id"):
        return None
    excerpts = []
    if tool == "query_metrics":
        text = "\n".join(f"{item['period']} · {item['scope_label']} · {item.get('dimension') or item['label']}：{item['label']} {item['value']} {item['unit']}" for item in result.get('facts', []))
        excerpts = [{"title": "财务指标与统计口径", "text": (text or str(result.get("context", "")))[:6000]}]
    elif tool == "search_policies":
        excerpts = [{"title": str(doc.get("source", "制度片段"))[:200],
                     "text": str(doc.get("content", ""))[:600]}
                    for doc in result.get("untrusted_documents", [])[:5]]
    elif tool == "list_periods":
        excerpts = [{"title": "当前可见期间", "text": "、".join(result.get("periods", [])[:120])}]
    elif tool == "compare_expenses":
        excerpts = [{"title": "费用差额", "text": f"基期 {result.get('base_period')} → 报告期 {result.get('current_period')}\n总差额：{result.get('total_change')} 元\n" +
                     "\n".join(f"{str(row.get('account', ''))[:120]}：基期 {row.get('base')} 元；报告期 {row.get('current')} 元；差额 {row.get('change')} 元"
                               for row in result.get("rows", [])[:10])}]
    return {"id": result["evidence_id"], "scope_label": scope_label(scope),
            "facts": result.get("facts", [])[:40],
            "periods": arguments.get("periods", []), "excerpts": excerpts,
            "note": "本次查询的证据快照；片段可能截断，不能据此确定业务原因。"}
