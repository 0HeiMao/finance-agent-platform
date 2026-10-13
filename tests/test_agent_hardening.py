import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from backend.agent_access import allows, merge, snapshot
from backend.agent_facts import fact
from backend.agent_grounding import validate
from backend.models import AgentMessage, AgentSession, User


def binding():
    entry = fact('p1:revenue', 'revenue', '收入', '2026-07', ('A',), '120.00')
    ref = {"evidence_id": "E1", **{key: entry[key] for key in ('fact_id', 'metric', 'period', 'scope', 'value', 'unit')}}
    return {"claims": [{"text": "任意模型改写", "evidence_ids": ["E1"], "fact_refs": [ref]}]}, {"E1": {"tool": "query_metrics", "result": {"facts": [entry]}}}


@pytest.mark.parametrize('key,value', [('metric', 'expense'), ('period', '2026-08'), ('scope', ['B']), ('scope', None), ('value', '180.00'), ('unit', '万元'), ('fact_id', 'absent'), ('evidence_id', 'E2')])
def test_bound_claim_rejects_swapped_fields(key, value):
    claim, ledger = binding()
    claim['claims'][0]['fact_refs'][0][key] = value
    answer, reason = validate(json.dumps(claim), ledger)
    assert answer is None and reason


def test_matching_reference_cannot_smuggle_wrong_prose():
    claim, ledger = binding()
    claim['claims'][0]['text'] = '2027年部门B费用120元，原因是员工舞弊'
    answer, error = validate(json.dumps(claim), ledger)
    assert error is None
    assert '2026-07 · 部门 A：收入 120.00 元' in answer
    assert '部门B' not in answer and '舞弊' not in answer and '2027' not in answer


def test_same_number_in_other_metric_is_not_a_valid_binding():
    claim, ledger = binding()
    ledger['E1']['result']['facts'].append(fact('p2:expense', 'expense', '费用', '2026-08', ('B',), '120.00'))
    claim['claims'][0]['fact_refs'][0]['metric'] = 'expense'
    assert validate(json.dumps(claim), ledger)[0] is None


def test_legacy_truncated_scope_never_grants_restricted_history():
    user = SimpleNamespace(departments=['A'], is_admin=False)
    assert not allows(user, None)
    assert not allows(user, snapshot(None))
    assert not allows(user, snapshot(['A', 'B']))
    assert allows(user, snapshot(['A']))
    assert merge(snapshot(['A']), ['B']) == snapshot(['A', 'B'])
    assert merge(snapshot(['A']), None) == snapshot(None)


@pytest.mark.anyio
async def test_permission_reduction_hides_titles_messages_sources_and_blocks_continuation(anon_client, auth_headers, credentials, db_session, monkeypatch):
    user = db_session.scalar(select(User).where(User.username == credentials['username']))
    user.is_admin = False
    user.departments = ['A']
    user.department = 'A'
    sessions = []
    for saved in [snapshot(['A']), snapshot(['A', 'B']), snapshot(None), None]:
        session = AgentSession(user_id=user.id, title='SENSITIVE_TITLE', scope='A', scope_snapshot=saved)
        db_session.add(session)
        db_session.flush()
        db_session.add(AgentMessage(user_id=user.id, session_id=session.id, role='assistant', status='ok', content='SENSITIVE_ANSWER', sources=[{'name': 'SENSITIVE_SOURCE'}], execution_trace=[{'evidence': 'SENSITIVE_SNAPSHOT'}], scope_snapshot=saved))
        sessions.append(session.id)
    db_session.commit()
    response = await anon_client.get('/api/v1/ai/agent/sessions?limit=1', headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 1
    assert [item['id'] for item in response.json()['data']] == sessions[:1]
    assert (await anon_client.get(f'/api/v1/ai/agent/sessions/{sessions[0]}', headers=auth_headers)).status_code == 200
    monkeypatch.setattr('backend.agent_task_api.get_llm', lambda: pytest.fail('Denied history must not invoke model'))
    for session_id in sessions[1:]:
        read = await anon_client.get(f'/api/v1/ai/agent/sessions/{session_id}', headers=auth_headers)
        assert read.status_code == 403 and 'SENSITIVE' not in read.text
        message_id = db_session.scalar(select(AgentMessage.id).where(AgentMessage.session_id == session_id))
        recovered = await anon_client.get(f'/api/v1/ai/agent/sessions/{session_id}/messages/{message_id}', headers=auth_headers)
        assert recovered.status_code == 403 and 'SENSITIVE' not in recovered.text
        continued = await anon_client.post(f'/api/v1/ai/agent/chat/stream?session_id={session_id}', headers=auth_headers, json={'question': '继续', 'mode': 'agent'})
        assert continued.status_code == 403 and 'SENSITIVE' not in continued.text


@pytest.mark.anyio
async def test_history_context_cannot_bypass_current_permission(db_session, auth_headers, credentials):
    from fastapi import HTTPException

    from backend import agent_sessions
    user = db_session.scalar(select(User).where(User.username == credentials['username']))
    session = AgentSession(user_id=user.id, scope_snapshot=snapshot(['A', 'B']))
    db_session.add(session)
    db_session.flush()
    user.departments = ['A']
    user.department = 'A'
    user.is_admin = False
    with pytest.raises(HTTPException) as exc:
        agent_sessions.history_for(db_session, session.id, max_turns=5, max_tokens=1000, user=user)
    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_active_session_rejects_duplicate_generation_and_recovery_is_read_only(anon_client, db_session, auth_headers, credentials):
    from fastapi import HTTPException

    from backend import agent_sessions
    user = db_session.scalar(select(User).where(User.username == credentials['username']))
    session, question, answer = agent_sessions.start_exchange(db_session, user, '本地并发测试', scope=None)
    db_session.commit()
    with pytest.raises(HTTPException) as exc:
        agent_sessions.start_exchange(db_session, user, '重复提交', session_id=session.id, scope=None)
    assert exc.value.status_code == 409
    response = await anon_client.get(f'/api/v1/ai/agent/sessions/{session.id}/messages/{answer.id}', headers=auth_headers)
    assert response.status_code == 200 and response.json()['status'] == 'streaming'
    assert len(agent_sessions.messages_of(db_session, session.id)) == 2
