from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import uuid4

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Integer, String, Text, func, select
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column

from backend.db import Base

EMBEDDING_DIMENSION = 384  # multilingual-MiniLM-L12-v2


class DocumentChunk(Base):
    __tablename__ = "document_chunks"

    id: Mapped[str] = mapped_column(UUID(as_uuid=False), primary_key=True, default=lambda: str(uuid4()))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(500), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    document_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBEDDING_DIMENSION), nullable=False)
    created_at: Mapped[Any] = mapped_column(DateTime(timezone=True), server_default=func.now())


def add_documents(db: Session, documents: Iterable[dict[str, Any]]) -> list[str]:
    rows = [DocumentChunk(**item) for item in documents]
    db.add_all(rows)
    db.commit()
    return [row.id for row in rows]


def search_documents(db: Session, embedding: list[float], top_k: int = 5, max_distance: float | None = None, *, embedding_model: str) -> list[DocumentChunk]:
    if len(embedding) != EMBEDDING_DIMENSION:
        raise ValueError(f"embedding 必须包含 {EMBEDDING_DIMENSION} 个浮点数")
    distance = DocumentChunk.embedding.cosine_distance(embedding).label("distance")
    # 同维度不代表同一向量空间；旧模型及未标记的索引不能参与检索。
    statement = (select(DocumentChunk, distance)
                 .where(DocumentChunk.metadata_json["embedding_model"].astext == embedding_model)
                 .order_by(distance).limit(top_k))
    rows = db.execute(statement).all()
    return [row[0] for row in rows if max_distance is None or row[1] <= max_distance]
