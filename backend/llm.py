from functools import lru_cache

from backend.config import get_settings

SYSTEM_PROMPT = """你是严谨的智能财务分析助手。只能基于提供的财务数据和检索证据回答。证据不足时必须明确说数据不足，不得编造数字、原因或政策结论。金额保留两位小数，比率保留四位小数。回答包含：结论、数据依据、风险提示。"""


def _int_or_zero(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def usage_of(raw) -> dict:
    """把模型返回的 usage 压成三个数字，没有用量时返回空字典。

    兼容两种形状：openai SDK 的 `CompletionUsage` 对象，以及 OpenAI 兼容网关
    在 JSON 里直接给的那个 dict。全 0 也当没有——那通常是网关占位，不是真账单。
    """
    if raw is None:
        return {}
    if isinstance(raw, dict):
        def get(key):
            return raw.get(key)
    else:
        def get(key):
            return getattr(raw, key, None)
    prompt = _int_or_zero(get("prompt_tokens"))
    completion = _int_or_zero(get("completion_tokens"))
    total = _int_or_zero(get("total_tokens"))
    if not (prompt or completion or total):
        return {}
    return {"prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total or prompt + completion}

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
        # 让网关在最后一个 chunk 里带上本次用量（⑮ 的 token 统计靠它）
        self.include_usage = bool(getattr(settings, "llm_include_usage", True))

    def _messages(self, question: str, context: str, history: list[dict] | None = None) -> list[dict]:
        """系统提示 + 历史问答（⑰）+ 本轮提问与证据。

        顺序是刻意的：系统提示里的"只依据证据、不得编造"不能被历史里模型自己上一轮的话冲掉，
        所以历史夹在中间；本轮的检索证据必须紧挨着本轮提问，否则模型会把上一轮的证据
        当成本轮的（两轮证据长的很像，只差一个期间）。
        """
        messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        for item in history or []:
            role = str(item.get("role") or "")
            content = str(item.get("content") or "").strip()
            if role in ("user", "assistant") and content:
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": f"问题：{question}\n\n检索证据：\n{context or '无有效证据'}"})
        return messages

    def answer(self, question: str, context: str, usage: dict | None = None,
               history: list[dict] | None = None) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            temperature=0.1,
            messages=self._messages(question, context, history),
        )
        reported = usage_of(getattr(response, "usage", None))
        if usage is not None and reported:
            usage.update(reported)
        return response.choices[0].message.content or "数据不足，无法生成可靠回答。"

    def rank(self, question: str, instructions: str, model: str) -> str:
        """重排使用独立指令及指定模型，不混入财务回答格式要求。"""
        response = self.client.chat.completions.create(
            model=model, temperature=0,
            messages=[{"role": "system", "content": instructions},
                      {"role": "user", "content": question}],
        )
        return response.choices[0].message.content or ""

    def tool_turn(self, messages: list[dict], tools: list[dict], *, timeout: float,
                  max_tokens: int) -> tuple[dict, dict]:
        """一次原生工具选择；禁用 SDK 自动重试，重试预算由任务执行器管理。"""
        response = self.client.with_options(timeout=timeout, max_retries=0).chat.completions.create(
            model=self.model, temperature=0, messages=messages, tools=tools,
            max_tokens=max_tokens,
        )
        if not response.choices or response.choices[0].finish_reason == "length":
            raise RuntimeError("模型响应为空或超过单轮输出上限")
        return response.choices[0].message.model_dump(exclude_none=True), usage_of(response.usage)

    def _chunks(self, messages: list[dict], *, include_usage: bool):
        """逐块产出 `(文本, 用量)`：文本可能为 None（只有用量那个收尾块），用量通常是 {}。"""
        params = {"model": self.model, "temperature": 0.1, "stream": True, "messages": messages}
        if include_usage:
            params["stream_options"] = {"include_usage": True}
        response = self.client.chat.completions.create(**params)
        for chunk in response:
            reported = usage_of(getattr(chunk, "usage", None))
            text = chunk.choices[0].delta.content if getattr(chunk, "choices", None) else None
            yield text, reported

    def stream(self, question: str, context: str, usage: dict | None = None,
               history: list[dict] | None = None):
        """逐块吐字，`usage` 传字典就把这次调用的用量写进去。

        `history` 是同一个会话里前面的问答（⑰ 的预算由调用方裁好再传进来，这里不做截断）。

        `stream_options={"include_usage": True}` 并非所有 OpenAI 兼容网关都认：有的直接报错。
        所以只有「一个字都还没吐出去」的时候才允许不带该参数重试一次——已经吐过再重试，
        用户会看到重复正文。真拿不到用量时上层会退回按字数估算（usage_source="estimated"）。
        """
        messages = self._messages(question, context, history)
        produced = False
        try:
            for text, reported in self._chunks(messages, include_usage=self.include_usage):
                if reported and usage is not None:
                    usage.update(reported)
                if text:
                    produced = True
                    yield text
            return
        except Exception:
            if produced or not self.include_usage:
                raise
        for text, reported in self._chunks(messages, include_usage=False):
            if reported and usage is not None:
                usage.update(reported)
            if text:
                yield text

@lru_cache(maxsize=1)
def get_llm() -> DeepSeekClient:
    return DeepSeekClient()
