"""有限的追问条件继承：只继承程序解析的期间与指标，不继承权限或模型结论。"""
import re

from backend.rag.question_periods import TOKEN, parse

METRICS = ("收入", "费用", "成本", "净额", "利润")
FOLLOWUP = re.compile(r"^(那|那么|再|继续|还|它|这个|同样)")


def resolve(question: str, previous: dict | None = None) -> tuple[str, dict]:
    previous = previous or {}
    followup = bool(FOLLOWUP.search(question.strip()))
    resolved = question
    try:
        selection = parse(question)
    except ValueError:
        # 只有明确追问且上轮年份唯一才补全；两个年份比较后不得猜测。
        years = {value[:4] for value in previous.get("periods", [])}
        matches = list(TOKEN.finditer(question))
        if not followup or len(years) != 1 or not matches or any(
                item.lastgroup not in ("short_month", "short_quarter") for item in matches):
            raise
        year = next(iter(years))
        resolved = TOKEN.sub(lambda match: year + "年" + match.group(), question)
        selection = parse(resolved)
    if followup and not selection.groups and previous.get("periods"):
        resolved += "；期间：" + "、".join(value + "年" if len(value) == 4 else value for value in previous["periods"])
        selection = parse(resolved)
    metrics = [name for name in METRICS if name in question]
    if followup and not metrics and previous.get("metrics"):
        metrics = list(previous["metrics"])
        resolved += "；分析指标：" + "、".join(metrics)
    return resolved, {"periods": list(selection.groups), "metrics": metrics,
                      "inherited": resolved != question}
