"""RAG 向量入库和检索接口。

所有接口都需要 Bearer Token：知识库内容同样属于业务数据，不能匿名读写。
"""

import hashlib
from pathlib import Path
from typing import Iterable
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import audit
from backend.auth import current_user
from backend.db import get_db
from backend.limits import ai_quota
from backend.models import User
from backend.rag.pgvector_store import DocumentChunk, add_documents, search_documents
from backend.rag.document_parser import extract_text, split_text
from backend.rag.embeddings import get_embedding_service
from backend.rag.answer import generate_answer
from backend.rag.rerank import rerank
from backend.config import get_settings

router = APIRouter(prefix="/api/v1/rag", tags=["rag"])


class DocumentInput(BaseModel):
    content: str = Field(..., min_length=1, max_length=100_000)
    source: str = Field(..., min_length=1, max_length=500)
    page: int | None = Field(default=None, ge=1)
    document_type: str | None = None
    department: str | None = Field(default=None, max_length=100)


class SearchInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=2_000)
    top_k: int = Field(default=5, ge=1, le=20)
    embedding_model: str | None = None
    embedding: list[float] | None = Field(default=None, min_length=384, max_length=384)


class AnswerInput(BaseModel):
    query: str = Field(..., min_length=1, max_length=2_000)
    top_k: int = Field(default=5, ge=1, le=20)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 部门可见性规则（唯一实现在这里，/rag/* 与 /ai/* 共用）：
# - 片段没写 department：属于全员可见的公共资料；
# - 写了 department：只有同部门的人能看到；
# - 用户名下的 department 为空：不受部门限制（财务管理员口径）；
# - 用户名下是 finance-admin：同样不受限制。
# 之前只有 /rag/answer 做了过滤，而前端 Agent 走的是 /ai/agent/chat/stream，
# 等于权限形同虚设；现在统一走这个函数，且部门取自服务端用户记录，
# 不接受请求头自述（X-User-Department 只作为旧客户端的兼容参数保留）。
DEPARTMENT_ADMIN = "finance-admin"


def user_department_of(user: User, legacy_header: str | None = None) -> str | None:
    """部门只认服务端记录；旧客户端的请求头仅在用户尚未设置部门时用于兼容。"""
    return user.department or (legacy_header if legacy_header == DEPARTMENT_ADMIN else None)


def filter_visible(chunks: Iterable, user_department: str | None) -> list:
    if not user_department or user_department == DEPARTMENT_ADMIN:
        return list(chunks)
    return [
        item for item in chunks
        if not item.metadata_json.get("department") or item.metadata_json.get("department") == user_department
    ]


def retrieve_for_user(db: Session, query: str, user_department: str | None, top_k: int) -> list:
    """宽松阈值多召回候选 -> 部门过滤 -> 重排压缩到 top_k。"""
    settings = get_settings()
    service = get_embedding_service()
    embedding = service.encode([query])[0]
    candidates = search_documents(
        db, embedding, max(settings.rag_candidate_k, min(top_k * 3, 50)),
        max_distance=settings.rag_max_distance, embedding_model=service.model_name,
    )
    return rerank(query, filter_visible(candidates, user_department), top_k)


def _existing_hashes(db: Session, embedding_model: str, department: str | None) -> set[str]:
    """同一向量模型、同一部门可见范围下已入库片段的内容哈希。

    刻意**不按 source 限定**：同一份材料换个文件名（或先走 /documents/upload 再走 /documents）
    再入库一次，检索时会同时命中两条一模一样的片段，编辑口径文档时极易看串。
    部门口径必须参与比较——内容相同但可见范围不同不能算重复，否则会把受限片段当成已入库而漏建。
    """
    statement = select(DocumentChunk.metadata_json["content_hash"].astext).where(
        DocumentChunk.metadata_json["embedding_model"].astext == embedding_model,
        func.coalesce(DocumentChunk.metadata_json["department"].astext, "") == (department or "").strip(),
    )
    return {value for value in db.scalars(statement) if value}


@router.post("/documents")
def index_document(payload: DocumentInput, request: Request, db: Session = Depends(get_db),
                   user: User = Depends(current_user), _quota: None = Depends(ai_quota)) -> dict:
    try:
        model_name = get_embedding_service().model_name
        digest = _content_hash(payload.content)
        if digest in _existing_hashes(db, model_name, payload.department):
            return {"success": True, "ids": [], "skipped": 1,
                    "message": "内容与已入库片段完全一致（不看文件名），未重复入库"}
        ids = add_documents(db, [{"content": payload.content, "source": payload.source,
                                  "page": payload.page, "document_type": payload.document_type,
                                  "metadata_json": {"department": payload.department, "embedding_model": model_name,
                                                    "content_hash": digest},
                                  "embedding": get_embedding_service().encode([payload.content])[0]}])
        # 知识库内容会进入 Agent 的证据，写入必须留痕（记来源与片段数，不记正文）
        audit.record(db, user, "knowledge.add", target_type="knowledge", target_id=payload.source,
                     detail={"source": payload.source, "chunks": len(ids),
                             "department": payload.department or "不受限"}, request=request, commit=True)
        return {"success": True, "ids": ids, "skipped": 0}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"向量入库失败：{exc}") from exc


@router.post("/documents/upload")
async def upload_document(request: Request, file: UploadFile = File(...),
                          db: Session = Depends(get_db), user: User = Depends(current_user),
                          _quota: None = Depends(ai_quota)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".txt", ".md", ".csv", ".pdf", ".docx"}:
        raise HTTPException(status_code=400, detail="仅支持 txt、md、csv、pdf、docx 文件")
    source = file.filename or "未命名文档"
    # data/raw 按仓库约定只读，上传文件另放 data/uploads/documents。
    upload_dir = Path(get_settings().rag_upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / f"{uuid4()}{suffix}"
    target.write_bytes(await file.read())
    try:
        chunks = split_text(extract_text(str(target)))
        if not chunks:
            raise ValueError("文档没有可索引的文本")
        model_name = get_embedding_service().model_name
        # 上传的文件不带部门字段（metadata 里没有 department），所以按「不受限」范围去重。
        existing = _existing_hashes(db, model_name, None)
        pending = [(index, chunk) for index, chunk in enumerate(chunks) if _content_hash(chunk) not in existing]
        skipped = len(chunks) - len(pending)
        if not pending:
            return {"success": True, "source": source, "chunks": 0, "skipped": skipped,
                    "message": "该文档已入库且内容未变化，未重复入库"}
        embeddings = get_embedding_service().encode([chunk for _, chunk in pending])
        ids = add_documents(db, [
            {"content": chunk, "source": source,
             "metadata_json": {"stored_path": str(target), "chunk_index": index,
                               "embedding_model": model_name, "content_hash": _content_hash(chunk)},
             "embedding": embeddings[position]}
            for position, (index, chunk) in enumerate(pending)
        ])
        audit.record(db, user, "knowledge.add", target_type="knowledge", target_id=source,
                     detail={"source": source, "chunks": len(ids), "skipped": skipped,
                             "filename": file.filename}, request=request, commit=True)
        return {"success": True, "source": source, "chunks": len(ids), "skipped": skipped, "ids": ids}
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"文档处理失败：{exc}") from exc


@router.post("/search")
def search_documents_api(payload: SearchInput, db: Session = Depends(get_db), user: User = Depends(current_user),
                         x_user_department: str | None = Header(default=None),
                         _quota: None = Depends(ai_quota)) -> dict:
    if payload.embedding is not None and payload.embedding_model != get_embedding_service().model_name:
        raise HTTPException(status_code=422, detail="查询向量的模型标识与当前模型不一致")
    scope = user_department_of(user, x_user_department)
    try:
        settings = get_settings()
        if payload.embedding is None:
            results = retrieve_for_user(db, payload.query, scope, payload.top_k)
            candidate_count = settings.rag_candidate_k
        else:
            # 调用方自带向量（例如离线批量检索）：仍然先多召回、再按部门过滤、最后重排。
            candidates = search_documents(db, payload.embedding, settings.rag_candidate_k,
                                          max_distance=settings.rag_max_distance,
                                          embedding_model=get_embedding_service().model_name)
            results = rerank(payload.query, filter_visible(candidates, scope), payload.top_k)
            candidate_count = len(candidates)
        return {
            "success": True,
            "candidates": candidate_count,
            "data": [
                {"content": item.content, "metadata": item.metadata_json, "source": item.source}
                for item in results
            ],
        }
    except Exception as exc:
        raise HTTPException(status_code=503, detail="向量检索失败，请检查 PostgreSQL 和 pgvector") from exc


@router.post("/answer")
def answer_question(payload: AnswerInput, db: Session = Depends(get_db), user: User = Depends(current_user),
                    x_user_department: str | None = Header(default=None),
                    _quota: None = Depends(ai_quota)) -> dict:
    """检索、按部门过滤并生成带来源回答；带 department 标记的资料仅同部门可见。"""
    try:
        results = retrieve_for_user(db, payload.query, user_department_of(user, x_user_department), payload.top_k)
        answer, sources = generate_answer(payload.query, results)
        return {"success": True, "answer": answer, "sources": sources}
    except Exception as exc:
        raise HTTPException(status_code=503, detail="问答服务暂时不可用，请检查数据库和模型配置") from exc
