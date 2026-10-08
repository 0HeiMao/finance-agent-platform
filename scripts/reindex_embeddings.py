"""使用指定模型重建全部向量；任一步失败均回滚，不修改原文。"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sqlalchemy import select, text
from backend.db import SessionLocal
from backend.rag.embeddings import EmbeddingService
from backend.rag.pgvector_store import DocumentChunk


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', required=True)
    args = parser.parse_args()
    service = EmbeddingService(args.model)
    with SessionLocal.begin() as db:
        # 排他锁覆盖整个重建过程，阻止同时查询或写入半成品索引。
        db.execute(text('LOCK TABLE document_chunks IN ACCESS EXCLUSIVE MODE'))
        rows = db.scalars(select(DocumentChunk).order_by(DocumentChunk.id)).all()
        for start in range(0, len(rows), 32):
            batch = rows[start:start + 32]
            vectors = service.encode([row.content for row in batch])
            for row, vector in zip(batch, vectors):
                row.embedding = vector
                row.metadata_json = {**row.metadata_json, 'embedding_model': service.model_name}
    print(f'Reindexed {len(rows)} chunks; committed; original text preserved.')


if __name__ == '__main__':
    main()
