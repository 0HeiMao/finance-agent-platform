"""按「期间」加事务级互斥锁 —— 重算指标/落库的并发闸门。

背景：重算是一段**读改写**——读一段时间窗里的明细、删掉旧指标与预警、重新写一行。
两个请求同时重算同一个期间时，彼此都读不到对方还没提交的明细，于是各自写出一份
少算了对方数据的指标；没有任何报错，也没有任何东西会再来重算这一期，
**从此看板上的数字就一直是错的**。用 Postgres 的事务级 advisory lock 把「同一期间的重算」
串起来：锁随事务结束自动释放（提交、回滚、连接断掉都释放，不需要谁去清理）。

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
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from backend import periods as period_mod

# key 的命名空间：advisory lock 的 key 空间是整个库共享的，加前缀免得和别处（导入去重那份
# 用文件哈希当 key）撞上同一个数字。
NAMESPACE = "finance:recalc"


def lock_key(period: str) -> int:
    """把期间名映射成 advisory lock 的 key：稳定（跨进程、跨重启一致）且落在有符号 64 位内。"""
    digest = hashlib.sha256(f"{NAMESPACE}:{period}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


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
