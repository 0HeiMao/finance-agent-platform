"""登录失败锁定与指数退避（④）。

为什么要有它：`backend/ratelimit.py` 的滑动窗口是按「用户名 + 来源」计的，挡脚本化的连续
尝试够用，但有两个缺口——(1) 同一账号换来源（分布式尝试）绕得过去；(2) 计数在进程内存里，
多 worker 各算一份、重启清零。**账号**被锁这件事必须落在库上（`users.failed_attempts` /
`users.locked_until`），否则同一句「账号已锁定」在另一台机器上就不成立。

退避公式：连续失败次数达到 `login_lockout_threshold` 之后才锁，锁定时长
`login_lockout_base_seconds * 2 ** (次数 - threshold)`，封顶 `login_lockout_max_seconds`。
默认 5 次 → 60 秒、6 次 → 120 秒、7 次 → 240 秒、8 次 → 480 秒、9 次起 900 秒（封顶）：
正常用户手滑几次几乎无感（前 4 次不锁），而穷举脚本的成本指数上升。

计数会被「静默期」清零（`login_failure_ttl_seconds`，默认 15 分钟）：昨天失败 4 次、今天
再错 1 次不该凑成第 5 次把账号锁住——那不是安全，那是骚扰。登录成功也清零。

注意与限流的分工：限流是**每次尝试**的粗筛（进程内、按用户名+来源，429 秒级），这里是
**账号级**的惩罚（落库、跨进程、随失败次数变长）。两道都留着：前者挡住打不中的海量尝试，
后者保证「这个账号现在谁都别想登」。
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from backend.config import get_settings
from backend.models import User

UTC = UTC


def _aware(value: datetime | None) -> datetime | None:
    """统一成带时区的 datetime：老行/手写的测试数据可能是 naive 的。"""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def lock_seconds(attempts: int) -> int:
    """第 attempts 次失败要锁多久（未达阈值返回 0）。纯函数，单测直接打它。"""
    settings = get_settings()
    threshold = max(1, int(settings.login_lockout_threshold))
    base = max(1, int(settings.login_lockout_base_seconds))
    ceiling = max(base, int(settings.login_lockout_max_seconds))
    if attempts < threshold:
        return 0
    return int(min(ceiling, base * (2 ** (attempts - threshold))))


def locked_for(user: User, now: datetime | None = None) -> int:
    """还剩几秒解锁（未锁定返回 0）。"""
    until = _aware(getattr(user, "locked_until", None))
    if until is None:
        return 0
    current = now or datetime.now(UTC)
    return max(0, math.ceil((until - current).total_seconds()))


def is_locked(user: User, now: datetime | None = None) -> bool:
    return locked_for(user, now=now) > 0


def register_failure(user: User, now: datetime | None = None) -> dict:
    """记一次失败并（可能）上锁，返回可写进审计与响应头的数字。"""
    settings = get_settings()
    current = now or datetime.now(UTC)
    ttl = max(1, int(settings.login_failure_ttl_seconds))
    last = _aware(getattr(user, "last_failed_at", None))
    attempts = int(user.failed_attempts or 0)
    if last is not None and (current - last).total_seconds() > ttl:
        # 静默期已过：这一串失败不再累积，从 1 重新数
        attempts = 0
    attempts += 1
    user.failed_attempts = attempts
    user.last_failed_at = current
    seconds = lock_seconds(attempts)
    # 没到阈值就把锁定时间清掉：否则管理页会显示一个「锁到过去某时刻」的 locked_until，
    # 而 `locked` 又是 false，看的人只能猜哪个才算数。
    user.locked_until = current + timedelta(seconds=seconds) if seconds else None
    return {
        "failed_attempts": attempts,
        "threshold": max(1, int(settings.login_lockout_threshold)),
        "locked_for": seconds,
        "locked_until": _aware(user.locked_until).isoformat() if user.locked_until else None,
    }


def clear_failures(user: User) -> int:
    """登录成功后清零，返回清掉前的失败次数（给审计用）。"""
    previous = int(user.failed_attempts or 0)
    user.failed_attempts = 0
    user.locked_until = None
    user.last_failed_at = None
    return previous


def unlock(user: User) -> dict:
    """管理员解锁：清掉失败计数与锁定时间，返回解锁前的状态（给审计用）。

    只做这一件事：**不碰会话版本、不碰口令**。解锁不等于「让人换密码」，也不等于「踢人下线」
    ——那两件事分别归 `/admin/users/{id}/logout` 与账号本身的密码管理。
    """
    state = {"cleared_attempts": int(user.failed_attempts or 0),
             "was_locked_for": locked_for(user),
             "was_locked_until": _aware(user.locked_until).isoformat() if user.locked_until else None}
    clear_failures(user)
    state["unlocked"] = True
    return state
