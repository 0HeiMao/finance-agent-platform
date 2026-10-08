import json
import logging
import time

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import agent_sessions
from backend.ai_service import rag_answer
from backend.api_rag import ChunkScope, retrieve_for_user, user_department_of
from backend.auth import current_user
from backend.db import get_db
from backend.limits import ai_quota
from backend.llm import get_llm
from backend.models import AgentMessage, User
from backend.rag.financial_evidence import collect as collect_financial_evidence
from backend.scope import resolve_requested_scope, scope_of, scope_payload

logger = logging.getLogger("finance-ai")
router = APIRouter(prefix="/api/v1/ai", tags=["ai"])


class Question(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


def _retrieve(question: str, db, user_department: ChunkScope = None) -> list:
    """宽松阈值多召回 -> 部门过滤 -> 重排压缩到 5（规则与 /rag/answer 共用一份实现）。"""
    return retrieve_for_user(db, question, user_department, 5)


def sse(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _persist(message_id: int, *, content: str, status: str, sources, period, knowledge_chunks,
             usage: dict, prompt_text: str, duration_ms: int, count_estimate: bool,
             error: str | None = None) -> dict:
    """把生成结果写回占位行，返回给 SSE 用的那几条数字。

    **另开会话**是刻意的：出错时原来那条会话的事务可能已经废了（任何一条语句失败，
    后面都得先 rollback），而"把失败写下来"恰恰是这时候最要紧的事。
    """
    db = agent_sessions.open_session()
    try:
        message = db.get(AgentMessage, message_id)
        if message is None:  # 会话被删了：没什么可写的，但别让流式收尾炸掉
            return {}
        agent_sessions.finish_exchange(db, message, content=content, status=status, sources=sources,
                                       period=period, knowledge_chunks=knowledge_chunks, usage=usage,
                                       prompt_text=prompt_text, count_estimate=count_estimate,
                                       duration_ms=duration_ms, error=error)
        db.commit()
        return {
            "tokens": {"prompt_tokens": message.prompt_tokens,
                       "completion_tokens": message.completion_tokens,
                       "total_tokens": message.total_tokens,
                       "usage_source": message.usage_source or ""},
            "status": message.status,
        }
    except Exception:
        logger.exception("Agent 回答落库失败（message_id=%s）", message_id)
        db.rollback()
        return {}
    finally:
        db.close()


@router.post("/rag/chat")
def chat(payload: Question, user: User = Depends(current_user),
         x_user_department: str | None = Header(default=None),
         _quota: None = Depends(ai_quota)):
    return rag_answer(payload.question, user_department=user_department_of(user, x_user_department))


@router.post("/agent/ask")
def agent_ask(payload: Question, user: User = Depends(current_user),
              x_user_department: str | None = Header(default=None),
              _quota: None = Depends(ai_quota)):
    return {"tool": "rag_search", **rag_answer(payload.question, user_department=user_department_of(user, x_user_department))}


@router.post("/agent/chat/stream")
def agent_stream(payload: Question, department: str | None = Query(None, max_length=100),
                 session_id: int | None = Query(None, ge=1),
                 user: User = Depends(current_user),
                 x_user_department: str | None = Header(default=None),
                 db: Session = Depends(get_db),
                 _quota: None = Depends(ai_quota)):
    """财务 Agent 流式问答：证据 = 已导入财务数据 + 知识库检索，模型只能依据证据作答。

    这一轮问答在流开始之前就先落库（⑮）：用户提问 + 一条 `status="streaming"` 的助手占位行。
    两件事因此成为可能——前端在流当中就知道 `message_id` 并能请求停止；
    进程半路没了也能在会话里看到一条"没写完的回答"，而不是什么都没有。
    """
    scope = user_department_of(user, x_user_department)
    # 财务数据的范围只认服务端记录的部门（scope_of），不认请求头：知识库那条兼容路径允许
    # 老客户端用 finance-admin 自述，如果同一套值拿去过滤财务数据，等于把公司口径的开关交给请求头。
    # department 只对不受限账号生效（受限账号填别的部门直接 403），与看板/报告同一套 resolve 规则。
    financial_scope, violation = resolve_requested_scope(user, department)
    if violation:
        raise HTTPException(403, violation)
    explicit = financial_scope[0] if (financial_scope and len(financial_scope) == 1 and scope_of(user) is None) else None

    try:
        session, user_message, assistant = agent_sessions.start_exchange(
            db, user, payload.question, session_id=session_id, scope=financial_scope)
    except agent_sessions.SessionNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    # 必须在这里提交：生成器用的是另一条连接，看不到未提交的占位行
    db.commit()

    event = agent_sessions.register(assistant.id)
    assistant_id = assistant.id

    def events():
        started = time.monotonic()
        parts: list[str] = []
        usage: dict = {}
        sources: list = []
        period = None
        knowledge_chunks = 0
        evidence = ""
        status = "ok"
        count_estimate = True
        error_text: str | None = None
        outcome: dict = {}
        yield sse("start", {"session_id": session.id, "message_id": assistant_id,
                            "question_message_id": user_message.id, "title": session.title})
        try:
            db2 = agent_sessions.open_session()
            try:
                financial = collect_financial_evidence(db2, payload.question, financial_scope)
                docs = _retrieve(payload.question, db2, scope)
            finally:
                db2.close()
            if financial["has_data"]:
                evidence += financial["context"] + "\n\n"
            evidence += "【知识库检索片段】\n" + ("\n".join(f"来源：{x.source}\n内容：{x.content}" for x in docs) or "无。")
            sources = [dict(item) for item in financial["sources"]]
            sources += [{"name": x.source, "kind": "knowledge_base"} for x in docs]
            period = financial.get("period")
            knowledge_chunks = len(docs)
            if not financial["has_data"] and not docs:
                # 没有证据就不花模型的 token，直接把平台自己的话吐出去（用量记 0，见 _persist）
                count_estimate = False
                text = "当前知识库里没有相关材料，系统内也没有已导入的财务数据，请先导入数据或补充材料后人工复核。"
                for part in [text[i:i+12] for i in range(0, len(text), 12)]:
                    if event.is_set():
                        status = "stopped"
                        break
                    parts.append(part)
                    yield sse("delta", {"text": part})
            else:
                for part in get_llm().stream(payload.question, evidence, usage):
                    # 每块一次检查：用户点了"停止"就立刻收尾，而不是等模型把剩下的 token 烧完
                    if event.is_set():
                        status = "stopped"
                        break
                    parts.append(part)
                    yield sse("delta", {"text": part})
        except GeneratorExit:
            # 客户端直接断开（关标签页、abort）。没点停止就是"中断"，不是用户要的结果
            status = "stopped" if event.is_set() else "interrupted"
            raise
        except Exception as exc:
            status = "error"
            error_text = str(exc)
            yield sse("error", {"message": error_text})
        finally:
            agent_sessions.release(assistant_id)
            outcome.update(_persist(assistant_id, content="".join(parts), status=status, sources=sources,
                                    period=period, knowledge_chunks=knowledge_chunks, usage=usage,
                                    prompt_text=f"{payload.question}\n{evidence}", count_estimate=count_estimate,
                                    duration_ms=int((time.monotonic() - started) * 1000),
                                    error=error_text))
        if error_text is None:
            yield sse("done", {"session_id": session.id, "message_id": assistant_id,
                               "status": status, "stopped": status == "stopped",
                               "sources": sources, "has_financial_data": financial["has_data"],
                               "matched_period": period, "knowledge_chunks": knowledge_chunks,
                               "scope": scope_payload(financial_scope, requested=explicit),
                               "tokens": outcome.get("tokens", {}),
                               "duration_ms": int((time.monotonic() - started) * 1000)})
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
