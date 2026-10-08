"""进程内的接口限流（滑动窗口）。

为什么放内存：这个部署没有可用的 Redis（`redis` 包本身都没装，见 requirements 注释），
而登录爆破是**最廉价**的攻击面——密码只有长度下限、没有复杂度要求，不限速等于随便试；
导出与 RAG/Agent 是另两个「一次请求很贵」的出口（一个把整库拖出来，一个调模型算钱）。
内存实现能挡住脚本化的连续尝试，代价是**每个进程各算一份**（多 worker 或重启后计数清零），
这条边界写进了 README；真要多副本共享，把 `_limiters` 换成 Redis 计数即可，调用点不用改。

三组配额各自独立计数（`login` / `export` / `ai`）：登录被限流不该影响看数据，
导出打满也不该让人问不出问题来——限流的目标是挡住脚本，不是惩罚用户。
"""
from __future__ import annotations

import math
import threading
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


# ------------------------------------------------------------------ 通用配额（导出 / RAG·Agent）

LIMIT_WINDOW_SECONDS = 60.0
_lock = threading.Lock()
_limiters: dict[str, SlidingWindowLimiter] = {}


def quota(name: str) -> tuple[int, float]:
    """各配额的（次数上限，窗口秒数）。读配置，测试里改环境变量再 reset 就能换。"""
    from backend.config import get_settings

    settings = get_settings()
    if name == "export":
        return max(1, settings.export_max_per_minute), LIMIT_WINDOW_SECONDS
    if name == "ai":
        return max(1, settings.ai_max_per_minute), LIMIT_WINDOW_SECONDS
    raise KeyError(f"没有登记的配额：{name}")


def get_limiter(name: str) -> SlidingWindowLimiter:
    """按名字取进程级限流器；`login` 复用登录那一份，其它按配置惰性创建。"""
    if name == "login":
        return get_login_limiter()
    with _lock:
        limiter = _limiters.get(name)
        if limiter is None:
            limit, window = quota(name)
            limiter = SlidingWindowLimiter(limit=limit, window_seconds=window)
            _limiters[name] = limiter
        return limiter


def reset_limiters() -> None:
    """测试与启动时清空：把配置改动立刻反映到配额上。"""
    with _lock:
        _limiters.clear()
    reset_login_limiter()


def install_limiter(name: str, limiter: SlidingWindowLimiter) -> SlidingWindowLimiter:
    """换掉某个配额的限流器（测试用：把窗口调小，免得用例里真打 30 个请求）。"""
    with _lock:
        _limiters[name] = limiter
    return limiter


def consume(name: str, username: str) -> Decision:
    """记一次请求并返回判定。key 是「配额名 + 账号」：同一账号的导出与问答各算各的。"""
    return get_limiter(name).check(f"{name}:{username}")
