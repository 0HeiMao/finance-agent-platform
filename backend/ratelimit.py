"""进程内的登录限流（滑动窗口）。

为什么放内存：这个部署没有可用的 Redis（`redis` 包本身都没装，见 requirements 注释），
而登录爆破是**最廉价**的攻击面——密码只有长度下限、没有复杂度要求，不限速等于随便试。
内存实现能挡住脚本化的连续尝试，代价是**每个进程各算一份**（多 worker 或重启后计数清零），
这条边界写进了 README；真要多副本共享，把 `_limiter` 换成 Redis 计数即可，调用点不用改。
"""
from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Decision:
    """一次限流判定：allowed=False 时 retry_after 是建议等待的秒数。"""

    allowed: bool
    retry_after: int = 0
    remaining: int = 0


@dataclass
class SlidingWindowLimiter:
    """每个 key 保留最近 window_seconds 内的尝试时间戳，超过 limit 次就拒绝。"""

    limit: int
    window_seconds: float
    clock: Callable[[], float] = time.monotonic
    _hits: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))

    def check(self, key: str) -> Decision:
        """记一次尝试并返回判定结果（被拒绝的尝试也计数，避免无限重试）。"""
        now = self.clock()
        hits = self._hits[key]
        cutoff = now - self.window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            retry_after = max(1, math.ceil(self.window_seconds - (now - hits[0])))
            return Decision(False, retry_after, 0)
        hits.append(now)
        return Decision(True, 0, max(0, self.limit - len(hits)))

    def reset(self, key: str) -> None:
        """登录成功后清空计数，避免用户自己把自己锁住。"""
        self._hits.pop(key, None)

    def prune(self) -> None:
        """丢掉已经过期的 key，长期运行时内存不会随用户名无限增长。"""
        cutoff = self.clock() - self.window_seconds
        for key in [k for k, hits in self._hits.items() if not hits or hits[-1] <= cutoff]:
            self._hits.pop(key, None)

    @property
    def tracked_keys(self) -> int:
        return len(self._hits)


def login_limiter():
    """按当前配置构造限流器（每次调用读一次配置，便于测试改阈值）。"""
    from backend.config import get_settings

    settings = get_settings()
    return SlidingWindowLimiter(
        limit=max(1, settings.login_max_attempts),
        window_seconds=max(1.0, float(settings.login_window_seconds)),
    )


_limiter: SlidingWindowLimiter | None = None


def get_login_limiter() -> SlidingWindowLimiter:
    """进程级单例：配置在启动时就固定，测试用 reset_login_limiter() 换新的。"""
    global _limiter
    if _limiter is None:
        _limiter = login_limiter()
    return _limiter


def reset_login_limiter(limiter: SlidingWindowLimiter | None = None) -> SlidingWindowLimiter:
    global _limiter
    _limiter = limiter or login_limiter()
    return _limiter
