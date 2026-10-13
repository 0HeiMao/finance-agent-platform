from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.agent_context import alert_context, evidence_snapshot


def fake_db(department):
    row = SimpleNamespace(id=7, period="2026-07", department=department,
                          alert_type="利润异常", description="待核验", metric_name="profit",
                          current_value=-12, threshold_value=0)
    return SimpleNamespace(get=lambda *args: row)


@pytest.mark.parametrize("departments,admin,alert_department", [([], False, "A"), (["A"], False, "B"), (["A"], True, None)])
def test_alert_scope_cannot_be_bypassed(departments, admin, alert_department):
    user = SimpleNamespace(departments=departments, is_admin=admin)
    with pytest.raises(HTTPException) as error:
        alert_context(fake_db(alert_department), user, 7)
    assert error.value.status_code == 403


def test_multi_department_user_is_narrowed_to_alert_department():
    context, scope = alert_context(fake_db("A"), SimpleNamespace(departments=["A", "B"], is_admin=False), 7)
    assert scope == ("A",)
    assert context["period"] == "2026-07"
    assert context["current_value"] == "-12"


def test_deleted_alert_is_not_used():
    with pytest.raises(HTTPException) as error:
        alert_context(SimpleNamespace(get=lambda *args: None), None, 7)
    assert error.value.status_code == 404


def test_snapshot_has_bounded_excerpts_and_no_arbitrary_private_fields():
    snapshot = evidence_snapshot("search_policies", {
        "evidence_id": "E2", "private": "secret", "untrusted_documents": [
            {"source": "policy.md", "content": "x" * 5000, "private": "secret"} for _ in range(20)]
    }, {}, ("A",))
    assert len(snapshot["excerpts"]) == 5
    assert all(len(item["text"]) == 600 for item in snapshot["excerpts"])
    assert "secret" not in str(snapshot)
    assert snapshot["scope_label"] == "部门 A"
    assert evidence_snapshot("query_metrics", {"error": "no data"}, {}, None) is None


@pytest.mark.anyio
async def test_alert_context_http_and_stream_recheck(anon_client, api_app, monkeypatch):
    from backend import agent_context as context_module
    from backend.auth import current_user
    from backend.limits import ai_quota
    user = SimpleNamespace(departments=["A"], is_admin=False)
    api_app.dependency_overrides[current_user] = lambda: user
    api_app.dependency_overrides[ai_quota] = lambda: None
    calls = []
    def denied(db, actual_user, alert_id):
        calls.append((actual_user, alert_id))
        raise HTTPException(403, "范围已变更")
    monkeypatch.setattr(context_module, "alert_context", denied)
    monkeypatch.setattr("backend.agent_task_api.alert_context", denied)
    try:
        response = await anon_client.get('/api/v1/ai/agent/alerts/7/context')
        assert response.status_code == 403
        response = await anon_client.post('/api/v1/ai/agent/chat/stream', json={"question": "分析预警", "mode": "agent", "alert_id": 7})
        assert response.status_code == 403
        assert calls == [(user, 7), (user, 7)]
    finally:
        api_app.dependency_overrides.pop(current_user, None)
        api_app.dependency_overrides.pop(ai_quota, None)
