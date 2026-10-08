"""⑬ Prometheus 指标（手写 exposition 文本，不引 prometheus_client）。

为什么手写：这里要的东西只有三样（计数器、直方图、仪表），而 Prometheus 的文本格式本身
只有几条规则（`# HELP` / `# TYPE` / 样本行、标签值转义、直方图桶是累计值且必须有 `+Inf`）。
一百行能被单测逐行断言的代码，比多一个要跟随版本升级的依赖划算。

两个必须记住的口径：

- **标签用路由模板，不用原始路径**。`/api/v1/financial/transactions/123` 要记成
  `route="/api/v1/financial/transactions/{row_id}"`。用原始路径等于「一个 id 一个时间序列」，
  爬几次就把进程内存和 Prometheus 的基数一起撑爆；没匹配到路由的（404）统一记成 `unmatched`。
- **直方图的桶是累计的**：`le="0.05"` 那条**包含** `le="0.01"` 的，最后一条 `le="+Inf"`
  恒等于 `_count`。写成「每个桶各有多少」是这类指标最常见的错，所以桶值在渲染时累加。

进程内计数、重启即清零（与 `backend/observability.py` 的 JSON 快照同一口径）：
`/metrics` 是给 Prometheus 爬的原始数据，`GET /api/v1/observability/metrics` 是给管理页看的
人读快照，两者刻意不合并 —— 前者改口径会打断仪表盘，后者改口径只影响一个页面。
"""
from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from threading import Lock

logger = logging.getLogger("finance-api.metrics")

# Prometheus 官方建议的秒级桶，覆盖到本项目最慢的接口（报告导出/Agent 问答在几秒级）。
BUCKETS_SECONDS: tuple[float, ...] = (
    0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
)
CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
UNMATCHED_ROUTE = "unmatched"

LabelKey = tuple[tuple[str, str], ...]


def escape_label_value(value: str) -> str:
    """标签值里的反斜杠、换行、双引号必须转义，否则整行文本就散了（一行一个样本）。"""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def format_number(value: float) -> str:
    """整数按整数写（`3` 而不是 `3.0`），小数给 6 位有效数字（够用且不会被浮点尾巴撑长）。"""
    number = float(value)
    if number.is_integer() and abs(number) < 1e15:
        return str(int(number))
    return f"{number:.6g}"


def label_key(labelnames: tuple[str, ...], labels: dict[str, str] | None) -> LabelKey:
    """把标签字典规整成有序键；标签多一个少一个都直接报错，而不是静默记成另一条序列。"""
    provided = labels or {}
    missing = [name for name in labelnames if name not in provided]
    unknown = [name for name in provided if name not in labelnames]
    if missing or unknown:
        raise ValueError(
            f"标签不匹配：缺少 {missing}、多出 {unknown}（该指标声明的是 {list(labelnames)}）")
    return tuple((name, str(provided[name])) for name in labelnames)


def label_text(key: LabelKey) -> str:
    if not key:
        return ""
    inner = ",".join(f'{name}="{escape_label_value(value)}"' for name, value in key)
    return "{" + inner + "}"


def with_label(key: LabelKey, name: str, value: str) -> LabelKey:
    return (*key, (name, value))


