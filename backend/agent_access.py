"""Historical content requires ownership AND a currently permitted complete scope."""
from fastapi import HTTPException

from backend.scope import scope_of


def snapshot(scope):
    return {"version": 1, "departments": sorted(set(scope)) if scope is not None else None}


def allows(user, saved):
    current = scope_of(user)
    if current is None:
        return True
    # Legacy display labels can be truncated or ambiguous; never infer an ACL from them.
    if not isinstance(saved, dict) or saved.get("version") != 1:
        return False
    departments = saved.get("departments")
    return isinstance(departments, list) and all(isinstance(item, str) for item in departments) and set(departments) <= set(current)


def require_access(user, session):
    if not allows(user, session.scope_snapshot):
        raise HTTPException(403, "当前权限已不覆盖此会话的数据范围，请新建会话；历史内容不会返回。")


def merge(saved, scope):
    if not isinstance(saved, dict) or saved.get("version") != 1 or saved.get("departments") is None or scope is None:
        return snapshot(None)
    return snapshot(set(saved["departments"]) | set(scope))
