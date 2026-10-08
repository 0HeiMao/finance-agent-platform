"""把 `backend/ratelimit` 的配额接到 FastAPI 依赖上。

为什么单独一层：`ratelimit.py` 刻意不 import FastAPI（它是纯计数，能脱离 Web 单测），
把「怎么拒绝」这件事放在这里，`HTTPException` 的文案与响应头就只有一份。

两组配额：

- `export`：明细/指标/预警导出。一次请求能把整库拉成 CSV，`EXPORT_LIMIT` 只限制**单次**行数，
  拦不住「脚本每分钟导 100 次」。
- `ai`：RAG 问答与财务 Agent（含流式）。每次都要调模型，按量计费的东西必须有配额。

被拒绝时返回 `429` + `Retry-After`，**不写审计**：审计一条 429 反而给攻击者提供了
「用便宜请求写满审计表」的放大器，观测性里已经按 `429:路径` 记了计数（管理概览能看到）。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException

from backend import ratelimit
from backend.auth import current_user
from backend.models import User


def rate_limit(name: str, label: str):
    """返回一个依赖：按「配额名 + 当前账号」计数，超限抛 429。"""

    def dependency(user: User = Depends(current_user)) -> None:
        decision = ratelimit.consume(name, user.username)
        if not decision.allowed:
            raise HTTPException(
                429,
                f"{label}过于频繁，请在 {decision.retry_after} 秒后重试",
                headers={"Retry-After": str(decision.retry_after)},
            )

    return dependency


export_quota = rate_limit("export", "导出")
ai_quota = rate_limit("ai", "问答")
