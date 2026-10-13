"""Agent 会话落库、停止生成与 token 统计（⑮）。

三件事放在一个模块里，因为它们共用同一个「回答正在生成」的状态机：

1. **会话与消息落库。** 提问和回答都进 `agent_messages`，回答一开始是 `status="streaming"`
   的占位行，结束时（正常/被停/出错）才被写回正文与用量。落下占位行的意义是：
   回答还在流的时候，前端就有一个稳定的 `message_id` 可以拿来「停止」，
   而进程崩了也能在读取时看出一条「没写完的回答」。
2. **停止生成 = 服务端标志位（㉒ 起落库）。** 客户端 `abort()` 只断开连接，服务端要等到下一块
   吐出、写 SSE 失败时才知道——那期间模型还在跑、token 还在烧。所以停止做成一次显式的
   `POST …/sessions/{id}/stop`：端点把「用户点过停止」写进 `agent_messages.stop_requested_at`，
   生成器**按节拍读库**、看到就收尾；同一进程里还有一条本地事件快路径（零查询）。
   ⑮ 最初这只是一个进程内的 `threading.Event`，于是多 worker 部署时停止请求落在别的 worker 上
   就等于没点（README 里当时把这条写成了已知边界）；㉒ 把事实搬进库里 —— 与 ⑱ 的任务执行权、
   ㉑ 的限流计数同一个道理：**进程里的状态只是缓存，事实要在库里**。
   附带的好处是重启后读得出区别：挂着不动的 streaming 有这一列就是「已停止」，没有才是「已中断」。
3. **token 统计。** 模型报的用量记 `usage_source="api"`，按字数估的记 `"estimated"`，
   没调用模型（错误、或"没有证据"的固定文案）记 `None` 并把 token 记 0。
   把估算和账单混在一起报账等于自欺，界面上也分开显示。
4. **多轮上下文（⑰）。** 落库的消息同时是这个会话的"记忆"：`history_for` 按预算挑出最近
   几轮写完的问答，交给模型才答得了「那上个月呢？」。挑法（只带成对写完的、从最近往回、
   整对进整对出）与那两个成本旋钮见 `history_for` 的 docstring。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from backend import biztime
from backend.db import SessionLocal
from backend.models import AgentMessage, AgentSession, User

logger = logging.getLogger(__name__)

ROLES = ("user", "assistant")
ROLE_LABELS = {"user": "提问", "assistant": "回答"}

# streaming 是"正在生成"；ok/stopped/error 都是终态；interrupted 是读取时把超时的 streaming
# 归一出来的（进程崩了没人写回，只能靠时间判断），它和 stopped 的区别是"停"是不是用户要的。
STATUSES = ("streaming", "ok", "stopped", "error", "interrupted")
STATUS_LABELS = {
    "streaming": "生成中",
    "ok": "已完成",
    "stopped": "已停止",
    "error": "出错",
    "interrupted": "已中断",
}

TITLE_MAX = 120
UNTITLED = "未命名会话"
# 超过这个时长还挂在 streaming 上的回答，读取时按"已中断"显示：进程被杀/重启时没人写回终态
STREAMING_STALE_SECONDS = 120
# 中文大致 1.5 字一个 token（含标点与英文）；只用于"模型没报用量"时的量级估算
CHARS_PER_TOKEN = 1.5

SESSION_FACTORY = SessionLocal


def open_session():
    """流式生成器跑在 Starlette 的线程池里，要用自己的会话（请求会话那时可能已经关了）。

    做成模块级可替换点是为了测试：用例里换成绑在测试事务上的会话，
    生成器写的东西才会随用例回滚，而不是真写进演示库。
    """
    return SESSION_FACTORY()


@contextmanager
def _session(db=None):
    """有现成的会话就用它（端点），没有就开一条短的读完即关（生成器的读库节拍）。"""
    if db is not None:
        yield db
        return
    session = open_session()
    try:
        yield session
    finally:
        session.close()


# ---------------------------------------------------------------- 停止注册表
#
# ㉒ 起「用户点过停止」这件事的**事实**在 `agent_messages.stop_requested_at` 里，
# 进程内的事件只是一条快路径（同一个 worker 上零查询地查到）。这样：
#   ① 停止请求落在别的 worker 上也能生效（生成器按节拍读库，而不是只在本地事件里找）；
#   ② 进程重启后，一条挂着的 streaming 有停止标志就显示「已停止」，没有才是「已中断」；
#   ③ 不再需要 `_EARLY_STOPS` 那套「停止比流先到」的补记 —— 占位行在流开始前就落库了，
#      「停止先到」就是这一列已经有值，生成器第一次检查（构造后立刻做，见 `_last_poll` 初值）
#      就会读到它。

_STOP_EVENTS: dict[int, StopSignal] = {}
_STOP_LOCK = threading.Lock()
# 生成器隔多久回头看一眼库（秒）。1 秒是「停得够快」与「别把库问爆」之间的折中：
# 真模型一次回答通常几十块、几秒到几十秒，1 秒的延迟用户感觉不到，而每块一查库没必要。
STOP_POLL_SECONDS = 1.0
_poll_seconds = STOP_POLL_SECONDS


def poll_seconds() -> float:
    return _poll_seconds


def set_poll_seconds(value: float | None) -> float:
    """调节读库节拍（测试与真机脚本用；`None` = 还原默认值）。返回生效值。"""
    global _poll_seconds
    _poll_seconds = STOP_POLL_SECONDS if value is None else max(0.0, float(value))
    return _poll_seconds


def stop_requested(message_id: int, *, db=None) -> bool:
    """库里的停止标志（㉒）。读不到行（已被删）也返回 False —— 没有事实就是「没点过」。"""
    with _session(db) as session:
        value = session.execute(
            select(AgentMessage.stop_requested_at).where(AgentMessage.id == message_id)
        ).scalar_one_or_none()
    return value is not None


class StopSignal:
    """一条正在生成的回答的「该不该停」句柄：本地事件是快路径，事实在库里。

    接口与 `threading.Event` 一致（只用了 `is_set()` 与 `set()`），生成器那边看不出区别。
    **库读不出来时不把它当成"用户要停"**：取消是尽力而为的动作，一次查询失败不该把用户
    正在等的回答掐掉——这与限流（㉑）必须 fail-closed 是两回事，那边是授权闸门，
    读不到就必须拒绝；这边读不到只是这一拍没看见停止请求，下一拍再问。
    """

    def __init__(self, message_id: int):
        self.message_id = message_id
        self._event = threading.Event()
        # 初值是 -inf：第一次检查一定读一次库，所以「停止比第一块先到」也能立刻生效
        self._last_poll = float("-inf")

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        if self._event.is_set():
            return True
        now = time.monotonic()
        if now - self._last_poll < poll_seconds():
            return False
        self._last_poll = now
        try:
            if stop_requested(self.message_id):
                self._event.set()
        except SQLAlchemyError as exc:      # 库连不上/表还没建：这一拍当作没看见
            logger.warning("读停止标志失败（这一拍当作没停）：message_id=%s %s",
                           self.message_id, exc.__class__.__name__)
        return self._event.is_set()


def register(message_id: int) -> StopSignal:
    """登记一条正在生成的回答。已登记的返回同一个句柄（幂等，且不重置读库节拍）。"""
    with _STOP_LOCK:
        signal = _STOP_EVENTS.get(message_id)
        if signal is None:
            signal = StopSignal(message_id)
            _STOP_EVENTS[message_id] = signal
        return signal


def request_stop(db, message_id: int) -> bool:
    """请求停止（㉒）：把标志写进库里，同进程里顺手置一下本地事件。

    返回 True 表示这条回答**此刻**确实还在 streaming、标志已经写下去；False 表示它已经
    不在生成了（调用方应当如实告诉用户「已经结束了」，而不是回一个 stopped=true）。
    **不 commit**：调用方（端点）用自己的请求会话提交，提交之后别的进程才看得见。
    """
    result = db.execute(
        update(AgentMessage)
        .where(AgentMessage.id == message_id, AgentMessage.status == "streaming")
        .values(stop_requested_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )
    if not result.rowcount:
        return False
    with _STOP_LOCK:
        signal = _STOP_EVENTS.get(message_id)
    if signal is not None:
        signal.set()
    return True


def is_stopped(message_id: int, *, db=None) -> bool:
    """本地事件（快路径）或库里的标志。测试与真机脚本用它判断"停下来了没有"。"""
    with _STOP_LOCK:
        signal = _STOP_EVENTS.get(message_id)
    if signal is not None and signal._event.is_set():
        return True
    return stop_requested(message_id, db=db)


def release(message_id: int) -> None:
    """生成结束（不管什么结局）后必须调用，否则注册表会随问答数一直涨。

    **不动库里的标志**：终态行要留着它，读取时才能把「用户停的」与「进程崩的」分开
    （`effective_status`）。本地句柄删掉不影响这件事。
    """
    with _STOP_LOCK:
        _STOP_EVENTS.pop(message_id, None)


def active_count() -> int:
    with _STOP_LOCK:
        return len(_STOP_EVENTS)


def reset() -> None:
    """清空本地注册表（测试与真机脚本收尾用）。库里的标志归 `agent_messages` 自己管。"""
    with _STOP_LOCK:
        _STOP_EVENTS.clear()


# ---------------------------------------------------------------- 小工具


def scope_text(departments) -> str | None:
    """把 resolve_requested_scope 的结果压成一行存进会话/消息，供列表里解释口径。"""
    if not departments:
        return None
    if isinstance(departments, str):
        return departments[:100]
    return ", ".join(str(item) for item in departments)[:100] or None


def derive_title(question: str, limit: int = TITLE_MAX) -> str:
    """用第一条提问当会话标题：空白压成一个空格，超长截断加省略号。"""
    text = " ".join((question or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def estimate_tokens(*texts: str) -> int:
    total = sum(len(text or "") for text in texts)
    if total <= 0:
        return 0
    return max(1, round(total / CHARS_PER_TOKEN))


def effective_status(message: AgentMessage, now: datetime | None = None) -> str:
    """读取时的状态：挂太久的 streaming 归一成终态 —— 是「用户停的」还是「进程崩的」看标志位。

    ㉒ 之后两种挂法在库里是分得开的：`stop_requested_at` 有值 = 用户明确点过停止，
    只是那一块之后再没有进程写回终态（多 worker、或重启）；没有值 = 进程崩了没人管。
    都算 `interrupted` 会把用户自己要的停止说成故障。
    """
    if message.status != "streaming":
        return message.status
    created = message.created_at
    if created is None:
        return message.status
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now - created > timedelta(seconds=STREAMING_STALE_SECONDS):
        return "stopped" if getattr(message, "stop_requested_at", None) is not None else "interrupted"
    return message.status


# ---------------------------------------------------------------- 写入


class SessionNotFound(LookupError):
    """会话不存在，或者不是这个用户的（不区分，免得变成别人的会话探针）。"""


def resolve_session(db, user: User, session_id: int | None, question: str, scope=None) -> tuple[AgentSession, bool]:
    """取会话；没有就建一个（标题取第一条提问）。返回 (会话, 是否新建)。"""
    if session_id is not None:
        session = db.scalar(select(AgentSession).where(AgentSession.id == session_id).with_for_update())
        if session is None or session.user_id != user.id:
            raise SessionNotFound(f"会话 {session_id} 不存在")
        from backend.agent_access import merge, require_access
        require_access(user, session)
        session.scope_snapshot = merge(session.scope_snapshot, scope)
        if not session.title:
            session.title = derive_title(question)
        if scope is not None and not session.scope:
            session.scope = scope_text(scope)
        return session, False
    from backend.agent_access import snapshot
    session = AgentSession(user_id=user.id, title=derive_title(question), scope=scope_text(scope), scope_snapshot=snapshot(scope))
    db.add(session)
    db.flush()
    return session, True


def start_exchange(db, user: User, question: str, *, session_id: int | None = None,
                   scope=None) -> tuple[AgentSession, AgentMessage, AgentMessage]:
    """开一轮问答：写「用户提问 + 助手占位（streaming）」，返回三行。

    调用方负责 commit——端点先提交再返回 StreamingResponse，生成器才看得到占位行；
    否则生成器只能用自己那条连接去写一行还不存在（或不可见）的消息。
    """
    session, _ = resolve_session(db, user, session_id, question, scope=scope)
    # resolve_session locks the row: concurrent tabs cannot launch another paid run in this session.
    active = streaming_message(db, session.id)
    if active is not None and effective_status(active) == "streaming":
        from fastapi import HTTPException
        raise HTTPException(409, "此会话仍有回答正在生成，请先恢复结果或停止生成")
    from backend.agent_access import snapshot
    label = scope_text(scope)
    user_message = AgentMessage(session_id=session.id, user_id=user.id, role="user",
                                content=question, status="ok", scope=label,
                                sources=[], knowledge_chunks=0, scope_snapshot=snapshot(scope))
    assistant = AgentMessage(session_id=session.id, user_id=user.id, role="assistant",
                             content="", status="streaming", scope=label,
                             sources=[], knowledge_chunks=0, scope_snapshot=snapshot(scope))
    db.add_all([user_message, assistant])
    session.updated_at = datetime.now(UTC)
    # autoflush=False：不 flush 就拿不到两个新行的 id，SSE 的 event: start 也就报不出 message_id
    db.flush()
    return session, user_message, assistant


def finish_exchange(db, message: AgentMessage, *, content: str, status: str = "ok", sources=None,
                    period: str | None = None, knowledge_chunks: int = 0, usage: dict | None = None,
                    prompt_text: str = "", count_estimate: bool = True,
                    duration_ms: int | None = None, error: str | None = None) -> AgentMessage:
    """把生成结果写回占位行。**不 commit**（调用方决定什么时候落盘）。"""
    message.content = content or ""
    message.status = status
    message.error = (error or "")[:2000] or None
    message.sources = list(sources or [])
    message.period = period
    message.knowledge_chunks = int(knowledge_chunks or 0)
    if duration_ms is not None:
        message.duration_ms = int(duration_ms)

    usage = usage or {}
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    total = int(usage.get("total_tokens") or 0)
    if prompt or completion or total:
        # 模型报了用量：这是账单口径
        message.prompt_tokens = prompt
        message.completion_tokens = completion
        message.total_tokens = total or prompt + completion
        message.usage_source = "api"
    elif count_estimate and status in ("ok", "stopped"):
        # 模型没报（老网关 / 流式回退），按字数估：量级对，但不是账单
        message.prompt_tokens = estimate_tokens(prompt_text)
        message.completion_tokens = estimate_tokens(message.content)
        message.total_tokens = message.prompt_tokens + message.completion_tokens
        message.usage_source = "estimated"
    else:
        # 没调用模型（错误、或"没有证据"的固定文案）：记 0，别把平台自己的话算成模型用量
        message.prompt_tokens = 0
        message.completion_tokens = 0
        message.total_tokens = 0
        message.usage_source = None
    db.flush()
    return message


def delete_session(db, session: AgentSession) -> int:
    """删会话连同消息。不靠 ORM 级联（模型里没有 relationship），显式删更清楚。"""
    removed = db.execute(delete(AgentMessage).where(AgentMessage.session_id == session.id)).rowcount or 0
    db.delete(session)
    db.flush()
    return int(removed)


# ---------------------------------------------------------------- 读取


def session_counts(db, ids: list[int]) -> dict[int, dict]:
    """一次查完各会话的消息数与累计 token（列表页一屏几十行也只有一个查询）。"""
    if not ids:
        return {}
    rows = db.execute(
        select(AgentMessage.session_id,
               func.count(AgentMessage.id),
               func.coalesce(func.sum(AgentMessage.total_tokens), 0))
        .where(AgentMessage.session_id.in_(ids), AgentMessage.role == "assistant")
        .group_by(AgentMessage.session_id)
    ).all()
    return {session_id: {"answers": int(count or 0), "total_tokens": int(tokens or 0)}
            for session_id, count, tokens in rows}


def session_payload(session: AgentSession, counts: dict | None = None) -> dict:
    counts = counts or {}
    return {
        "id": session.id,
        "title": session.title or "",
        "display_title": session.title or UNTITLED,
        "scope": session.scope or "",
        "answers": int(counts.get("answers") or 0),
        "total_tokens": int(counts.get("total_tokens") or 0),
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def message_payload(message: AgentMessage, now: datetime | None = None) -> dict:
    status = effective_status(message, now=now)
    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "role_label": ROLE_LABELS.get(message.role, message.role),
        "content": message.content or "",
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "error": message.error or "",
        "sources": list(message.sources or []),
        "execution_trace": list(message.execution_trace or []),
        "period": message.period or "",
        "knowledge_chunks": int(message.knowledge_chunks or 0),
        "scope": message.scope or "",
        "prompt_tokens": int(message.prompt_tokens or 0),
        "completion_tokens": int(message.completion_tokens or 0),
        "total_tokens": int(message.total_tokens or 0),
        "usage_source": message.usage_source or "",
        "duration_ms": message.duration_ms,
        # ㉒ 用户点过停止没有（事实在库里）。前端据此把「已停止」与「已中断」分开说，
        # 真机脚本也用它断言「停止请求确实落到了行上」。
        "stop_requested": getattr(message, "stop_requested_at", None) is not None,
        "created_at": message.created_at,
    }


def list_sessions(db, *, user_id: int, limit: int = 50, offset: int = 0, user=None) -> tuple[list[AgentSession], int]:
    from backend.scope import scope_of
    filters = [AgentSession.user_id == user_id]
    if user is not None and scope_of(user) is not None:
        filters += [AgentSession.scope_snapshot["version"].as_integer() == 1,
                    AgentSession.scope_snapshot["departments"].contained_by(list(scope_of(user)))]
    total = db.execute(select(func.count(AgentSession.id)).where(*filters)).scalar_one()
    rows = db.execute(
        select(AgentSession).where(*filters)
        .order_by(AgentSession.updated_at.desc(), AgentSession.id.desc())
        .limit(limit).offset(offset)
    ).scalars().all()
    return list(rows), int(total or 0)


def messages_of(db, session_id: int, *, limit: int = 400) -> list[AgentMessage]:
    rows = db.execute(
        select(AgentMessage).where(AgentMessage.session_id == session_id)
        .order_by(AgentMessage.created_at.asc(), AgentMessage.id.asc())
        .limit(limit)
    ).scalars().all()
    return list(rows)


def history_for(db, session_id: int, *, exclude_ids=(), max_turns: int, max_tokens: int,
                limit: int = 400, user=None, scope_filter=None) -> dict:
    """挑出这个会话里能当上下文用的历史问答（⑰）——「那上个月呢？」能不能答，全看这里。

    三条规则，都是为了让「多带一点上下文」不变成「把成本带飞」：

    1. **只带写完的成对问答。** `error`/`interrupted`/还在 `streaming` 的回答不进上下文，
       空正文同样跳过：它们是"没说完的话"，带上去只会让模型顺着半句话往下编。
       缺了回答的提问（用户问了但那一轮失败了）也整对丢掉，不给模型半轮对话。
    2. **从最近往回挑，整对进整对出。** 超预算就从**最老的一整对**开始丢——宁可少一轮，
       也不给半轮。`max_turns` 按问答对数算，`max_tokens` 按 `estimate_tokens` 估算。
    3. **任一为 0 就等于关掉多轮**（行为回到⑯之前：只带本轮的检索证据）。

    `exclude_ids` 是本轮刚落库的提问与占位回答：占位回答正文是空的，本来就进不了上下文，
    但显式排除能让"这一轮"与"历史"的边界由调用方说了算，而不是靠正文非空这个副作用。
    返回值里的 `truncated` 只表示"有更早的对话没带上"，不区分是被预算截断还是被关掉。
    """
    from backend.agent_access import allows, require_access
    if user is not None:
        session = db.get(AgentSession, session_id)
        if session is None or session.user_id != user.id:
            raise SessionNotFound("会话不存在")
        require_access(user, session)
    rows = messages_of(db, session_id, limit=limit)
    excluded = {int(item) for item in exclude_ids}
    pairs: list[list[AgentMessage]] = []
    pending: AgentMessage | None = None
    for row in rows:
        if user is not None and (not allows(user, row.scope_snapshot) or (scope_filter is not None and row.scope_snapshot != scope_filter)):
            pending = None
            continue
        if row.id in excluded:
            continue
        if row.role == "user":
            pending = row
            continue
        if row.role != "assistant" or pending is None:
            continue
        content = (row.content or "").strip()
        if row.status not in ("ok", "stopped") or not content or not (pending.content or "").strip():
            pending = None  # 这一轮没写完（或提问是空的）：整对丢掉，不给模型半轮对话
            continue
        pairs.append([pending, row])
        pending = None

    turns_budget = max(0, int(max_turns or 0))
    token_budget = max(0, int(max_tokens or 0))
    chosen: list[list[AgentMessage]] = []
    used = 0
    if turns_budget and token_budget:
        for pair in reversed(pairs):
            cost = estimate_tokens(pair[0].content, pair[1].content)
            if len(chosen) >= turns_budget or used + cost > token_budget:
                break
            chosen.append(pair)
            used += cost
        chosen.reverse()
    messages = [{"role": row.role, "content": (row.content or "").strip()}
                for pair in chosen for row in pair]
    return {
        "messages": messages,
        "turns": len(chosen),
        "tokens": used,
        "truncated": len(chosen) < len(pairs),
        "available_turns": len(pairs),
    }


def streaming_message(db, session_id: int) -> AgentMessage | None:
    """该会话当前正在生成的那条回答（停止生成要找它）。"""
    return db.execute(
        select(AgentMessage)
        .where(AgentMessage.session_id == session_id, AgentMessage.role == "assistant",
               AgentMessage.status == "streaming")
        .order_by(AgentMessage.id.desc())
        .limit(1)
    ).scalars().first()


def usage_stats(db, *, user_id: int | None = None, days: int = 30,
                now: datetime | None = None) -> dict:
    """按天/按状态（管理员再加按人）统计问答用量。`user_id=None` = 全平台口径。"""
    now = biztime.to_business(now) if now is not None else biztime.now()
    # 窗口是「含今天的 N 个自然日」，起点对齐到那一天的 00:00（**业务时区**，⑯）；
    # ① by_day 与 daily_series 从同一天开始铺，天数严格对齐，今天的问答不会从图上消失；
    # ② `agent_messages.created_at` 用的是数据库的 now()（事务开始时刻，可能早于这里的
    #    Python 时刻），若按「此刻」当起点，同一事务里刚写下的行会被 `created_at >= start` 筛掉；
    # ③ 用业务时区而不是 UTC：UTC+8 的凌晨属于「今天」而不是「昨天」，否则早上八点前的问答
    #    会被算到前一天，与页面上写的日期对不上。
    start_date = (now - timedelta(days=max(1, days) - 1)).date()
    start = biztime.day_start(start_date)
    base = [AgentMessage.role == "assistant", AgentMessage.created_at >= start]
    if user_id is not None:
        base.append(AgentMessage.user_id == user_id)

    def totals(*extra):
        return db.execute(
            select(func.count(AgentMessage.id),
                   func.coalesce(func.sum(AgentMessage.prompt_tokens), 0),
                   func.coalesce(func.sum(AgentMessage.completion_tokens), 0),
                   func.coalesce(func.sum(AgentMessage.total_tokens), 0))
            .where(*base, *extra)
        ).one()

    answers, prompt_tokens, completion_tokens, total_tokens = totals()
    questions = db.execute(
        select(func.count(AgentMessage.id)).where(
            AgentMessage.role == "user", AgentMessage.created_at >= start,
            *([AgentMessage.user_id == user_id] if user_id is not None else []))
    ).scalar_one()
    sessions = db.execute(
        select(func.count(func.distinct(AgentMessage.session_id)))
        .where(*base)
    ).scalar_one()

    by_status = []
    for status, count in db.execute(
        select(AgentMessage.status, func.count(AgentMessage.id)).where(*base).group_by(AgentMessage.status)
    ).all():
        label = STATUS_LABELS.get(status, status)
        by_status.append({"status": status, "label": label, "count": int(count or 0)})
    by_status.sort(key=lambda item: item["status"])

    # 按业务时区切天（⑯）：`func.date(timestamptz)` 取的是**数据库会话时区**的日期（通常是 UTC），
    # 直接 group by 会和上面的窗口起点错开一天，凌晨的回答落到「昨天」那一格。
    business_day = func.date(func.timezone(biztime.current_name(), AgentMessage.created_at)).label("day")
    by_day_rows = db.execute(
        select(business_day,
               func.count(AgentMessage.id),
               func.coalesce(func.sum(AgentMessage.total_tokens), 0))
        .where(*base)
        .group_by("day")
        .order_by("day")
    ).all()
    by_day = [{"date": str(day), "answers": int(count or 0), "total_tokens": int(tokens or 0)}
              for day, count, tokens in by_day_rows]

    usage_rows = db.execute(
        select(AgentMessage.usage_source, func.count(AgentMessage.id)).where(*base)
        .group_by(AgentMessage.usage_source)
    ).all()
    usage_sources = {(source or ""): int(count or 0) for source, count in usage_rows}

    stats = {
        "days": days,
        "window_start": start,
        "answers": int(answers or 0),
        "questions": int(questions or 0),
        "sessions": int(sessions or 0),
        "prompt_tokens": int(prompt_tokens or 0),
        "completion_tokens": int(completion_tokens or 0),
        "total_tokens": int(total_tokens or 0),
        "api_answers": int(usage_sources.get("api", 0)),
        "estimated_answers": int(usage_sources.get("estimated", 0)),
        "free_answers": int(usage_sources.get("", 0)),
        "by_status": by_status,
        "by_day": by_day,
    }
    stats["stopped"] = next((item["count"] for item in by_status if item["status"] == "stopped"), 0)
    stats["failed"] = next((item["count"] for item in by_status if item["status"] in ("error", "interrupted")), 0)

    if user_id is None:
        rows = db.execute(
            select(User.username, User.department, User.departments, func.count(AgentMessage.id),
                   func.coalesce(func.sum(AgentMessage.total_tokens), 0))
            .join(User, User.id == AgentMessage.user_id)
            .where(*base)
            .group_by(User.username, User.department, User.departments)
            .order_by(func.coalesce(func.sum(AgentMessage.total_tokens), 0).desc(),
                      func.count(AgentMessage.id).desc())
        ).all()
        by_user = []
        for username, department, departments, count, tokens in rows:
            scope = department or ""
            if departments:
                scope = "、".join(str(item) for item in departments)
            by_user.append({"username": username, "department": scope or "不受限",
                            "answers": int(count or 0), "total_tokens": int(tokens or 0)})
        stats["by_user"] = by_user
    return stats


def daily_series(stats: dict) -> list[dict]:
    """给前端画图：把 by_day 补齐到窗口内的每一天（没问的日子也是 0，曲线才不会跳）。"""
    days = int(stats.get("days") or 0)
    if days <= 0:
        return list(stats.get("by_day") or [])
    filled: dict[str, dict] = defaultdict(lambda: {"answers": 0, "total_tokens": 0})
    for item in stats.get("by_day") or []:
        filled[item["date"]] = {"answers": item["answers"], "total_tokens": item["total_tokens"]}
    # window_start 由 usage_stats 给出（一定是带业务时区的 datetime），这里的兜底只防手搓的 stats
    start = stats.get("window_start") or biztime.now()
    start_date = biztime.to_business(start).date()
    series = []
    for offset in range(days):
        day = str(start_date + timedelta(days=offset))
        series.append({"date": day, **filled[day]})
    return series
