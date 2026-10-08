"""系统状态与配置体检：把「跑的是什么配置、连的什么库、缺什么」一次说清楚。

只读接口，不返回任何密钥原文（`model_api_key` 之类一律只报「有没有配」）。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from backend.auth import current_user
from backend.config import get_settings
from backend.db import get_db
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.rag.embeddings import get_embedding_service
from backend.rag.pgvector_store import EMBEDDING_DIMENSION, DocumentChunk
from backend.scope import (departments_of, scope_alert, scope_metric, scope_of, scope_payload,
                           scope_transactions)

router = APIRouter(prefix="/api/v1/system", tags=["system"])


def _mask(url: str) -> str:
    """postgresql+psycopg://user:pass@host/db -> postgresql+psycopg://user:***@host/db"""
    if "@" not in url or "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    credentials, host = rest.rsplit("@", 1)
    user = credentials.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}" if ":" in credentials else url


def _database(db: Session) -> dict:
    settings = get_settings()
    info: dict = {"url": _mask(settings.database_url)}
    try:
        info["version"] = str(db.execute(text("SHOW server_version")).scalar())
        info["pgvector"] = bool(db.execute(text(
            "select count(*) from pg_extension where extname = 'vector'")).scalar())
        info["ok"] = True
    except Exception as exc:  # 数据库挂了也要能返回，页面才能显示成「异常」而不是白屏
        info["ok"] = False
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def _embedding() -> dict:
    settings = get_settings()
    service = get_embedding_service()
    return {
        "model": service.model_name,
        "dimension": EMBEDDING_DIMENSION,
        "configured": settings.embedding_model,
        "using_fallback": bool(getattr(service, "using_fallback", False)),
    }


def _upload_dir(kind: str, configured: str) -> dict:
    """报告目录是否可写：真写一个探针文件再删，避免只看路径存在就宣称正常。"""
    path = Path(configured)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[1] / path
    result = {"name": kind, "path": configured, "exists": path.exists()}
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        result["writable"] = True
        result["exists"] = True
    except Exception as exc:
        result["writable"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


@router.get("/status")
def system_status(db: Session = Depends(get_db), user: User = Depends(current_user)):
    settings = get_settings()
    # 这两张表在加了部门列之后，一次 calculate 会写「全公司 1 行 + 每个部门 1 行」，
    # 不按范围过滤就会把部门行也算进总数（数字翻数倍却看不出错）。
    department = scope_of(user)
    periods = list(db.scalars(scope_metric(
        select(FinancialMetric.period).distinct(), department)
        .order_by(FinancialMetric.period.desc())).all())
    counts = {
        "transactions": db.scalar(scope_transactions(select(func.count()).select_from(FinancialTransaction), department)) or 0,
        "metrics": db.scalar(scope_metric(select(func.count()).select_from(FinancialMetric), department)) or 0,
        "alerts": db.scalar(scope_alert(select(func.count()).select_from(FinancialAlert), department)) or 0,
        "knowledge_chunks": db.scalar(select(func.count()).select_from(DocumentChunk)) or 0,
        "users": db.scalar(select(func.count()).select_from(User)) or 0,
    }
    return {
        "app": {
            "env": settings.app_env,
            "strict_config": bool(settings.strict_config),
            "is_development": settings.is_development,
            "api_prefix": settings.api_prefix,
            "log_level": settings.log_level,
            "cors_origins": settings.cors_origin_list,
            "token_expire_minutes": settings.access_token_expire_minutes,
        },
        "llm": {
            "provider_configured": bool(settings.effective_api_key and settings.effective_base_url),
            "model": settings.deepseek_model,
            "base_url": settings.effective_base_url or "",
            "rerank_model": settings.rerank_model or "",
        },
        "database": _database(db),
        "embedding": _embedding(),
        "rag": {
            "max_distance": settings.rag_max_distance,
            "candidate_k": settings.rag_candidate_k,
            "upload_dir": settings.rag_upload_dir,
        },
        "directories": [
            _upload_dir("财务导入上传目录", settings.upload_dir),
            _upload_dir("知识库上传目录", settings.rag_upload_dir),
            _upload_dir("原始数据目录（只读约定）", "data/raw"),
        ],
        "data": {"periods": periods, "latest_period": periods[0] if periods else None, "counts": counts,
                 "scope": scope_payload(department)},
        "me": {
            "id": user.id,
            "username": user.username,
            "department": user.department,
            # 系统设置页要按这一份渲染「我的部门」输入框，只给单值列会让多部门账号
            # 一进页面就只看到第一个部门，点保存就把自己缩成了一个部门。
            "departments": list(departments_of(user)),
            "unrestricted": not departments_of(user),
        },
        "config_problems": settings.validate(),
    }
