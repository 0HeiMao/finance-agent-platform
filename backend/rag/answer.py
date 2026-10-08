"""RAG answer orchestration: retrieval stays deterministic, generation is optional."""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from backend.config import get_settings


def build_context(results: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    sources: list[dict[str, Any]] = []
    blocks: list[str] = []
    for index, item in enumerate(results, 1):
        metadata = getattr(item, "metadata_json", {}) or {}
        source = {"id": index, "source": item.source, "page": item.page,
                  "document_type": item.document_type, "metadata": metadata}
        sources.append(source)
        blocks.append(f"[{index}] 来源：{item.source} 页码：{item.page or '未知'}\n{item.content}")
    return "\n\n".join(blocks), sources


def generate_answer(query: str, results: list[Any]) -> tuple[str, list[dict[str, Any]]]:
    context, sources = build_context(results)
    if not context:
        return "未检索到与问题相关的财务资料，无法基于现有资料作答。", sources
    settings = get_settings()
    api_key = settings.effective_api_key
    base_url = settings.effective_base_url
    # model_name 默认是空字符串；DeepSeek 会直接拒掉空模型名，所以回退到 deepseek_model。
    model_name = settings.model_name or settings.deepseek_model
    if not api_key or not base_url:
        return f"已检索到 {len(sources)} 条资料。当前未配置大模型，以下为可核验依据：\n\n{context}", sources
    prompt = ("你是财务审计助手。只能依据提供的资料回答，不得编造数字。"
              "若资料不足，请明确说明。回答中的关键结论必须标注 [编号]。\n"
              f"问题：{query}\n资料：\n{context}")
    payload = json.dumps({"model": model_name, "messages": [{"role": "user", "content": prompt}], "temperature": 0}).encode()
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        answer = body["choices"][0]["message"]["content"]
        return answer, sources
    except Exception:
        return "模型服务暂时不可用。请依据下方检索来源人工核验：\n\n" + context, sources
