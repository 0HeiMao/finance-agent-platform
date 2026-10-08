"""候选检索结果的重排序。

背景：本地多语言 embedding 模型（384 维）对中文财务短问题的区分度有限，实测相关片段
余弦距离 0.33~0.57、不相关片段 0.52~0.99，两者重叠，单靠阈值无法分开（见 README 的
「向量检索阈值」一节）。因此先用宽松阈值多召回候选，再用可配置的重排模型压缩到 top_k。

重排默认关闭（RERANK_MODEL 为空），此时保持按余弦距离的原始顺序，行为与修复前一致。
"""

from __future__ import annotations

from typing import Any, Sequence

from backend.config import get_settings

RERANK_PROMPT = """你是检索重排器。下面有用户问题和若干编号文档片段。
请只输出一个 JSON，形如 {"order": [编号,...]}，把与问题最相关的片段编号按相关度从高到低排列，只保留真正相关的编号，最多 {limit} 个。
不要输出任何解释、Markdown 代码块或其他文字。"""


def _order_from_text(text: str, count: int) -> list[int]:
    import json
    import re

    cleaned = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        parsed = None
    candidates: Any = parsed.get("order") if isinstance(parsed, dict) else parsed
    if candidates is None:
        candidates = re.findall(r"\d+", cleaned)
    order: list[int] = []
    for item in candidates if isinstance(candidates, list) else []:
        try:
            index = int(item) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < count and index not in order:
            order.append(index)
    return order


def rerank(question: str, chunks: Sequence[Any], top_k: int) -> list[Any]:
    """chunks 为已按余弦距离排序的候选片段（需带 content 属性，可选 source）。"""
    if not get_settings().rerank_model or len(chunks) <= top_k:
        return list(chunks[:top_k])
    try:
        from backend.llm import get_llm

        catalog = "\n".join(
            f"[{index + 1}] 来源：{getattr(chunk, 'source', '') or '未知'}\n{(getattr(chunk, 'content', '') or '')[:500]}"
            for index, chunk in enumerate(chunks)
        )
        prompt = f"用户问题：{question}\n\n文档片段：\n{catalog}"
        text = get_llm().answer(prompt, RERANK_PROMPT.format(limit=top_k))
        order = _order_from_text(text, len(chunks))
    except Exception:
        return list(chunks[:top_k])
    if not order:
        return list(chunks[:top_k])
    picked = [chunks[index] for index in order[:top_k]]
    taken = {index for index in order[:top_k]}
    for index in range(len(chunks)):  # 重排模型漏掉的候选，按余弦顺序补齐到 top_k
        if len(picked) >= top_k:
            break
        if index not in taken:
            picked.append(chunks[index])
    return picked
