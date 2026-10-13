"""有限证据检查：引用白名单、逐条数字对应及财务工具类型；不是语义事实证明器。"""
import json
import re
from decimal import Decimal

from backend.agent_facts import render_bound_claim

NUMBER = re.compile(r"(?<![\w.])-?\d+(?:,\d{3})*(?:\.\d+)?(?![\d.])")
FINANCIAL = re.compile(r"收入|费用|成本|利润|净额|差额|增长|金额")


def numbers(text):
    # 中文旁的数字同样需要匹配，\w 包含中文，先将汉字换成空格。
    text = re.sub(r"[\u4e00-\u9fff]", " ", text)
    return {Decimal(value.replace(",", "")) for value in NUMBER.findall(text)}


def validate(content: str, ledger: dict) -> tuple[str | None, str | None]:
    """模型必须把每一条结论绑定本轮证据ID；失败返回可修正错误而不是原始模型文本。"""
    try:
        body = json.loads(content)
        claims = body["claims"]
        if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
            raise ValueError()
        rendered = []
        for claim in claims:
            text = claim["text"]
            ids = claim["evidence_ids"]
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError()
            if not isinstance(ids, list) or not ids or any(not isinstance(key, str) or key not in ledger for key in ids):
                return None, "每条结论必须引用本轮实际返回的证据ID。"
            evidence = [ledger[key] for key in ids]
            if any(item["tool"] in ("query_metrics", "compare_expenses") for item in evidence):
                try:
                    bound = render_bound_claim(claim, ledger)
                except ValueError as exc:
                    return None, str(exc)
                rendered.append(f"- {bound}\n  依据：{'、'.join(ids)}（指标字段、期间、范围与单位已逐项核对）")
                continue
            if claim.get("fact_refs"):
                return None, "只有财务工具支持字段绑定。"
            allowed = set()
            for item in evidence:
                allowed |= numbers(json.dumps(item["result"], ensure_ascii=False))
            # 不允许模型自行做单位、百分比换算；应原样引用程序结果。
            if not numbers(text) <= allowed:
                return None, "结论含有引用证据中没有的数字；金额、比率须使用工具原值。"
            if FINANCIAL.search(text) and re.search(r"\d[\d,.]*\s*(?:元|%|％)", text):
                financial = [item for item in evidence if item["tool"] in ("query_metrics", "compare_expenses")]
                if not financial:
                    return None, "财务金额结论必须引用本轮财务指标或费用对比工具。"
            source_names = {source["name"] for item in evidence for source in item["result"].get("sources", [])}
            for filename in re.findall(r"[\w\u4e00-\u9fff-]+\.(?:md|pdf|docx|xlsx|csv)", text):
                if filename not in source_names:
                    return None, "引用文件名不在对应工具返回的来源中。"
            sources = "、".join(sorted(source_names)) or "当前可见期间列表"
            rendered.append(f"- {text.strip()}\n  依据：{sources}（{'、'.join(ids)}）")
        return "\n\n".join(rendered), None
    except (ValueError, TypeError, KeyError):
        return None, '回答格式必须为 {"claims":[{"text":"结论","evidence_ids":["E1"]}]}。'


def validate_parts(content: str, ledger: dict) -> tuple[str | None, str | None]:
    """逐条保留可核验结论，单条坏引用不抹掉其他独立子任务的正确结果。"""
    try:
        claims = json.loads(content)["claims"]
        if not isinstance(claims, list) or not 1 <= len(claims) <= 12:
            return validate(content, ledger)
    except (ValueError, TypeError, KeyError):
        return validate(content, ledger)
    accepted, rejected = [], []
    for claim in claims:
        answer, problem = validate(json.dumps({"claims": [claim]}, ensure_ascii=False), ledger)
        if answer:
            accepted.append(answer)
        if problem:
            rejected.append(problem)
    return "\n\n".join(accepted) or None, "；".join(dict.fromkeys(rejected)) or None
