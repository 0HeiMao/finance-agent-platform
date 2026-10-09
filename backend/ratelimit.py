"""接口限流与出口配额 —— 计数落在 PostgreSQL 里，多个进程与重启共享同一本账（㉑）。

三组闸门，各自独立计数：

- `login`：按「用户名 + 来源 IP」分桶的登录尝试（默认 10 次 / 300 秒）。
  密码只有长度下限、没有复杂度要求，不限速等于允许随便试；
- `export`：明细/指标/预警导出（默认 30 次 / 分钟）。`EXPORT_LIMIT` 只限制**单次**行数，
  拦不住「脚本每分钟导 100 次」；
- `ai`：RAG 问答与财务 Agent（含流式，默认 60 次 / 分钟）。每次都要调模型，按量计费。

登录被限流不该影响看数据，导出打满也不该让人问不出问题来 —— 所以是三本账，不是一本。

**为什么计数落库（㉑ 相对之前的唯一变化）**：这一层原来是一个进程内的 `deque`，
于是 10 次 / 300 秒的登录闸门在多 worker 下变成「每个 worker 各 10 次」、重启一次就清零。
这两条以前只能写进 README 当已知边界，现在换成库里的一行行尝试：

- **多进程共享**：所有进程查同一张 `rate_limit_hits`，同一个桶谁先谁后由
  `pg_advisory_xact_lock` 串行（key 由桶名哈希出来，与期间锁/任务锁同一套哈希、不同命名空间）；
- **重启不清零**：进程里没有任何计数状态，重启后读到的还是库里那些尝试时间戳；
- **窗口语义与原来一字不差**：存的是尝试的**时间戳**而不是一个计数，所以还是真正的滑动窗口，
  不会退化成「固定窗口边界突发翻倍」。被拒绝的尝试**不落库**（与旧的 `deque` 版本一致）：
  窗口里已经攒够 `limit` 次就拒绝，直到最早那一条滑出窗口 —— 拒绝本身不延长封锁时间。

代价与取舍写清楚：

- 每次判定多几条数据库往返（同桶裁剪 + 取窗口 + 插入，都走 `(bucket, hit_at)` 复合索引）。
  被限的这三个出口本来就贵（导出拖全库、问答调模型、登录要查库验口令），这几条 SQL 不构成负担；
- **fail-closed**：库连不上时判定抛错（请求 500，带请求 id 与安全头），而不是静默放行。
  对一个反爆破的闸门来说，这比对攻击者敞开要正确；
- 表会随尝试增长，但每个桶只留最近 `window` 内的行（`_prune`），另有全局清扫
  （`purge_expired`，每进程最多每 60 秒一次）兜住没人再碰的桶；启动时也会扫一次并在日志里报数。

排障用的读接口：`snapshot()` 按桶给出条数与最早/最晚尝试时间，`tracked_buckets()` 给桶数，
`/system/status` 的 `rate_limits` 段用的就是它们。
"""
from __future__ import annotations

import hashlib
import math
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend.db import SessionLocal

# advisory lock 的命名空间：key 空间是整个库共享的，加前缀与期间锁（finance:recalc）、
# 任务锁（finance:job-claim / finance:job-resume）分开，免得撞上同一个数字。
NAMESPACE = "finance:ratelimit"

# 三组配额的登记名（`/system/status` 与文档按这个顺序列）。
NAMES = ("login", "export", "ai")

# 通用配额的窗口：一分钟。登录有自己的窗口（配置项 LOGIN_WINDOW_SECONDS）。
LIMIT_WINDOW_SECONDS = 60.0

# 全局清扫的保留时长与频率：任何窗口都不会超过一小时，所以一小时前的行一定是死的。
# 频率只是「别每个请求都扫一次」的节流，进程级的，重启（甚至多副本各扫一次）都无害。
SWEEP_KEEP_SECONDS = 3600.0
SWEEP_INTERVAL_SECONDS = 60.0

# 上一次全局清扫的单调时刻（进程内节流）。初值 0 = 启动后第一次 consume 就会扫一次。
_last_sweep = 0.0


@dataclass
class Decision:
    """一次限流判定：allowed=False 时 retry_after 是建议等待的秒数。"""

    allowed: bool
    retry_after: int = 0
    remaining: int = 0


# ── SQL：全部用**数据库的时钟**（`now()`）─────────────────────────────────────
#
# 为什么不在 Python 里取时间：多个副本的机器时钟不一定对得上（容器里尤其常见），
# 窗口边界由「谁的表快」决定的话，同一本账会被读成两个窗口。让 Postgres 统一给时间戳，
# 顺带获得一个好处：一次事务里 `now()` 是固定的（事务开始时刻），
# 所以「判定的当下」与「插入的 hit_at」天然是同一个值，不用额外对齐。

_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:key)")

# 同桶裁剪：把滑出窗口的行删掉。放在判定之前，于是后面那句聚合取到的就是窗口内的全集，
# 不必在 Python 里再过滤一遍（`decide()` 里仍会过滤，那是给纯逻辑单测与防御用的）。
_PRUNE_SQL = text(
    "DELETE FROM rate_limit_hits"
    " WHERE bucket = :bucket AND hit_at <= now() - make_interval(secs => :window)")

