"""进程内请求指标：全局计数 + 按路径的耗时 + 慢接口告警。

刻意不引 Prometheus 客户端：这个部署只需要「刚才哪个接口慢」这一个问题有答案，
而 `measure` 的数据结构越简单越不会出错（重启即清零是已知且可接受的边界）。

光有 P95 不够：没人会一直盯着管理页，所以**单次请求超过阈值就主动写一条 WARNING**。
告警按路径限流（默认每路径每分钟最多一条，被压掉的条数累计到下一次里报出来）——
一个持续几十秒的 SSE 连接会不停产生「慢请求」，不限流会把日志刷满，真出问题时反而看不见。
"""
import logging
import time
from collections import Counter
from datetime import UTC, datetime
from threading import Lock

logger = logging.getLogger("finance-api.slow")

_lock = Lock()
_requests = Counter()
_durations_ms: list[float] = []
# 按路径分别留一份采样：平均 300ms 这种数字没法定位到具体接口，路径维度的 P95 才能。
_path_durations: dict[str, list[float]] = {}
# 超阈值的累计情况（也是管理页「慢接口告警」那一块的数据源）。
_slow: dict[str, dict] = {}
# 惰性从配置读；测试与验证脚本用 set_thresholds()/reload_thresholds() 覆盖。
_thresholds: dict | None = None

_GLOBAL_SAMPLES = 1000
_PATH_SAMPLES = 200


def _p95(values: list[float]) -> float:
    """最近采样里的 P95；与全局口径保持一致（排序后取 int(n*0.95) 位）。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def _load_thresholds() -> dict:
    from backend.config import get_settings

    settings = get_settings()
    return {
        "default_ms": max(1, int(settings.slow_path_ms)),
        "overrides": dict(settings.slow_path_thresholds),
        "log_seconds": max(0, int(settings.slow_path_log_seconds)),
    }


def thresholds() -> dict:
    """当前阈值（首次调用时从配置读，之后缓存）。"""
    global _thresholds
    if _thresholds is None:
        _thresholds = _load_thresholds()
    return _thresholds


def reload_thresholds() -> dict:
    """丢掉缓存，下次 `thresholds()` 重新读配置（改了 .env 或环境变量后用）。"""
    global _thresholds
    _thresholds = None
    return thresholds()


def set_thresholds(*, default_ms: int | None = None, overrides: dict[str, int] | None = None,
                    log_seconds: int | None = None) -> dict:
    """运行时覆盖阈值；只改传进来的项。传 None 的项保持当前值。

    只给测试和验证脚本用：真跑起来时阈值来自 `.env`（`SLOW_PATH_MS` / `SLOW_PATH_OVERRIDES`）。
    """
    current = dict(thresholds())
    if default_ms is not None:
        current["default_ms"] = max(1, int(default_ms))
    if overrides is not None:
        current["overrides"] = {str(k): max(1, int(v)) for k, v in overrides.items()}
    if log_seconds is not None:
        current["log_seconds"] = max(0, int(log_seconds))
    global _thresholds
    _thresholds = current
    return current


def threshold_for(path: str) -> int:
    """这条路径的阈值：精确匹配优先，没有就回落到全局默认值。"""
    settings = thresholds()
    return int(settings["overrides"].get(path, settings["default_ms"]))


def reset() -> None:
    """清空所有指标与阈值缓存（测试用）。"""
    global _thresholds
    with _lock:
        _requests.clear()
        _durations_ms.clear()
        _path_durations.clear()
        _slow.clear()
    _thresholds = None


def record_request(path: str, status_code: int, duration_ms: float) -> None:
    limit = threshold_for(path)
    alert: tuple | None = None
    with _lock:
        _requests[f"{status_code}:{path}"] += 1
        _durations_ms.append(duration_ms)
        del _durations_ms[:-_GLOBAL_SAMPLES]
        samples = _path_durations.setdefault(path, [])
        samples.append(duration_ms)
        del samples[:-_PATH_SAMPLES]

        if duration_ms >= limit:
            entry = _slow.setdefault(path, {
                "threshold_ms": limit, "exceeded": 0, "suppressed": 0,
                "worst_ms": 0.0, "last_ms": 0.0, "last_at": None, "last_logged": 0.0,
            })
            entry["threshold_ms"] = limit
            entry["exceeded"] += 1
            entry["worst_ms"] = max(entry["worst_ms"], duration_ms)
            entry["last_ms"] = duration_ms
            entry["last_at"] = datetime.now(UTC).isoformat(timespec="seconds")
            now = time.monotonic()
            if now - entry["last_logged"] >= thresholds()["log_seconds"]:
                # 同样的慢请求连着来时只报第一条，后面的计数攒到下一次告警里一起说。
                entry["last_logged"] = now
                alert = (path, duration_ms, limit, entry["exceeded"], entry["suppressed"])
                entry["suppressed"] = 0
            else:
                entry["suppressed"] += 1

    if alert is not None:
        # 结构化日志（`LOG_FORMAT=json`）要的是**字段**，不只是拼进正文的数字：这些名字
        # 都在 `backend/logging_setup.py` 的白名单里，正文一个字没改，文本格式的日志不受影响。
        logger.warning(
            "慢接口告警 path=%s duration_ms=%s threshold_ms=%s 累计超阈值=%s 次%s",
            alert[0], alert[1], alert[2], alert[3],
            f"（期间另有 {alert[4]} 次被限流未逐条上报）" if alert[4] else "",
            extra={
                "path": alert[0],
                "duration_ms": alert[1],
                "threshold_ms": alert[2],
                "exceeded": alert[3],
                "suppressed": alert[4],
            },
        )


def snapshot() -> dict:
    """全局请求计数与 P95，外加每条路径的耗时（`paths`）与慢接口告警（`slow_paths`）。

    `paths` 是给「哪个接口慢」这个问题用的：管理概览据此列出最慢的几条路径，
    排查时不必去翻日志行。每路径只保留最近 `samples_per_path` 次采样。
    `slow_paths` 只列出**真的超过过阈值**的路径，按超阈值次数排序：它比 P95 更接近
    「现在需要有人去看一眼」——P95 高但没有一次超过阈值（阈值被特意放宽的重接口）不算告警。
    """
    with _lock:
        durations = list(_durations_ms)
        paths = {
            path: {"count": len(samples), "p95_ms": _p95(samples), "max_ms": max(samples)}
            for path, samples in sorted(_path_durations.items())
        }
        slow_paths = [
            {
                "path": path,
                "threshold_ms": entry["threshold_ms"],
                "exceeded": entry["exceeded"],
                "worst_ms": entry["worst_ms"],
                "last_ms": entry["last_ms"],
                "last_at": entry["last_at"],
                "suppressed": entry["suppressed"],
            }
            for path, entry in _slow.items()
        ]
        settings = dict(thresholds())
    slow_paths.sort(key=lambda item: (item["exceeded"], item["worst_ms"]), reverse=True)
    return {
        "requests": dict(_requests),
        "total": len(durations),
        "p95_duration_ms": _p95(durations),
        "paths": paths,
        "samples_per_path": _PATH_SAMPLES,
        "slow_paths": slow_paths,
        "slow_default_ms": settings["default_ms"],
        "slow_overrides": settings["overrides"],
        "slow_log_seconds": settings["log_seconds"],
    }
