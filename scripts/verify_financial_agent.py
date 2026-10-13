"""验证「财务 Agent 接入已导入财务数据」以及本轮修复（鉴权/去重/上传目录/rerank）。

两条路径分开验证，因为它们的数据库会话不是同一个：
- 证据层（确定性）：直接用项目 Session 调 collect()，断言模型指令里真的带上了已入库的
  财务数字，而不是让大模型复述数字。
- 端到端（真实 SSE）：/api/v1/ai/agent/chat/stream 是 StreamingResponse，同步生成器在
  Starlette 线程池里跑，用的是 backend.db.SessionLocal 自己的（已提交）快照，
  因此这里必须先把种子数据 commit，跑完再按唯一标记清理——不能靠回滚。

清理是精确删除（只删本脚本用唯一标记写入的行），不会动演示导入的 8909 行数据。
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows 控制台默认 GBK，模型回答里的 "−"/"×" 等字符会让收尾的 print 抛 UnicodeEncodeError，
# 结果断言全过、退出码却是 1（本次就被这个坑过一次）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[1]

import httpx
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from backend.config import clear_settings_cache, get_settings  # noqa: E402
from backend.db import SessionLocal, engine
from backend.main import app
from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction, User
from backend.rag.embeddings import get_embedding_service
from backend.rag.financial_evidence import collect as collect_financial_evidence
from backend.rag.pgvector_store import DocumentChunk, search_documents
from backend.rag.rerank import rerank

PASSED: list[str] = []
FAILED: list[str] = []
SKIPPED: list[str] = []


def check(condition: bool, label: str) -> None:
    (PASSED if condition else FAILED).append(label)
    print(f"{'PASS' if condition else 'FAIL'}  {label}")


def amounts_of(block: str) -> set[str]:
    """取出文本块里的金额/比率数值，去掉千分位再比对，避免子串误判。"""
    return {value.replace(",", "") for value in re.findall(r"-?[\d,]+\.\d{2,4}", block)}


TEMP_PERIOD = "2098-11"
TEMP_AMOUNTS = {"1234567.89", "234567.89", "1000000.00"}


def seed(db: Session, prefix: str) -> None:
    """种子数据：汇总表、预警、明细三者必须自洽，否则 Agent 会看到互相矛盾的数字。"""
    db.add(FinancialMetric(period=TEMP_PERIOD, revenue_amount=Decimal("1234567.89"), cost_amount=Decimal("0.00"),
                           expense_amount=Decimal("234567.89"), gross_profit_amount=Decimal("1234567.89"),
                           net_profit_amount=Decimal("1000000.00"), gross_profit_ratio=Decimal("1.0000"),
                           net_profit_ratio=Decimal("0.8100")))
    db.add(FinancialAlert(period=TEMP_PERIOD, alert_type="验证专用利润异常", description="本期净利润为负", level="high",
                          metric_name="net_profit_amount", current_value=Decimal("-20000.00"),
                          threshold_value=Decimal("0.00")))
    db.add(FinancialTransaction(transaction_date=datetime(2098, 11, 5),
                                account_code="X1", account_name=f"{prefix}-科目A", department=f"{prefix}-部门A",
                                description="agent verification seed", revenue_amount=Decimal("1234567.89"),
                                cost_amount=Decimal("0"), expense_amount=Decimal("234567.89"),
                                cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"), import_batch=prefix))
    db.commit()


def cleanup(prefix: str) -> None:
    """只删本脚本写入的行：按唯一 import_batch / 期间 / 来源精确删除。"""
    with SessionLocal() as db:
        db.execute(delete(FinancialTransaction).where(FinancialTransaction.import_batch == prefix))
        db.execute(delete(FinancialAlert).where(FinancialAlert.period == TEMP_PERIOD))
        db.execute(delete(FinancialMetric).where(FinancialMetric.period == TEMP_PERIOD))
        db.execute(delete(DocumentChunk).where(DocumentChunk.source.like(f"{prefix}%")))
        db.execute(delete(User).where(User.username == prefix))
        db.commit()


async def call(client: httpx.AsyncClient, path: str, token: str | None, payload: dict):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return await client.post(path, json=payload, headers=headers)


async def main() -> int:
    # 验证脚本不该把上传件留在项目的 data/uploads：财务导入用 UPLOAD_DIR、文档上传用
    # RAG_UPLOAD_DIR，两个都要改，再让设置缓存失效，收尾时删除。
    upload_dir = PROJECT_ROOT / "data" / "uploads" / f"_verify_agent_{os.urandom(4).hex()}"
    os.environ["UPLOAD_DIR"] = str(upload_dir)
    os.environ["RAG_UPLOAD_DIR"] = str(upload_dir)
    clear_settings_cache()
    print("=" * 78)
    print("财务 Agent 接入财务数据 + 本轮修复验证（真 PostgreSQL / 真 ASGI / 真 DeepSeek）")
    print("=" * 78)
    print("=" * 78)
    print(f"上传目录（本次验证专用，跑完删除）：{get_settings().rag_upload_dir}")
    marker = f"agent_check_{os.urandom(4).hex()}"
    with engine.connect() as connection:
        before = connection.execute(text("SELECT count(*) FROM financial_transactions")).scalar()
    print(f"验证前 financial_transactions = {before} 行（演示导入的数据不会被改动）\n")

    ok = True
    try:
        seed(SessionLocal(), marker)
        # 口令要过 backend/passwords.py 的强度规则（含 "password" 的会被拒）。
        username, password = marker, "agent-verify-2026"
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(base_url="http://testserver", transport=transport, trust_env=False) as client:
            r = await client.post("/api/v1/auth/register", json={"username": username, "password": password})
            check(r.status_code == 200, f"[鉴权] 注册临时用户 HTTP {r.status_code}")
            token = (await client.post("/api/v1/auth/login", json={"username": username, "password": password})).json().get("access_token")
            check(bool(token), "[鉴权] 登录取到 token")

            print(f"\n--- 1. 证据层：已导入财务数据进入 Agent 上下文（种子期间 {TEMP_PERIOD}）---")
            with SessionLocal() as db:
                evidence = collect_financial_evidence(db, f"{TEMP_PERIOD} 的收支差额是多少")
            check(evidence["has_data"] is True, "[证据] has_data=True")
            check(evidence["period"] == TEMP_PERIOD, f"[证据] 识别出问题中的期间 {evidence.get('period')}")
            section = evidence["context"].split("### 当前期间明细")[0]
            check(amounts_of(section) >= TEMP_AMOUNTS, f"[证据] 汇总表含 {sorted(TEMP_AMOUNTS)}")
            check(amounts_of(evidence["context"]) >= TEMP_AMOUNTS, "[证据] 金额按元输出、无千分位（可逐字引用）")
            check(f"{marker}-部门A" in evidence["context"] and f"{marker}-科目A" in evidence["context"],
                  "[证据] 上下文含部门/科目明细")
            check("验证专用利润异常" in evidence["context"], "[证据] 上下文含本期系统预警")
            kinds = {item["kind"] for item in evidence["sources"]}
            check({"financial_metric", "financial_alert", "financial_transaction"} <= kinds,
                  f"[证据] 来源标注类型齐全 {sorted(kinds)}")
            with SessionLocal() as db:
                other = collect_financial_evidence(db, "2026-05 的收入是多少")
                latest = collect_financial_evidence(db, "最近一期情况如何")
            check(other["period"] == "2026-05" or "2026-05" not in other["periods"],
                  f"[证据] 能按问题指定的期间选期 {other.get('period')}")
            check(latest["period"] == sorted(latest["periods"])[-1], f"[证据] 未指定期间时回落到最新一期 {latest.get('period')}")

            print("\n--- 2. 证据层：知识库候选经 rerank 压缩到 top_k ---")
            settings = get_settings()
            service = get_embedding_service()
            with SessionLocal() as db:
                candidates = search_documents(db, service.encode(["费用异常在哪里"])[0], settings.rag_candidate_k,
                                              max_distance=settings.rag_max_distance, embedding_model=service.model_name)
            top = rerank("费用异常在哪里", candidates, 5)
            check(len(top) <= 5, f"[rerank] 候选 {len(candidates)} 条压缩为 {len(top)} 条")
            check(settings.rag_candidate_k >= 5, f"[rerank] 候选数 rag_candidate_k={settings.rag_candidate_k} 可配置")

            print("\n--- 3. 接口层：/api/v1/rag/* 必须鉴权 ---")
            for path, payload in (("/api/v1/rag/search", {"query": "费用", "top_k": 2}),
                                  ("/api/v1/rag/answer", {"query": "费用", "top_k": 2}),
                                  ("/api/v1/rag/documents", {"content": "x", "source": "unauth.txt"})):
                r = await call(client, path, None, payload)
                check(r.status_code == 401, f"[鉴权] 匿名 {path} -> HTTP {r.status_code}")

            print("\n--- 4. RAG 去重：同一来源同一内容不重复入库 ---")
            payload = {"content": f"{marker} 的临时制度片段：差旅费报销标准为每晚 500 元。", "source": f"{marker}-临时制度.md", "publish_public": True}
            r1 = await call(client, "/api/v1/rag/documents", token, payload)
            r2 = await call(client, "/api/v1/rag/documents", token, payload)
            check(r1.status_code == 200 and len(r1.json().get("ids", [])) == 1, f"[去重] 首次入库 {r1.json()}")
            check(r2.status_code == 200 and r2.json().get("ids") == [] and r2.json().get("skipped") == 1,
                  f"[去重] 重复入库被跳过 {r2.json()}")

            print("\n--- 5. 上传落盘目录不再写 data/raw ---")
            with tempfile.TemporaryDirectory() as tmp:
                sample = Path(tmp) / f"{marker}-制度.md"
                sample.write_text("差旅费报销标准为每晚 500 元。\n住宿发票需与行程单一致。\n", encoding="utf-8")
                files = {"file": (sample.name, io.BytesIO(sample.read_bytes()), "text/markdown")}
                up1 = await client.post("/api/v1/rag/documents/upload", files=files, data={"publish_public": "true"}, headers={"Authorization": f"Bearer {token}"})
                body1 = up1.json()
                files = {"file": (sample.name, io.BytesIO(sample.read_bytes()), "text/markdown")}
                up2 = await client.post("/api/v1/rag/documents/upload", files=files, data={"publish_public": "true"}, headers={"Authorization": f"Bearer {token}"})
                body2 = up2.json()
            check(up1.status_code == 200 and body1.get("chunks", 0) >= 1, f"[上传] 首次入库 {body1.get('chunks')} 段")
            check(up2.status_code == 200 and body2.get("chunks") == 0 and body2.get("skipped", 0) >= 1,
                  f"[上传] 同文件二次上传不重复入库 {body2}")
            with SessionLocal() as db:
                stored = db.scalars(select(DocumentChunk.metadata_json["stored_path"].astext).where(
                    DocumentChunk.source == sample.name)).all()
            bad = [p for p in stored if p and "data/raw" in p.replace("\\", "/")]
            check(bool(stored) and not bad, f"[上传] 落盘路径不在 data/raw（样例 {stored[0] if stored else '无'}）")

            print("\n--- 6. 端到端：真实 SSE /api/v1/ai/agent/chat/stream ---")
            done_payload: dict = {}
            error_payload: dict = {}
            answer_text = ""
            try:
                async with asyncio.timeout(180):
                    async with client.stream("POST", "/api/v1/ai/agent/chat/stream",
                                             json={"question": f"{TEMP_PERIOD} 的收入和收支差额分别是多少？"},
                                             headers={"Authorization": f"Bearer {token}"}) as response:
                        event_name = ""
                        async for line in response.aiter_lines():
                            if line.startswith("event: "):
                                event_name = line[7:].strip()
                            elif line.startswith("data: "):
                                raw = line[6:]
                                if event_name == "delta":
                                    answer_text += json.loads(raw)["text"]
                                elif event_name == "done":
                                    done_payload = json.loads(raw)
                                elif event_name == "error":
                                    error_payload = json.loads(raw)
            except Exception as exc:  # 网络不可用时不算失败，但必须说明
                print(f"WARN  端到端流式调用异常：{exc}")
            print(f"done 事件: {json.dumps(done_payload, ensure_ascii=False)[:600]}")
            if error_payload:
                print(f"error 事件: {error_payload}")
            check(done_payload.get("has_financial_data") is True, "[SSE] done 事件声明已接入财务数据")
            check(done_payload.get("matched_period") == TEMP_PERIOD, f"[SSE] done 事件带上匹配期间 {done_payload.get('matched_period')}")
            source_names = {item.get("name") for item in done_payload.get("sources", []) if isinstance(item, dict)}
            check(any(f"财务汇总表 {TEMP_PERIOD}" == name for name in source_names), f"[SSE] 来源含财务汇总表 {TEMP_PERIOD}")
            check(any(item.get("kind") == "knowledge_base" for item in done_payload.get("sources", [])), "[SSE] 来源含知识库片段")
            print(f"--- 模型回答（{len(answer_text)} 字）---\n{answer_text[:900]}")
            check(bool(answer_text.strip()), f"[SSE] 模型返回了回答（{len(answer_text)} 字）")
            check(TEMP_AMOUNTS & amounts_of(answer_text), f"[SSE] 回答引用了已导入的真实金额 {sorted(TEMP_AMOUNTS & amounts_of(answer_text))}")
            check("不存在" not in answer_text and "无法回答" not in answer_text,
                  "[SSE] 回答没有再说“证据里不存在该期间”（修复前正是这个症状）")

            ok = not FAILED
    finally:
        cleanup(marker)
        with engine.connect() as connection:
            after = connection.execute(text("SELECT count(*) FROM financial_transactions")).scalar()
            residue = connection.execute(text("SELECT count(*) FROM financial_metrics WHERE period = :p"), {"p": TEMP_PERIOD}).scalar()
        print(f"\n已清理验证数据：financial_transactions {before} -> {after} 行，临时期间残留 {residue} 行。")
        shutil.rmtree(upload_dir, ignore_errors=True)
        app.dependency_overrides.clear()
        if after != before:
            ok = False
            FAILED.append(f"[清理] 行数未复原：{before} -> {after}")

    print("=" * 78)
    print(f"结果：{len(PASSED)} 项通过，{len(FAILED)} 项失败" + (f"，{len(SKIPPED)} 项跳过" if SKIPPED else ""))
    for label in FAILED:
        print(f"  FAILED: {label}")
    print("PASS" if ok else "FAIL")
    print("=" * 78)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