# 一次往返同时拿到「库里的现在」与「这个桶里所有尝试的时间戳」。
_WINDOW_SQL = text(
    "SELECT extract(epoch FROM now())::float8 AS db_now,"
    "       COALESCE(array_agg(extract(epoch FROM hit_at) ORDER BY hit_at),"
    "                ARRAY[]::float8[]) AS hits"
    "  FROM rate_limit_hits WHERE bucket = :bucket")

_INSERT_SQL = text("INSERT INTO rate_limit_hits (bucket, hit_at) VALUES (:bucket, now())")

_DELETE_BUCKET_SQL = text("DELETE FROM rate_limit_hits WHERE bucket = :bucket")
_DELETE_PREFIX_SQL = text("DELETE FROM rate_limit_hits WHERE bucket LIKE :pattern")

_SWEEP_SQL = text(
    "DELETE FROM rate_limit_hits WHERE hit_at <= now() - make_interval(secs => :keep)")

_COUNT_BUCKETS_SQL = text("SELECT count(DISTINCT bucket) FROM rate_limit_hits")
_COUNT_PREFIX_SQL = text("SELECT count(DISTINCT bucket) FROM rate_limit_hits WHERE bucket LIKE :pattern")

# 这段故意是**普通字符串**（不是 text(...)）：可按前缀拼一段 WHERE 再交给 text()。
_SNAPSHOT_SQL = (
    "SELECT bucket, count(*) AS hits,"
    "       extract(epoch FROM min(hit_at))::float8 AS oldest,"
    "       extract(epoch FROM max(hit_at))::float8 AS newest,"
    "       extract(epoch FROM now())::float8 AS db_now"
    "  FROM rate_limit_hits%s"
    " GROUP BY bucket ORDER BY bucket")


