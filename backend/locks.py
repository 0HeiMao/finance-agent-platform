"""按「期间」加事务级互斥锁 —— 重算指标/落库的并发闸门。

背景：重算是一段**读改写**——读一段时间窗里的明细、删掉旧指标与预警、重新写一行。
两个请求同时重算同一个期间时，彼此都读不到对方还没提交的明细，于是各自写出一份
少算了对方数据的指标；没有任何报错，也没有任何东西会再来重算这一期，
**从此看板上的数字就一直是错的**。用 Postgres 的事务级 advisory lock 把「同一期间的重算」
串起来：锁随事务结束自动释放（提交、回滚、连接断掉都释放，不需要谁去清理）。

模块里有两族锁，别混用：

* **事务级**（`lock_periods()`，文件上半部分）：管「同一期间的重算串行」，锁随事务结束自动放；
* **会话级**（`try_session_lock()` / `release_session_lock()`，文件末尾）：管「这条后台任务
  现在有没有人在跑」，锁钉在一条专用连接上、必须显式解锁 —— 后台任务的跨进程抢占与
  「进程死了没有」都靠它（⑱，见 `backend/jobs.py`）。

为什么不用行锁/表锁：这里要保护的不是某几行，而是「某一期的明细行集合 + 它的指标行」
这个逻辑对象；明细行本身还在被插入和删除，行锁锁不住它。advisory lock 的 key 由期间名算出来，
正好就是「这一期」这个对象。

**加锁顺序**：所有调用方都走 `lock_periods()`，它先把期间补全（月 → 月 + 季 + 年，见
`period_lock_set()`）、再按期间名排序后逐个加。多期调用方必须**一次性把全集传进来**，
不要在一圈循环里逐期加锁 —— 两个多期请求各持一半、又都在等对方剩下那半，就是 Postgres 的 deadlock。

调用方必须遵守的不变量：**任何「改动某一期的明细行」或「重算某一期」的路径，
都要先持有那一期的锁**（导入、更正、冲销、撤销批次、重算接口、落库重建）。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from backend import periods as period_mod

# key 的命名空间：advisory lock 的 key 空间是整个库共享的，加前缀免得和别处（导入去重那份
# 用文件哈希当 key）撞上同一个数字。
NAMESPACE = "finance:recalc"


def _hash_key(name: str) -> int:
    """把名字映射成 advisory lock 的 key：稳定（跨进程、跨重启一致）且落在有符号 64 位内。"""
    digest = hashlib.sha256(name.encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def lock_key(period: str) -> int:
    """把期间名映射成 advisory lock 的 key。"""
    return _hash_key(f"{NAMESPACE}:{period}")


def period_lock_set(periods: Iterable[str]) -> list[str]:
    """要锁的期间全集：这些月本身 + 它们所在的季与年（落库行跟着月口径走）。

    `2026-05` → `['2026', '2026-05', '2026-Q2']`：重算一个月会连带刷新它所在的季与年，
    所以「季/年的落库行」也是同一个临界区的一部分，不然两个月份的并发重算会互相覆盖季度合计。
    认不出来的期间只锁它自己（接口层另有格式校验，这里只保证同名字请求互相串行）。
    """
    names: set[str] = set()
    for period in periods:
        name = (period or "").strip()
        if not name:
            continue
        names.add(name)
        if period_mod.is_month(name):
            names.update(period_mod.containing_periods(name))
    return sorted(names)


def lock_periods(db: Session, periods: Iterable[str]) -> list[str]:
    """按排序后的顺序取事务级 advisory lock，返回真正锁住的期间名。

    传入的期间先经 `period_lock_set()` 补全：传 `2026-05` 会连 `2026` 与 `2026-Q2` 一起锁，
    传已经补全过的集合也幂等。把补全收在**唯一一个真正加锁的函数**里，是为了让调用方漏了
    `period_lock_set()` 也不会漏锁——漏锁的后果（两个月的并发重算各自刷新季度合计、互相覆盖）
    不报任何错，只能靠真机脚本或事后对数才发现。这一点正是
    `scripts/verify_recalc_lock.py` 第一版抓出来的：那时 `lock_periods()` 只锁了传入的名字，
    脚本里直接用原始月名调用，于是「季/年锁有没有生效」当场露馅。

    `pg_advisory_xact_lock` 会一直等（不是 try 版），等到对方事务结束为止；
    同一个事务里重复锁同一期是安全的（只增加重入计数，不会自己把自己堵死）。
    """
    locked = period_lock_set(periods)
    for period in locked:
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key(period)})
    return locked


# ── 会话级锁：后台任务「现在到底有没有人在跑」（⑱）─────────────────────────────
#
# 上面那把是**事务级**的（`pg_advisory_xact_lock`），管的是「几秒内串行一段读改写」；
# 后台任务是另一回事：一次撤销批次可能要跑几十秒，而且真正要回答的问题是
# **「这条 running 记录背后的进程还活着吗」** —— 进程要是没了，任务就永远停在 running，
# 界面上一直转圈（⑩ 的 `resume_interrupted_jobs()` 就是来收拾它的）。
#
# 所以这里用**会话级**锁（`pg_advisory_lock` 家族）：它不随事务提交释放，而是钉在持锁的那条
# **物理连接**上。好处是「进程死了」这件事不需要任何超时阈值来猜 —— 连接一断，Postgres 立刻
# 把锁收回去；`resume` 那边只要试抢一下，抢得到就说明没人跑了。
#
# 代价有两条，调用方必须遵守：
# * **一定要显式解锁**（`release_session_lock`）。连接池会把物理连接还回来再用，
#   池子里的连接是**不会**因为事务回滚而丢锁的，忘了解锁就等于把这条任务永久占住；
# * 锁要借一条**专用连接**（`engine.connect()`），不能借用请求/任务里那个 Session 的连接 ——
#   那个 Session 一 `commit()` 就可能把连接还回池子，后面 `pg_advisory_unlock` 落到另一条
#   连接上会返回 false，锁却还留在原来那条连接里。
JOB_NAMESPACE = "finance:job-claim"
RESUME_NAMESPACE = "finance:job-resume"


def job_lock_key(job_id: int) -> int:
    """一条后台任务的执行权 key（`background_jobs.id`）。"""
    return _hash_key(f"{JOB_NAMESPACE}:{job_id}")


def resume_lock_key() -> int:
    """「启动接续」这份活的全局 key：多副本同时启动时只让一个进程真的去接续。"""
    return _hash_key(f"{RESUME_NAMESPACE}:all")


def try_session_lock(connection: Connection, key: int) -> bool:
    """试着独占一把会话级锁。返回是否拿到 —— 拿不到说明别的会话（进程）正持有。"""
    return bool(connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}).scalar())


def release_session_lock(connection: Connection, key: int) -> bool:
    """显式放掉一把会话级锁。返回是否真的放掉了（不是自己持有的会返回 False）。"""
    return bool(connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key}).scalar())

