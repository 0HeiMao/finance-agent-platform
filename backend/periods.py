"""期间口径（月 / 季 / 年）的纯函数。

`financial_metrics` 只存**月**口径的行；季与年是「若干月的合计」，要落库就得先把
「一个期间由哪几个月构成、它的规范写法是什么、它在时间轴上的先后」讲清楚。
这些都是纯字符串运算，抽出来能在 `tests/test_units.py` 里直接验，不用起库。

规范写法（落库与接口都用这一套，别在别处再写一遍）：
- 月：`2026-10`
- 季：`2026-Q3`
- 年：`2026`

注意 `previous_period()` 是**同粒度**的上一个期间（月的上一期是上个月，季的上一期是上个季度），
因为它服务的是「环比」——拿本年比去年是同比，不是同一件事。
"""

from __future__ import annotations

import re

MONTH = "month"
QUARTER = "quarter"
YEAR = "year"

GRAINS = (MONTH, QUARTER, YEAR)
GRAIN_LABELS = {MONTH: "月", QUARTER: "季", YEAR: "年"}

MONTH_PATTERN = r"^\d{4}-(0[1-9]|1[0-2])$"
QUARTER_PATTERN = r"^\d{4}-Q[1-4]$"
YEAR_PATTERN = r"^\d{4}$"

_MONTH_RE = re.compile(MONTH_PATTERN)
_QUARTER_RE = re.compile(QUARTER_PATTERN)
_YEAR_RE = re.compile(YEAR_PATTERN)


def is_month(period: str) -> bool:
    return bool(_MONTH_RE.match(period or ""))


def is_quarter(period: str) -> bool:
    return bool(_QUARTER_RE.match(period or ""))


def is_year(period: str) -> bool:
    return bool(_YEAR_RE.match(period or ""))


def grain_of(period: str) -> str | None:
    """这个期间串是月、季还是年？认不出来返回 None（调用方负责 422）。"""
    if is_month(period):
        return MONTH
    if is_quarter(period):
        return QUARTER
    if is_year(period):
        return YEAR
    return None


def granularity_label(period: str) -> str:
    grain = grain_of(period)
    return GRAIN_LABELS.get(grain or "", "")


def quarter_of(month: str) -> str:
    year, month_number = month.split("-")
    return f"{year}-Q{(int(month_number) - 1) // 3 + 1}"


def year_of(month: str) -> str:
    return month.split("-")[0]


def months_of(period: str) -> tuple[str, ...]:
    """这个期间覆盖的月份（月=1 个、季=3 个、年=12 个）。认不出来返回空元组。"""
    if is_month(period):
        return (period,)
    if is_quarter(period):
        year, quarter = period.split("-Q")
        first = (int(quarter) - 1) * 3 + 1
        return tuple(f"{year}-{month:02d}" for month in range(first, first + 3))
    if is_year(period):
        return tuple(f"{period}-{month:02d}" for month in range(1, 13))
    return ()


def months_count(period: str) -> int:
    return len(months_of(period))


def containing_periods(month: str) -> tuple[str, str]:
    """一个月落在哪个季度、哪个年度（落库时用来决定要刷新哪两条汇总）。"""
    return quarter_of(month), year_of(month)


def period_label(period: str) -> str:
    """给人看的期间名，报告与提示条用。"""
    if is_month(period):
        year, month = period.split("-")
        return f"{year} 年 {int(month)} 月"
    if is_quarter(period):
        year, quarter = period.split("-Q")
        return f"{year} 年第 {int(quarter)} 季度"
    if is_year(period):
        return f"{period} 年度"
    return period


def previous_period(period: str) -> str | None:
    """同粒度的上一个期间；最早的一期（0001-01 之类）没有上一期时返回 None。"""
    if is_month(period):
        year, month = (int(part) for part in period.split("-"))
        if month == 1:
            year, month = year - 1, 12
        else:
            month -= 1
        return f"{year:04d}-{month:02d}"
    if is_quarter(period):
        year, quarter = (int(part) for part in period.split("-Q"))
        if quarter == 1:
            year, quarter = year - 1, 4
        else:
            quarter -= 1
        return f"{year:04d}-Q{quarter}"
    if is_year(period):
        year = int(period) - 1
        return f"{year:04d}" if year > 0 else None
    return None


def sort_key(period: str) -> tuple[int, int, int]:
    """时间顺序（同一年里 月 < 季 < 年，但它们互不比较大小，只在同粒度内排序）。

    返回 `(年, 粒度序号, 期内序号)`：月 0/1-12、季 1/1-4、年 2/1。
    """
    grain = grain_of(period)
    if grain == MONTH:
        year, month = (int(part) for part in period.split("-"))
        return (year, 0, month)
    if grain == QUARTER:
        year, quarter = (int(part) for part in period.split("-Q"))
        return (year, 1, quarter)
    if grain == YEAR:
        return (int(period), 2, 1)
    return (0, 9, 0)