def bucket_lock_key(bucket: str) -> int:
    """把桶名映射成 advisory lock 的 key：稳定（跨进程、跨重启一致）且落在有符号 64 位内。"""
    digest = hashlib.sha256(f"{NAMESPACE}:{bucket}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def decide(hits: Sequence[float], *, limit: int, window_seconds: float, now: float) -> Decision:
    """纯判定：给定窗口内的尝试时间戳（epoch 秒）与上限，返回这次尝试能不能过。

    这是整套限流里唯一一处**策略**，所以单独抽成不碰数据库、不碰时钟的纯函数：
    SQL 只负责「把窗口内的尝试捞出来」，边界语义（够不够 limit、还要等几秒）全在这里。
    `retry_after` 按**最早那条**算 —— 窗口是滑动的，等到它滑出去就又有名额了。

    被拒绝的尝试不进 `hits`（调用方也不会为它落库）：拒绝不延长封锁时间，
    否则一个持续打请求的脚本可以让自己永远出不来 —— 那种「越试越久」是账号锁定
    （`backend/lockout.py`，按账号失败次数指数退避）该干的事，不是这一层的职责。
    """
    cutoff = now - window_seconds
    recent = sorted(hit for hit in hits if hit > cutoff)
    if len(recent) >= limit:
        return Decision(False, max(1, math.ceil(window_seconds - (now - recent[0]))), 0)
    return Decision(True, 0, max(0, limit - (len(recent) + 1)))


@contextmanager
def _session(db: Session | None) -> Iterator[Session]:
    """借一个会话：调用方给就用它的（测试常这么干，计数随用例事务一起回滚），否则自己开一个并提交。

    自己开的那种必须**立刻提交**：出口配额的依赖跑在请求的只读事务上，
    要是把计数挂在调用方的事务里，导出成功但事务没提交（甚至被回滚），这次尝试就等于没发生，
    配额永远攒不起来。而 advisory lock 是事务级的，提交即释放，正好也是我们要的。
    """
    if db is not None:
        yield db
        return
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _sweep_if_due(session: Session) -> int:
    """偶尔扫一次全局过期行（进程内节流）。返回删掉的条数，0 表示这次没扫。"""
    global _last_sweep
    now = time.monotonic()
    if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
        return 0
    _last_sweep = now
    return int(session.execute(_SWEEP_SQL, {"keep": SWEEP_KEEP_SECONDS}).rowcount or 0)


def _hit_once(session: Session, bucket: str, limit: int, window_seconds: float) -> Decision:
    """取锁 → 裁剪 → 判定 → （允许时）落一条尝试。全部在调用方的事务里。"""
    session.execute(_LOCK_SQL, {"key": bucket_lock_key(bucket)})
    session.execute(_PRUNE_SQL, {"bucket": bucket, "window": float(window_seconds)})
    row = session.execute(_WINDOW_SQL, {"bucket": bucket}).one()
    decision = decide([float(value) for value in row.hits], limit=limit,
                      window_seconds=float(window_seconds), now=float(row.db_now))
    if decision.allowed:
        session.execute(_INSERT_SQL, {"bucket": bucket})
    _sweep_if_due(session)
    return decision


# ── 配额登记表 ────────────────────────────────────────────────────────────────

# 测试用的覆盖（`install_quota`）：不碰配置、不改进程里的计数，只换「上限与窗口」这两条策略。
_overrides: dict[str, tuple[int, float]] = {}


def quota(name: str) -> tuple[int, float]:
    """各配额的（次数上限，窗口秒数）。读配置，测试里用 `install_quota` 覆盖。"""
    if name in _overrides:
        return _overrides[name]
    from backend.config import get_settings

    settings = get_settings()
    if name == "login":
        return max(1, settings.login_max_attempts), max(1.0, float(settings.login_window_seconds))
    if name == "export":
        return max(1, settings.export_max_per_minute), LIMIT_WINDOW_SECONDS
    if name == "ai":
        return max(1, settings.ai_max_per_minute), LIMIT_WINDOW_SECONDS
    raise KeyError(f"没有登记的配额：{name}")


def all_quotas() -> dict[str, dict]:
    """三组配额的当前值（`/system/status` 用）。"""
    return {name: {"limit": quota(name)[0], "window_seconds": quota(name)[1]} for name in NAMES}


def install_quota(name: str, limit: int, window_seconds: float) -> tuple[int, float]:
    """换掉某个配额的上限与窗口（测试用：免得用例里真打 30 个请求）。"""
    if name not in NAMES:
        raise KeyError(f"没有登记的配额：{name}")
    _overrides[name] = (max(1, int(limit)), max(0.001, float(window_seconds)))
    return _overrides[name]


def reset_quotas() -> None:
    """丢掉覆盖，回到配置里的值。**不动库里的计数** —— 计数是事实，不是缓存。"""
    _overrides.clear()


# 旧名字：⑰–⑳ 那段叫 reset_limiters()，含义一样（回到配置值）。保留别名免得脚本/文档一夜失效。
reset_limiters = reset_quotas


# ── 对外的三个动作：消费、清零、清理 ─────────────────────────────────────────

def consume(name: str, key: str, *, db: Session | None = None) -> Decision:
    """记一次请求并返回判定。桶名是「配额名 + 分桶键」：同一账号的导出与问答各算各的。"""
    limit, window = quota(name)
    bucket = f"{name}:{key}"
    with _session(db) as session:
        return _hit_once(session, bucket, limit, window)


def reset(name: str, key: str, *, db: Session | None = None) -> int:
    """清空某个桶（登录成功后调用：避免正常用户自己敲错几次就把自己关在门外）。"""
    bucket = f"{name}:{key}"
    with _session(db) as session:
        session.execute(_LOCK_SQL, {"key": bucket_lock_key(bucket)})
        return int(session.execute(_DELETE_BUCKET_SQL, {"bucket": bucket}).rowcount or 0)


def purge(*, bucket_prefix: str | None = None, db: Session | None = None) -> int:
    """删掉桶（可选按前缀）。测试夹具与真机脚本用：计数是跨请求、跨进程的事实，清理要显式做。"""
    with _session(db) as session:
        if bucket_prefix is None:
            return int(session.execute(text("DELETE FROM rate_limit_hits")).rowcount or 0)
        return int(session.execute(_DELETE_PREFIX_SQL,
                                   {"pattern": f"{bucket_prefix}%"}).rowcount or 0)


def purge_expired(*, db: Session | None = None) -> int:
    """全局清扫：删掉 `SWEEP_KEEP_SECONDS` 之前的所有行（启动时调一次，日志里报数）。"""
    with _session(db) as session:
        return int(session.execute(_SWEEP_SQL, {"keep": SWEEP_KEEP_SECONDS}).rowcount or 0)


def tracked_buckets(*, prefix: str | None = None, db: Session | None = None) -> int:
    """当前有计数的桶数（排障用：桶数只增不减就是裁剪失效了）。"""
    with _session(db) as session:
        if prefix is None:
            return int(session.scalar(_COUNT_BUCKETS_SQL) or 0)
        return int(session.scalar(_COUNT_PREFIX_SQL, {"pattern": f"{prefix}%"}) or 0)


def snapshot(*, prefix: str | None = None, db: Session | None = None) -> list[dict]:
    """每个桶的条数、最早/最晚尝试时间与「还要多久滑出窗口」（排障与真机脚本用）。"""
    pattern = f"{prefix}%" if prefix is not None else None
    sql = text(_SNAPSHOT_SQL % (" WHERE bucket LIKE :pattern" if pattern else ""))
    params = {"pattern": pattern} if pattern else {}
    with _session(db) as session:
        rows = session.execute(sql, params).all()
    out = []
    for row in rows:
        name, count, oldest, newest, db_now = (row[0], int(row[1]), float(row[2]),
                                               float(row[3]), float(row[4]))
        out.append({
            "bucket": name,
            "hits": count,
            "oldest_seconds_ago": round(db_now - oldest, 3),
            "newest_seconds_ago": round(db_now - newest, 3),
        })
    return out
