"""知识资料发布与维护权限；公共发布需要显式选择且具备全公司管理范围。"""
from fastapi import HTTPException
from sqlalchemy import true

from backend.rag.pgvector_store import DocumentChunk
from backend.scope import is_admin, scope_of


def write_department(user, department: str | None, publish_public: bool) -> str | None:
    if not is_admin(user):
        raise HTTPException(403, "只有管理员可以维护知识库")
    scope = scope_of(user)
    target = (department or "").strip() or None
    if publish_public:
        if target:
            raise HTTPException(422, "公共资料不能同时指定部门")
        if scope is not None:
            raise HTTPException(403, "只有全公司管理员可以发布公共资料")
        return None
    if not target:
        raise HTTPException(422, "请选择所属部门，或明确选择发布公共资料")
    if scope is not None and target not in scope:
        raise HTTPException(403, "不能向未授权部门发布资料")
    return target


def manageable_chunks(user):
    """管理端只展示和删除可维护片段；公共资料仍可通过普通检索读取。"""
    scope = scope_of(user)
    return true() if scope is None else DocumentChunk.metadata_json["department"].astext.in_(scope)
