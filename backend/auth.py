from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from backend.db import get_db
from backend.models import User
from backend.scope import departments_of
from backend.config import get_settings
from backend.ratelimit import get_login_limiter
from backend import audit

SECRET_KEY = get_settings().auth_secret_key
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], pbkdf2_sha256__default_rounds=600000)

def verify_password(password: str, stored_hash: str) -> bool:
    # 兼容已有 bcrypt 密码；新用户采用 PBKDF2，避免旧 passlib 与 bcrypt 5 的冲突。
    if stored_hash.startswith(("$2a$", "$2b$", "$2y$")):
        import bcrypt
        try:
            return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('ascii'))
        except ValueError:
            return False
    return pwd_context.verify(password, stored_hash)
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")
router = APIRouter(prefix="/api/v1/auth", tags=["auth"])

class Credentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=6, max_length=128)

def token_for(user: User) -> str:
    expires = datetime.now(timezone.utc) + timedelta(minutes=get_settings().access_token_expire_minutes)
    return jwt.encode({"sub": str(user.id), "username": user.username, "exp": expires}, SECRET_KEY, algorithm=ALGORITHM)

@router.post("/register")
def register(payload: Credentials, request: Request, db: Session = Depends(get_db)):
    if db.scalar(select(User).where(User.username == payload.username)):
        raise HTTPException(409, "用户名已存在")
    user = User(username=payload.username, password_hash=pwd_context.hash(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "用户名已存在")
    db.refresh(user)
    # 只记用户名，不记密码（audit._scrub 还会再兜一层）
    audit.record(db, user, "user.register", target_type="user", target_id=user.id,
                 detail={"username": user.username}, request=request, commit=True)
    return {"id": user.id, "username": user.username}

@router.post("/login")
def login(payload: Credentials, request: Request, db: Session = Depends(get_db)):
    """登录。失败尝试按「用户名 + 来源」限速，超限返回 429（见 backend/ratelimit.py）。"""
    limiter = get_login_limiter()
    client = request.client.host if request.client else "unknown"
    key = f"{payload.username.strip().lower()}@{client}"
    decision = limiter.check(key)
    if not decision.allowed:
        audit.record(db, payload.username, "user.login_blocked", target_type="user",
                     detail={"retry_after": decision.retry_after, "reason": "限流"},
                     request=request, commit=True)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"登录尝试过于频繁，请在 {decision.retry_after} 秒后重试",
            headers={"Retry-After": str(decision.retry_after)},
        )
    user = db.scalar(select(User).where(User.username == payload.username))
    if not user or not verify_password(payload.password, user.password_hash):
        audit.record(db, payload.username, "user.login_failed", target_type="user",
                     detail={"username": payload.username, "reason": "用户名或密码错误"},
                     request=request, commit=True)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    limiter.reset(key)
    audit.record(db, user, "user.login", target_type="user", target_id=user.id,
                 detail={"username": user.username}, request=request, commit=True)
    return {"access_token": token_for(user), "token_type": "bearer"}

def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    try:
        user_id = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM]).get("sub")
        user = db.get(User, int(user_id))
    except (JWTError, TypeError, ValueError):
        user = None
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    return user


class DepartmentInput(BaseModel):
    # 单值字段保留（老客户端 PATCH 的就是它）；多部门用列表，两个都给时以列表为准。
    department: str | None = Field(default=None, max_length=100)
    departments: list[str] | None = Field(default=None, max_length=20)


def _profile(user: User) -> dict:
    """账号档案：部门列表是权威值，`department` 只是给老前端的单值回落。"""
    departments = list(departments_of(user))
    return {"id": user.id, "username": user.username,
            "department": departments[0] if departments else None,
            "departments": departments,
            "unrestricted": not departments}


@router.get("/me")
def read_me(user: User = Depends(current_user)) -> dict:
    return _profile(user)


@router.patch("/me")
def update_me(payload: DepartmentInput, request: Request, user: User = Depends(current_user),
              db: Session = Depends(get_db)) -> dict:
    """设置/清空自己的部门（可以一次给多个）。留空表示不限制部门（财务管理员口径）。

    刻意不让前端用请求头自述部门：那样任何登录用户都能伪造 finance-admin 看到全部资料。
    """
    if payload.departments is not None:
        wanted = [item.strip() for item in payload.departments if (item or "").strip()]
    else:
        single = (payload.department or "").strip()
        wanted = [single] if single else []
    previous = "、".join(departments_of(user)) or "不受限"
    user.departments = wanted or None
    # 单值列同步成第一个部门，只用于展示/审计文案（读权限一律走 scope_of）。
    user.department = wanted[0] if wanted else None
    db.add(user)
    # 改自己的部门 = 改自己的可见范围，必须留痕（旧值 → 新值）
    audit.record(db, user, "user.department_self", target_type="user", target_id=user.id,
                 detail={"from": previous, "to": "、".join(wanted) or "不受限", "username": user.username},
                 request=request)
    db.commit()
    db.refresh(user)
    return _profile(user)
