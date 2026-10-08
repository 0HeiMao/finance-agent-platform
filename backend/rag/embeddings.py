from functools import lru_cache
from pathlib import Path


from backend.rag.pgvector_store import EMBEDDING_DIMENSION
from backend.config import get_settings


class EmbeddingService:
    def __init__(self, model_name: str | None = None) -> None:
        configured_model = model_name or get_settings().embedding_model or "data/models/finance-embedding"
        candidate = Path(configured_model)
        if not candidate.is_absolute():
            candidate = Path(__file__).resolve().parents[2] / candidate
        local_model = candidate.is_dir()
        if configured_model.replace("\\", "/").startswith("data/") and not local_model:
            raise RuntimeError("本地 Embedding 模型不存在，请先安装模型")
        self.model_name = candidate.resolve().as_posix() if local_model else configured_model
        self.using_fallback = False
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError("正式 Embedding 依赖尚未安装，已停止向量化") from exc
        self.model = SentenceTransformer(self.model_name, local_files_only=local_model)

    def encode(self, texts: list[str]) -> list[list[float]]:
        vectors = self.model.encode(texts, normalize_embeddings=True)
        result = vectors.tolist()
        if result and len(result[0]) != EMBEDDING_DIMENSION:
            raise RuntimeError(f"Embedding 维度为 {len(result[0])}，但数据库要求 {EMBEDDING_DIMENSION}")
        return result


@lru_cache(maxsize=1)
def get_embedding_service() -> EmbeddingService:
    return EmbeddingService()
