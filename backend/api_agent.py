"""Agent 会话与用量的读写端点（⑮）。

会话落库之后，「问答」这件事就有了一份可回看的记录：谁在什么时候问了什么、回答依据了哪些
片段、花了多少 token、中途有没有被停止。这个模块只做读写与权限判断，状态机都在
`backend/agent_sessions.py` 里。

权限只有一条规则：**会话属于提问的人**。别人的会话 id 一律按「不存在」处理（不区分
"没有" 和 "不是你的"），免得它变成探测他人会话的探针；跨用户的用量只在管理员端点看汇总。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import agent_sessions, audit
from backend.agent_access import require_access, snapshot
from backend.auth import current_user
from backend.db import get_db
from backend.models import AgentMessage, AgentSession, User
from backend.scope import scope_of

router = APIRouter(prefix="/api/v1/ai", tags=["agent"])

DAYS_DEFAULT = 30
DAYS_MAX = 365
PAGE_MAX = 200


class SessionCreate(BaseModel):
    """显式建会话：前端先拿到 id 再开始流式，这样回答一开始就能被"停止"。"""
    title: str = Field("", max_length=agent_sessions.TITLE_MAX)


def _session_or_404(db: Session, user: User, session_id: int) -> AgentSession:
    session = db.get(AgentSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(404, f"会话 {session_id} 不存在")
    require_access(user, session)
    return session


@router.get("/agent/sessions")
def list_agent_sessions(limit: int = Query(50, ge=1, le=PAGE_MAX), offset: int = Query(0, ge=0),
                        user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows, total = agent_sessions.list_sessions(db, user_id=user.id, limit=limit, offset=offset, user=user)
    counts = agent_sessions.session_counts(db, [row.id for row in rows])
    return {"data": [agent_sessions.session_payload(row, counts.get(row.id)) for row in rows],
            "total": total, "limit": limit, "offset": offset, "untitled": agent_sessions.UNTITLED}


@router.post("/agent/sessions", status_code=201)
def create_agent_session(payload: SessionCreate, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    session = AgentSession(user_id=user.id, title=payload.title.strip(),
                           scope=agent_sessions.scope_text(scope_of(user)), scope_snapshot=snapshot(scope_of(user)))
    db.add(session)
    db.commit()
    db.refresh(session)
    return agent_sessions.session_payload(session)


@router.get("/agent/sessions/{session_id}")
def get_agent_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = _session_or_404(db, user, session_id)
    messages = agent_sessions.messages_of(db, session.id)
    counts = agent_sessions.session_counts(db, [session.id])
    return {"data": agent_sessions.session_payload(session, counts.get(session.id)),
            "messages": [agent_sessions.message_payload(row) for row in messages],
            "status_labels": agent_sessions.STATUS_LABELS}


@router.delete("/agent/sessions/{session_id}")
def delete_agent_session(session_id: int, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    session = _session_or_404(db, user, session_id)
    title = session.title or ""
    removed = agent_sessions.delete_session(db, session)
    audit.record(db, user, "agent_session.delete", target_type="agent_session", target_id=session_id,
                 detail={"title": title, "messages": removed})
    db.commit()
    return {"id": session_id, "title": title, "deleted_messages": removed, "deleted": True}


@router.get("/agent/sessions/{session_id}/messages/{message_id}")
def get_agent_message(session_id: int, message_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """断线后只读查询原回答状态；不重新调用模型，复核会话与消息当前权限。"""
    from backend.agent_access import allows
    _session_or_404(db, user, session_id)
    message = db.get(AgentMessage, message_id)
    if message is None or message.session_id != session_id or message.user_id != user.id:
        raise HTTPException(404, "回答不存在")
    if not allows(user, message.scope_snapshot):
        raise HTTPException(403, "当前权限不允许读取该回答")
    return agent_sessions.message_payload(message)


@router.post("/agent/sessions/{session_id}/stop")
def stop_agent_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """停止正在生成的回答（㉒ 起落库）。

    只写一个标志位、**不等它写完**：生成器按节拍读 `agent_messages.stop_requested_at`，
    看到就收尾并把已经生成的正文按 `status="stopped"` 落库（前端的 abort 只断开连接，
    服务端要到下一块才发现，那期间模型还在烧 token，所以停止必须是显式请求）。

    这一列就是「用户点过停止」的**事实**，所以停不下来的情况只有一种：这条回答已经不在生成。
    那种情况下这里回 409，而不是回一个 `stopped=true` —— 假装成功会让用户以为停下了。
    """
    session = _session_or_404(db, user, session_id)
    message = agent_sessions.streaming_message(db, session.id)
    if message is None:
        raise HTTPException(409, "这个会话当前没有正在生成的回答")
    if not agent_sessions.request_stop(db, message.id):
        # 查出来还在生成、写下去却不在生成了：这一瞬间它自己收尾了。如实说不，别谎报
        db.rollback()
        raise HTTPException(409, "这个会话当前没有正在生成的回答")
    db.commit()
    return {"session_id": session.id, "message_id": message.id, "stopped": True}


@router.get("/agent/usage")
def agent_usage(days: int = Query(DAYS_DEFAULT, ge=1, le=DAYS_MAX), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """本人近 N 天的问答用量。`estimated` 与 `api` 分开报，估算不当账单。"""
    stats = agent_sessions.usage_stats(db, user_id=user.id, days=days)
    return {"data": stats, "series": agent_sessions.daily_series(stats),
            "status_labels": agent_sessions.STATUS_LABELS}
