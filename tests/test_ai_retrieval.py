"""真实 PostgreSQL 检查：别的部门不能占用当前用户的候选名额。"""
from uuid import uuid4

from backend.rag.pgvector_store import DocumentChunk, search_documents


def test_department_filter_precedes_top_k(db_session):
    model = f"scope-test-{uuid4()}"
    query = [1.0] + [0.0] * 383
    other = DocumentChunk(content="other", source="other", embedding=query,
                          metadata_json={"embedding_model": model, "department": "B"})
    own = DocumentChunk(content="own", source="own", embedding=[0.8, 0.2] + [0.0] * 382,
                        metadata_json={"embedding_model": model, "department": "A"})
    public = DocumentChunk(content="public", source="public", embedding=[0.6, 0.4] + [0.0] * 382,
                           metadata_json={"embedding_model": model})
    db_session.add_all([other, own, public])
    db_session.flush()
    assert search_documents(db_session, query, 1, embedding_model=model, departments=("A",)) == [own]
    assert search_documents(db_session, query, 1, embedding_model=model, departments=()) == [public]
    assert search_documents(db_session, query, 1, embedding_model=model, departments=None) == [other]
    assert search_documents(db_session, query, 3, embedding_model=model, departments=("A", "B")) == [other, own, public]
