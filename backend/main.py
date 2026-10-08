import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware

from backend import observability
from backend.api_admin import router as admin_router
from backend.api_ai import router as ai_router
from backend.api_analysis import router as analysis_router
from backend.api_dashboard import router as dashboard_router
from backend.api_export import router as export_router
from backend.api_financial import router as financial_router
from backend.api_forecast import router as forecast_router
from backend.api_rag import router as rag_router
from backend.api_reports import router as reports_router
from backend.api_system import router as system_router
from backend.auth import router as auth_router
from backend.config import get_settings
from backend.db import engine
from backend.jobs import reap_stale_jobs
from backend.migrations import ensure_schema, missing_columns, missing_indexes
from backend.observability import record_request, snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("finance-api")


def check_startup_config() -> None:
    """生产环境带着占位密钥启动就直接失败；开发环境只告警，不影响本地联调。

    uvicorn 的 lifespan 里抛异常会终止启动，所以这里只在 strict 场景 raise。
    """
    settings = get_settings()
    problems = settings.validate()
    if not problems:
        return
    strict = bool(settings.strict_config) or not settings.is_development
    prefix = "配置错误" if strict else "配置告警"
    for problem in problems:
        logger.error("%s：%s", prefix, problem)
    if strict:
        raise RuntimeError("；".join(problems))

class ObservabilityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info("request path=%s method=%s status=%s duration_ms=%s",
                    request.url.path, request.method, response.status_code, elapsed_ms)
        record_request(request.url.path, response.status_code, elapsed_ms)
        response.headers["X-Response-Time-Ms"] = str(elapsed_ms)
        return response

app = FastAPI(title="智能财务分析平台 API", version="1.0.0")
app.add_middleware(ObservabilityMiddleware)
app.add_middleware(
    CORSMiddleware,
    # 允许来源跟随后端配置，不再硬编码；.env 里用逗号分隔多个来源。
    allow_origins=get_settings().cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(rag_router)
app.include_router(auth_router)
app.include_router(financial_router)
app.include_router(analysis_router)
app.include_router(ai_router)
app.include_router(forecast_router)
app.include_router(reports_router)
app.include_router(system_router)
app.include_router(admin_router)
app.include_router(export_router)
# 看板概览：接口与它那串 Decimal 汇总助手都在 backend/api_dashboard.py（曾内联在这个文件里）
app.include_router(dashboard_router)

@app.on_event("startup")
def _validate_config_on_startup() -> None:
    check_startup_config()
    # 结构变更（加表/加列）在启动时补齐：老库不会有人记得手工跑迁移脚本，
    # 少一列的表现是接口 500，很难一眼看出是结构问题。失败只告警，不拦启动。
    try:
        applied = ensure_schema()
        if applied:
            logger.info("schema 迁移已应用：%s", "、".join(applied))
        # 补列靠人工维护 COLUMNS：漏一行的表现是接口 500，所以在启动时就点名。
        gaps = missing_columns()
        if gaps:
            logger.error("数据库结构与模型不一致，缺失：%s（检查 backend/migrations.py 的 COLUMNS）",
                         "、".join(gaps))
        # 索引漏了不会报错、只会一直顺序扫，所以同样点名（对应 migrations.INDEXES）。
        index_gaps = missing_indexes()
        if index_gaps:
            logger.error("数据库索引与模型不一致，缺失：%s（检查 backend/migrations.py 的 INDEXES）",
                         "、".join(index_gaps))
    except Exception as exc:  # noqa: BLE001
        logger.error("schema 迁移失败（数据库可能不可用）：%s", exc)

    # 后台任务是进程内队列：进程重启时还挂在 queued/running 的记录再也不会有人来跑，
    # 必须在这里收尸，否则界面上会永远显示"运行中"。
    try:
        with Session(engine) as session:
            reaped = reap_stale_jobs(session)
        if reaped:
            logger.info("启动清理：%s 条未完成的后台任务已标记为失败", reaped)
    except Exception as exc:  # noqa: BLE001
        logger.error("后台任务清理失败：%s", exc)

    # 慢接口告警的阈值只在启动时说一次：出问题时看日志的人得知道「超过多少才算慢」，
    # 否则一条告警到底严不严无从判断。
    limits = observability.thresholds()
    logger.info("慢接口告警阈值：默认 %s ms；按路径覆盖 %s；同路径告警间隔 %s 秒",
                limits["default_ms"], limits["overrides"] or "无", limits["log_seconds"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: str | None = None


class ChatResponse(BaseModel):
    answer: str
    conversation_id: str | None = None
    sources: list[dict] = []


@app.get("/api/health")
@app.get("/api/v1/health")
def health():
    return {"status": "ok", "service": "finance-api"}


@app.get("/api/v1/observability/metrics")
def observability_metrics():
    """返回当前进程的基础请求指标，生产环境可由 Prometheus 采集。"""
    return snapshot()


@app.post("/api/chat", response_model=ChatResponse)
@app.post("/api/v1/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    """基础问答占位接口，后续接入 RAG/Agent 编排。"""
    return ChatResponse(
        answer="已收到问题。RAG 和财务 Agent 尚未接入，请稍后重试。",
        conversation_id=payload.conversation_id,
        sources=[],
    )


async def chat_stream(payload: ChatRequest) -> AsyncIterator[str]:
    """以 SSE 格式返回占位流，保持前端可以先完成流式联调。"""
    answer = "已收到问题。RAG 和财务 Agent 尚未接入，请稍后重试。"
    for index in range(0, len(answer), 8):
        yield f"data: {json.dumps({'type': 'token', 'content': answer[index:index + 8]}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.01)
    yield f"data: {json.dumps({'type': 'done', 'conversation_id': payload.conversation_id}, ensure_ascii=False)}\n\n"


@app.post("/api/chat/stream")
@app.post("/api/v1/chat/stream")
async def chat_stream_endpoint(payload: ChatRequest):
    return StreamingResponse(
        chat_stream(payload),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