class _Metric:
    """三种指标共用的东西：名字、帮助文本、标签声明、一把锁。"""

    kind = ""

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.labelnames = labelnames
        self._values: dict[LabelKey, float] = {}
        self._lock = Lock()

    def clear(self) -> None:
        with self._lock:
            self._values.clear()

    def value(self, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._values.get(label_key(self.labelnames, labels), 0.0)

    def header(self) -> list[str]:
        return [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} {self.kind}"]

    def render(self) -> list[str]:
        return [*self.header(), *self.samples()]

    def samples(self) -> list[str]:
        with self._lock:
            items = sorted(self._values.items())
        return [f"{self.name}{label_text(key)} {format_number(value)}" for key, value in items]


class Counter(_Metric):
    kind = "counter"

    def inc(self, labels: dict[str, str] | None = None, amount: float = 1.0) -> None:
        key = label_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + float(amount)


class Gauge(_Metric):
    kind = "gauge"

    def set(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = label_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = float(value)

    def change(self, delta: float, labels: dict[str, str] | None = None) -> None:
        """增减并**夹在 0 以上**：并发下先减后加会让「正在处理中」变成负数，那是肉眼可见的假数据。"""
        key = label_key(self.labelnames, labels)
        with self._lock:
            self._values[key] = max(0.0, self._values.get(key, 0.0) + float(delta))


class Histogram(_Metric):
    """直方图：内部只记「落在哪个桶」，渲染时再累加成累计值（Prometheus 只认累计桶）。"""

    kind = "histogram"

    def __init__(self, name: str, help_text: str, labelnames: tuple[str, ...] = (),
                 buckets: tuple[float, ...] = BUCKETS_SECONDS) -> None:
        super().__init__(name, help_text, labelnames)
        self.buckets = tuple(sorted(float(edge) for edge in buckets))
        self._counts: dict[LabelKey, list[int]] = {}
        self._sums: dict[LabelKey, float] = {}
        self._totals: dict[LabelKey, int] = {}

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self._counts.clear()
            self._sums.clear()
            self._totals.clear()

    def observe(self, value: float, labels: dict[str, str] | None = None) -> None:
        key = label_key(self.labelnames, labels)
        amount = float(value)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for index, edge in enumerate(self.buckets):
                # 只记第一个装得下它的桶：累计是渲染时的事，这里再加一遍就会翻倍
                # （真机踩过：`0.003/0.02/0.4` 三次观测把 `le="0.01"` 记成了 2）。
                if amount <= edge:
                    counts[index] += 1
                    break
            else:
                # 比所有上界都大：只进 `_count`/`_sum`，不落任何有限桶（`+Inf` 会兜住它）。
                pass
            self._sums[key] = self._sums.get(key, 0.0) + amount
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, labels: dict[str, str] | None = None) -> int:
        with self._lock:
            return self._totals.get(label_key(self.labelnames, labels), 0)

    def total(self, labels: dict[str, str] | None = None) -> float:
        with self._lock:
            return self._sums.get(label_key(self.labelnames, labels), 0.0)

    def samples(self) -> list[str]:
        with self._lock:
            keys = sorted(set(self._counts) | set(self._sums) | set(self._totals))
            snapshot = {
                key: (list(self._counts.get(key, [])), self._sums.get(key, 0.0), self._totals.get(key, 0))
                for key in keys
            }
        lines: list[str] = []
        for key in keys:
            counts, total, count = snapshot[key]
            cumulative = 0
            for index, edge in enumerate(self.buckets):
                cumulative += counts[index] if index < len(counts) else 0
                lines.append(f"{self.name}_bucket"
                             f"{label_text(with_label(key, 'le', format_number(edge)))} {cumulative}")
            # `+Inf` 是规矩里要求的最后一桶，它必须等于 _count（上面的累加已经包含了所有观测值）。
            lines.append(f"{self.name}_bucket{label_text(with_label(key, 'le', '+Inf'))} {count}")
            lines.append(f"{self.name}_sum{label_text(key)} {format_number(total)}")
            lines.append(f"{self.name}_count{label_text(key)} {count}")
        return lines


HTTP_REQUESTS = Counter(
    "finance_http_requests_total", "HTTP 请求数（按路由模板、方法与状态码）",
    ("method", "route", "status"))
HTTP_DURATION = Histogram(
    "finance_http_request_duration_seconds", "HTTP 请求耗时秒数（按路由模板与方法）",
    ("method", "route"))
HTTP_IN_FLIGHT = Gauge("finance_http_requests_in_flight", "当前正在处理中的 HTTP 请求数")
APP_INFO = Gauge("finance_app_info", "应用信息，恒为 1", ("version", "environment"))
PROCESS_START = Gauge("finance_process_start_time_seconds", "进程启动的 Unix 时间戳（秒）")
DB_UP = Gauge("finance_db_up", "数据库是否可用（1=能用，0=采集时查询失败）")
JOBS = Gauge("finance_background_jobs", "后台任务按状态计数（取自数据库）", ("status",))
ALERTS = Gauge("finance_alerts", "预警按处理状态计数（取自数据库）", ("review_status",))

METRICS: tuple[_Metric, ...] = (
    HTTP_REQUESTS, HTTP_DURATION, HTTP_IN_FLIGHT, APP_INFO, PROCESS_START, DB_UP, JOBS, ALERTS,
)

_STARTED_AT = time.time()
_app_info: tuple[str, str] = ("", "")


def init_app_info(version: str, environment: str) -> None:
    """记下应用版本与运行环境（启动时调一次；`reset()` 之后也能复原）。"""
    global _app_info
    _app_info = (str(version), str(environment))
    _apply_app_info()


def _apply_app_info() -> None:
    version, environment = _app_info
    APP_INFO.set(1, {"version": version, "environment": environment})
    PROCESS_START.set(_STARTED_AT)


def route_label(request) -> str:
    """请求命中的路由模板；没命中（404）记成 `unmatched`，绝不让原始路径进标签。

    Starlette 匹配路由时会把路由对象写进 `scope["route"]`，而 `BaseHTTPMiddleware`
    里的 `request.scope` 与内层是**同一个 dict**，所以在观测中间件里读得到（已实测：
    `/api/v1/admin/jobs/abc-def` → `/api/v1/admin/jobs/{job_id}`，404 → 没有 route）。
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path) if path else UNMATCHED_ROUTE


def request_started() -> None:
    HTTP_IN_FLIGHT.change(1)


def request_finished() -> None:
    HTTP_IN_FLIGHT.change(-1)


def observe_request(*, method: str, route: str, status_code: int, duration_ms: float) -> None:
    labels = {"method": method, "route": route}
    HTTP_REQUESTS.inc({**labels, "status": str(status_code)})
    HTTP_DURATION.observe(max(0.0, float(duration_ms)) / 1000.0, labels)


def refresh_runtime() -> None:
    """从数据库刷新「任务/预警按状态」「数据库可用」三组指标；失败只把 `finance_db_up` 记 0。

    指标端点必须比数据库**更不容易挂**：数据库炸了的时候监控正好瞎掉是最糟的结果，
    所以这里吞掉异常。同时**清空**两个按状态的仪表 —— 留着上一次的数字等于拿旧数据骗告警。
    """
    try:
        from sqlalchemy import text

        from backend.db import SessionLocal

        with SessionLocal() as session:
            job_rows = session.execute(
                text("SELECT status, count(*) FROM background_jobs GROUP BY status")).all()
            alert_rows = session.execute(
                text("SELECT review_status, count(*) FROM financial_alerts GROUP BY review_status")).all()
    except Exception as exc:  # noqa: BLE001 —— 采集失败不能影响 /metrics 的可用性
        DB_UP.set(0)
        JOBS.clear()
        ALERTS.clear()
        logger.warning("指标采集：读数据库失败，finance_db_up=0（%s: %s）",
                       type(exc).__name__, exc, extra={"path": "/metrics"})
        return
    DB_UP.set(1)
    JOBS.clear()
    ALERTS.clear()
    for status, count in job_rows:
        JOBS.set(float(count), {"status": str(status)})
    for review_status, count in alert_rows:
        ALERTS.set(float(count), {"review_status": str(review_status or "unknown")})


def render_prometheus() -> str:
    """把当前指标渲染成 Prometheus 文本格式（末尾有换行：文本格式要求最后一行为空）。"""
    lines: list[str] = []
    for metric in METRICS:
        lines.extend(metric.render())
    return "\n".join(lines) + "\n"


def collect() -> str:
    """`/metrics` 的实现：先采集运行时仪表，再渲染。"""
    refresh_runtime()
    return render_prometheus()


def reset() -> None:
    """清空所有计数（测试用）；应用信息与进程启动时间会被复原，不然它俩也被清掉了。"""
    for metric in METRICS:
        metric.clear()
    _apply_app_info()


def started_at_iso() -> str:
    """进程启动时间的人读形式（日志里说明「这些计数是从什么时候开始记的」）。"""
    return datetime.fromtimestamp(_STARTED_AT, UTC).isoformat(timespec="seconds")
