import logging

from backend.api_rag import retrieve_for_user
from backend.db import SessionLocal
from backend.llm import get_llm
from backend.rag.embeddings import get_embedding_service

logger = logging.getLogger("finance-ai")

def rag_answer(question: str, top_k: int = 5, user_department: str | None = None) -> dict:
    db = SessionLocal()
    try:
        service = get_embedding_service()
        results = retrieve_for_user(db, question, user_department, top_k)
        if not results:
            return {"answer_status": "insufficient_data", "answer": "当前知识库中未找到足够相关的数据，请补充材料或人工复核。", "sources": [], "embedding_model": service.model_name, "using_fallback_embedding": service.using_fallback}
        context = "\n".join(f"来源：{x.source}\n内容：{x.content}" for x in results)
        try:
            answer = get_llm().answer(question, context)
        except Exception:
            logger.exception("DeepSeek request failed")
            return {"answer_status": "model_unavailable", "answer": "DeepSeek 当前不可用，请检查网络、API Key 或服务状态。", "sources": [x.source for x in results], "embedding_model": get_embedding_service().model_name, "using_fallback_embedding": get_embedding_service().using_fallback}
        return {"answer_status": "supported", "answer": answer, "sources": [x.source for x in results], "embedding_model": get_embedding_service().model_name, "using_fallback_embedding": get_embedding_service().using_fallback}
    finally: db.close()
