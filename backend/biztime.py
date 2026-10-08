"""业务时区（⑯）：库里一律存 UTC，但「今天」「本月」是人对人说的话，必须显式声明是哪个时区。

默认 `Asia/Shanghai`（环境变量 `BUSINESS_TIMEZONE` 可改）。所有「业务日历日」计算都从这里走，
业务代码里不要再出现 `datetime.now()`：那读的是服务器本机时区，容器里通常是 UTC，于是
「今天导入的数据」在 UTC+8 的早上 8 点前会被算成昨天；反过来，服务器换一台机器（或换成
UTC 的容器）同一个接口的「今天」就变了，而接口、报告、审计导出都不会说自己换了口径。

两条约定：

* **运算用带时区的 `datetime`，展示用 `label()` / `describe()`**：前者保证跨时区加减不出错，
  后者让页面和脚本能说出「这个『今天』是 Asia/Shanghai 的今天（UTC+08:00）」。
* **naive 的输入一律当 UTC 读**（`to_business()`）。库里 `DateTime(timezone=True)` 的列、
  审计导出的 `since/until` 都有可能给出 naive 值，猜成本机时区会让结果随部署环境漂移。
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# 演示数据、审计留痕、报告都按这个时区算「今天」；改它只影响「日历日」口径，不影响存库的 UTC 时刻。
DEFAULT_TIMEZONE = "Asia/Shanghai"


class TimezoneConfigError(ValueError):
    """业务时区名字不合法（不是 IANA 名字，或本机没装 tzdata）。"""


def _configured_name() -> str:
    # 局部 import：backend/config.py 的 validate() 也要校验时区名，模块级互相 import 会成环。
    from backend.config import get_settings

    return (getattr(get_settings(), "business_timezone", "") or DEFAULT_TIMEZONE).strip()


@lru_cache(maxsize=16)
def zone(name: str) -> ZoneInfo:
    """按 IANA 名字取时区；名字不合法直接报错（这是配置错误，不该悄悄退回默认值）。"""
    candidate = (name or "").strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise TimezoneConfigError(
            f"业务时区 {candidate!r} 不是一个合法的 IANA 时区名（例如 Asia/Shanghai、America/New_York）；"
            f"Windows 上需要 tzdata 包（pip install tzdata）") from exc


def is_valid(name: str) -> bool:
    """校验时区名：配置体检与真机脚本都要用同一把尺子。"""
    try:
        zone(name)
    except TimezoneConfigError:
        return False
    return True


def current_name() -> str:
    return _configured_name()


def current_zone() -> ZoneInfo:
    return zone(_configured_name())


def to_business(value: datetime, *, name: str | None = None) -> datetime:
    """转成业务时区。naive 输入当 UTC 读（见模块 docstring 的第二条约定）。"""
    moment = value if value.tzinfo else value.replace(tzinfo=UTC)
    return moment.astimezone(zone(name or _configured_name()))


def now(*, name: str | None = None) -> datetime:
    return datetime.now(zone(name or _configured_name()))


def today(*, name: str | None = None) -> date:
    return now(name=name).date()


def day_start(day: date, *, name: str | None = None) -> datetime:
    """那一天的 00:00（业务时区）。跨夏令时的那一天就是 23 或 25 小时，不是硬加 24 小时。"""
    return datetime.combine(day, time.min, tzinfo=zone(name or _configured_name()))


def day_bounds(day: date, *, name: str | None = None) -> tuple[datetime, datetime]:
    """[那天的 00:00, 下一天的 00:00)：左闭右开，审计导出与用量窗口都按这个口径。"""
    return day_start(day, name=name), day_start(day + timedelta(days=1), name=name)


def month_of(value: datetime | None = None, *, name: str | None = None) -> str:
    """那一刻所属的业务月份，`YYYY-MM`。默认取此刻——预警测试通知的 `period` 用它。"""
    moment = to_business(value, name=name) if value is not None else now(name=name)
    return moment.strftime("%Y-%m")


def offset_text(moment: datetime | None = None, *, name: str | None = None) -> str:
    """`UTC+08:00` 这种给人看的偏移，直接由那一刻的 `utcoffset()` 算，夏令时也对。"""
    moment = moment or now(name=name)
    offset = moment.utcoffset() or timedelta(0)
    sign = "+" if offset >= timedelta(0) else "-"
    total = abs(int(offset.total_seconds()))
    hours, minutes = divmod(total // 60, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def label(*, name: str | None = None, moment: datetime | None = None) -> str:
    """`Asia/Shanghai (UTC+08:00)`：页面、脚本、报告副标题都用这一份文案。"""
    shown_name = name or _configured_name()
    return f"{shown_name} ({offset_text(moment, name=shown_name)})"


def describe(*, name: str | None = None, moment: datetime | None = None) -> dict:
    """给 `/system/status` 与真机脚本用的一整块说明，避免各写一份格式化代码。"""
    shown_name = name or _configured_name()
    current = moment or now(name=shown_name)
    business = to_business(current, name=shown_name)
    return {
        "name": shown_name,
        "label": label(name=shown_name, moment=business),
        "offset": offset_text(business, name=shown_name),
        "now": business.isoformat(timespec="seconds"),
        "today": business.date().isoformat(),
    }
