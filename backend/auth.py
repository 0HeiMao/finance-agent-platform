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
    """签发访问令牌。

    `ver` 是签发那一刻的会话版本号：JWT 是无状态的、签出去就收不回来，所以「作废」只能靠
    「令牌里的版本 ≠ 账号当前的版本」来判断（见 `current_user()`）。退出登录与管理员强制下线
    都只是把 `users.token_version` 加一。
    """
    expires = datetime.now(timezone.utc) + timedelta(minutes=get_settings().access_token_expire_minutes)
    return jwt.encode({"sub": str(user.id), "username": user.username, "ver": token_version_of(user),
                       "exp": expires}, SECRET_KEY, algorithm=ALGORITHM)


def token_version_of(user: User) -> int:
    """账号当前的会话版本号。NULL（老库、老行）一律当 0，与「老令牌没有 `ver` 声明」同一个口径。"""
    return int(user.token_version or 0)


def revoke_tokens(user: User) -> int:
    """把该账号所有已签发的令牌作废（版本号 +1），返回新的版本号。

    只加不减：版本号回退会让「上一次下线之前签发的令牌」重新生效，那是安全洞。
    """
    user.token_version = token_version_of(user) + 1
    return user.token_version

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
    return {"access_token": token_for(user), "token_type": "bearer", "token_version": token_version_of(user)}

def current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> User:
    """解析令牌并核对**会话版本号**：版本不一致就当作令牌已作废。

    没有 `ver` 声明（本功能上线之前签发的令牌）按 0 处理，所以老令牌还能用到该账号第一次
    退出登录为止——升级当天不会把所有人踢下线。
    """
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user = db.get(User, int(payload.get("sub")))
        issued = int(payload.get("ver") or 0)
    except (JWTError, TypeError, ValueError):
        user, issued = None, 0
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效")
    if issued != token_version_of(user):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已失效（该账号已在别处退出，请重新登录）")
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
            "unrestricted": not departments,
            # 会话版本号：每次退出登录/被强制下线 +1，用来解释「为什么旧令牌突然 401」
            "token_version": token_version_of(user)}


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


@router.post("/logout")
def logout(request: Request, user: User = Depends(current_user),
           db: Session = Depends(get_db)) -> dict:
    """退出登录：作废该账号**所有**已经签发的令牌，而不只是清掉浏览器里的那一份。

    JWT 是无状态的，服务端没有「已签发令牌」的名单可删，所以作废只能靠把会话版本号 +1：
    所有旧令牌里的 `ver` 从此对不上，再用就是 401（见 `current_user()`）。
    代价是**该账号在其他设备上的登录也会一起掉**——这正是退出登录该有的语义。
    """
    version = revoke_tokens(user)
    db.add(user)
    # detail 里叫 session_version 而不是 token_version：`audit._scrub()` 会把键名含
    # "token" 的字段整条丢掉（防的是把令牌原文写进日志），叫 token_version 会静默消失。
    audit.record(db, user, "user.logout", target_type="user", target_id=user.id,
                 detail={"username": user.username, "session_version": version,
                         "effect": "该账号此前签发的所有令牌立即失效"},
                 request=request)
    db.commit()
    return {"username": user.username, "token_version": version, "logged_out": True}
