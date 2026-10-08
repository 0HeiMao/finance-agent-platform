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
from datetime import date

from sqlalchemy import and_, false

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


def month_bounds(month: str) -> tuple[date, date] | None:
    """月的前闭后开区间 `[本月首日, 次月首日)`；认不出来返回 None。"""
    if not is_month(month):
        return None
    year, month_number = (int(part) for part in month.split("-"))
    start = date(year, month_number, 1)
    end = date(year + 1, 1, 1) if month_number == 12 else date(year, month_number + 1, 1)
    return start, end


def period_bounds(period: str) -> tuple[date, date] | None:
    """期间的前闭后开区间：月/季/年都走 `months_of()`，季与年就是首月首日到末月的次月首日。"""
    months = months_of(period)
    if not months:
        return None
    first = month_bounds(months[0])
    last = month_bounds(months[-1])
    if first is None or last is None:
        return None
    return first[0], last[1]


def period_range(column, period: str):
    """「期间」条件写成能走索引的前闭后开区间比较。

    以前到处是 `to_char(transaction_date, 'YYYY-MM') = '2026-10'`：**函数套在列上**，
    btree 索引对它没用——`(department, transaction_date)` 复合索引的第二列等于白搭，
    而且每一行都要算一次 `to_char` 才能比较。区间比较是 sargable 的（列单独在一边），
    语义完全一样：`transaction_date` 存的是当天零点，月末最后一条也在 `[本月首日, 次月首日)` 内。

    好处不只是快：`2026-10-31 23:59:59` 这种带时间的行，旧的字符串相等比较会漏掉（不入任何月份），
    区间比较把它算进 10 月——这才是「按期间筛选」该有的行为。

    期间串认不出来时返回恒假条件，与旧的相等比较行为一致（一行都不匹配），
    这样调用方不用为「非法期间」单独写分支（接口层本来就会先做格式校验）。
    """
    bounds = period_bounds(period)
    if bounds is None:
        return false()
    start, end = bounds
    return and_(column >= start, column < end)


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
