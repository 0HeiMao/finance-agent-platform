import json

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.ai_service import rag_answer
from backend.api_rag import ChunkScope, retrieve_for_user, user_department_of
from backend.auth import current_user
from backend.db import SessionLocal
from backend.limits import ai_quota
from backend.llm import get_llm
from backend.models import User
from backend.rag.financial_evidence import collect as collect_financial_evidence
from backend.scope import resolve_requested_scope, scope_of, scope_payload

router = APIRouter(prefix="/api/v1/ai", tags=["ai"])
class Question(BaseModel):
    question: str = Field(..., min_length=1, max_length=4000)


def _retrieve(question: str, db, user_department: ChunkScope = None) -> list:
    """宽松阈值多召回 -> 部门过滤 -> 重排压缩到 5（规则与 /rag/answer 共用一份实现）。"""
    return retrieve_for_user(db, question, user_department, 5)


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
                 user: User = Depends(current_user),
                 x_user_department: str | None = Header(default=None),
                 _quota: None = Depends(ai_quota)):
    """财务 Agent 流式问答：证据 = 已导入财务数据 + 知识库检索，模型只能依据证据作答。"""
    scope = user_department_of(user, x_user_department)
    # 财务数据的范围只认服务端记录的部门（scope_of），不认请求头：知识库那条兼容路径允许
    # 老客户端用 finance-admin 自述，如果同一套值拿去过滤财务数据，等于把公司口径的开关交给请求头。
    # department 只对不受限账号生效（受限账号填别的部门直接 403），与看板/报告同一套 resolve 规则。
    financial_scope, violation = resolve_requested_scope(user, department)
    if violation:
        raise HTTPException(403, violation)
    explicit = financial_scope[0] if (financial_scope and len(financial_scope) == 1 and scope_of(user) is None) else None

    def events():
        db = SessionLocal()
        try:
            financial = collect_financial_evidence(db, payload.question, financial_scope)
            docs = _retrieve(payload.question, db, scope)
            evidence = ""
            if financial["has_data"]:
                evidence += financial["context"] + "\n\n"
            evidence += "【知识库检索片段】\n" + ("\n".join(f"来源：{x.source}\n内容：{x.content}" for x in docs) or "无。")
            sources = [dict(item) for item in financial["sources"]]
            sources += [{"name": x.source, "kind": "knowledge_base"} for x in docs]
            if not financial["has_data"] and not docs:
                text = "当前知识库里没有相关材料，系统内也没有已导入的财务数据，请先导入数据或补充材料后人工复核。"
                for part in [text[i:i+12] for i in range(0, len(text), 12)]:
                    yield f"event: delta\ndata: {json.dumps({'text': part}, ensure_ascii=False)}\n\n"
            else:
                for part in get_llm().stream(payload.question, evidence):
                    yield f"event: delta\ndata: {json.dumps({'text': part}, ensure_ascii=False)}\n\n"
            yield f"event: done\ndata: {json.dumps({'sources': sources, 'has_financial_data': financial['has_data'], 'matched_period': financial.get('period'), 'knowledge_chunks': len(docs), 'scope': scope_payload(financial_scope, requested=explicit)}, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally: db.close()
    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})
