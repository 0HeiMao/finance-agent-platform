"""确定性时间解析；相对时间按业务日历，歧义表达要求澄清，不偷换最新期。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from backend import biztime, periods

TOKEN = re.compile(
    r"(?P<quarter>(?<!\d)\d{4}\s*(?:-?[Qq]\s*\d+|年\s*第?\s*[一二三四123456789]\s*季度))"
    r"|(?P<month>(?<!\d)\d{4}\s*[-/年]\s*\d{1,2}(?!\d)\s*月?)"
    r"|(?P<relative>上个月|这个月|本月|上月|本季度|这个季度|上个季度|上季度|今年|去年)"
    r"|(?P<short_month>(?<!\d)\d{1,2}\s*月)"
    r"|(?P<short_quarter>第?\s*[一二三四123456789]\s*季度)"
    r"|(?P<year>(?<!\d)\d{4}\s*年(?:度)?)"
)
AMBIGUOUS = re.compile(r"最近|近\s*[一二三四五六七八九十\d]+\s*[个]?月|同期|同比|环比|前年|下月|下个月|[上下]半年|前\s*\d+\s*月|\d+\s*日|\d{4}-[Qq]\d+")


@dataclass(frozen=True)
class Selection:
    groups: tuple[str, ...]
    months: tuple[str, ...]
    reference_date: str
    timezone: str


def parse(question: str, *, today: date | None = None) -> Selection:
    anchor = today or biztime.today()
    current = anchor.strftime("%Y-%m")
    relative = {"本月": current, "这个月": current,
                "上月": periods.previous_period(current), "上个月": periods.previous_period(current),
                "本季度": periods.quarter_of(current), "这个季度": periods.quarter_of(current),
                "上季度": periods.previous_period(periods.quarter_of(current)),
                "上个季度": periods.previous_period(periods.quarter_of(current)),
                "今年": str(anchor.year), "去年": str(anchor.year - 1)}
    matches = list(TOKEN.finditer(question))
    groups: list[str] = []
    inherited_year: str | None = None
    remainder = list(question)
    for match in matches:
        raw = re.sub(r"\s+", "", match.group())
        kind = match.lastgroup
        if kind == "relative":
            value = relative[raw]
        elif kind == "month":
            year, month = re.findall(r"\d+", raw)
            inherited_year = year
            value = f"{year}-{int(month):02d}"
        elif kind in {"quarter", "short_quarter"}:
            if kind == "quarter":
                inherited_year = raw[:4]
                raw = raw[4:]
            if inherited_year is None:
                raise ValueError("季度没有指定年份，请写明例如2026年第一季度。")
            number = re.search(r"[一二三四\d]+", raw).group()
            quarter = {"一": 1, "二": 2, "三": 3, "四": 4}.get(number)
            quarter = quarter if quarter is not None else int(number)
            value = f"{inherited_year}-Q{quarter}"
        elif kind == "short_month":
            if inherited_year is None:
                raise ValueError("月份没有指定年份，请写明例如2026年3月。")
            month = int(re.search(r"\d+", raw).group())
            value = f"{inherited_year}-{month:02d}"
        else:
            inherited_year = raw[:4]
            value = inherited_year
        if not value or not periods.months_of(value) or int(value[:4]) < 1:
            raise ValueError(f"时间表达无效：{match.group()}。请检查年份、月份或季度。")
        groups.append(value)
        remainder[match.start():match.end()] = " " * (match.end() - match.start())
    remaining = "".join(remainder)
    if AMBIGUOUS.search(remaining):
        raise ValueError("时间范围尚不明确，请写明完整年月、季度或起止月份。")
    if len(matches) > 2 and any(re.fullmatch(r"\s*(?:到|至|~|～|—)\s*", question[a.end():b.start()])
                                for a, b in zip(matches, matches[1:], strict=False)):
        raise ValueError("一次请使用一个起止范围，或列出需要比较的明确期间。")
    # 起止范围必须按同一粒度递增，不能把两端误当作整个范围。
    if len(matches) == 2 and re.fullmatch(r"\s*(?:到|至|~|～|—)\s*", question[matches[0].end():matches[1].start()]):
        first, last = groups
        if periods.grain_of(first) != periods.grain_of(last):
            raise ValueError("起止范围请使用相同粒度，例如2026-01至2026-03。")
        start = periods.months_of(first)[0]
        end = periods.months_of(last)[-1]
        if start > end:
            raise ValueError("开始期间不能晚于结束期间。")
        index = int(start[:4]) * 12 + int(start[5:]) - 1
        finish = int(end[:4]) * 12 + int(end[5:]) - 1
        if finish - index >= 24:
            raise ValueError("一次最多分析24个月，请缩小时间范围。")
        groups = [f"{i // 12:04d}-{i % 12 + 1:02d}" for i in range(index, finish + 1)]
    unique = tuple(dict.fromkeys(groups))
    months = tuple(dict.fromkeys(month for group in unique for month in periods.months_of(group)))
    if len(months) > 24:
        raise ValueError("一次最多分析24个月，请缩小时间范围。")
    return Selection(unique, months, anchor.isoformat(), biztime.current_name())
