"""Agent 会话落库、停止生成与 token 统计（⑮）。

三件事放在一个模块里，因为它们共用同一个「回答正在生成」的状态机：

1. **会话与消息落库。** 提问和回答都进 `agent_messages`，回答一开始是 `status="streaming"`
   的占位行，结束时（正常/被停/出错）才被写回正文与用量。落下占位行的意义是：
   回答还在流的时候，前端就有一个稳定的 `message_id` 可以拿来「停止」，
   而进程崩了也能在读取时看出一条「没写完的回答」。
2. **停止生成 = 服务端标志位。** 客户端 `abort()` 只断开连接，服务端要等到下一块吐出、
   写 SSE 失败时才知道——那期间模型还在跑、token 还在烧。所以停止做成一次显式的
   `POST …/sessions/{id}/stop`：端点给这条回答的 `message_id` 置一个 `threading.Event`，
   生成器每吐一块查一次，看到就收尾。
   **这是单进程内存态**（和限流一样）：多 worker 部署时停止只对该回答所在进程有效——
   要跨进程得换成 Redis 之类的共享介质，README 里写明了这个边界。
3. **token 统计。** 模型报的用量记 `usage_source="api"`，按字数估的记 `"estimated"`，
   没调用模型（错误、或"没有证据"的固定文案）记 `None` 并把 token 记 0。
   把估算和账单混在一起报账等于自欺，界面上也分开显示。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from backend import biztime
from backend.db import SessionLocal
from backend.models import AgentMessage, AgentSession, User

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


# ---------------------------------------------------------------- 停止注册表

_STOP_EVENTS: dict[int, threading.Event] = {}
_EARLY_STOPS: set[int] = set()
_STOP_LOCK = threading.Lock()


def register(message_id: int) -> threading.Event:
    """登记一条正在生成的回答。已登记的返回同一个事件（幂等）。"""
    with _STOP_LOCK:
        event = _STOP_EVENTS.get(message_id)
        if event is None:
            event = threading.Event()
            _STOP_EVENTS[message_id] = event
            # 停止请求比流先到（前端在建会话后立刻点了停止）：登记时就把标志补上
            if message_id in _EARLY_STOPS:
                _EARLY_STOPS.discard(message_id)
                event.set()
        return event


def request_stop(message_id: int) -> bool:
    """请求停止。返回 True 表示要么这条回答正在生成、要么停止请求已先记下。"""
    with _STOP_LOCK:
        event = _STOP_EVENTS.get(message_id)
        if event is None:
            _EARLY_STOPS.add(message_id)
            return False
        event.set()
        return True


def is_stopped(message_id: int) -> bool:
    with _STOP_LOCK:
        event = _STOP_EVENTS.get(message_id)
        return bool(event is not None and event.is_set())


def release(message_id: int) -> None:
    """生成结束（不管什么结局）后必须调用，否则注册表会随问答数一直涨。"""
    with _STOP_LOCK:
        _STOP_EVENTS.pop(message_id, None)
        _EARLY_STOPS.discard(message_id)


def active_count() -> int:
    with _STOP_LOCK:
        return len(_STOP_EVENTS)


def reset() -> None:
    """清空注册表（测试与真机脚本收尾用）。"""
    with _STOP_LOCK:
        _STOP_EVENTS.clear()
        _EARLY_STOPS.clear()


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
    """读取时的状态：挂太久的 streaming 当成 interrupted（进程崩了没人写回终态）。"""
    if message.status != "streaming":
        return message.status
    created = message.created_at
    if created is None:
        return message.status
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    now = now or datetime.now(UTC)
    if now - created > timedelta(seconds=STREAMING_STALE_SECONDS):
        return "interrupted"
    return message.status


# ---------------------------------------------------------------- 写入


class SessionNotFound(LookupError):
    """会话不存在，或者不是这个用户的（不区分，免得变成别人的会话探针）。"""


def resolve_session(db, user: User, session_id: int | None, question: str, scope=None) -> tuple[AgentSession, bool]:
    """取会话；没有就建一个（标题取第一条提问）。返回 (会话, 是否新建)。"""
    if session_id is not None:
        session = db.get(AgentSession, session_id)
        if session is None or session.user_id != user.id:
            raise SessionNotFound(f"会话 {session_id} 不存在")
        if not session.title:
            session.title = derive_title(question)
        if scope is not None and not session.scope:
            session.scope = scope_text(scope)
        return session, False
    session = AgentSession(user_id=user.id, title=derive_title(question), scope=scope_text(scope))
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
    label = scope_text(scope) or session.scope
    user_message = AgentMessage(session_id=session.id, user_id=user.id, role="user",
                                content=question, status="ok", scope=label,
                                sources=[], knowledge_chunks=0)
    assistant = AgentMessage(session_id=session.id, user_id=user.id, role="assistant",
                             content="", status="streaming", scope=label,
                             sources=[], knowledge_chunks=0)
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
        "period": message.period or "",
        "knowledge_chunks": int(message.knowledge_chunks or 0),
        "scope": message.scope or "",
        "prompt_tokens": int(message.prompt_tokens or 0),
        "completion_tokens": int(message.completion_tokens or 0),
        "total_tokens": int(message.total_tokens or 0),
        "usage_source": message.usage_source or "",
        "duration_ms": message.duration_ms,
        "created_at": message.created_at,
    }


def list_sessions(db, *, user_id: int, limit: int = 50, offset: int = 0) -> tuple[list[AgentSession], int]:
    total = db.execute(select(func.count(AgentSession.id)).where(AgentSession.user_id == user_id)).scalar_one()
    rows = db.execute(
        select(AgentSession).where(AgentSession.user_id == user_id)
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
