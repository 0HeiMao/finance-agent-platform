"""进程内请求指标：全局计数 + 按路径的耗时。

刻意不引 Prometheus 客户端：这个部署只需要「刚才哪个接口慢」这一个问题有答案，
而 `measure` 的数据结构越简单越不会出错（重启即清零是已知且可接受的边界）。
"""
from collections import Counter
from threading import Lock

_lock = Lock()
_requests = Counter()
_durations_ms: list[float] = []
# 按路径分别留一份采样：平均 300ms 这种数字没法定位到具体接口，路径维度的 P95 才能。
_path_durations: dict[str, list[float]] = {}

_GLOBAL_SAMPLES = 1000
_PATH_SAMPLES = 200


def _p95(values: list[float]) -> float:
    """最近采样里的 P95；与全局口径保持一致（排序后取 int(n*0.95) 位）。"""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


def record_request(path: str, status_code: int, duration_ms: float) -> None:
    with _lock:
        _requests[f"{status_code}:{path}"] += 1
        _durations_ms.append(duration_ms)
        del _durations_ms[:-_GLOBAL_SAMPLES]
        samples = _path_durations.setdefault(path, [])
        samples.append(duration_ms)
        del samples[:-_PATH_SAMPLES]


def snapshot() -> dict:
    """全局请求计数与 P95，外加每条路径的耗时（`paths`）。

    `paths` 是给「哪个接口慢」这个问题用的：管理概览据此列出最慢的几条路径，
    排查时不必去翻日志行。每路径只保留最近 `samples_per_path` 次采样。
    """
    with _lock:
        durations = list(_durations_ms)
        paths = {
            path: {"count": len(samples), "p95_ms": _p95(samples), "max_ms": max(samples)}
            for path, samples in sorted(_path_durations.items())
        }
    return {
        "requests": dict(_requests),
        "total": len(durations),
        "p95_duration_ms": _p95(durations),
        "paths": paths,
        "samples_per_path": _PATH_SAMPLES,
    }
