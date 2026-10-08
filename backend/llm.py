from functools import lru_cache

from backend.config import get_settings

SYSTEM_PROMPT = """你是严谨的智能财务分析助手。只能基于提供的财务数据和检索证据回答。证据不足时必须明确说数据不足，不得编造数字、原因或政策结论。金额保留两位小数，比率保留四位小数。回答包含：结论、数据依据、风险提示。"""

class DeepSeekClient:
    def __init__(self):
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("未安装 openai，请执行 pip install openai") from exc
        settings = get_settings()
        if not settings.deepseek_api_key:
            raise RuntimeError("未配置 DEEPSEEK_API_KEY")
        self.client = OpenAI(api_key=settings.deepseek_api_key, base_url=settings.deepseek_base_url)
        self.model = settings.deepseek_model

    def answer(self, question: str, context: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.1,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": f"问题：{question}\n\n检索证据：\n{context or '无有效证据'}"},
            ],
        )
        return response.choices[0].message.content or "数据不足，无法生成可靠回答。"

    def stream(self, question: str, context: str):
        response = self.client.chat.completions.create(model=self.model, temperature=0.1, stream=True, messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"问题：{question}\n\n检索证据：\n{context or '无有效证据'}"},
        ])
        for chunk in response:
            text = chunk.choices[0].delta.content if chunk.choices else None
            if text: yield text

@lru_cache(maxsize=1)
def get_llm() -> DeepSeekClient:
    return DeepSeekClient()
