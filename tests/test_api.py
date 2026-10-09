"""真 PostgreSQL + 真 ASGI 的接口测试。

隔离方式：conftest 的外层事务 + savepoint，所以这里可以放心地让接口真的 commit、
真的写库，用例结束统一回滚（库里不留数据）。
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

pytestmark = pytest.mark.anyio

from backend.config import get_settings  # noqa: E402
from backend.models import FinancialAlert, FinancialMetric  # noqa: E402

# ---------------------------------------------------------------- 基础

async def test_health_endpoints(anon_client):
    for path in ("/api/health", "/api/v1/health"):
        response = await anon_client.get(path)
        assert response.status_code == 200, path
        assert response.json()["status"] == "ok"
    assert (await anon_client.get("/api/v1/observability/metrics")).status_code == 200


async def test_protected_endpoints_require_token(anon_client):
    """匿名访问业务接口必须 401（曾经 /api/v1/rag/* 完全没有鉴权）。"""
    checks = [
        ("get", "/api/v1/dashboard?period=year"),
        ("get", "/api/v1/analysis/summary?period=2026-10"),
        ("get", "/api/v1/analysis/alerts/all"),
        ("get", "/api/v1/reports/monthly?period=2026-10"),
        ("get", "/api/v1/financial/template"),
        ("post", "/api/v1/rag/search"),
        ("post", "/api/v1/rag/answer"),
        ("post", "/api/v1/rag/documents"),
        ("post", "/api/v1/ai/rag/chat"),
        ("post", "/api/v1/ai/agent/ask"),
    ]
    for method, path in checks:
        call = getattr(anon_client, method)
        # GET 不接受 json= 关键字，只有 POST 需要空 body 让 FastAPI 走到鉴权依赖。
        response = await (call(path, json={}) if method == "post" else call(path))
        assert response.status_code == 401, f"{method.upper()} {path} 未要求鉴权：{response.status_code}"


# ---------------------------------------------------------------- 认证

async def test_register_login_and_duplicate(anon_client, credentials, db_session):
    assert (await anon_client.post("/api/v1/auth/register", json=credentials)).status_code == 200
    duplicate = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert duplicate.status_code == 409
    assert "已存在" in duplicate.json()["detail"]

    bad = await anon_client.post("/api/v1/auth/login", json={**credentials, "password": "wrong-password"})
    assert bad.status_code == 401
    assert "用户名或密码错误" in bad.json()["detail"]

    good = await anon_client.post("/api/v1/auth/login", json=credentials)
    assert good.status_code == 200
    assert good.json()["token_type"] == "bearer"
    assert good.json()["access_token"]


async def test_self_service_scope_change_is_closed(anon_client, auth_headers, db_session):
    """⑳：数据范围与权限都由服务端记，而且**自己改不了**。

    这里同时是那个提权洞的回归测试：⑳ 之前 `PATCH /auth/me {"departments": []}` 等于一键升权
    （当时「没有部门」就是「管理员」的定义），任何注册出来的账号都能读全库、进管理端。
    """
    from backend.models import AuditLog

    me = await anon_client.get("/api/v1/auth/me", headers=auth_headers)
    assert me.status_code == 200
    assert me.json()["department"] is None
    assert me.json()["unrestricted"] is True
    assert me.json()["is_admin"] is True  # 夹具显式授予的管理员标识

    # 伪造请求头自称管理员：不影响服务端记录
    forged = await anon_client.get("/api/v1/auth/me", headers={**auth_headers, "X-User-Department": "finance-admin"})
    assert forged.json()["department"] is None

    # 给自己配部门、清空部门、顺手把管理员标识也带上：全部 403，且一个字段都没改
    for body in ({"department": " 部门-097 "}, {"departments": ["部门A"]}, {"departments": []},
                 {"departments": [], "is_admin": False}):
        denied = await anon_client.patch("/api/v1/auth/me", headers=auth_headers, json=body)
        assert denied.status_code == 403, (body, denied.text)
        assert "只能由管理员" in denied.json()["detail"]

    after = (await anon_client.get("/api/v1/auth/me", headers=auth_headers)).json()
    assert after["department"] is None and after["departments"] == []
    assert after["unrestricted"] is True and after["is_admin"] is True

    # 被拒也留痕：这是「账号试图给自己扩权」的信号，值班时要看得见
    db_session.expire_all()
    row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "user.department_self_denied")
        .order_by(AuditLog.id.desc())).first()
    assert row is not None and row.detail["username"] == after["username"]


async def test_expired_and_forged_tokens_are_rejected(anon_client):
    response = await anon_client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert response.status_code == 401
    assert response.json()["detail"] == "登录已失效"


# ---------------------------------------------------------------- 分析链路

def _seed_transactions(db, period: str, rows):
    from backend.models import FinancialTransaction

    for index, (revenue, cost, expense) in enumerate(rows):
        db.add(FinancialTransaction(
            transaction_date=f"{period}-{index + 1:02d}",
            account_name="测试科目", account_code="6001", department="部门-097",
            description="pytest 种子", revenue_amount=Decimal(revenue),
            cost_amount=Decimal(cost), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"),
            import_batch="pytest-seed",
        ))
    db.flush()


async def test_calculate_summary_and_alerts(anon_client, auth_headers, db_session):
    period = "2097-03"
    # 种子金额写成两位小数（Numeric(18,2) 是最终口径）；三位小数会被 PostgreSQL 再舍入一次，
    # 断言就会变成在测数据库而不是在测聚合逻辑。
    _seed_transactions(db_session, period, [("100", "0", "20"), ("200", "0", "30")])

    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)
    assert calculated.status_code == 200
    payload = calculated.json()
    assert Decimal(str(payload["revenue_amount"])) == Decimal("300.00")
    assert Decimal(str(payload["net_profit_amount"])) == Decimal("250.00")
    assert payload["alerts"] == 0

    summary = await anon_client.get(f"/api/v1/analysis/summary?period={period}", headers=auth_headers)
    assert summary.json()["has_data"] is True
    assert Decimal(str(summary.json()["net_profit_amount"])) == Decimal("250.00")

    alerts = await anon_client.get(f"/api/v1/analysis/alerts?period={period}", headers=auth_headers)
    assert alerts.json()["data"] == []

    every = await anon_client.get("/api/v1/analysis/alerts/all?limit=5", headers=auth_headers)
    assert every.status_code == 200
    assert period in every.json()["periods"]


async def test_calculate_raises_profit_alert_when_net_is_negative(anon_client, auth_headers, db_session):
    period = "2097-04"
    _seed_transactions(db_session, period, [("100", "0", "150")])

    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)
    assert calculated.json()["alerts"] == 1

    alerts = await anon_client.get(f"/api/v1/analysis/alerts?period={period}", headers=auth_headers)
    items = alerts.json()["data"]
    assert [item["type"] for item in items] == ["利润异常"]
    assert items[0]["metric"] == "net_profit_amount"

    detail = await anon_client.get(f"/api/v1/analysis/alerts/{items[0]['id']}/transactions", headers=auth_headers)
    assert detail.status_code == 200
    assert detail.json()["alert"]["level"] == "high"
    assert len(detail.json()["data"]) == 1
    assert detail.json()["data"][0]["account"] == "测试科目"

    missing = await anon_client.get("/api/v1/analysis/alerts/999999/transactions", headers=auth_headers)
    assert missing.status_code == 200
    assert missing.json()["data"] == []


async def test_unsupported_period_is_rejected(anon_client, auth_headers):
    assert (await anon_client.post("/api/v1/analysis/calculate?period=2097-13", headers=auth_headers)).status_code == 422
    assert (await anon_client.get("/api/v1/dashboard?period=week", headers=auth_headers)).status_code == 422


# ---------------------------------------------------------------- 看板

async def test_dashboard_amounts_are_exact_strings(anon_client, auth_headers, db_session):
    period = "2097-05"
    _seed_transactions(db_session, period, [("100.005", "0", "20")])
    await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)

    response = await anon_client.get("/api/v1/dashboard?period=year", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_data"] is True
    assert payload["range"]["year"] == 2097

    labels = [kpi["label"] for kpi in payload["kpi"]]
    assert labels == ["营业收入", "成本金额", "费用金额", "收支差额（简化口径）", "收支差额率", "成本率"]

    # 金额必须是字符串：float 在万亿量级会丢分位，和源文件对不上
    assert isinstance(payload["revenue"][-1], str)
    assert all(isinstance(item["value"], str) for item in payload["expenses"])
    assert isinstance(payload["cash"]["inflow"][0], str)
    for kpi in payload["kpi"]:
        assert isinstance(kpi["value"], str)
        # 无可比期间时不允许编造同比
        assert kpi["change"] is None
        assert kpi["note"]
    assert set(payload["chart_notes"]) == {"revenue", "expenses", "cash", "cash_note"}
    assert payload["compare"]["available"] is False
    assert "原因" not in payload["compare"] and payload["compare"]["reason"]


async def test_dashboard_without_financial_data(anon_client, auth_headers, db_session):
    from sqlalchemy import delete

    from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction

    for model in (FinancialAlert, FinancialMetric, FinancialTransaction):
        db_session.execute(delete(model))

    response = await anon_client.get("/api/v1/dashboard?period=year", headers=auth_headers)
    payload = response.json()
    assert payload["has_data"] is False
    assert payload["message"]


# ---------------------------------------------------------------- 报告

async def test_monthly_report_renders_markdown(anon_client, auth_headers, db_session):
    period = "2097-06"
    _seed_transactions(db_session, period, [("100", "0", "150")])
    await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)

    response = await anon_client.get(f"/api/v1/reports/monthly?period={period}", headers=auth_headers)
    assert response.status_code == 200
    body = response.text
    assert body.startswith(f"# {period} 财务分析报告")
    assert "## 核心指标" in body and "## 风险预警" in body and "## 口径与限制" in body
    assert "100.00 元" in body
    # 预警行必须带 alert_type（报告里的名字要与看板/预警页一致，否则读者引用不到同一口径）
    assert "- [high] 利润异常：" in body


async def test_monthly_report_without_data(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/reports/monthly?period=2097-12", headers=auth_headers)
    assert response.status_code == 200
    assert "数据不足" in response.text


# ---------------------------------------------------------------- 导入

CSV_HEADER = "交易日期,科目编码,科目名称,部门,摘要,收入金额,成本金额,费用金额,现金流入,现金流出"


def _csv_bytes(*rows: str) -> bytes:
    return ("\n".join([CSV_HEADER, *rows]) + "\n").encode("utf-8-sig")


async def test_financial_template_has_expected_header(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/financial/template", headers=auth_headers)
    assert response.status_code == 200
    # 模板以 utf-8-sig 返回（Excel 打开中文表头不乱码），首行会带 BOM。
    assert response.text.lstrip("\ufeff").splitlines()[0] == CSV_HEADER


async def test_preview_reports_row_errors(anon_client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.api_financial.get_settings",
                        lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    payload = _csv_bytes("2097-07-01,6001,正常行,部门-097,ok,100.005,0,20,0,0",
                         "不是日期,6001,坏行,部门-097,bad,1,0,0,0,0")
    response = await anon_client.post("/api/v1/financial/preview", headers=auth_headers,
                                      files={"file": ("preview.csv", io.BytesIO(payload), "text/csv")})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["valid"] is False
    assert body["error_count"] == 1
    assert "交易日期" in body["errors"][0]["error"]


async def test_import_rounds_amounts_and_rejects_duplicate(anon_client, auth_headers, db_session, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.api_financial.get_settings",
                        lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    payload = _csv_bytes("2097-08-01,6001,导入行,部门-097,ok,100.005,90,20,0,0")

    imported = await anon_client.post("/api/v1/financial/import", headers=auth_headers,
                                      files={"file": ("import.csv", io.BytesIO(payload), "text/csv")})
    assert imported.status_code == 200, imported.text
    body = imported.json()
    assert body["success"] is True and body["imported"] == 1
    # 这一行同时踩到两条规则：成本率 90.00/100.01=0.8999 > 0.8，且净利润 -9.99 < 0。
    assert body["calculated"] == [{"period": "2097-08", "alerts": 2}]

    # 每期同时写入「全公司汇总行(department IS NULL)」和各部门口径行，不加条件 .one() 会炸。
    metric = (db_session.query(FinancialMetric)
              .filter(FinancialMetric.period == "2097-08", FinancialMetric.department.is_(None)).one())
    assert metric.revenue_amount == Decimal("100.01")   # 100.005 半进位
    assert metric.net_profit_amount == Decimal("-9.99")  # 100.01 - 90.00 - 20.00

    duplicate = await anon_client.post("/api/v1/financial/import", headers=auth_headers,
                                       files={"file": ("import.csv", io.BytesIO(payload), "text/csv")})
    assert duplicate.status_code == 409
    assert "已导入" in duplicate.json()["detail"]

    # 上传件只是解析用的临时副本：处理完就从 upload_dir 删掉（否则目录只增不减），
    # 更不会落到只读的 data/raw。
    assert not list(tmp_path.glob("*.csv")), "导入临时件应当用完即删"
    assert not list(Path(get_settings().upload_dir, "documents").glob("*.csv"))


async def test_import_rejects_unsupported_suffix_and_bad_header(anon_client, auth_headers, tmp_path, monkeypatch):
    monkeypatch.setattr("backend.api_financial.get_settings",
                        lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    bad_suffix = await anon_client.post("/api/v1/financial/import", headers=auth_headers,
                                        files={"file": ("data.txt", io.BytesIO(b"x"), "text/plain")})
    assert bad_suffix.status_code == 400

    # 表头缺「科目名称」时必须整份拒绝，而不是导进去半份数据
    bad_header = await anon_client.post(
        "/api/v1/financial/import", headers=auth_headers,
        files={"file": ("bad.csv", io.BytesIO("交易日期,收入金额\n2097-09-01,100\n".encode("utf-8-sig")), "text/csv")})
    assert bad_header.status_code == 422
    assert "缺少字段" in bad_header.json()["detail"]
    # 失败的导入同样不能留下临时件
    assert not list(tmp_path.iterdir()), "解析失败的导入也应清掉临时件"


# ---------------------------------------------------------------- RAG 去重范围

def _insert_chunk(db, source: str, content: str, digest: str, department, stored_path=None):
    from backend.rag.pgvector_store import DocumentChunk

    metadata = {"embedding_model": "pytest-model", "content_hash": digest,
                "department": department}
    if stored_path is not None:
        # 只有 /rag/documents/upload 落盘的上传件才有这个字段（删来源时要靠它清理孤儿文件）
        metadata["stored_path"] = stored_path
    db.add(DocumentChunk(
        content=content, source=source, page=None, document_type="pytest",
        metadata_json=metadata,
        embedding=[0.0] * 384,
    ))
    db.flush()


def test_rag_content_dedup_ignores_source_name_but_respects_department(db_session):
    """内容哈希去重必须跨文件名生效，但不能跨部门可见范围生效。"""
    from backend.api_rag import _existing_hashes

    _insert_chunk(db_session, "财务口径.md", "毛利率 = 毛利 ÷ 收入。", "digest-a", "")
    _insert_chunk(db_session, "部门A/奖金.md", "季度奖金按 1.2 倍发放。", "digest-b", "部门A")

    # 同一份内容换个文件名（甚至换接口）再入库，应视为已存在
    assert "digest-a" in _existing_hashes(db_session, "pytest-model", None)
    assert "digest-b" in _existing_hashes(db_session, "pytest-model", "部门A")
    # 换部门可见范围不算重复：不受限范围里看不到「部门A」专属片段
    assert "digest-b" not in _existing_hashes(db_session, "pytest-model", None)
    assert "digest-a" not in _existing_hashes(db_session, "pytest-model", "部门A")
    # 换向量模型必须重算，旧模型的索引不能复用
    assert _existing_hashes(db_session, "another-model", None) == set()


# ---------------------------------------------------------------- RAG 部门隔离

class _StubChunk:
    def __init__(self, content: str, department):
        self.content = content
        self.source = "pytest-stub"
        self.metadata_json = {"department": department, "embedding_model": "stub"}


class _StubEmbedding:
    model_name = "stub-model"
    using_fallback = False

    def encode(self, texts):
        return [[0.0] * 384 for _ in texts]


@pytest.fixture
def stub_retrieval(monkeypatch):
    chunks = [
        _StubChunk("公共资料：差旅费报销标准为每晚500元。", None),
        _StubChunk("部门A内部资料：季度奖金方案。", "部门A"),
        _StubChunk("部门B内部资料：采购折扣条款。", "部门B"),
    ]
    monkeypatch.setattr("backend.api_rag.search_documents", lambda *args, **kwargs: list(chunks))
    monkeypatch.setattr("backend.api_rag.get_embedding_service", lambda: _StubEmbedding())
    return chunks


async def test_rag_search_filters_by_department(anon_client, api_app, auth_headers, stub_retrieval):
    response = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                      json={"query": "奖金方案", "top_k": 5})
    assert response.status_code == 200, response.text
    assert {item["source"] for item in response.json()["data"]} == {"pytest-stub"}
    contents = [item["content"] for item in response.json()["data"]]
    assert any("公共资料" in item for item in contents)
    assert any("部门A" in item for item in contents)

    # 收窄成部门账号这件事只能由管理员做（⑳），所以这里让夹具的管理员去分配，而不是自己 PATCH
    scoped_headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        scoped_response = await scoped.post("/api/v1/rag/search", headers=scoped_headers,
                                           json={"query": "奖金方案", "top_k": 5})
        contents = [item["content"] for item in scoped_response.json()["data"]]
        assert any("部门A" in item for item in contents)
        assert not any("部门B" in item for item in contents), "不应看到其他部门的资料"

        # 伪造请求头不能提权
        forged = await scoped.post("/api/v1/rag/search",
                                   headers={**scoped_headers, "X-User-Department": "finance-admin"},
                                   json={"query": "奖金方案", "top_k": 5})
        assert not any("部门B" in item["content"] for item in forged.json()["data"])
    finally:
        await scoped.aclose()


async def test_rag_search_gives_unassigned_accounts_only_public_chunks(anon_client, stub_retrieval):
    """⑳：知识库口径也不再认请求头。

    以前「没分配部门」= 空元组，`filter_visible` 把它当不受限放行，加上 `X-User-Department:
    finance-admin` 这条兼容路径，任何新注册的账号带一个请求头就能读到全公司的片段。
    """
    headers = await _plain_headers(anon_client, prefix="rag-plain")
    for extra in ({}, {"X-User-Department": "finance-admin"}):
        response = await anon_client.post("/api/v1/rag/search", headers={**headers, **extra},
                                          json={"query": "奖金方案", "top_k": 5})
        assert response.status_code == 200, response.text
        contents = [item["content"] for item in response.json()["data"]]
        assert any("公共资料" in item for item in contents), "没标部门的公共资料仍然可读"
        assert not any("部门A" in item or "部门B" in item for item in contents), f"没分配部门不该读到部门资料：{extra}"


async def test_rag_search_rejects_mismatched_embedding_model(anon_client, auth_headers, stub_retrieval):
    response = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                      json={"query": "奖金", "embedding_model": "other-model",
                                            "embedding": [0.0] * 384})
    assert response.status_code == 422
    assert "模型标识" in response.json()["detail"]


async def test_agent_stream_requires_token_and_streams_sse(anon_client, auth_headers, stub_retrieval, monkeypatch):
    def fake_stream(question, context, usage=None, history=None):
        yield "依据"
        yield "证据"

    monkeypatch.setattr("backend.api_ai.get_llm", lambda: SimpleNamespace(stream=fake_stream))
    async with anon_client.stream("POST", "/api/v1/ai/agent/chat/stream", headers=auth_headers,
                                  json={"question": "差旅费标准是多少？"}) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])
    assert "event: start" in body
    assert "event: delta" in body
    assert "event: done" in body
    assert "knowledge_chunks" in body


# ---------------------------------------------------------------- PPT 报告

def _seed_metric(db, period: str, *, revenue="1000000.00", cost="0.00", expense="800000.00"):
    from sqlalchemy import delete

    from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction

    db.execute(delete(FinancialAlert).where(FinancialAlert.period == period))
    db.execute(delete(FinancialMetric).where(FinancialMetric.period == period))
    db.execute(delete(FinancialTransaction).where(
        FinancialTransaction.import_batch == f"pytest-{period}"))
    revenue, cost, expense = Decimal(revenue), Decimal(cost), Decimal(expense)
    net = revenue - cost - expense
    db.add(FinancialMetric(
        period=period, revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
        gross_profit_amount=revenue - cost, net_profit_amount=net,
        gross_profit_ratio=Decimal("1.0000"),
        net_profit_ratio=(net / revenue).quantize(Decimal("0.0001")),
    ))
    # 一条费用明细：PPT 的「费用构成（按部门 Top5）」页只有存在可归集明细时才会画图
    db.add(FinancialTransaction(
        transaction_date=datetime(int(period[:4]), int(period[5:7]), 15),
        account_code="pytest", account_name="测试科目", department="数据准备部",
        expense_amount=expense, import_batch=f"pytest-{period}",
    ))
    db.commit()


async def test_pptx_outline_has_five_slides(anon_client, auth_headers, db_session):
    _seed_metric(db_session, "2097-05")
    response = await anon_client.get("/api/v1/reports/pptx/outline?period=2097-05", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["has_data"] is True
    assert payload["filename"] == "财务分析报告-2097-05.pptx"
    titles = [slide["title"] for slide in payload["slides"]]
    assert titles[0].startswith("2097-05")
    assert "核心指标" in titles and "风险预警" in titles
    assert any("费用构成" in title for title in titles)
    assert "口径与限制" in titles
    # 指标页要带上真实数值行，否则前端大纲看不出这一页有什么
    table_slide = next(slide for slide in payload["slides"] if slide["kind"] == "table")
    assert [row[0] for row in table_slide["table"]] == ["收入", "成本", "费用", "净利润", "毛利率", "净利率"]
    assert table_slide["table"][0][1] == "1000000.00 元"
    assert table_slide["table"][2][1] == "800000.00 元"
    assert payload["slides"][0]["subtitle"], "封面页要有副标题"
    # 口径限制必须跟报告一起走，否则「毛利率 100%」会被读成经营结论
    assert any("成本金额、成本率恒为 0" in note for note in payload["notes"])


async def test_docx_report_is_a_real_word_document(anon_client, auth_headers, db_session):
    """Word 报告要能被 python-docx 真打开：标题、指标表、预警表、口径清单都在里面。

    只断言「返回 200 且是 PK 开头」是不够的——那样一个空 zip 也能过。
    """
    docx = pytest.importorskip("docx")
    _seed_metric(db_session, "2097-05")
    # _seed_metric 会清掉该期间已有预警，这里补一条：不补的话「二、风险预警」表不存在，
    # 测试会以为「少一张表」是代码的问题，实际上是种子数据没造出这个分支。
    db_session.add(FinancialAlert(period="2097-05", alert_type="利润异常", description="本期净利润为负",
                                  level="high", metric_name="net_profit_amount",
                                  current_value=Decimal("-800000.00"), threshold_value=Decimal("0")))
    db_session.commit()
    response = await anon_client.get("/api/v1/reports/docx?period=2097-05", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert response.content[:2] == b"PK"
    disposition = response.headers["content-disposition"]
    assert "financial-report-2097-05.docx" in disposition
    assert "filename*=UTF-8''" in disposition  # 中文名走 RFC 5987

    document = docx.Document(io.BytesIO(response.content))
    paragraph_text = "\n".join(item.text for item in document.paragraphs)
    assert "2097-05 财务分析报告" in paragraph_text
    # 正文里的中文必须显式指定东亚字体，否则在只装了中文字体的机器上会走字体替换
    normal = document.styles["Normal"]
    assert normal.font.name == "微软雅黑"

    headings = [item.text for item in document.paragraphs if item.style.name.startswith("Heading")]
    assert "一、核心指标" in headings and "二、风险预警" in headings
    assert any("费用构成" in item for item in headings) and "四、口径与限制" in headings

    tables = document.tables
    assert len(tables) == 3, [table.rows[0].cells[0].text for table in tables]
    metric_table = {(row.cells[0].text, row.cells[1].text) for row in tables[0].rows[1:]}
    assert ("收入", "1000000.00 元") in metric_table
    assert ("费用", "800000.00 元") in metric_table
    assert tables[2].rows[1].cells[0].text == "数据准备部"
    alert_table = {(row.cells[0].text, row.cells[1].text) for row in tables[1].rows[1:]}
    assert ("high", "利润异常") in alert_table

    bullets = [item.text for item in document.paragraphs if item.style.name == "List Bullet"]
    assert any("成本金额、成本率恒为 0" in item for item in bullets)
    assert any("收支差额" in item for item in bullets)


async def test_docx_report_without_data_is_still_a_valid_file(anon_client, auth_headers):
    """没有指标时也要给一份能打开的文档，而不是 500 或者 0 字节。"""
    docx = pytest.importorskip("docx")
    response = await anon_client.get("/api/v1/reports/docx?period=1999-01", headers=auth_headers)
    assert response.status_code == 200, response.text
    document = docx.Document(io.BytesIO(response.content))
    assert "数据不足" in "\n".join(item.text for item in document.paragraphs)
    assert len(document.tables) == 0


async def test_docx_report_marks_department_scope(anon_client, auth_headers, db_session):
    """指定口径的报告必须在正文里写明范围，否则「部门 097 的费用」会被当成公司费用引用出去。"""
    docx = pytest.importorskip("docx")
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    response = await anon_client.get(f"/api/v1/reports/docx?period={SCOPE_PERIOD}&department=部门A",
                                     headers=auth_headers)
    assert response.status_code == 200, response.text
    # 兜底名里不能出现中文部门名（HTTP 头是 latin-1），但英文/数字部分要能看出是哪份报告
    disposition = response.headers["content-disposition"]
    assert f'filename="financial-report-{SCOPE_PERIOD}-A.docx"' in disposition
    assert "filename*=UTF-8''" in disposition
    document = docx.Document(io.BytesIO(response.content))
    assert "数据范围：部门 部门A" in "\n".join(item.text for item in document.paragraphs)


async def test_docx_report_requires_token_and_blocks_cross_department(anon_client, api_app, auth_headers,
                                                                     db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    assert (await anon_client.get(f"/api/v1/reports/docx?period={SCOPE_PERIOD}")).status_code == 401
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        denied = await scoped.get(f"/api/v1/reports/docx?period={SCOPE_PERIOD}&department=部门B",
                                  headers=headers)
        assert denied.status_code == 403
        assert "不能查看" in denied.json()["detail"]
    finally:
        await scoped.aclose()


async def test_pptx_outline_without_data(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/reports/pptx/outline?period=1999-01", headers=auth_headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload["has_data"] is False
    assert len(payload["slides"]) == 1
    assert "数据不足" in payload["slides"][0]["bullets"][0]


async def test_pptx_download_is_a_real_presentation(anon_client, auth_headers, db_session):
    _seed_metric(db_session, "2097-06")
    response = await anon_client.get("/api/v1/reports/pptx?period=2097-06", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.presentationml.presentation")
    assert "financial-report-2097-06.pptx" in response.headers["content-disposition"]
    # 必须是合法 zip（pptx 就是 zip）：不以 PK 开头说明返回的是错误 JSON
    assert response.content[:2] == b"PK"

    # 真用 python-pptx 打开一次，确认不是「长得像 pptx 的字节流」
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation(io.BytesIO(response.content))
    texts = "\n".join(
        shape.text_frame.text
        for slide in presentation.slides
        for shape in slide.shapes if shape.has_text_frame)
    assert "核心指标" in texts
    assert "口径与限制" in texts
    assert "800000.00" in texts, "费用金额应出现在指标页"
    # 图表页是真的原生图表对象，不是截图
    assert any(shape.has_chart for slide in presentation.slides for shape in slide.shapes)


async def test_pptx_download_without_data_still_valid(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/reports/pptx?period=1999-02", headers=auth_headers)
    assert response.status_code == 200
    assert response.content[:2] == b"PK"
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation(io.BytesIO(response.content))
    assert len(presentation.slides) >= 2
    assert presentation.slides[0].shapes.title.text.startswith("1999-02")


# ---------------------------------------------------------------- System 状态

async def test_system_status_never_leaks_secrets(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/system/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert set(payload) >= {"app", "llm", "database", "embedding", "rag", "directories", "data", "me",
                            "config_problems"}
    assert payload["app"]["api_prefix"] == "/api/v1"
    assert payload["database"]["ok"] is True
    # 数据库 URL 里的口令必须打码
    assert ":***@" in payload["database"]["url"]
    assert payload["embedding"]["dimension"] == 384
    assert payload["rag"]["candidate_k"] >= 5
    # 只报告「有没有配」，不回显密钥原文
    body = response.text
    settings = get_settings()
    if settings.effective_api_key:
        assert settings.effective_api_key not in body
    assert settings.auth_secret_key not in body
    assert {item["name"] for item in payload["directories"]} >= {"财务导入上传目录", "知识库上传目录"}
    assert all("writable" in item for item in payload["directories"])
    # 系统设置页按这一份渲染「我的部门」输入框与「当前：」一行，所以必须带上完整部门列表
    assert payload["me"]["departments"] == []
    assert payload["me"]["unrestricted"] is True


async def test_system_status_carries_full_department_list(anon_client, auth_headers):
    """多部门账号在 /system/status 的 me 里必须拿到**全部**部门。

    真机冒烟当场抓到的缺陷：只回单值列 `department` 时，设置页输入框预填成第一个部门，
    页面上「当前：限定 部门A」，用户点一次保存就把自己从两个部门缩成了一个部门
    （⑳ 起设置页不再能保存，但「给全」这件事反而更硬：页面要如实显示管理员分配了什么）。
    """
    credentials = {"username": f"multi_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    created = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert created.status_code == 200, created.text
    token = (await anon_client.post("/api/v1/auth/login", json=credentials)).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    assigned = await anon_client.patch(f"/api/v1/admin/users/{created.json()['id']}",
                                       json={"departments": ["部门A", "部门B"]}, headers=auth_headers)
    assert assigned.status_code == 200, assigned.text

    status = await anon_client.get("/api/v1/system/status", headers=headers)
    assert status.status_code == 200, status.text
    me = status.json()["me"]
    assert me["departments"] == ["部门A", "部门B"]
    assert me["department"] == "部门A"  # 单值列仍回落到第一个，供老文案使用
    assert me["unrestricted"] is False
    # 被分配部门 ≠ 变成管理员：设置页要分别显示这两件事
    assert me["is_admin"] is False


async def test_system_status_requires_token(anon_client):
    assert (await anon_client.get("/api/v1/system/status")).status_code == 401


async def test_system_status_publishes_the_business_timezone(anon_client, auth_headers):
    """业务时区（⑯）要说出去：名字、当前偏移、业务日、当下业务时刻，一个都不能少。"""
    from datetime import date, datetime

    from backend import biztime

    response = await anon_client.get("/api/v1/system/status", headers=auth_headers)
    assert response.status_code == 200, response.text
    info = response.json()["app"]["business_timezone"]
    assert set(info) == {"name", "label", "offset", "now", "today"}
    assert info["name"] == "Asia/Shanghai" == biztime.current_name()
    assert info["offset"] == "UTC+08:00"
    assert info["label"] == "Asia/Shanghai (UTC+08:00)"
    assert info["today"] == date.today().isoformat() == biztime.today().isoformat()
    # now 是带偏移的 ISO 串（±HH:MM 结尾），不是 naive 的
    moment = datetime.fromisoformat(info["now"])
    assert moment.tzinfo is not None and moment.utcoffset().total_seconds() == 8 * 3600
    assert moment.date().isoformat() == info["today"]


async def test_dashboard_updated_at_carries_the_business_offset(anon_client, auth_headers):
    """看板的时间戳以前是 naive 的「服务器本机时刻」，现在必须带业务时区偏移（⑯）。"""
    from datetime import datetime

    response = await anon_client.get("/api/v1/dashboard", headers=auth_headers)
    assert response.status_code == 200, response.text
    moment = datetime.fromisoformat(response.json()["updated_at"])
    assert moment.tzinfo is not None, "updated_at 仍然没有时区"
    assert moment.utcoffset().total_seconds() == 8 * 3600


async def test_export_scope_reports_the_timezone_it_uses(anon_client, auth_headers):
    response = await anon_client.get("/api/v1/export/scope", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["timezone"] == "Asia/Shanghai (UTC+08:00)"
    from datetime import datetime

    assert datetime.fromisoformat(payload["generated_at"]).utcoffset().total_seconds() == 8 * 3600


# ---------------------------------------------------------------- 管理员入口

ADMIN_PATHS = ("/api/v1/admin/overview", "/api/v1/admin/users",
               "/api/v1/admin/batches", "/api/v1/admin/knowledge")


async def test_admin_endpoints_require_token(anon_client):
    for path in ADMIN_PATHS:
        assert (await anon_client.get(path)).status_code == 401, path


async def test_admin_rejects_non_admin_account(anon_client, api_app, auth_headers):
    """⑳：管理接口只看**管理员标识**，不看部门。

    以前「部门为空」就等于管理员，所以这条用例是「部门不为空的账号 403」；现在普通账号
    （哪怕管理员刚给它分了部门）一样是 403 —— 范围收窄与能不能管是两件事，不能互相兑换。
    仍然必须是 403 而不是空数据：否则「看不到」会被误读成「没有」。
    """
    plain = await _plain_headers(anon_client, prefix="scoped")
    for path in ADMIN_PATHS:
        response = await anon_client.get(path, headers=plain)
        assert response.status_code == 403, path
        assert "管理员" in response.json()["detail"]
    assert (await anon_client.delete("/api/v1/admin/knowledge?source=x",
                                     headers=plain)).status_code == 403

    # 被分配了部门的账号也一样：有数据范围不等于有管理权
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        for path in ADMIN_PATHS:
            assert (await scoped.get(path, headers=headers)).status_code == 403, path
    finally:
        await scoped.aclose()


async def test_admin_overview_counts_and_quality_checks(anon_client, auth_headers, db_session):
    period = "2097-06"
    _seed_transactions(db_session, period, [("100", "0", "20")])
    _insert_chunk(db_session, "admin-overview.md", "口径片段", "digest-ov", "")

    response = await anon_client.get("/api/v1/admin/overview", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["counts"]["transactions"] >= 1
    assert payload["counts"]["admins"] >= 1
    assert {"requests", "total", "p95_duration_ms"} <= set(payload["observability"])
    # 按路径耗时：管理概览要能指出「哪个接口慢」，不能只给一个全局 P95。
    # ASGITransport 不经过中间件？这里至少断言结构存在且被这个请求喂过数据。
    observability = payload["observability"]
    assert "paths" in observability and observability["samples_per_path"] > 0
    for path, stat in observability["paths"].items():
        assert set(stat) == {"count", "p95_ms", "max_ms"}, path
        assert stat["count"] >= 1 and stat["max_ms"] >= stat["p95_ms"] >= 0
    # 慢接口告警：管理页要能直接说「哪条路径超过阈值、超了几次」，阈值本身也要下发
    assert {"slow_paths", "slow_default_ms", "slow_overrides", "slow_log_seconds"} <= set(observability)
    assert observability["slow_default_ms"] >= 1
    for row in observability["slow_paths"]:
        assert set(row) == {"path", "threshold_ms", "exceeded", "worst_ms", "last_ms",
                            "last_at", "suppressed"}, row
        assert row["exceeded"] >= 1 and row["worst_ms"] >= row["threshold_ms"]
    # periods 来自 financial_metrics：还没算指标的期间不该出现
    assert period not in payload["periods"]["all"]
    checks = {item["name"]: item for item in payload["quality"]}
    # 有明细但还没算指标 → 必须报出来，而不是显示成一切正常
    assert checks["有明细但缺指标的期间"]["value"] >= 1
    assert checks["有明细但缺指标的期间"]["level"] == "warn"
    assert period in checks["有明细但缺指标的期间"]["hint"]
    assert all(set(item) == {"name", "value", "level", "hint"} for item in payload["quality"])

    # 算完指标后：期间出现在列表里，体检项回到正常
    assert (await anon_client.post(f"/api/v1/analysis/calculate?period={period}",
                                   headers=auth_headers)).status_code == 200
    after = (await anon_client.get("/api/v1/admin/overview", headers=auth_headers)).json()
    assert period in after["periods"]["all"]
    rechecked = {item["name"]: item for item in after["quality"]}
    assert period not in rechecked["有明细但缺指标的期间"]["hint"]
    assert period not in rechecked["有指标但无明细的期间"]["hint"]


async def test_slow_path_alert_is_reported_by_observability_endpoint(anon_client):
    """阈值调低后真发几个请求走一遍中间件：观测接口必须报出「哪条路径超过了阈值」。

    这条用例把「中间件采样 → 阈值判定 → 接口下发」串起来；单测只覆盖了判定本身。
    """
    from backend import observability

    observability.reset()
    observability.set_thresholds(default_ms=1, log_seconds=0)
    try:
        for _ in range(5):
            assert (await anon_client.get("/api/v1/health")).status_code == 200
        payload = (await anon_client.get("/api/v1/observability/metrics")).json()
    finally:
        observability.reset()

    assert payload["slow_default_ms"] == 1
    row = next((item for item in payload["slow_paths"] if item["path"] == "/api/v1/health"), None)
    assert row is not None, payload["paths"]
    assert row["threshold_ms"] == 1 and row["exceeded"] >= 1
    assert row["worst_ms"] >= 1


async def test_admin_user_department_update_and_self_protection(anon_client, auth_headers, credentials):    # credentials 夹具已经通过 auth_headers 注册并登录（该账号部门为空 → 管理员）
    target = {"username": f"managed_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=target)).status_code == 200

    listing = await anon_client.get("/api/v1/admin/users", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    rows = {row["username"]: row for row in listing.json()["data"]}
    assert target["username"] in rows
    me_id = listing.json()["me"]["id"]
    assert rows[credentials["username"]]["is_self"] is True
    assert rows[credentials["username"]]["unrestricted"] is True

    target_id = rows[target["username"]]["id"]
    updated = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                      json={"department": "部门B"}, headers=auth_headers)
    assert updated.status_code == 200, updated.text
    assert updated.json() == {"id": target_id, "username": target["username"],
                              "department": "部门B", "departments": ["部门B"],
                              "is_admin": False, "unrestricted": False}

    # 多部门：一次给两个部门，单值列同步成第一个（老前端与审计文案读它）
    multi = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                    json={"departments": ["部门B", "部门C"]}, headers=auth_headers)
    assert multi.status_code == 200, multi.text
    assert multi.json()["departments"] == ["部门B", "部门C"]
    assert multi.json()["department"] == "部门B"
    assert multi.json()["unrestricted"] is False

    # 列表里能看到两个部门，且按其中一个部门筛选时这个账号不会被漏掉（JSONB 数组成员判断）
    listing = await anon_client.get("/api/v1/admin/users", headers=auth_headers)
    row = {item["username"]: item for item in listing.json()["data"]}[target["username"]]
    assert row["departments"] == ["部门B", "部门C"]
    filtered = await anon_client.get("/api/v1/admin/users?department=部门C", headers=auth_headers)
    assert target["username"] in {item["username"] for item in filtered.json()["data"]}
    by_first = await anon_client.get("/api/v1/admin/users?department=部门B", headers=auth_headers)
    assert target["username"] in {item["username"] for item in by_first.json()["data"]}
    assert "部门C" in by_first.json()["departments"]

    # 只发管理员标识的请求不许碰数据范围：`department` 的字段默认值是空串，照默认值执行就会
    # 把目标账号的部门一起清掉（前端「授予管理员」按钮发的正是这种请求）
    granted = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                      json={"is_admin": True}, headers=auth_headers)
    assert granted.status_code == 200, granted.text
    assert granted.json()["is_admin"] is True
    assert granted.json()["departments"] == ["部门B", "部门C"], granted.text
    assert granted.json()["unrestricted"] is False
    revoked = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                      json={"is_admin": False}, headers=auth_headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["is_admin"] is False
    assert revoked.json()["departments"] == ["部门B", "部门C"], revoked.text

    cleared = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                      json={"departments": []}, headers=auth_headers)
    assert cleared.json()["department"] == ""
    assert cleared.json()["departments"] == []
    # ⑳：清空部门**不再等于**变成管理员，只表示「什么都没分配，什么都看不到」。
    # `unrestricted=False` + 空部门列表就是这个状态，能不能管看的是 is_admin 那一位。
    assert cleared.json()["unrestricted"] is False
    assert cleared.json()["is_admin"] is False

    # 自改部门会把管理员锁在管理入口之外，必须被拒绝
    mine = await anon_client.patch(f"/api/v1/admin/users/{me_id}",
                                   json={"department": "部门C"}, headers=auth_headers)
    assert mine.status_code == 400
    assert "自己" in mine.json()["detail"]
    assert (await anon_client.delete(f"/api/v1/admin/users/{me_id}", headers=auth_headers)).status_code == 400

    deleted = await anon_client.delete(f"/api/v1/admin/users/{target_id}", headers=auth_headers)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert (await anon_client.delete(f"/api/v1/admin/users/{target_id}", headers=auth_headers)).status_code == 404


async def test_admin_batch_delete_recalculates_or_clears_periods(anon_client, auth_headers, db_session):
    period = "2097-08"
    from backend.models import FinancialTransaction

    marker = f"pytest-admin-{uuid.uuid4().hex[:8]}"
    for day, (revenue, cost, expense) in enumerate([("100", "0", "20"), ("200", "0", "30")], start=1):
        db_session.add(FinancialTransaction(
            transaction_date=f"{period}-{day:02d}", account_name="批次科目", account_code="6001",
            department="部门-097", description="pytest 批次", revenue_amount=Decimal(revenue),
            cost_amount=Decimal(cost), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"), import_batch=marker))
    db_session.flush()
    assert (await anon_client.post(f"/api/v1/analysis/calculate?period={period}",
                                   headers=auth_headers)).status_code == 200

    listed = await anon_client.get("/api/v1/admin/batches", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    batches = {item["batch"]: item for item in listed.json()["data"]}
    assert batches[marker]["rows"] == 2
    assert batches[marker]["periods"] == [period]
    assert batches[marker]["first_date"] == f"{period}-01"

    removed = await anon_client.delete(f"/api/v1/admin/batches/{marker}", headers=auth_headers)
    assert removed.status_code == 200, removed.text
    assert removed.json() == {"batch": marker, "deleted_rows": 2, "recalculated": [], "cleared": [period]}
    # 该期间没有剩余明细 → 指标必须一起清掉，不能留下与明细对不上的幽灵报表
    summary = await anon_client.get(f"/api/v1/analysis/summary?period={period}", headers=auth_headers)
    assert summary.json()["has_data"] is False
    assert (await anon_client.delete(f"/api/v1/admin/batches/{marker}", headers=auth_headers)).status_code == 404


async def test_admin_batch_delete_recalculates_when_rows_remain(anon_client, auth_headers, db_session):
    """同一期间还有别的批次时就重算，而不是整期清空。"""
    period = "2097-09"
    from backend.models import FinancialTransaction

    keep = f"pytest-keep-{uuid.uuid4().hex[:8]}"
    drop = f"pytest-drop-{uuid.uuid4().hex[:8]}"
    db_session.add(FinancialTransaction(
        transaction_date=f"{period}-01", account_name="保留科目", account_code="6001",
        department="部门-097", description="保留", revenue_amount=Decimal("100"),
        cost_amount=Decimal("0"), expense_amount=Decimal("10"),
        cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"), import_batch=keep))
    db_session.add(FinancialTransaction(
        transaction_date=f"{period}-02", account_name="撤销科目", account_code="6001",
        department="部门-097", description="待撤销", revenue_amount=Decimal("1000"),
        cost_amount=Decimal("0"), expense_amount=Decimal("500"),
        cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"), import_batch=drop))
    db_session.flush()
    assert (await anon_client.post(f"/api/v1/analysis/calculate?period={period}",
                                   headers=auth_headers)).status_code == 200

    removed = await anon_client.delete(f"/api/v1/admin/batches/{drop}", headers=auth_headers)
    assert removed.status_code == 200, removed.text
    assert removed.json()["recalculated"] == [period]
    assert removed.json()["cleared"] == []
    summary = await anon_client.get(f"/api/v1/analysis/summary?period={period}", headers=auth_headers)
    assert Decimal(str(summary.json()["revenue_amount"])) == Decimal("100.00")


async def test_admin_knowledge_groups_and_deletes_by_source(anon_client, auth_headers, db_session):
    source = f"admin-kb-{uuid.uuid4().hex[:8]}.md"
    _insert_chunk(db_session, source, "第一条片段", "digest-kb-1", "")
    _insert_chunk(db_session, source, "第二条片段", "digest-kb-2", "")

    listed = await anon_client.get("/api/v1/admin/knowledge", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    rows = {item["source"]: item for item in listed.json()["data"]}
    assert rows[source]["chunks"] == 2
    assert rows[source]["embedding_models"] == ["pytest-model"]
    assert "第一条片段" in rows[source]["preview"] or "第二条片段" in rows[source]["preview"]

    deleted = await anon_client.delete(f"/api/v1/admin/knowledge?source={source}", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"source": source, "deleted_chunks": 2, "removed_files": 0}
    again = await anon_client.delete("/api/v1/admin/knowledge?source=" + source, headers=auth_headers)
    assert again.status_code == 404


async def test_admin_knowledge_delete_removes_only_orphan_upload_files(
        anon_client, auth_headers, db_session, tmp_path, monkeypatch):
    """删来源时顺手清理孤儿上传件：还有别的来源引用就不删，引用归零才删，越界路径永不碰。"""
    from sqlalchemy import func, select

    from backend import api_admin
    from backend.rag.pgvector_store import DocumentChunk

    upload_dir = tmp_path / "documents"
    upload_dir.mkdir()
    monkeypatch.setattr(api_admin, "get_settings",
                        lambda: SimpleNamespace(rag_upload_dir=str(upload_dir)))
    shared, lonely, outside = (upload_dir / "shared.pdf", upload_dir / "lonely.pdf",
                               tmp_path / "outside.pdf")
    for path in (shared, lonely, outside):
        path.write_bytes(b"pdf")

    token = uuid.uuid4().hex[:8]
    shared_source, lonely_source, other_source = (f"kb-shared-{token}.md",
                                                  f"kb-lonely-{token}.md",
                                                  f"kb-other-{token}.md")
    # 两份来源共用一份上传件；另一份独占；再塞一条指向上传目录之外的坏路径
    _insert_chunk(db_session, shared_source, "共享片段", f"d-{token}-1", "", str(shared))
    _insert_chunk(db_session, other_source, "共享片段的另一来源", f"d-{token}-2", "", str(shared))
    _insert_chunk(db_session, lonely_source, "独占片段", f"d-{token}-3", "", str(lonely))
    _insert_chunk(db_session, lonely_source, "坏路径片段", f"d-{token}-4", "", str(outside))
    db_session.flush()

    first = await anon_client.delete(f"/api/v1/admin/knowledge?source={shared_source}",
                                     headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["removed_files"] == 0
    assert shared.exists()
    remaining = db_session.scalar(
        select(func.count()).select_from(DocumentChunk)
        .where(DocumentChunk.source == other_source)) or 0
    assert remaining == 1

    second = await anon_client.delete(f"/api/v1/admin/knowledge?source={lonely_source}",
                                      headers=auth_headers)
    assert second.status_code == 200, second.text
    assert second.json() == {"source": lonely_source, "deleted_chunks": 2, "removed_files": 1}
    assert not lonely.exists()
    assert outside.exists()

    third = await anon_client.delete(f"/api/v1/admin/knowledge?source={other_source}",
                                     headers=auth_headers)
    assert third.json()["removed_files"] == 1
    assert not shared.exists()


# ---------------------------------------------------------------- 部门数据隔离

SCOPE_PERIOD = "2097-09"


def _scoped_metric(period: str, department, revenue: str, cost: str, expense: str):
    from backend.models import FinancialMetric

    revenue, cost, expense = Decimal(revenue), Decimal(cost), Decimal(expense)
    net = revenue - cost - expense
    return FinancialMetric(
        period=period, department=department,
        revenue_amount=revenue, cost_amount=cost, expense_amount=expense,
        gross_profit_amount=revenue - cost, net_profit_amount=net,
        gross_profit_ratio=Decimal("1.0000"),
        net_profit_ratio=(net / revenue).quantize(Decimal("0.0001")) if revenue else Decimal("0.0000"),
    )


def _seed_scoped_period(db, period: str):
    """同一期间同时写「全公司汇总行」和两个部门口径行，明细也分属两个部门。

    这正是 calculate() 现在的写法：部门行 + 汇总行并存，所以任何读 metric/alert 的地方
    都必须显式选口径，否则数字会被悄悄加总。
    """
    from sqlalchemy import delete

    from backend.models import FinancialAlert, FinancialMetric, FinancialTransaction

    for model in (FinancialAlert, FinancialMetric):
        db.execute(delete(model).where(model.period == period))
    db.execute(delete(FinancialTransaction).where(FinancialTransaction.account_code == "scope-6001"))

    for department, revenue, expense in ((None, "1000.00", "400.00"),
                                         ("部门A", "300.00", "100.00"),
                                         ("部门B", "700.00", "300.00")):
        db.add(_scoped_metric(period, department, revenue, "0.00", expense))
        db.add(FinancialAlert(period=period, department=department, alert_type="利润异常",
                              description=f"{department or '全公司'}口径测试预警", level="high",
                              metric_name="net_profit_amount", current_value=Decimal("-1.00"),
                              threshold_value=Decimal("0.00")))
    for department, revenue, expense in (("部门A", "300.00", "100.00"), ("部门B", "700.00", "300.00")):
        db.add(FinancialTransaction(
            transaction_date=f"{period}-05", account_name="口径测试科目", account_code="scope-6001",
            department=department, description="pytest 部门隔离种子",
            revenue_amount=Decimal(revenue), cost_amount=Decimal("0.00"), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"), import_batch="pytest-scope",
        ))
    db.commit()


async def _plain_headers(anon_client, prefix: str = "plain") -> dict:
    """注册并登录一个**普通账号**：不是管理员，也没有任何部门。

    用来验「非管理员碰管理接口 = 403」和「没分配范围 = 什么都看不到」。
    别拿 `auth_headers` 当这种账号 —— 那个夹具特意提升了管理员标识。
    """
    credentials = {"username": f"{prefix}_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    created = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert created.status_code == 200, created.text
    token = (await anon_client.post("/api/v1/auth/login", json=credentials)).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


async def _scoped_headers(anon_client, api_app, auth_headers, department: str,
                          departments: list[str] | None = None) -> tuple[dict, httpx.AsyncClient]:
    """注册一个普通账号 → 由**管理员**给它分配部门 → 返回带 token 的**独立**客户端。

    ⑳ 之前这里是「新账号自己 PATCH /auth/me 把部门填上」；那条路已经封了（自我扩权），
    所以连测试也得走管理端分配范围这条真实路径 —— 每次调用顺带又把「范围只能由管理员分配」
    验了一遍。传 `departments` 可以一次分配多个部门。

    注意这个客户端已经发过请求，httpx 不允许再 `async with` 它
    （RuntimeError: Cannot open a client instance more than once），用完 `await aclose()`。
    """
    credentials = {"username": f"scoped_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    created = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    token = (await anon_client.post("/api/v1/auth/login", json=credentials)).json()["access_token"]
    assigned = await anon_client.patch(
        f"/api/v1/admin/users/{user_id}",
        json={"departments": departments if departments is not None else [department]},
        headers=auth_headers)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["unrestricted"] is False, "分配的部门没生效"
    assert assigned.json()["is_admin"] is False, "分配部门不该顺带把人变成管理员"
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app),
                               base_url="http://testserver", trust_env=False)
    headers = {"Authorization": f"Bearer {token}"}
    return headers, client


async def test_scoped_account_reads_only_its_own_department(anon_client, api_app, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        # 全公司口径是 1000，部门A 是 300：读错行会变成 1300（加总）或 1000（没过滤）
        summary = await scoped.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}", headers=headers)
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert Decimal(str(body["revenue_amount"])) == Decimal("300.00")
        assert Decimal(str(body["net_profit_amount"])) == Decimal("200.00")
        assert body["scope"]["unrestricted"] is False
        assert body["scope"]["department"] == "部门A"
        assert body["scope"]["label"] == "部门 部门A"

        alerts = await scoped.get(f"/api/v1/analysis/alerts?period={SCOPE_PERIOD}", headers=headers)
        assert [item["department"] for item in alerts.json()["data"]] == ["部门A"]
        assert all(item["period"] == SCOPE_PERIOD for item in alerts.json()["data"])

        # 看板要跟着同一口径走，否则同一页面两个数字会互相打架
        dashboard = await scoped.get("/api/v1/dashboard?period=year", headers=headers)
        assert dashboard.status_code == 200, dashboard.text
        assert dashboard.json()["scope"]["department"] == "部门A"
        revenue_kpi = next(item for item in dashboard.json()["kpi"] if item["label"] == "营业收入")
        assert revenue_kpi["value"] == "300.00"

        # 报告与 Agent 走的是同一套过滤
        report = await scoped.get(f"/api/v1/reports/monthly?period={SCOPE_PERIOD}", headers=headers)
        assert "> 数据范围：仅统计「部门 部门A」的明细，不是全公司口径。" in report.text
        outline = await scoped.get(f"/api/v1/reports/pptx/outline?period={SCOPE_PERIOD}", headers=headers)
        assert outline.json()["scope"]["department"] == "部门A"
        assert "数据范围：部门 部门A" in outline.json()["slides"][0]["subtitle"]

        # 管理员本人看到的是全公司那一行，两个口径在同一个库里互不影响
        company = await anon_client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}", headers=auth_headers)
        assert Decimal(str(company.json()["revenue_amount"])) == Decimal("1000.00")
        assert company.json()["scope"]["unrestricted"] is True
        assert company.json()["scope"]["label"] == "全公司"
    finally:
        await scoped.aclose()


async def test_scoped_account_cannot_drill_into_other_department_alert(anon_client, api_app, auth_headers, db_session):
    """拿到别人的预警 id 也不能钻取明细：明细接口必须按预警所属口径放行。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    company_alert_id = db_session.scalar(
        select(FinancialAlert.id).where(FinancialAlert.period == SCOPE_PERIOD,
                                        FinancialAlert.department.is_(None)))
    own_alert_id = db_session.scalar(
        select(FinancialAlert.id).where(FinancialAlert.period == SCOPE_PERIOD,
                                        FinancialAlert.department == "部门A"))
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        denied = await scoped.get(f"/api/v1/analysis/alerts/{company_alert_id}/transactions", headers=headers)
        assert denied.status_code == 200, denied.text
        assert denied.json()["data"] == []
        assert "数据范围" in denied.json()["message"]

        allowed = await scoped.get(f"/api/v1/analysis/alerts/{own_alert_id}/transactions", headers=headers)
        assert allowed.status_code == 200, allowed.text
        assert [row["department"] for row in allowed.json()["data"]] == ["部门A"]
    finally:
        await scoped.aclose()


async def test_admin_departments_groups_rows_and_rejects_scoped_account(anon_client, api_app, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    response = await anon_client.get("/api/v1/admin/departments", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    rows = {item["name"]: item for item in payload["departments"]}
    assert "部门A" in rows and "部门B" in rows
    assert rows["部门A"]["metric_rows"] == 1 and rows["部门A"]["alert_rows"] == 1
    assert Decimal(rows["部门A"]["revenue_amount"]) == Decimal("300.00")
    assert rows["部门A"]["transactions"] == 1
    assert rows["部门A"]["periods"] == [SCOPE_PERIOD]
    assert payload["summary"]["departments_with_data"] >= 2
    # 部门列表是按部门名归组的，不能被「未标注部门」之类的兜底值污染
    assert all(item["name"].strip() for item in payload["departments"])

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        assert (await scoped.get("/api/v1/admin/departments", headers=headers)).status_code == 403
    finally:
        await scoped.aclose()


# ---------------------------------------------------------------- 登录限流与占位密钥

async def test_login_is_rate_limited_by_username_and_source(anon_client):
    """连续失败到阈值后返回 429，并带 Retry-After；换一个用户名不受影响。"""
    from backend import ratelimit

    username = f"nobody_{uuid.uuid4().hex[:8]}"
    ratelimit.install_quota("login", 2, 60)
    try:
        for _ in range(2):
            response = await anon_client.post("/api/v1/auth/login",
                                              json={"username": username, "password": "wrong-password-2026"})
            assert response.status_code == 401, response.text
        blocked = await anon_client.post("/api/v1/auth/login",
                                         json={"username": username, "password": "wrong-password-2026"})
        assert blocked.status_code == 429, blocked.text
        assert "重试" in blocked.json()["detail"]
        assert int(blocked.headers["Retry-After"]) >= 1
        # 计数落在库里（㉑）：进程里的状态重启就没了，库里的尝试时间戳不会 ——
        # 而且被拒的那几次**不落库**（拒绝不延长封锁），所以桶里正好是那两次 401。
        hits = ratelimit.snapshot(prefix=f"login:{username}@")
        assert len(hits) == 1 and hits[0]["hits"] == 2, hits
        # 限速是按「用户名 + 来源」分桶的，不能把别人一起锁住
        other = await anon_client.post("/api/v1/auth/login",
                                       json={"username": f"other_{uuid.uuid4().hex[:6]}",
                                             "password": "wrong-password-2026"})
        assert other.status_code == 401
    finally:
        ratelimit.reset_quotas()
    assert ratelimit.quota("login")[0] == get_settings().login_max_attempts


async def test_login_limiter_resets_after_success(anon_client, credentials):  # noqa: ARG001
    """成功后清掉计数：否则正常用户偶尔敲错几次就会被自己的失败次数锁在门外。"""
    from backend import ratelimit

    username = f"reset_{uuid.uuid4().hex[:8]}"
    password = "pytest-login-2026"
    assert (await anon_client.post("/api/v1/auth/register",
                                   json={"username": username, "password": password})).status_code == 200
    ratelimit.install_quota("login", 3, 60)
    try:
        for _ in range(2):
            wrong = await anon_client.post("/api/v1/auth/login",
                                           json={"username": username, "password": "wrong-pass-2026"})
            assert wrong.status_code == 401
        assert ratelimit.tracked_buckets(prefix=f"login:{username}@") == 1
        ok = await anon_client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert ok.status_code == 200, ok.text
        assert ratelimit.tracked_buckets(prefix=f"login:{username}@") == 0
    finally:
        ratelimit.reset_quotas()


def test_placeholder_auth_secret_is_replaced_and_persisted(tmp_path, monkeypatch):
    """占位密钥会被换成随机串，并且**就地**写回 .env（保留其它行、不重复追加键）。"""
    from backend import config

    env = tmp_path / ".env"
    env.write_text("APP_ENV=development\nAUTH_SECRET_KEY=change-this-secret-in-env\nLOG_LEVEL=INFO\n",
                   encoding="utf-8")
    monkeypatch.setattr(config, "env_file_path", lambda: env)
    settings = SimpleNamespace(auth_secret_key=config.DEFAULT_AUTH_SECRET_KEY)

    assert config.provision_auth_secret(settings) is True
    assert settings.auth_secret_key != config.DEFAULT_AUTH_SECRET_KEY
    assert len(settings.auth_secret_key) >= 48
    text = env.read_text(encoding="utf-8")
    assert "AUTH_SECRET_KEY=change-this-secret-in-env" not in text
    assert "APP_ENV=development" in text and "LOG_LEVEL=INFO" in text
    assert text.count("AUTH_SECRET_KEY=") == 1
    # 已经是真密钥时不再改（否则每次启动都会踢掉所有登录态）
    assert config.provision_auth_secret(settings) is False
    assert env.read_text(encoding="utf-8") == text


# ---------------------------------------------------------------- 旧版 .xls 的失败方式

async def test_legacy_xls_returns_actionable_error(anon_client, auth_headers, monkeypatch):
    """没有 xlrd 时 `.xls` 要给「怎么解决」的 400，而不是 500 或者一句无法解析。"""
    import pandas as pd

    from backend.api_financial import LegacyExcelError, read_frame

    def explode(*args, **kwargs):
        raise ImportError("Import xlrd failed. Install xlrd >= 2.0.1 for xls Excel support")

    monkeypatch.setattr(pd, "read_excel", explode)
    with pytest.raises(LegacyExcelError) as info:
        read_frame(io.BytesIO(b"not-really-an-xls"), ".xls")
    assert "xlrd" in str(info.value) and ".xlsx" in str(info.value)

    # 接口层：.xls 走预览也必须落到 400（且带上同样的提示），不能变成 500
    response = await anon_client.post("/api/v1/financial/preview", headers=auth_headers,
                                      files={"file": ("legacy.xls", b"fake", "application/vnd.ms-excel")})
    assert response.status_code == 400, response.text
    assert "xlrd" in response.json()["detail"]


# ---------------------------------------------------------------- 管理员口径切换

async def test_unrestricted_account_can_switch_scope(anon_client, auth_headers, db_session):
    """不受限账号可以临时切到某个部门看，响应用 scope.requested 标明这是主动选择的口径。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    listing = await anon_client.get("/api/v1/analysis/departments", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    assert payload["switchable"] is True
    assert {item["name"] for item in payload["departments"]} >= {"部门A", "部门B"}

    switched = await anon_client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门A",
                                     headers=auth_headers)
    assert switched.status_code == 200, switched.text
    body = switched.json()
    assert Decimal(body["revenue_amount"]) == Decimal("300.00")
    assert body["scope"]["department"] == "部门A"
    assert body["scope"]["requested"] is True
    # scope.unrestricted 描述的是「这次看到的数据是不是全公司口径」，切到部门后它是 False；
    # 「账号本身不受限」要看 /auth/me（前端也用它决定是否显示口径下拉）。
    assert body["scope"]["unrestricted"] is False
    me = await anon_client.get("/api/v1/auth/me", headers=auth_headers)
    assert me.json()["unrestricted"] is True

    # 不传 department 时仍然是全公司口径
    company = await anon_client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}", headers=auth_headers)
    assert Decimal(company.json()["revenue_amount"]) == Decimal("1000.00")
    assert "requested" not in company.json()["scope"]


async def test_scoped_account_cannot_switch_scope(anon_client, api_app, auth_headers, db_session):
    """受限账号填别的部门必须 403（不是静默回落到自己的范围），否则会以为自己在看别人的数。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        listing = await scoped.get("/api/v1/analysis/departments", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["switchable"] is False
        assert [item["name"] for item in listing.json()["departments"]] == ["部门A"]

        for path in (f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门B",
                     "/api/v1/dashboard?period=month&department=部门B",
                     f"/api/v1/reports/monthly?period={SCOPE_PERIOD}&department=部门B",
                     f"/api/v1/reports/pptx/outline?period={SCOPE_PERIOD}&department=部门B",
                     "/api/v1/export/transactions?department=部门B"):
            response = await scoped.get(path, headers=headers)
            assert response.status_code == 403, f"{path} -> {response.status_code}"
            assert "不能查看" in response.json()["detail"]

        # 填自己那个部门是允许的（前端可能把当前口径带在 URL 上）
        own = await scoped.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门A", headers=headers)
        assert own.status_code == 200 and Decimal(own.json()["revenue_amount"]) == Decimal("300.00")
    finally:
        await scoped.aclose()


async def test_multi_department_account_can_narrow_its_own_scope(anon_client, api_app, auth_headers, db_session):
    """绑定多个部门的账号在「自己那几个部门」之间可以换口径，所以下拉要对它开放。

    早先 `switchable` 只看「账号是否不受限」，多部门账号在看板上看不到口径下拉——
    而它恰恰是最需要在两个部门之间来回收窄的那个账号。
    """
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    # 部门由管理员分配（⑳ 起自己没有这个入口），一次给两个
    headers, client = await _scoped_headers(anon_client, api_app, auth_headers, "部门A",
                                            departments=["部门A", "部门B"])
    try:
        listing = await client.get("/api/v1/analysis/departments", headers=headers)
        assert listing.status_code == 200, listing.text
        assert listing.json()["switchable"] is True
        assert {item["name"] for item in listing.json()["departments"]} == {"部门A", "部门B"}

        # 不带参数＝并集（300 + 700），带上自己的一个＝收窄
        both = await client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}", headers=headers)
        assert Decimal(both.json()["revenue_amount"]) == Decimal("1000.00")
        narrowed = await client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门B",
                                    headers=headers)
        assert narrowed.status_code == 200, narrowed.text
        assert Decimal(narrowed.json()["revenue_amount"]) == Decimal("700.00")

        # 收窄仍然只能在范围内
        beyond = await client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门C",
                                  headers=headers)
        assert beyond.status_code == 403
        assert "不能查看" in beyond.json()["detail"]
    finally:
        await client.aclose()


async def test_unassigned_account_sees_nothing_at_all(anon_client, db_session):
    """⑳：没分配部门既不是「不受限」，也不该顺手把口径下拉喂给它。

    这条以前是个真口子：`scope_of` 返回的空元组在 `if scope:` 判断里是假值，于是
    `/analysis/departments` 会当它不受限、把全公司的部门名单和各部明细行数一起端出来。
    """
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    headers = await _plain_headers(anon_client, prefix="unassigned")
    listing = await anon_client.get("/api/v1/analysis/departments", headers=headers)
    assert listing.status_code == 200, listing.text
    assert listing.json()["departments"] == []
    assert listing.json()["switchable"] is False

    summary = await anon_client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}", headers=headers)
    assert summary.status_code == 200, summary.text
    assert Decimal(summary.json()["revenue_amount"]) == Decimal("0.00")
    assert summary.json()["scope"]["unrestricted"] is False

    picked = await anon_client.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门A",
                                   headers=headers)
    assert picked.status_code == 403
    assert "不能查看" in picked.json()["detail"]


# ---------------------------------------------------------------- 数据导出

# 明细导出表头（⑲）：新增「明细 id」与冲销/更正标记四列——界面上看得到的状态，导出必须看得到。
EXPORT_TX_HEADER = ["明细 id", "交易日期", "科目编码", "科目名称", "部门", "摘要", "收入金额", "成本金额",
                    "费用金额", "现金流入", "现金流出", "导入批次", "冲销状态", "原始行 id", "冲销时间",
                    "冲销操作人", "更正次数"]
# 指标导出表头（⑲）：加「粒度 / 包含月份 / 是否完整」，月行与季/年行共用一套列。
EXPORT_METRIC_HEADER = ["期间", "粒度", "口径", "包含月份", "是否完整", "收入金额", "成本金额", "费用金额",
                        "毛利金额", "净利润", "毛利率", "净利率"]
# 导出用例自己的期间（2097 年不属于演示数据的 2026，与其他用例互不打扰）。
EXPORT_TX_PERIOD = "2097-11"


def _csv_rows(content: bytes) -> tuple[list[str], list[dict]]:
    """把导出内容读成「表头 + 按列名取值的行」，断言时不用数第几列。"""
    reader = csv.reader(io.StringIO(content.decode("utf-8-sig")))
    header = next(reader)
    return header, [dict(zip(header, row, strict=True)) for row in reader]


def _amounts(row: dict, *names: str) -> dict:
    """金额列按 Decimal 比：导出写的是原样的 `str(x.amount)`，`0` 与 `0.00` 是同一个数。"""
    return {name: Decimal(row[name]) for name in names}


async def test_export_transactions_csv_and_xlsx(anon_client, auth_headers, db_session):
    """CSV 带 BOM 且列名固定；XLSX 是真表格、数字列写成数字、部门号保持文本。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    csv_response = await anon_client.get(
        f"/api/v1/export/transactions?period={SCOPE_PERIOD}&format=csv", headers=auth_headers)
    assert csv_response.status_code == 200, csv_response.text
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    assert int(csv_response.headers["X-Export-Rows"]) == 2
    text = csv_response.content.decode("utf-8-sig")
    lines = [line for line in text.strip().splitlines() if line]
    assert lines[0].split(",") == EXPORT_TX_HEADER
    assert len(lines) == 3
    assert csv_response.content.startswith(b"\xef\xbb\xbf")

    xlsx_response = await anon_client.get(
        f"/api/v1/export/transactions?period={SCOPE_PERIOD}&format=xlsx", headers=auth_headers)
    assert xlsx_response.status_code == 200
    assert xlsx_response.content[:2] == b"PK"
    assert "spreadsheetml" in xlsx_response.headers["content-type"]
    openpyxl = pytest.importorskip("openpyxl")
    sheet = openpyxl.load_workbook(io.BytesIO(xlsx_response.content)).active
    header = [cell.value for cell in sheet[1]]
    assert header == EXPORT_TX_HEADER
    assert sheet.max_row == 3
    revenue_column = header.index("收入金额") + 1
    revenues = [sheet.cell(row=row, column=revenue_column).value for row in range(2, sheet.max_row + 1)]
    assert all(isinstance(value, (int, float, Decimal)) for value in revenues), revenues
    assert {Decimal(str(value)) for value in revenues} == {Decimal("300.00"), Decimal("700.00")}
    department_column = header.index("部门") + 1
    departments = {sheet.cell(row=row, column=department_column).value for row in range(2, sheet.max_row + 1)}
    assert departments == {"部门A", "部门B"}
    # 明细 id 与更正次数是数字列（Excel 里能直接筛/排），冲销状态是文本
    detail_id_column = header.index("明细 id") + 1
    assert all(isinstance(sheet.cell(row=row, column=detail_id_column).value, (int, float))
               for row in range(2, sheet.max_row + 1))
    assert all(sheet.cell(row=row, column=header.index("冲销状态") + 1).value in (None, "")
               for row in range(2, sheet.max_row + 1)), "没冲销过的行不该有状态"


async def test_export_transactions_marks_reversal_and_correction(anon_client, auth_headers, db_session):
    """冲销行在导出里必须看得出来：一条 -300 的负数行没有解释就是脏数据。

    界面上「已冲销 / 冲销行 / 更正 N 次」是三态（`transactions.reverse()` 不许冲销一条冲销行），
    导出后这几列是线下复核唯一的线索；`明细 id` 也在同一批新增列里，用来对「原始行 id」。
    """
    corrected = _seed_row(db_session, period=EXPORT_TX_PERIOD, day=6, department="部门-097",
                          revenue="100.00", expense="20.00", account_name="导出更正科目")
    reversed_row = _seed_row(db_session, period=EXPORT_TX_PERIOD, day=7, department="部门-097",
                             revenue="300.00", expense="30.00", account_name="导出冲销科目")

    fixed = await anon_client.patch(f"/api/v1/financial/transactions/{corrected.id}", headers=auth_headers,
                                    json={"revenue_amount": "150.00"})
    assert fixed.status_code == 200, fixed.text
    reversal = await anon_client.post(f"/api/v1/financial/transactions/{reversed_row.id}/reverse",
                                      headers=auth_headers, json={"reason": "凭证重复"})
    assert reversal.status_code == 200, reversal.text
    reversal_row = reversal.json()["reversal"]
    actor = (await anon_client.get("/api/v1/auth/me", headers=auth_headers)).json()["username"]

    response = await anon_client.get(f"/api/v1/export/transactions?period={EXPORT_TX_PERIOD}&format=csv",
                                     headers=auth_headers)
    assert response.status_code == 200, response.text
    header, rows = _csv_rows(response.content)
    assert header == EXPORT_TX_HEADER
    by_id = {int(row["明细 id"]): row for row in rows}
    assert set(by_id) == {corrected.id, reversed_row.id, reversal_row["id"]}

    assert by_id[corrected.id]["冲销状态"] == "" and by_id[corrected.id]["更正次数"] == "1"
    assert by_id[corrected.id]["收入金额"] == "150.00"  # 导出的是更正之后的值

    original = by_id[reversed_row.id]
    assert original["冲销状态"] == "已冲销"
    assert original["冲销操作人"] == actor
    # 冲销时间是「什么时候冲的」（业务时区的此刻），不是这条明细的期间
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", original["冲销时间"]), original["冲销时间"]
    assert original["原始行 id"] == "" and original["更正次数"] == "0"

    reversal_out = by_id[reversal_row["id"]]
    assert reversal_out["冲销状态"] == "冲销行"
    assert reversal_out["原始行 id"] == str(reversed_row.id)
    assert reversal_out["收入金额"] == "-300.00" and reversal_out["费用金额"] == "-30.00"
    assert reversal_out["冲销时间"] == "" and reversal_out["冲销操作人"] == ""

    # 更正与冲销都会重算期间，更正次数不会因为导出而变
    again = await anon_client.get(f"/api/v1/export/transactions?period={EXPORT_TX_PERIOD}&format=csv",
                                  headers=auth_headers)
    assert _csv_rows(again.content)[1] == rows


async def test_export_transactions_period_accepts_quarter_and_year(anon_client, auth_headers, db_session):
    """明细导出的 period 与明细列表同语义：月 / 季 / 年都认，而且都用同一套区间比较。"""
    _seed_row(db_session, period="2097-10", day=3, department="部门-097", account_name="十月科目")
    _seed_row(db_session, period="2097-11", day=4, department="部门-097", account_name="十一月科目")

    for period, expected in (("2097-10", {"十月科目"}), ("2097-Q4", {"十月科目", "十一月科目"}),
                             ("2097", {"十月科目", "十一月科目"})):
        response = await anon_client.get(f"/api/v1/export/transactions?period={period}&format=csv",
                                         headers=auth_headers)
        assert response.status_code == 200, response.text
        _, rows = _csv_rows(response.content)
        assert {row["科目名称"] for row in rows} == expected, period
        assert all(row["交易日期"].startswith("2097-1") for row in rows)
    # 文件名也要带上用户实际选的期间，否则导出目录里分不出这一份是哪个季度
    quarter = await anon_client.get("/api/v1/export/transactions?period=2097-Q4&format=csv",
                                    headers=auth_headers)
    assert "transactions-2097-Q4-" in quarter.headers["content-disposition"]

    for bad in ("2097-13", "2097-Q5", "2097-4", "2097-Q"):
        assert (await anon_client.get(f"/api/v1/export/transactions?period={bad}&format=csv",
                                      headers=auth_headers)).status_code == 422, bad


async def test_export_transactions_honours_the_same_filters_as_the_page(anon_client, auth_headers, db_session):
    """明细页筛了批次/关键字再点导出，导出的必须就是屏幕上那一批。

    列表与导出共用 `transactions.apply_filters`：各写一遍的话，导出「多一行少一行」没人看得出来。
    """
    mark = "⑲导出筛选"
    _seed_row(db_session, period=EXPORT_TX_PERIOD, batch="pytest-batch-a",
              account_name=f"{mark}甲", description="甲")
    _seed_row(db_session, period=EXPORT_TX_PERIOD, day=2, batch="pytest-batch-b",
              account_name=f"{mark}乙", description="乙")

    batch = await anon_client.get(f"/api/v1/export/transactions?period={EXPORT_TX_PERIOD}"
                                  "&batch=pytest-batch-a&format=csv", headers=auth_headers)
    assert batch.status_code == 200, batch.text
    _, rows = _csv_rows(batch.content)
    assert [row["导入批次"] for row in rows] == ["pytest-batch-a"]

    keyword = await anon_client.get(f"/api/v1/export/transactions?query={mark}乙&format=csv",
                                    headers=auth_headers)
    assert keyword.status_code == 200, keyword.text
    assert [row["科目名称"] for row in _csv_rows(keyword.content)[1]] == [f"{mark}乙"]

    # 两个筛选叠起来是「且」不是「或」：批次 A 里没有「乙」
    both = await anon_client.get(f"/api/v1/export/transactions?batch=pytest-batch-a&query={mark}乙"
                                 "&format=csv", headers=auth_headers)
    assert both.status_code == 200 and int(both.headers["X-Export-Rows"]) == 0


async def test_export_only_contains_callers_scope(anon_client, api_app, auth_headers, db_session):
    """导出是最容易漏权限的出口：受限账号导出的明细里不能出现别的部门。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    # 季/年口径走另一张表（financial_rollups），所以口径守卫要在这张表上单独验一遍：
    # 落库行不会因为换了张表就自动带上部门过滤。
    rebuild = await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                     json={"periods": ["2097-Q3"]})
    assert rebuild.status_code == 200, rebuild.text
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        response = await scoped.get("/api/v1/export/transactions?format=csv", headers=headers)
        assert response.status_code == 200, response.text
        body = response.content.decode("utf-8-sig")
        assert "部门A" in body and "部门B" not in body
        metrics = await scoped.get("/api/v1/export/metrics?format=csv", headers=headers)
        metric_text = metrics.content.decode("utf-8-sig")
        assert "部门 部门A" in metric_text and "全公司" not in metric_text
        assert "1000.00" not in metric_text  # 全公司口径的收入不能出现在部门导出里

        quarter = await scoped.get("/api/v1/export/metrics?grain=quarter&format=csv", headers=headers)
        assert quarter.status_code == 200, quarter.text
        quarter_header, quarter_rows = _csv_rows(quarter.content)
        assert quarter_header == EXPORT_METRIC_HEADER
        assert {row["口径"] for row in quarter_rows} == {"部门 部门A"}
        assert {row["期间"] for row in quarter_rows} == {"2097-Q3"}
        assert "1000.00" not in quarter.content.decode("utf-8-sig")
    finally:
        await scoped.aclose()


async def test_export_metrics_does_not_add_department_rows_together(anon_client, auth_headers, db_session):
    """管理员导出指标时只能拿到全公司行：两套行混在一起是这套权限模型最危险的失效方式。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    response = await anon_client.get("/api/v1/export/metrics?format=csv", headers=auth_headers)
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    rows = [line for line in text.strip().splitlines() if line][1:]
    matching = [row for row in rows if SCOPE_PERIOD in row]
    assert len(matching) == 1, matching
    assert "全公司" in matching[0] and "1000.00" in matching[0]

    scoped_export = await anon_client.get(
        "/api/v1/export/metrics?format=csv&department=部门B", headers=auth_headers)
    scoped_rows = [line for line in scoped_export.content.decode("utf-8-sig").strip().splitlines() if line][1:]
    scoped_matching = [row for row in scoped_rows if SCOPE_PERIOD in row]
    assert len(scoped_matching) == 1 and "700.00" in scoped_matching[0]


async def test_export_metrics_grain_reads_rollups_and_flags_incomplete_periods(anon_client, auth_headers,
                                                                              db_session):
    """季/年指标导出读的是落库表，并且必须如实写出「这个合计只覆盖哪几个月」。

    `financial_rollups.complete=False` 的期间只有部分月落库——年度合计看起来像全年，
    这是 ⑧ 自己警告过的坑，导出是它最容易骗到人的地方（Excel 里没人会去点开看）。
    """
    _drop_rollups(db_session)
    for month in (*ROLLUP_MONTHS, *INCOMPLETE_MONTHS):
        _seed_company_rows(db_session, month, [("1000.00", "0.00", "200.00")])
    for month in (*ROLLUP_MONTHS, *INCOMPLETE_MONTHS):
        calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={month}", headers=auth_headers)
        assert calculated.status_code == 200, calculated.text

    # 默认 grain=month 与加 grain 之前完全一样，只是多了粒度/包含月份/是否完整三列
    months = await anon_client.get(f"/api/v1/export/metrics?period_from={ROLLUP_MONTHS[0]}"
                                   f"&period_to={ROLLUP_MONTHS[0]}&format=csv", headers=auth_headers)
    assert months.status_code == 200, months.text
    header, rows = _csv_rows(months.content)
    assert header == EXPORT_METRIC_HEADER
    assert len(rows) == 1
    assert {key: rows[0][key] for key in ("期间", "粒度", "口径", "包含月份", "是否完整")} == {
        "期间": ROLLUP_MONTHS[0], "粒度": "月", "口径": "全公司", "包含月份": ROLLUP_MONTHS[0], "是否完整": "是"}
    assert _amounts(rows[0], "收入金额", "成本金额", "费用金额", "毛利金额", "净利润") == {
        "收入金额": Decimal("1000.00"), "成本金额": Decimal("0"), "费用金额": Decimal("200.00"),
        "毛利金额": Decimal("1000.00"), "净利润": Decimal("800.00")}

    quarter = await anon_client.get("/api/v1/export/metrics?grain=quarter&format=csv",
                                    headers=auth_headers)
    assert quarter.status_code == 200, quarter.text
    assert "metrics-quarter-" in quarter.headers["content-disposition"]
    _, quarter_rows = _csv_rows(quarter.content)
    complete = next(row for row in quarter_rows if row["期间"] == ROLLUP_QUARTER)
    assert {key: complete[key] for key in ("粒度", "口径", "包含月份", "是否完整")} == {
        "粒度": "季", "口径": "全公司", "包含月份": "、".join(ROLLUP_MONTHS), "是否完整": "是"}
    assert _amounts(complete, "收入金额", "费用金额", "净利润") == {
        "收入金额": Decimal("3000.00"), "费用金额": Decimal("600.00"), "净利润": Decimal("2400.00")}
    incomplete = next(row for row in quarter_rows if row["期间"] == INCOMPLETE_QUARTER)
    assert incomplete["是否完整"] == "否"
    assert incomplete["包含月份"] == "、".join(INCOMPLETE_MONTHS)
    assert _amounts(incomplete, "收入金额")["收入金额"] == Decimal("2000.00")

    year = await anon_client.get(f"/api/v1/export/metrics?grain=year&period_from={ROLLUP_TEST_YEAR}"
                                 f"&period_to={ROLLUP_TEST_YEAR}&format=xlsx", headers=auth_headers)
    assert year.status_code == 200, year.text
    openpyxl = pytest.importorskip("openpyxl")
    sheet = openpyxl.load_workbook(io.BytesIO(year.content)).active
    assert [cell.value for cell in sheet[1]] == EXPORT_METRIC_HEADER
    year_row = {name: sheet.cell(row=2, column=index).value
                for index, name in enumerate(EXPORT_METRIC_HEADER, start=1)}
    # 年度期间串是纯数字（"2098"），按 `_cell` 的既有规则在 XLSX 里写成数字而不是文本
    assert int(year_row["期间"]) == int(ROLLUP_TEST_YEAR) and year_row["粒度"] == "年"
    assert year_row["包含月份"] == "、".join((*ROLLUP_MONTHS, *INCOMPLETE_MONTHS))
    assert year_row["是否完整"] == "否" and year_row["收入金额"] == 5000
    assert sheet.max_row == 2, "只导这一年，不该把别的期间的落库行带上"

    # grain 与区间端点必须同粒度，且端点顺序要挡：把 "2026-07" 拿去比落库表只会一行都匹配不上
    assert (await anon_client.get("/api/v1/export/metrics?grain=week", headers=auth_headers)).status_code == 422
    bad_grain = await anon_client.get("/api/v1/export/metrics?grain=week", headers=auth_headers)
    assert "grain 只支持" in bad_grain.json()["detail"]
    mixed = await anon_client.get(f"/api/v1/export/metrics?grain=quarter&period_from={ROLLUP_MONTHS[0]}",
                                  headers=auth_headers)
    assert mixed.status_code == 422 and "同粒度" in mixed.json()["detail"]
    assert "2026-Q3" in mixed.json()["detail"]
    backwards = await anon_client.get("/api/v1/export/metrics?grain=quarter&period_from=2098-Q4"
                                      "&period_to=2098-Q2", headers=auth_headers)
    assert backwards.status_code == 422 and "不能晚于" in backwards.json()["detail"]
    none = await anon_client.get("/api/v1/export/metrics?grain=quarter&period_from=2099-Q1"
                                 "&period_to=2099-Q2&format=csv", headers=auth_headers)
    assert none.status_code == 200 and int(none.headers["X-Export-Rows"]) == 0
    assert "X-Export-Truncated" not in none.headers


async def test_export_metrics_reports_truncation_instead_of_always_false(anon_client, auth_headers, db_session,
                                                                        monkeypatch):
    """指标导出原来把 `truncated` 写死成 False（连上限都没有）：导了半张表却告诉用户「没问题」。

    真跑到 20000 行太慢，这里把上限压成 1 行——验的是「按上限截断 + 如实上报 + 辅助接口报同一个上限」这条链路。
    """
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    monkeypatch.setattr("backend.api_export.EXPORT_LIMIT", 1)

    cut = await anon_client.get("/api/v1/export/metrics?format=csv", headers=auth_headers)
    assert cut.status_code == 200, cut.text
    assert cut.headers["X-Export-Rows"] == "1"
    assert cut.headers["X-Export-Truncated"] == "true"
    assert len(_csv_rows(cut.content)[1]) == 1
    # 辅助接口报的上限要与真正生效的一致，否则「为什么只给我一行」无从解释
    helper = await anon_client.get("/api/v1/export/scope", headers=auth_headers)
    assert helper.json()["limit"] == 1

    # 没被截断时**不带**这个头：前端据「有没有这个头」提示，而不是去读它的值
    monkeypatch.setattr("backend.api_export.EXPORT_LIMIT", 100)
    whole = await anon_client.get("/api/v1/export/metrics?format=csv", headers=auth_headers)
    assert whole.status_code == 200
    assert "X-Export-Truncated" not in whole.headers
    assert int(whole.headers["X-Export-Rows"]) == len(_csv_rows(whole.content)[1]) > 0


async def test_export_alerts_and_scope_helper_require_token(anon_client, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    alerts = await anon_client.get(f"/api/v1/export/alerts?period={SCOPE_PERIOD}&format=csv",
                                   headers=auth_headers)
    assert alerts.status_code == 200
    text = alerts.content.decode("utf-8-sig")
    assert text.startswith("期间,口径,预警类型")
    assert "需人工复核" in text.splitlines()[0]
    assert "全公司口径测试预警" in text and "部门B口径测试预警" not in text
    # 预警行本身就是月口径（`financial_alerts.period` 存 "2026-10"），季/年不该被安静地当成空结果
    quarter = await anon_client.get("/api/v1/export/alerts?period=2097-Q3&format=csv", headers=auth_headers)
    assert quarter.status_code == 422

    helper = await anon_client.get("/api/v1/export/scope", headers=auth_headers)
    assert helper.status_code == 200
    assert helper.json()["formats"] == ["csv", "xlsx"]
    assert helper.json()["scope"]["unrestricted"] is True

    for path in ("/api/v1/export/transactions", "/api/v1/export/metrics", "/api/v1/export/alerts",
                 "/api/v1/export/scope"):
        assert (await anon_client.get(path)).status_code == 401, path

    bad_format = await anon_client.get("/api/v1/export/transactions?format=pdf", headers=auth_headers)
    assert bad_format.status_code == 422


# ---------------------------------------------------------------- 预警处理闭环

REVIEW_PERIOD = "2097-06"
RULE_PERIOD = "2097-07"


async def _company_alert(client, headers, period: str) -> dict:
    """取该期间的全公司口径预警（`department` 为空的那条）。

    种子明细的部门是「部门-097」，`calculate()` 会另外生成部门口径的预警行，
    断言处理状态时必须挑准口径，否则会拿到部门行。
    """
    response = await client.get(f"/api/v1/analysis/alerts?period={period}", headers=headers)
    assert response.status_code == 200, response.text
    items = [item for item in response.json()["data"] if not item["department"]]
    assert items, response.text
    return items[0]


async def test_alert_review_roundtrip(anon_client, auth_headers, credentials, db_session):
    """确认/备注能写进库并读回来；原来 `needs_human_review` 建行时是 True 之后就再也没人改过。"""
    _seed_transactions(db_session, REVIEW_PERIOD, [("100", "0", "150")])
    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={REVIEW_PERIOD}",
                                        headers=auth_headers)
    assert calculated.json()["alerts"] == 1

    before = await anon_client.get(f"/api/v1/analysis/alerts/stats?period={REVIEW_PERIOD}",
                                   headers=auth_headers)
    assert before.json()["counts"]["pending"] == 1
    assert before.json()["counts"]["confirmed"] == 0
    assert before.json()["counts"]["total"] == 1

    alert = await _company_alert(anon_client, auth_headers, REVIEW_PERIOD)
    assert alert["status"] == "pending" and alert["status_label"] == "待处理"
    assert alert["needs_human_review"] is True

    patched = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                      json={"status": "confirmed", "note": "已核对凭证原件"},
                                      headers=auth_headers)
    assert patched.status_code == 200, patched.text
    payload = patched.json()
    assert payload["status"] == "confirmed" and payload["status_label"] == "已确认"
    assert payload["note"] == "已核对凭证原件"
    assert payload["reviewed_by"] == credentials["username"]
    assert payload["reviewed_at"]
    assert payload["needs_human_review"] is False

    # 列表与明细两处都要能看到同一个状态：列表用 _alert_json，明细接口以前另写了一份字典
    listed = await _company_alert(anon_client, auth_headers, REVIEW_PERIOD)
    assert listed["status"] == "confirmed" and listed["note"] == "已核对凭证原件"
    detail = await anon_client.get(f"/api/v1/analysis/alerts/{alert['id']}/transactions",
                                   headers=auth_headers)
    assert detail.json()["alert"]["status"] == "confirmed"
    assert detail.json()["alert"]["reviewed_by"] == credentials["username"]

    after = await anon_client.get(f"/api/v1/analysis/alerts/stats?period={REVIEW_PERIOD}",
                                  headers=auth_headers)
    assert after.json()["counts"] == {"pending": 0, "confirmed": 1, "false_positive": 0,
                                      "resolved": 0, "total": 1}

    # 退回待处理要清掉处理人与时间，否则界面上会出现「待处理 + 张三已处理」的矛盾状态
    # （序列化把 None 统一成空串，前端只判断真假，不必再分 null 与 ""）
    reverted = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                       json={"status": "pending"}, headers=auth_headers)
    assert reverted.json()["reviewed_by"] == ""
    assert reverted.json()["reviewed_at"] == ""
    assert reverted.json()["needs_human_review"] is True


async def test_alert_review_rejects_bad_status_and_noteless_false_positive(anon_client, auth_headers, db_session):
    _seed_transactions(db_session, REVIEW_PERIOD, [("100", "0", "150")])
    await anon_client.post(f"/api/v1/analysis/calculate?period={REVIEW_PERIOD}", headers=auth_headers)
    alert = await _company_alert(anon_client, auth_headers, REVIEW_PERIOD)

    bad_status = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                         json={"status": "ignore"}, headers=auth_headers)
    assert bad_status.status_code == 422
    assert "状态只能是" in bad_status.json()["detail"]

    # 判成误报必须写理由：否则「为什么不是问题」这件事就丢了，没人能复核
    blank = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                    json={"status": "false_positive", "note": "   "},
                                    headers=auth_headers)
    assert blank.status_code == 422
    assert blank.json()["detail"] == "标记为误报时必须填写原因"

    note_ok = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                      json={"status": "false_positive", "note": "年末冲销，次月已冲回"},
                                      headers=auth_headers)
    assert note_ok.status_code == 200
    assert note_ok.json()["note"] == "年末冲销，次月已冲回"

    missing = await anon_client.patch("/api/v1/analysis/alerts/999999",
                                      json={"status": "confirmed"}, headers=auth_headers)
    assert missing.status_code == 404


async def test_alert_review_survives_recalculation(anon_client, auth_headers, db_session):
    """`calculate()` 是「先删再建」，处理状态必须被继承下来，否则导入/撤销批次会把人工结论静默抹掉。"""
    _seed_transactions(db_session, REVIEW_PERIOD, [("100", "0", "150")])
    await anon_client.post(f"/api/v1/analysis/calculate?period={REVIEW_PERIOD}", headers=auth_headers)
    alert = await _company_alert(anon_client, auth_headers, REVIEW_PERIOD)
    await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                            json={"status": "resolved", "note": "已按凭证调整预算"},
                            headers=auth_headers)

    again = await anon_client.post(f"/api/v1/analysis/calculate?period={REVIEW_PERIOD}",
                                   headers=auth_headers)
    assert again.json()["alerts"] == 1

    rebuilt = await _company_alert(anon_client, auth_headers, REVIEW_PERIOD)
    assert rebuilt["id"] != alert["id"]  # 行是新建的，状态是继承的
    assert rebuilt["status"] == "resolved"
    assert rebuilt["note"] == "已按凭证调整预算"
    assert rebuilt["needs_human_review"] is False

    # 备注在、状态在，但把状态改回 pending 之后 needs_human_review 也要跟着回到 True
    back = await anon_client.patch(f"/api/v1/analysis/alerts/{rebuilt['id']}",
                                   json={"status": "pending", "note": "重新核查"}, headers=auth_headers)
    assert back.json()["needs_human_review"] is True


async def test_scoped_account_cannot_review_another_scope_alert(anon_client, api_app, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    company = await anon_client.get(f"/api/v1/analysis/alerts?period={SCOPE_PERIOD}", headers=auth_headers)
    company_alert = [item for item in company.json()["data"] if not item["department"]][0]

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        other = await scoped.patch(f"/api/v1/analysis/alerts/{company_alert['id']}",
                                   json={"status": "confirmed"}, headers=headers)
        assert other.status_code == 403
        assert "不能处理" in other.json()["detail"]

        # 自己部门那条可以处理
        own = await scoped.get(f"/api/v1/analysis/alerts?period={SCOPE_PERIOD}", headers=headers)
        own_alert = own.json()["data"][0]
        assert own_alert["department"] == "部门A"
        ok = await scoped.patch(f"/api/v1/analysis/alerts/{own_alert['id']}",
                                json={"status": "confirmed"}, headers=headers)
        assert ok.status_code == 200 and ok.json()["status"] == "confirmed"

        stats = await scoped.get(f"/api/v1/analysis/alerts/stats?period={SCOPE_PERIOD}", headers=headers)
        assert stats.json()["counts"]["total"] == 1  # 只统计自己口径
    finally:
        await scoped.aclose()
    # 全公司那条不受影响
    assert (await _company_alert(anon_client, auth_headers, SCOPE_PERIOD))["status"] == "pending"


async def test_admin_alert_rules_are_editable_and_used_by_recalculation(anon_client, auth_headers, db_session):
    rules = await anon_client.get("/api/v1/admin/alert-rules", headers=auth_headers)
    assert rules.status_code == 200, rules.text
    listed = {rule["alert_type"]: rule for rule in rules.json()["data"]}
    assert set(listed) == {"成本率异常", "利润异常"}
    assert rules.json()["comparators"] == {"gt": "大于", "lt": "小于"}
    assert rules.json()["metrics"]["cost_ratio"] == "成本率（成本 ÷ 收入）"
    assert listed["成本率异常"]["threshold_value"] == "0.8000"
    assert listed["成本率异常"]["comparator_label"] == "大于"
    assert listed["利润异常"]["comparator_label"] == "小于"

    unknown = await anon_client.put("/api/v1/admin/alert-rules/不存在的规则",
                                    json={"threshold_value": "1"}, headers=auth_headers)
    assert unknown.status_code == 404

    too_big = await anon_client.put("/api/v1/admin/alert-rules/成本率异常",
                                    json={"threshold_value": "10000000000000000"}, headers=auth_headers)
    assert too_big.status_code == 422
    assert too_big.json()["detail"] == "阈值超出可表示范围"

    invalid_level = await anon_client.put("/api/v1/admin/alert-rules/成本率异常",
                                          json={"level": "urgent"}, headers=auth_headers)
    assert invalid_level.status_code == 422

    # 收入 1000、成本 600 → 成本率 0.6：默认阈值 0.8 不触发；把阈值调到 0.5 后重算就该触发；
    # 关掉「利润异常」后，即使净利润为负也不再产生那条预警。
    _seed_transactions(db_session, RULE_PERIOD, [("1000", "600", "0")])
    first = await anon_client.post(f"/api/v1/analysis/calculate?period={RULE_PERIOD}", headers=auth_headers)
    assert first.json()["alerts"] == 0

    lowered = await anon_client.put("/api/v1/admin/alert-rules/成本率异常",
                                    json={"threshold_value": "0.5"}, headers=auth_headers)
    assert lowered.status_code == 200
    assert lowered.json()["threshold_value"] == "0.5000"

    second = await anon_client.post(f"/api/v1/analysis/calculate?period={RULE_PERIOD}", headers=auth_headers)
    assert second.json()["alerts"] == 1
    triggered = await _company_alert(anon_client, auth_headers, RULE_PERIOD)
    assert triggered["type"] == "成本率异常" and triggered["level"] == "high"
    # 预警上存的是当时生效的阈值（Numeric(18,2) → "0.50"），所以报告里能解释「为什么这条会亮」
    assert Decimal(str(triggered["threshold_value"])) == Decimal("0.5")

    disabled = await anon_client.put("/api/v1/admin/alert-rules/利润异常",
                                     json={"enabled": False, "level": "low"}, headers=auth_headers)
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False
    assert disabled.json()["level"] == "low"

    _seed_transactions(db_session, "2097-08", [("100", "0", "150")])
    off = await anon_client.post("/api/v1/analysis/calculate?period=2097-08", headers=auth_headers)
    assert off.json()["alerts"] == 0  # 净利润为负但规则被关掉了

    on_again = await anon_client.put("/api/v1/admin/alert-rules/利润异常",
                                     json={"enabled": True}, headers=auth_headers)
    assert on_again.json()["enabled"] is True
    back = await anon_client.post("/api/v1/analysis/calculate?period=2097-08", headers=auth_headers)
    assert back.json()["alerts"] == 1
    restored = await _company_alert(anon_client, auth_headers, "2097-08")
    assert restored["type"] == "利润异常" and restored["level"] == "low"  # 级别也跟着规则走


async def test_alert_rules_require_admin(anon_client, api_app, auth_headers):
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        listed = await scoped.get("/api/v1/admin/alert-rules", headers=headers)
        assert listed.status_code == 403
        assert "需要管理员权限" in listed.json()["detail"]
        updated = await scoped.put("/api/v1/admin/alert-rules/利润异常",
                                   json={"enabled": False}, headers=headers)
        assert updated.status_code == 403
    finally:
        await scoped.aclose()
    assert (await anon_client.get("/api/v1/admin/alert-rules")).status_code == 401


TIER_PERIOD = "2097-11"


def _seed_department_rows(db, period: str, rows):
    """按 (部门, 收入, 成本, 费用) 播种子——分档要两个部门各一本账，而 `_seed_transactions`
    把部门写死成「部门-097」，没法用来验证「部门A 的规则不影响部门B」。"""
    from backend.models import FinancialTransaction

    for index, (department, revenue, cost, expense) in enumerate(rows):
        db.add(FinancialTransaction(
            transaction_date=f"{period}-{index + 1:02d}",
            account_name="测试科目", account_code="6001", department=department,
            description="pytest 分档种子", revenue_amount=Decimal(revenue),
            cost_amount=Decimal(cost), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"),
            import_batch="pytest-seed"))
    db.flush()


async def _alerts_in_scope(client, headers, period: str, department: str | None) -> list[dict]:
    response = await client.get(f"/api/v1/analysis/alerts?period={period}", headers=headers)
    assert response.status_code == 200, response.text
    return [item for item in response.json()["data"]
            if (item["department"] or "") == (department or "")]


async def test_scoped_alert_rule_overrides_default_only_in_its_scope(anon_client, api_app, auth_headers, db_session):
    """分档规则是「覆盖」：部门A + 该期间取最具体的那条，其它部门/期间仍走默认或更宽的规则。

    构造：部门A 成本率 0.9（默认 0.8 会报）、部门B 成本率 0.1（默认不报）、
    全公司 0.833（部门为空那笔金额够大，把合计率拉过 0.8，默认规则同样会报）。

    部门级结果一律用**受限账号**去读：`scope_alert` 对不受限账号只放行 `department IS NULL`
    的全公司行（否则每个部门各来一条，看板上同一件事会重复出现），所以管理员账号看不到部门行。
    """
    _seed_department_rows(db_session, TIER_PERIOD, [
        ("部门A", "1000", "900", "0"),
        ("部门B", "1000", "100", "0"),
        (None, "10000", "9000", "0"),
    ])
    headers_a, client_a = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    headers_b, client_b = await _scoped_headers(anon_client, api_app, auth_headers, "部门B")
    first = await anon_client.post(f"/api/v1/analysis/calculate?period={TIER_PERIOD}", headers=auth_headers)
    assert first.status_code == 200, first.text
    # 默认规则下：部门A（0.9）报、部门B（0.1）不报、全公司（0.833）报
    assert (await _alerts_in_scope(client_a, headers_a, TIER_PERIOD, "部门A"))[0]["type"] == "成本率异常"
    assert await _alerts_in_scope(client_b, headers_b, TIER_PERIOD, "部门B") == []
    assert (await _alerts_in_scope(anon_client, auth_headers, TIER_PERIOD, None))[0]["threshold_value"] == "0.80"

    created = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "成本率异常", "department": "部门A", "period": TIER_PERIOD,
        "metric_name": "cost_ratio", "comparator": "gt", "threshold_value": "0.95",
        "level": "medium", "description": "部门A 单独从严"})
    assert created.status_code == 201, created.text
    strict = created.json()
    assert strict["is_default"] is False
    assert strict["scope_label"] == f"部门A · {TIER_PERIOD}"
    assert strict["threshold_value"] == "0.9500"

    # 同范围的第二条：前端重复提交会得到明确冲突，而不是库里多出一条谁生效看 id 的规则
    duplicate = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "成本率异常", "department": "部门A", "period": TIER_PERIOD,
        "metric_name": "cost_ratio", "threshold_value": "0.99"})
    assert duplicate.status_code == 409
    assert "已有同样范围" in duplicate.json()["detail"]

    # scope 全空的分档规则不允许建：它会和默认规则抢同一个类型
    unscoped = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "毛利率异常", "metric_name": "gross_profit_ratio", "threshold_value": "0.1"})
    assert unscoped.status_code == 422
    assert "至少要限定部门或期间" in unscoped.json()["detail"]

    bad_metric = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "成本率异常", "period": TIER_PERIOD,
        "metric_name": "不存在的指标", "threshold_value": "0.5"})
    assert bad_metric.status_code == 422
    bad_period = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "成本率异常", "period": "2097-13",
        "metric_name": "cost_ratio", "threshold_value": "0.5"})
    assert bad_period.status_code == 422

    # 再来一条只限定期间的（更宽）：部门B 与全公司会用它，部门A 仍然用更具体的那条
    wider = await anon_client.post("/api/v1/admin/alert-rules", headers=auth_headers, json={
        "alert_type": "成本率异常", "period": TIER_PERIOD, "metric_name": "cost_ratio",
        "threshold_value": "0.05", "level": "low", "description": "本期整体收紧"})
    assert wider.status_code == 201, wider.text

    second = await anon_client.post(f"/api/v1/analysis/calculate?period={TIER_PERIOD}", headers=auth_headers)
    assert second.status_code == 200
    # 部门A 用更具体的 0.95：0.9 不再报；部门B 与全公司用期间级的 0.05，都报
    assert await _alerts_in_scope(client_a, headers_a, TIER_PERIOD, "部门A") == []
    department_b = await _alerts_in_scope(client_b, headers_b, TIER_PERIOD, "部门B")
    assert [item["threshold_value"] for item in department_b] == ["0.05"]
    assert department_b[0]["level"] == "low"
    company = await _alerts_in_scope(anon_client, auth_headers, TIER_PERIOD, None)
    assert [item["threshold_value"] for item in company] == ["0.05"]

    # 按 id 改分档阈值（走的是新端点，默认规则的 PUT 不受影响）
    relaxed = await anon_client.put(f"/api/v1/admin/alert-rules/id/{strict['id']}",
                                    headers=auth_headers, json={"threshold_value": "0.85"})
    assert relaxed.status_code == 200, relaxed.text
    assert relaxed.json()["threshold_value"] == "0.8500"
    third = await anon_client.post(f"/api/v1/analysis/calculate?period={TIER_PERIOD}", headers=auth_headers)
    assert third.status_code == 200
    assert [item["threshold_value"] for item in
            await _alerts_in_scope(client_a, headers_a, TIER_PERIOD, "部门A")] == ["0.85"]

    # 不能把分档规则改成全公司口径（那会变成第二条默认规则）
    globalize = await anon_client.put(f"/api/v1/admin/alert-rules/id/{strict['id']}",
                                      headers=auth_headers, json={"department": "", "period": ""})
    assert globalize.status_code == 422
    assert "至少要限定部门或期间" in globalize.json()["detail"]
    missing = await anon_client.put("/api/v1/admin/alert-rules/id/999999", headers=auth_headers,
                                    json={"threshold_value": "0.5"})
    assert missing.status_code == 404

    # 默认规则只允许停用不允许删；分档规则可以删
    listed = await anon_client.get("/api/v1/admin/alert-rules", headers=auth_headers)
    defaults = [rule for rule in listed.json()["data"] if rule["is_default"]]
    assert defaults and all(rule["scope_label"] == "全公司 · 所有期间" for rule in defaults)
    kept = await anon_client.delete(f"/api/v1/admin/alert-rules/id/{defaults[0]['id']}", headers=auth_headers)
    assert kept.status_code == 400
    assert "不能删除" in kept.json()["detail"]

    removed = await anon_client.delete(f"/api/v1/admin/alert-rules/id/{strict['id']}", headers=auth_headers)
    assert removed.status_code == 200 and removed.json()["deleted"] is True
    assert (await anon_client.delete(f"/api/v1/admin/alert-rules/id/{strict['id']}",
                                     headers=auth_headers)).status_code == 404
    # 删掉最具体的那条后，部门A 回到「期间规则」（0.05）
    fourth = await anon_client.post(f"/api/v1/analysis/calculate?period={TIER_PERIOD}", headers=auth_headers)
    assert fourth.status_code == 200
    assert [item["threshold_value"] for item in
            await _alerts_in_scope(client_a, headers_a, TIER_PERIOD, "部门A")] == ["0.05"]

    # 分档规则的建/改/删都要留痕，否则管理页改完没人知道谁改的
    audit = await anon_client.get("/api/v1/admin/audit?action=alert_rule.create", headers=auth_headers)
    assert audit.status_code == 200
    assert len(audit.json()["data"]) >= 2
    detail = audit.json()["data"][0]["detail"]
    assert detail["scope"] and "alert_type" in detail and "after" in detail
    deleted = await anon_client.get("/api/v1/admin/audit?action=alert_rule.delete", headers=auth_headers)
    assert deleted.json()["data"][0]["detail"]["before"]["threshold_value"] == "0.8500"

    # 列表接口给出分档可选的部门/期间与级别（页面上的下拉不让人手打，打错字规则永远匹配不上）
    options = listed.json()
    assert options["levels"] == {"high": "高", "medium": "中", "low": "低"}
    assert "部门A" in options["departments"] and TIER_PERIOD in options["periods"]

    await client_a.aclose()
    await client_b.aclose()


# ---------------------------------------------------------------- 审计日志

AUDIT_PERIOD = "2097-08"


async def _audit(client, headers, **params) -> dict:
    query = "&".join(f"{key}={value}" for key, value in params.items() if value not in (None, ""))
    response = await client.get("/api/v1/admin/audit" + (f"?{query}" if query else ""), headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _find(rows: list[dict], action: str, target_id=None) -> dict | None:
    for row in rows:
        if row["action"] != action:
            continue
        if target_id is not None and row["target_id"] != str(target_id):
            continue
        return row
    return None


async def test_audit_log_records_register_login_and_failure(anon_client, credentials, auth_headers):
    """注册/登录/登录失败都要留痕，且**不允许**把密码写进日志。

    `auth_headers` 会先注册并登录一次，所以这里能同时看到 register 与 login 两条。
    """
    failed = await anon_client.post("/api/v1/auth/login",
                                    json={"username": credentials["username"], "password": "wrong-password-2026"})
    assert failed.status_code == 401

    payload = await _audit(anon_client, auth_headers)
    texts = str(payload["data"])
    assert credentials["password"] not in texts
    assert "wrong-password-2026" not in texts

    actions = {row["action"] for row in payload["data"]}
    assert {"user.register", "user.login", "user.login_failed"} <= actions
    failed_row = _find(payload["data"], "user.login_failed")
    assert failed_row["actor"] == credentials["username"]
    assert failed_row["action_label"] == "登录失败"
    assert "reason" in failed_row["detail"]


async def test_audit_log_records_admin_and_data_writes(anon_client, auth_headers, credentials, db_session):
    """七类写操作各写一条：导入/删账号/撤销批次/删知识库/改规则/处理预警/导出。"""
    from backend.models import FinancialTransaction, User
    from backend.rag.pgvector_store import DocumentChunk  # chunk 表在 RAG 模块里，不在 models.py

    # 1) 改预警规则（旧值/新值都要在 detail 里）
    rule = await anon_client.put("/api/v1/admin/alert-rules/利润异常",
                                 json={"threshold_value": "-1", "level": "medium"}, headers=auth_headers)
    assert rule.status_code == 200, rule.text

    # 2) 改他人部门 + 3) 删账号（另外造一个账号，避免动到自己）
    victim = {"username": f"audit_{uuid.uuid4().hex[:8]}", "password": "audit-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=victim)).status_code == 200
    victim_id = db_session.scalar(select(User.id).where(User.username == victim["username"]))
    changed = await anon_client.patch(f"/api/v1/admin/users/{victim_id}",
                                      json={"department": "审计测试部"}, headers=auth_headers)
    assert changed.status_code == 200, changed.text
    grand = await anon_client.patch(f"/api/v1/admin/users/{victim_id}",
                                    json={"is_admin": True}, headers=auth_headers)
    assert grand.status_code == 200 and grand.json()["is_admin"] is True, grand.text
    removed = await anon_client.delete(f"/api/v1/admin/users/{victim_id}", headers=auth_headers)
    assert removed.status_code == 200, removed.text

    # 4) 删知识库来源（先塞一条片段）
    db_session.add(DocumentChunk(source="audit-source.md", content="审计测试片段",
                                 metadata_json={"embedding_model": get_settings().embedding_model},
                                 embedding=[0.0] * 384))
    db_session.commit()
    deleted = await anon_client.delete("/api/v1/admin/knowledge?source=audit-source.md", headers=auth_headers)
    assert deleted.status_code == 200, deleted.text

    # 5) 重算指标 + 6) 处理预警
    _seed_transactions(db_session, AUDIT_PERIOD, [("100", "0", "150")])
    assert (await anon_client.post(f"/api/v1/analysis/calculate?period={AUDIT_PERIOD}",
                                   headers=auth_headers)).status_code == 200
    alert = await _company_alert(anon_client, auth_headers, AUDIT_PERIOD)
    reviewed = await anon_client.patch(f"/api/v1/analysis/alerts/{alert['id']}",
                                       json={"status": "resolved", "note": "审计留痕用例"}, headers=auth_headers)
    assert reviewed.status_code == 200, reviewed.text

    # 7) 导出
    exported = await anon_client.get("/api/v1/export/metrics", headers=auth_headers)
    assert exported.status_code == 200, exported.text

    payload = await _audit(anon_client, auth_headers, limit=200)
    rows = payload["data"]
    for action in ("alert_rule.update", "user.department_change", "user.admin_change", "user.delete",
                   "knowledge.delete", "metrics.calculate", "alert.review", "data.export"):
        assert _find(rows, action) is not None, f"缺少审计：{action}"

    rule_row = _find(rows, "alert_rule.update", "利润异常")
    assert rule_row["detail"]["before"]["threshold_value"] == "0.0000"
    assert rule_row["detail"]["after"]["threshold_value"] == "-1.0000"
    assert rule_row["detail"]["after"]["level"] == "medium"

    department_row = _find(rows, "user.department_change", victim_id)
    assert department_row["detail"]["username"] == victim["username"]
    # ⑳ 起「没有部门」不再叫「不受限」：范围那件事叫「未分配」，能不能管是另一条审计
    assert department_row["detail"]["from"] == "未分配"
    assert department_row["detail"]["to"] == "审计测试部"
    admin_row = _find(rows, "user.admin_change", victim_id)
    assert admin_row["detail"]["from"] == "普通账号"
    assert admin_row["detail"]["to"] == "管理员"

    # 操作人一律是当前登录的管理员，不是被操作对象
    assert rule_row["actor"] == credentials["username"]
    assert department_row["actor"] == credentials["username"]

    review_row = _find(rows, "alert.review", alert["id"])
    assert review_row["detail"]["to"] == "resolved"
    assert review_row["detail"]["from"] == "pending"

    # 撤销批次单独一条（用它自己的批次号查）
    batch = db_session.scalar(select(FinancialTransaction.import_batch)
                              .where(FinancialTransaction.import_batch.like("pytest-%")).limit(1))
    reverted = await anon_client.delete(f"/api/v1/admin/batches/{batch}", headers=auth_headers)
    assert reverted.status_code == 200, reverted.text
    reverted_row = _find((await _audit(anon_client, auth_headers, limit=200))["data"], "batch.revert", batch)
    assert reverted_row is not None
    assert reverted_row["detail"]["deleted_rows"] >= 1

    # 规则改回默认，别影响同库其它用例（savepoint 会回滚，这里只是让断言更干净）
    await anon_client.put("/api/v1/admin/alert-rules/利润异常",
                          json={"threshold_value": "0", "level": "high"}, headers=auth_headers)


async def test_audit_log_filters_and_paginates(anon_client, auth_headers):
    """按动作/操作人过滤 + limit/offset 翻页，total 不受 limit 影响。"""
    # 造两条不同动作：登录成功 + 导出
    assert (await anon_client.get("/api/v1/export/scope", headers=auth_headers)).status_code == 200
    login = await anon_client.post("/api/v1/auth/login", json={"username": "审计不存在的人",
                                                              "password": "whatever-2026"})
    assert login.status_code == 401

    filtered = await _audit(anon_client, auth_headers, action="user.login_failed", limit=5)
    assert filtered["data"], filtered
    assert {row["action"] for row in filtered["data"]} == {"user.login_failed"}
    assert filtered["total"] >= 1

    # 动作下拉与操作人下拉来自接口，前端不写死
    assert any(item["value"] == "user.login_failed" and item["label"] == "登录失败"
               for item in filtered["actions"])
    assert filtered["actors"]

    unknown = await _audit(anon_client, auth_headers, actor="根本不存在的人")
    assert unknown["total"] == 0 and unknown["data"] == []

    first = await _audit(anon_client, auth_headers, limit=1)
    second = await _audit(anon_client, auth_headers, limit=1, offset=1)
    assert len(first["data"]) == 1 and len(second["data"]) == 1
    assert first["total"] == second["total"] >= 2
    assert first["data"][0]["id"] != second["data"][0]["id"]
    # 倒序：最新的在前
    assert first["data"][0]["id"] > second["data"][0]["id"]


async def test_audit_log_is_admin_only_and_read_only(anon_client, api_app, auth_headers):
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        denied = await scoped.get("/api/v1/admin/audit", headers=headers)
        assert denied.status_code == 403
        assert "需要管理员权限" in denied.json()["detail"]
    finally:
        await scoped.aclose()

    truly_anon = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://testserver",
                                   trust_env=False)
    async with truly_anon:
        assert (await truly_anon.get("/api/v1/admin/audit")).status_code == 401

    # 审计行本身没有修改与删除入口：能改的审计等于没有审计。
    methods = set(api_app.openapi()["paths"]["/api/v1/admin/audit"].keys())
    assert methods == {"get"}, methods
    # 唯一的删除路径是按保留期清理，而且它自己也要留痕；导出是 GET（不改数据）。
    assert set(api_app.openapi()["paths"]["/api/v1/admin/audit/purge"].keys()) == {"post"}
    assert set(api_app.openapi()["paths"]["/api/v1/admin/audit/export"].keys()) == {"get"}
    assert "audit.purge" in _audit_labels() and "audit.export" in _audit_labels()


def _audit_labels() -> dict:
    from backend import audit

    return audit.ACTIONS


def test_audit_scrub_drops_sensitive_keys():
    """脱敏规则单测：含 password/token/secret 的键整条丢掉，超长字符串截断。"""
    from backend import audit

    cleaned = audit._scrub({
        "password": "pytest-login-2026",
        "access_token": "eyJhbGciOi",
        "api_key": "sk-xxx",
        "nested": {"token": "should-be-stringified"},
        "long": "x" * 500,
        "rows": 12,
        "periods": ["2097-08", "2097-09"],
        "nothing": None,
    })
    assert "password" not in cleaned and "access_token" not in cleaned and "api_key" not in cleaned
    assert cleaned["rows"] == 12
    assert cleaned["periods"] == ["2097-08", "2097-09"]
    assert cleaned["nothing"] is None
    assert len(cleaned["long"]) == audit._MAX_TEXT
    # 嵌套 dict 递归保留结构（before/after 前后对比靠它），但敏感键在任何深度都会被丢掉
    assert cleaned["nested"] == {}
    nested_kept = audit._scrub({"before": {"threshold_value": "0.0000", "token": "x"}})
    assert nested_kept["before"] == {"threshold_value": "0.0000"}


# ---------------------------------------------------------------- 审计导出与清理

async def test_audit_export_writes_csv_and_leaves_a_trail(anon_client, api_app, auth_headers):
    """导出：CSV + BOM（Excel 中文不乱码）、筛选在导出前生效、导出这件事自己也留痕。"""
    # 先造一条待筛的失败登录，导出的筛选才有东西可证
    assert (await anon_client.post("/api/v1/auth/login", json={"username": "导出筛选用的人",
                                                              "password": "whatever-2026"})).status_code == 401

    exported = await anon_client.get("/api/v1/admin/audit/export?action=user.login_failed",
                                     headers=auth_headers)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    assert exported.headers["content-disposition"].endswith('.csv"')
    assert exported.headers["x-export-truncated"] == "false"
    rows = int(exported.headers["x-export-rows"])
    assert rows >= 1

    body = exported.content.decode("utf-8-sig")
    # BOM 必须在字节层面存在（Excel 靠它认 UTF-8），而解析时要去掉
    assert exported.content.startswith("\ufeff".encode("utf-8"))
    lines = body.splitlines()
    assert lines[0].split(",")[0] == "时间(UTC)"
    assert len(lines) == rows + 1
    assert all("user.login_failed" in line for line in lines[1:]), lines[:3]
    # 别的用例也在同一个库里写过登录失败，所以只要求"这一个"在导出结果里
    assert any("导出筛选用的人" in line for line in lines[1:]), lines[1:4]

    # 导出自己进了审计（合规口径：谁把审计捞走和谁改了数据一样重要）
    me = await anon_client.get("/api/v1/auth/me", headers=auth_headers)
    trail = await _audit(anon_client, auth_headers, action="audit.export", limit=5)
    assert trail["total"] >= 1
    latest = trail["data"][0]
    assert latest["detail"]["rows"] == rows
    assert latest["detail"]["action"] == "user.login_failed"
    assert latest["actor"] == me.json()["username"]

    # 受限账号既不能看也不能导
    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        assert (await scoped.get("/api/v1/admin/audit/export", headers=headers)).status_code == 403
        assert (await scoped.post("/api/v1/admin/audit/purge", json={}, headers=headers)).status_code == 403
    finally:
        await scoped.aclose()


async def test_audit_purge_respects_retention_and_is_dry_run_first(anon_client, auth_headers, db_session):
    """清理：默认只预览，真删只动过期行，且清理自己留一条 `audit.purge`。"""
    from datetime import datetime, timedelta

    from backend import audit
    from backend.models import AuditLog

    # 直接在库里造一条 400 天前的旧记录与一条今天的记录（接口不会产生"过去"的时间）
    old = AuditLog(actor="purge-history", action="user.login", target_type="user", target_id="old",
                   detail={"note": "老到该被清掉"}, ip="", created_at=datetime.now(UTC) - timedelta(days=400))
    fresh = AuditLog(actor="purge-today", action="user.login", target_type="user", target_id="new",
                     detail={}, ip="", created_at=datetime.now(UTC))
    db_session.add_all([old, fresh])
    db_session.commit()

    listed = await _audit(anon_client, auth_headers, actor="purge-history")
    assert listed["total"] == 1
    assert listed["retention_days"] == 180
    assert listed["export_limit"] == 5000

    # 先预览：报出会删几条，但一条都不删
    preview = await anon_client.post("/api/v1/admin/audit/purge", json={}, headers=auth_headers)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["dry_run"] is True and body["keep_days"] == 180
    assert body["deleted"] >= 1
    assert (await _audit(anon_client, auth_headers, actor="purge-history"))["total"] == 1

    # 真删：老行没了、今天的还在、清理动作留痕
    purged = await anon_client.post("/api/v1/admin/audit/purge",
                                    json={"dry_run": False, "keep_days": 180}, headers=auth_headers)
    assert purged.status_code == 200, purged.text
    assert purged.json()["dry_run"] is False
    assert purged.json()["deleted"] == body["deleted"]
    assert (await _audit(anon_client, auth_headers, actor="purge-history"))["total"] == 0
    assert (await _audit(anon_client, auth_headers, actor="purge-today"))["total"] == 1
    trail = await _audit(anon_client, auth_headers, action="audit.purge", limit=5)
    assert trail["total"] >= 1
    assert trail["data"][0]["detail"]["deleted"] == body["deleted"]
    assert trail["data"][0]["detail"]["keep_days"] == 180

    # 不能"保留 0 天"= 清空；超范围直接 422，而且这种请求不该留下 purge 痕迹
    before = (await _audit(anon_client, auth_headers, action="audit.purge", limit=50))["total"]
    for bad in ({"keep_days": 0}, {"keep_days": 40000}, {"keep_days": "很久"}):
        rejected = await anon_client.post("/api/v1/admin/audit/purge", json=bad, headers=auth_headers)
        assert rejected.status_code == 422, (bad, rejected.text)
    assert (await _audit(anon_client, auth_headers, action="audit.purge", limit=50))["total"] == before

    # 单测直接调底层：dry_run 纯计数、cutoff 是"days 天前"
    cutoff = audit.retention_cutoff(180)
    assert audit.count_older_than(db_session, cutoff) == 0
    assert audit.purge(db_session, cutoff=cutoff, dry_run=True) == 0


async def test_audit_log_filters_by_ip_and_date_range(anon_client, auth_headers, db_session):
    """IP 前缀与日期范围筛选：日期是**业务时区**的日历日（含首尾），不是"字符串比大小"。"""
    from datetime import date, datetime, timedelta

    from backend.models import AuditLog

    today = date.today()
    # 造一条"三天前"的记录：它必须在三天前的窗口里出现、在今天的窗口里消失
    older = AuditLog(actor="range-history", action="user.login", target_type="user", target_id="old",
                     detail={}, ip="198.51.100.7",
                     created_at=datetime.now(UTC) - timedelta(days=3))
    db_session.add(older)
    db_session.commit()

    listed = await _audit(anon_client, auth_headers, limit=1)
    assert isinstance(listed["ips"], list) and "198.51.100.7" in listed["ips"]

    window = (today - timedelta(days=3)).isoformat()
    assert (await _audit(anon_client, auth_headers, actor="range-history",
                         since=window, until=window))["total"] == 1
    assert (await _audit(anon_client, auth_headers, actor="range-history",
                         since=today.isoformat(), until=today.isoformat()))["total"] == 0
    # 久远的空窗口干脆返回 0，而不是把整表都当命中
    assert (await _audit(anon_client, auth_headers, since="2000-01-01", until="2000-01-02"))["total"] == 0
    # 只给 since：从那天起全部命中（含刚才那条三天前的）
    assert (await _audit(anon_client, auth_headers, actor="range-history",
                         since=window))["total"] == 1

    # IP 前缀匹配（拿刚写进去的那个 IP 的前 3 位当前缀）
    prefix = "198.51.100"
    matched = await _audit(anon_client, auth_headers, ip=prefix, limit=50)
    assert matched["data"] and all(row["ip"].startswith(prefix) for row in matched["data"])
    assert (await _audit(anon_client, auth_headers, ip="203.0.113.250"))["total"] == 0


async def test_audit_log_date_range_follows_the_business_timezone(anon_client, auth_headers, db_session):
    """审计导出的 `since/until` 是**业务时区**的日历日（⑯）。

    这一条是 UTC+8 的经典坑：北京时间 10-09 00:30 那条记录，UTC 看还在 10-08。
    以前 `datetime.combine(...).astimezone()` 读的是服务器本机时区，于是同一份数据在
    容器里（UTC）和本机跑出来的日期不一样，用户看到的「10 月 9 日」会漏掉这条。
    """
    from datetime import UTC, datetime

    from backend.models import AuditLog

    # 业务 10-09 00:00 正好等于 UTC 10-08 16:00；左闭右开，边界上的记录算 10-09
    rows = [
        ("biztime-edge", datetime(2026, 10, 8, 16, 0, tzinfo=UTC)),      # 业务 10-09 00:00
        ("biztime-morning", datetime(2026, 10, 8, 16, 30, tzinfo=UTC)),  # 业务 10-09 00:30
        ("biztime-late", datetime(2026, 10, 8, 15, 30, tzinfo=UTC)),     # 业务 10-08 23:30
    ]
    for actor, moment in rows:
        db_session.add(AuditLog(actor=actor, action="user.login", target_type="user",
                                target_id=actor, detail={}, ip="198.51.100.9", created_at=moment))
    db_session.commit()

    ninth = await _audit(anon_client, auth_headers, since="2026-10-09", until="2026-10-09", limit=50)
    actors = {row["actor"] for row in ninth["data"]}
    assert {"biztime-edge", "biztime-morning"} <= actors
    assert "biztime-late" not in actors

    eighth = await _audit(anon_client, auth_headers, since="2026-10-08", until="2026-10-08", limit=50)
    assert "biztime-late" in {row["actor"] for row in eighth["data"]}
    assert "biztime-morning" not in {row["actor"] for row in eighth["data"]}


# ---------------------------------------------------------------- 分页与筛选

async def test_admin_users_pagination_filters_and_total(anon_client, auth_headers, db_session):
    """用户列表：`total` 必须是筛选后的总行数、`offset` 真的换页、部门下拉的选项由后端给。

    改造前 `total = len(data)`——被 `limit` 一截断就变成本页条数，前端「共 N 个账号」于是随口撒谎。
    """
    from backend.models import User

    prefix = f"pg_{uuid.uuid4().hex[:6]}"
    wanted = {"a": "分页测试部A", "b": "分页测试部B", "c": None}
    for suffix, department in wanted.items():
        username = f"{prefix}_{suffix}"
        created = await anon_client.post("/api/v1/auth/register",
                                        json={"username": username, "password": "pagination-2026"})
        assert created.status_code == 200, created.text
        if department:
            user_id = db_session.scalar(select(User.id).where(User.username == username))
            changed = await anon_client.patch(f"/api/v1/admin/users/{user_id}",
                                              json={"department": department}, headers=auth_headers)
            assert changed.status_code == 200, changed.text

    async def fetch(query: str = ""):
        response = await anon_client.get(f"/api/v1/admin/users?query={prefix}{query}", headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()

    first = await fetch("&limit=1")
    assert first["total"] == 3          # 本页只回 1 条，total 仍是 3
    assert len(first["data"]) == 1 and first["limit"] == 1 and first["offset"] == 0
    second = await fetch("&limit=1&offset=1")
    third = await fetch("&limit=1&offset=2")
    assert {second["total"], third["total"]} == {3}
    assert all(len(page["data"]) == 1 for page in (second, third))
    assert len({first["data"][0]["id"], second["data"][0]["id"], third["data"][0]["id"]}) == 3

    only_a = await fetch("&department=分页测试部A")
    assert only_a["total"] == 1 and only_a["data"][0]["department"] == "分页测试部A"

    unrestricted = await fetch("&unrestricted=true")
    assert unrestricted["total"] == 1 and unrestricted["data"][0]["username"] == f"{prefix}_c"
    scoped = await fetch("&unrestricted=false")
    assert scoped["total"] == 2

    # 下拉选项来自后端，不是拿本页数据凑的——翻到第 2 页也不会缺选项
    assert {"分页测试部A", "分页测试部B"} <= set(first["departments"])
    assert first["unrestricted_count"] >= 1

    for bad in ("limit=0", "limit=501", "offset=-1"):
        assert (await anon_client.get(f"/api/v1/admin/users?{bad}", headers=auth_headers)).status_code == 422


async def test_admin_knowledge_pagination_and_filters(anon_client, auth_headers, db_session):
    """知识库列表按来源分页：片段计数与总数都要在筛选之后算。"""
    from backend.rag.pgvector_store import DocumentChunk

    prefix = f"pg-{uuid.uuid4().hex[:6]}"
    plan = ((f"{prefix}-A.md", "制度", 3), (f"{prefix}-B.md", "合同", 1), (f"{prefix}-C.md", None, 1))
    for source, document_type, count in plan:
        for index in range(count):
            db_session.add(DocumentChunk(
                source=source, content=f"分页测试片段 {index}", document_type=document_type,
                metadata_json={"embedding_model": "pytest"}, embedding=[0.0] * 384))
    db_session.flush()

    async def fetch(query: str = ""):
        response = await anon_client.get(f"/api/v1/admin/knowledge?source={prefix}{query}",
                                         headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()

    first = await fetch("&limit=2")
    assert first["total"] == 3 and first["matched_chunks"] == 5 and first["all_chunks"] >= 5
    assert len(first["data"]) == 2 and first["offset"] == 0
    second = await fetch("&limit=2&offset=2")
    assert second["total"] == 3 and len(second["data"]) == 1

    listed = first["data"] + second["data"]
    assert {item["source"] for item in listed} == {row[0] for row in plan}
    assert sum(item["chunks"] for item in listed) == 5

    only_policy = await fetch("&document_type=制度")
    assert only_policy["total"] == 1 and only_policy["matched_chunks"] == 3
    assert only_policy["data"][0]["source"] == f"{prefix}-A.md" and only_policy["data"][0]["chunks"] == 3
    assert {"制度", "合同"} <= set(only_policy["document_types"])

    nothing = await fetch("&document_type=不存在的类型")
    assert nothing["total"] == 0 and nothing["matched_chunks"] == 0 and nothing["data"] == []

    assert (await anon_client.get("/api/v1/admin/knowledge?limit=0", headers=auth_headers)).status_code == 422
    assert (await anon_client.get("/api/v1/admin/knowledge?offset=-1", headers=auth_headers)).status_code == 422


async def test_admin_batches_total_and_paging(anon_client, auth_headers, db_session):
    """批次列表的 `total` 是批次个数（不是本页行数），且能按批次翻页。"""
    from backend.models import FinancialTransaction

    async def fetch(query: str) -> dict:
        response = await anon_client.get(f"/api/v1/admin/batches?{query}", headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()

    before = await fetch("limit=200")
    stamp = uuid.uuid4().hex[:6]
    for name, day in ((f"pytest-{stamp}-a", "01"), (f"pytest-{stamp}-b", "02")):
        db_session.add(FinancialTransaction(
            transaction_date=f"2097-09-{day}", account_name="批次测试", department="部门-097",
            description="pytest 批次", revenue_amount=Decimal("10"), cost_amount=Decimal("0"),
            expense_amount=Decimal("0"), cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"),
            import_batch=name))
    db_session.flush()

    after = await fetch("limit=200")
    assert after["total"] == before["total"] + 2
    assert len(after["data"]) == after["total"]

    paged = await fetch("limit=1&offset=0")
    shifted = await fetch("limit=1&offset=1")
    assert paged["total"] == after["total"] == shifted["total"]
    assert len(paged["data"]) == 1 and paged["data"][0]["batch"] != shifted["data"][0]["batch"]

    assert (await anon_client.get("/api/v1/admin/batches?offset=-1", headers=auth_headers)).status_code == 422
    assert (await anon_client.get("/api/v1/admin/batches?limit=201", headers=auth_headers)).status_code == 422


async def test_alerts_all_status_filter_happens_before_paging(anon_client, auth_headers, db_session):
    """`/alerts/all` 的 `status` 必须在服务端过滤：前端拿一页数据再筛会静默漏掉第 2 页的误报。"""
    period = "2097-10"
    # 成本 900 / 收入 1010 ≈ 0.8911 > 0.8 → 成本率异常；第二行净利 -490 → 利润异常
    _seed_transactions(db_session, period, [("1000", "900", "0"), ("10", "0", "500")])
    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)
    assert calculated.status_code == 200, calculated.text

    async def fetch(query: str) -> dict:
        response = await anon_client.get(f"/api/v1/analysis/alerts/all?period={period}{query}",
                                         headers=auth_headers)
        assert response.status_code == 200, response.text
        return response.json()

    everything = await fetch("&limit=100")
    assert everything["total"] == 2 and len(everything["data"]) == 2

    company = await _company_alert(anon_client, auth_headers, period)
    patched = await anon_client.patch(f"/api/v1/analysis/alerts/{company['id']}",
                                      json={"status": "false_positive", "note": "分页测试：判定为误报"},
                                      headers=auth_headers)
    assert patched.status_code == 200, patched.text

    false_positive = await fetch("&status=false_positive&limit=100")
    assert false_positive["total"] == 1
    assert [row["id"] for row in false_positive["data"]] == [company["id"]]
    assert false_positive["data"][0]["status_label"] == "误报"

    pending = await fetch("&status=pending&limit=100")
    assert pending["total"] == everything["total"] - 1
    assert company["id"] not in [row["id"] for row in pending["data"]]

    # 筛选发生在分页之前：limit=1 时 total 仍是筛选后的总数，不会退化成 1
    narrow = await fetch("&limit=1")
    shifted = await fetch("&limit=1&offset=1")
    assert narrow["total"] == shifted["total"] == 2
    assert narrow["data"][0]["id"] != shifted["data"][0]["id"]
    assert {narrow["data"][0]["id"], shifted["data"][0]["id"]} == {row["id"] for row in everything["data"]}

    bogus = await anon_client.get("/api/v1/analysis/alerts/all?status=ignore", headers=auth_headers)
    assert bogus.status_code == 422
    assert "状态只能是" in bogus.json()["detail"]


# ---------------------------------------------------------------- 后台任务

def _seed_batch_rows(db, batch: str, period: str, rows) -> None:
    """按批次名播种明细（`_seed_transactions` 的批次号写死了，撤销用例必须自己指定）。"""
    from backend.models import FinancialTransaction

    for index, (revenue, cost, expense) in enumerate(rows):
        db.add(FinancialTransaction(
            transaction_date=f"{period}-{index + 1:02d}",
            account_name="批次测试", account_code="6001", department="部门-097",
            description="pytest 批次种子", revenue_amount=Decimal(revenue),
            cost_amount=Decimal(cost), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"),
            import_batch=batch,
        ))
    db.flush()


async def test_batch_revert_runs_as_background_job(anon_client, auth_headers, db_session):
    """撤销批次走后台任务：接口立刻 202，执行完状态变 succeeded，明细被删、期间被重算、审计留痕。

    手工调 `jobs.run_job` 而不是依赖 `BackgroundTasks`：ASGITransport 下后台任务会在响应
    之后才跑，断言就得跟它赛跑；把它当成一个纯函数调用，测的是同一段逻辑。
    """
    from sqlalchemy import func, select

    from backend import jobs
    from backend.models import FinancialTransaction

    period = "2097-11"
    stamp = uuid.uuid4().hex[:6]
    doomed = f"pytest-{stamp}-revert"
    keeper = f"pytest-{stamp}-keep"
    _seed_batch_rows(db_session, doomed, period, [("100", "0", "0"), ("200", "0", "0")])
    # 同期间再放一条别的批次：「撤销后重算」与「撤销后清空」是两条不同的分支，这里走重算
    _seed_batch_rows(db_session, keeper, period, [("50", "0", "0")])
    # 让期间指标存在，重算才有东西可对照
    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={period}", headers=auth_headers)
    assert calculated.status_code == 200, calculated.text

    submitted = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": doomed},
                                       headers=auth_headers)
    assert submitted.status_code == 202, submitted.text
    job = submitted.json()
    assert job["status"] == "queued" and job["kind"] == "batch.revert"
    assert job["payload"]["batch"] == doomed
    # 提交时还没执行：明细必须还在（提交接口只写任务记录）
    assert db_session.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch == doomed)) == 2

    jobs.run_job(job["id"], db_session)

    fetched = await anon_client.get(f"/api/v1/admin/jobs/{job['id']}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    finished = fetched.json()
    assert finished["status"] == "succeeded", finished
    assert finished["error"] is None
    assert finished["result"]["deleted_rows"] == 2
    assert finished["result"]["recalculated"] == [period]
    assert finished["result"]["cleared"] == []
    assert finished["finished_at"]

    assert db_session.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch == doomed)) == 0
    # 同期间的其它批次不能被牵连
    assert db_session.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch == keeper)) == 1

    trail = await anon_client.get("/api/v1/admin/audit?action=batch.revert&limit=50", headers=auth_headers)
    assert trail.status_code == 200, trail.text
    entries = [row for row in trail.json()["data"] if row["target_id"] == doomed]
    assert entries, trail.text
    assert entries[0]["detail"]["job"] == job["id"]
    assert entries[0]["detail"]["deleted_rows"] == 2
    assert entries[0]["detail"]["periods"] == [period]


async def test_batch_revert_job_rejects_unknown_batch(anon_client, auth_headers, db_session):
    """批次不存在时直接 404，而不是排一条注定失败的任务让用户在界面上看到 failed。"""
    before = (await anon_client.get("/api/v1/admin/jobs?limit=1", headers=auth_headers)).json()
    missing = await anon_client.post("/api/v1/admin/jobs/batch-revert",
                                     json={"batch": f"nope-{uuid.uuid4().hex}"}, headers=auth_headers)
    assert missing.status_code == 404
    assert "批次不存在" in missing.json()["detail"]
    after = (await anon_client.get("/api/v1/admin/jobs?limit=1", headers=auth_headers)).json()
    assert after["total"] == before["total"]

    # 批次号不允许为空（省掉一次没有意义的查询）
    empty = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": ""}, headers=auth_headers)
    assert empty.status_code == 422


async def test_unknown_job_kind_fails_loudly(anon_client, auth_headers, db_session):
    """没有注册处理函数的任务必须变成 failed + 可读原因，不能静默什么都不做。"""
    from backend import jobs
    from backend.models import BackgroundJob

    with pytest.raises(ValueError):
        jobs.submit(db_session, "not.registered", {})

    # 直接写库模拟"历史遗留 / 手写的坏记录"，因为 submit 已经拦住了这条路径
    orphan = BackgroundJob(kind="not.registered", status="queued", actor="pytest", payload={})
    db_session.add(orphan)
    db_session.flush()

    jobs.run_job(orphan.id, db_session)

    fetched = await anon_client.get(f"/api/v1/admin/jobs/{orphan.id}", headers=auth_headers)
    assert fetched.status_code == 200, fetched.text
    body = fetched.json()
    assert body["status"] == "failed"
    assert "没有注册的处理函数" in body["error"]
    # 未知 kind 没有中文名时退回原始码，界面不至于显示空白
    assert body["kind_label"] == "not.registered"


async def test_jobs_list_filters_and_restart_resume(anon_client, api_app, auth_headers, db_session):
    """任务列表的筛选/分页与「进程重启接续」，以及受限账号不得查看后台任务。"""
    from backend import jobs
    from backend.models import BackgroundJob

    queued, _ = jobs.submit(db_session, "batch.revert", {"batch": "pytest-queued"}, actor="pytest")
    running, _ = jobs.submit(db_session, "batch.revert", {"batch": "pytest-running"}, actor="pytest")
    running.status = "running"
    # 预算已经用完：这种情况下重启也没有重跑的意义，只能判死
    running.attempts = running.max_attempts
    db_session.flush()

    listed = await anon_client.get("/api/v1/admin/jobs?limit=100", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert body["total"] >= 2
    assert len(body["data"]) == body["total"]
    assert [item["value"] for item in body["statuses"]] == ["queued", "running", "succeeded", "failed"]
    assert {"value": "batch.revert", "label": "撤销导入批次"} in body["kinds"]
    assert {item["value"]: item["label"] for item in body["statuses"]}["failed"] == "失败"

    only_running = await anon_client.get("/api/v1/admin/jobs?status=running&limit=100", headers=auth_headers)
    assert only_running.status_code == 200
    assert running.id in [row["id"] for row in only_running.json()["data"]]
    assert all(row["status"] == "running" for row in only_running.json()["data"])

    # limit=1 时 total 仍是筛选后的总数（和其余列表接口同一口径）
    narrow = await anon_client.get("/api/v1/admin/jobs?limit=1", headers=auth_headers)
    assert narrow.json()["total"] == body["total"] and len(narrow.json()["data"]) == 1

    assert (await anon_client.get("/api/v1/admin/jobs?status=bogus", headers=auth_headers)).status_code == 422
    assert (await anon_client.get("/api/v1/admin/jobs?limit=0", headers=auth_headers)).status_code == 422
    assert (await anon_client.get("/api/v1/admin/jobs?offset=-1", headers=auth_headers)).status_code == 422
    missing = await anon_client.get("/api/v1/admin/jobs/99999999", headers=auth_headers)
    assert missing.status_code == 404
    assert "没有这条后台任务" in missing.json()["detail"]

    # 进程重启（⑩）：还有预算的**重新排队重跑**（处理函数幂等，重跑安全），用完预算的才判死。
    # schedule=False：用例里不许真开定时器，否则断言跑完还会有一段真撤销在后台跑。
    orphan = BackgroundJob(kind="not.registered", status="queued", actor="pytest", payload={})
    db_session.add(orphan)
    db_session.flush()
    resumed = jobs.resume_interrupted_jobs(db_session, schedule=False)
    assert resumed["total"] >= 2
    assert queued.id in resumed["requeued"] and running.id in resumed["failed"]
    assert orphan.id in resumed["failed"]
    fetched = await anon_client.get(f"/api/v1/admin/jobs/{queued.id}", headers=auth_headers)
    assert fetched.json()["status"] == "queued"
    assert "重新排队" in fetched.json()["error"]
    fetched = await anon_client.get(f"/api/v1/admin/jobs/{running.id}", headers=auth_headers)
    assert fetched.json()["status"] == "failed"
    assert "已用完" in fetched.json()["error"]
    fetched = await anon_client.get(f"/api/v1/admin/jobs/{orphan.id}", headers=auth_headers)
    assert fetched.json()["status"] == "failed"
    assert "已经没有处理函数" in fetched.json()["error"]

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        forbidden = await scoped.get("/api/v1/admin/jobs", headers=headers)
        assert forbidden.status_code == 403
        forbidden_post = await scoped.post("/api/v1/admin/jobs/batch-revert",
                                           json={"batch": "whatever"}, headers=headers)
        assert forbidden_post.status_code == 403
    finally:
        await scoped.aclose()

    # 收尾：把本用例造的任务标记清楚，避免影响后续断言（用例结束整体回滚，这里只是语义整洁）
    for job in (queued, running):
        assert isinstance(db_session.get(BackgroundJob, job.id), BackgroundJob)


async def test_job_claim_is_atomic_and_refuses_a_running_job(anon_client, auth_headers, db_session):
    """⑱ 抢占是原子的：`queued → running` 只发生一次，第二个抢的人什么都不改。"""
    from backend import jobs
    from backend.models import BackgroundJob

    job, created = jobs.submit(
        db_session, "batch.revert", {"batch": f"pytest-claim-{uuid.uuid4().hex}"}, actor="pytest"
    )
    assert created

    claimed = jobs.claim(db_session, job.id)
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.attempts == 1
    assert claimed.worker_id == jobs.WORKER_ID
    assert claimed.started_at is not None and claimed.next_attempt_at is None

    # 输的那一方：同一个 `queued` 条件已经不再成立，`rowcount == 0` → 它不会去跑这条任务。
    # 关键断言是 attempts **没有**被加第二次 —— 加了就说明两个进程都认为自己抢到了。
    assert jobs.claim(db_session, job.id) is None
    assert db_session.get(BackgroundJob, job.id).attempts == 1

    # 已经在跑的（或已经跑完的）任务不会被 `run_job` 再跑一遍：
    # ⑱ 之前判的是「在不在 ACTIVE_STATUSES 里」，`running` 也在其中 → 同一个任务被并发跑两遍。
    job.status = "running"
    db_session.flush()
    jobs.run_job(job.id, db_session)
    assert db_session.get(BackgroundJob, job.id).attempts == 1

    # 不存在的任务同样安静返回
    assert jobs.claim(db_session, 99999999) is None


async def test_job_claim_lock_is_a_real_cross_process_lock(anon_client, auth_headers):
    """⑱ 执行权锁是**全库**可见的会话级锁：别的连接抢不到；同一连接可重入，重入几次就要放几次。"""
    from backend.db import engine
    from backend.locks import job_lock_key, release_session_lock, try_session_lock

    # 负数 key：绝不会和真实任务 id（自增正数）撞号
    key = job_lock_key(-12345)
    with engine.connect() as first:
        assert try_session_lock(first, key) is True
        with engine.connect() as second:
            # 另一个会话（= 另一个进程）抢不到 —— 这就是「这条任务已经有人在跑」的判据
            assert try_session_lock(second, key) is False
            # 而且它没资格解锁：`pg_advisory_unlock` 对自己不持有的锁返回 false
            assert release_session_lock(second, key) is False
        # 同一会话里再抢一次是重入（计数 +1），所以下面要放两次才真的空出来
        assert try_session_lock(first, key) is True
        assert release_session_lock(first, key) is True
        assert release_session_lock(first, key) is True
        assert release_session_lock(first, key) is False  # 已经空了
    # 连接关掉之后锁一定不在（进程崩了也一样）——「抢得到 = 没人在跑」正是接续判断僵尸的依据
    with engine.connect() as third:
        assert try_session_lock(third, key) is True
        assert release_session_lock(third, key) is True


async def test_run_job_skips_when_another_process_holds_the_claim(anon_client, auth_headers, db_session):
    """⑱ 执行权锁被别人持有时，本进程不碰这条任务；自己跑完一定把锁放掉（否则任务永久卡住）。"""
    from backend import jobs
    from backend.db import engine
    from backend.locks import release_session_lock, try_session_lock

    job, _ = jobs.submit(
        db_session, "batch.revert", {"batch": f"pytest-busy-{uuid.uuid4().hex}"}, actor="pytest"
    )
    db_session.flush()
    key = jobs.job_lock_key(job.id)

    with engine.connect() as holder:
        assert try_session_lock(holder, key) is True
        # `run_job(job_id)`（生产路径）就是走这个上下文管理器进出执行段的
        with jobs.job_claim_lock(job.id) as locked:
            assert locked is False
        assert release_session_lock(holder, key) is True

    # 锁空着 → 抢得到，而且退出上下文时锁必须已经放掉（不然这条任务再也抢不动）
    with jobs.job_claim_lock(job.id) as locked:
        assert locked is True
    with engine.connect() as after:
        assert try_session_lock(after, key) is True
        assert release_session_lock(after, key) is True


async def test_restart_resume_leaves_jobs_that_are_really_running(anon_client, auth_headers, db_session):
    """⑱ 接续只重排僵尸：执行权锁被持有的 `running` 行原样不动，锁空着的才重排重跑。"""
    from backend import jobs
    from backend.db import engine
    from backend.locks import job_lock_key, release_session_lock, try_session_lock
    from backend.models import BackgroundJob

    live, _ = jobs.submit(
        db_session, "batch.revert", {"batch": f"pytest-live-{uuid.uuid4().hex}"}, actor="pytest"
    )
    live.status = "running"
    live.worker_id = "pytest-other-worker"
    dead, _ = jobs.submit(
        db_session, "batch.revert", {"batch": f"pytest-dead-{uuid.uuid4().hex}"}, actor="pytest"
    )
    dead.status = "running"
    dead.worker_id = "pytest-gone-worker"
    db_session.flush()

    key = job_lock_key(live.id)
    with engine.connect() as holder:
        # 模拟「另一个进程正在跑 live」：它持有执行权锁
        assert try_session_lock(holder, key) is True
        resumed = jobs.resume_interrupted_jobs(db_session, schedule=False)
        assert resumed["skipped"] is False
        assert live.id in resumed["still_running"] and live.id not in resumed["requeued"]
        assert dead.id in resumed["requeued"]
        # 正在跑的那条一个字都没改（状态、归属进程都还在）
        db_session.expire_all()
        assert db_session.get(BackgroundJob, live.id).status == "running"
        assert db_session.get(BackgroundJob, live.id).worker_id == "pytest-other-worker"
        assert release_session_lock(holder, key) is True

    # 那个进程没了（锁随连接断开自动释放）→ 下一次接续才把它当僵尸重排，归属进程清空
    resumed = jobs.resume_interrupted_jobs(db_session, schedule=False)
    assert live.id in resumed["requeued"]
    db_session.expire_all()
    fetched = db_session.get(BackgroundJob, live.id)
    assert fetched.status == "queued" and fetched.worker_id is None
    assert "重新排队" in fetched.error


async def test_restart_resume_only_one_process_does_the_work(anon_client, auth_headers, db_session):
    """⑱ 多副本同时启动时只有抢到全局锁的那个进程动这些行，其它进程返回 skipped 且一个字都不改。"""
    from backend import jobs
    from backend.db import engine
    from backend.locks import release_session_lock, resume_lock_key, try_session_lock
    from backend.models import BackgroundJob

    job, _ = jobs.submit(
        db_session, "batch.revert", {"batch": f"pytest-skip-{uuid.uuid4().hex}"}, actor="pytest"
    )
    # 有预算的 running：如果后面那个进程真的接续了，它一定会被重排 —— 所以「还是 running」就是证据
    job.status = "running"
    job.worker_id = "pytest-other-worker"
    db_session.flush()

    key = resume_lock_key()
    with engine.connect() as holder:
        assert try_session_lock(holder, key) is True
        try:
            resumed = jobs.resume_interrupted_jobs(db_session, schedule=False)
            assert resumed == {
                "requeued": [], "failed": [], "still_running": [], "skipped": True, "total": 0
            }
            db_session.expire_all()
            untouched = db_session.get(BackgroundJob, job.id)
            assert untouched.status == "running"
            assert untouched.worker_id == "pytest-other-worker"
            assert untouched.error is None
        finally:
            assert release_session_lock(holder, key) is True


async def test_batch_revert_submit_is_idempotent(anon_client, auth_headers, db_session):
    """⑩ 重复提交不会排出第二条任务：默认拦「同一批次正在撤销中」，给了 Idempotency-Key 连成功过的也复用。"""
    from sqlalchemy import func, select

    from backend import jobs
    from backend.models import BackgroundJob, FinancialTransaction

    period = "2097-11"
    batch = f"pytest-{uuid.uuid4().hex[:6]}-idem"
    _seed_batch_rows(db_session, batch, period, [("100", "0", "0"), ("200", "0", "0")])

    first = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": batch},
                                   headers=auth_headers)
    assert first.status_code == 202, first.text
    one = first.json()
    assert one["deduplicated"] is False
    # 新字段：重试相关的状态要能在任务记录里读出来（界面靠它显示"第几次"
    assert one["attempts"] == 0 and one["max_attempts"] == 3
    assert one["next_attempt_at"] is None and one["idempotency_key"] is None

    # 双击「撤销」：第二次点击不该再排一条（否则同一批会被重算两遍）
    second = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": batch},
                                    headers=auth_headers)
    assert second.status_code == 202, second.text
    assert second.json()["id"] == one["id"]
    assert second.json()["deduplicated"] is True
    assert db_session.scalar(
        select(func.count()).select_from(BackgroundJob)
        .where(BackgroundJob.payload["batch"].astext == batch)) == 1

    # 带上 Idempotency-Key：任务执行完（批次明细已经没有）之后，同一个键的请求重放
    # 仍然拿回**同一条**任务，而不是 404 —— 客户端重试的意义就在这里
    keyed_batch = f"pytest-{uuid.uuid4().hex[:6]}-keyed"
    _seed_batch_rows(db_session, keyed_batch, period, [("100", "0", "0")])
    key = f"pytest-key-{uuid.uuid4().hex[:8]}"
    keyed_headers = {**auth_headers, "Idempotency-Key": key}
    created = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": keyed_batch},
                                     headers=keyed_headers)
    assert created.status_code == 202, created.text
    job_id = created.json()["id"]
    assert created.json()["idempotency_key"] == key
    jobs.run_job(job_id, db_session)
    assert db_session.scalar(
        select(func.count()).select_from(FinancialTransaction)
        .where(FinancialTransaction.import_batch == keyed_batch)) == 0

    replayed = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": keyed_batch},
                                      headers=keyed_headers)
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["id"] == job_id and replayed.json()["deduplicated"] is True
    assert replayed.json()["status"] == "succeeded"

    # 换个键就是另一件事：批次已经撤掉了，这时必须 404，而不是伪造一条新任务
    other = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": keyed_batch},
                                   headers={**auth_headers, "Idempotency-Key": f"{key}-other"})
    assert other.status_code == 404
    # 键太长直接 422（别让一个超长字符串去撞 VARCHAR(200)）
    too_long = await anon_client.post(
        "/api/v1/admin/jobs/batch-revert", json={"batch": batch},
        headers={**auth_headers, "Idempotency-Key": "x" * 201})
    assert too_long.status_code == 422
    assert "Idempotency-Key" in too_long.json()["detail"]


async def test_failed_job_retry_is_idempotent(anon_client, auth_headers, db_session):
    """⑩ 失败任务能人工重试，而且重试是**幂等**的：上次已经删掉的行不会让这次再判失败。"""
    from backend import batch_ops, jobs
    from backend.models import BackgroundJob

    period = "2097-11"
    batch = f"pytest-{uuid.uuid4().hex[:6]}-retry"
    _seed_batch_rows(db_session, batch, period, [("100", "0", "0")])

    submitted = await anon_client.post("/api/v1/admin/jobs/batch-revert", json={"batch": batch},
                                       headers=auth_headers)
    assert submitted.status_code == 202, submitted.text
    job_id = submitted.json()["id"]
    # 还在排队中的任务不接受重试（重试会排出第二条一样的活儿）
    queued_retry = await anon_client.post(f"/api/v1/admin/jobs/{job_id}/retry", headers=auth_headers)
    assert queued_retry.status_code == 409
    assert "只有失败的任务" in queued_retry.json()["detail"]

    # 模拟「任务还没跑，数据已经被同步接口撤销掉了」：第一次执行必然失败（一行都没删）。
    # 还有预算 → 只是放回队列等重试，不是判死。
    batch_ops.revert_batch(db_session, batch, actor="pytest")
    jobs.run_job(job_id, db_session)
    row = db_session.get(BackgroundJob, job_id)
    assert row.status == "queued", row.error
    assert row.attempts == 1 and row.next_attempt_at is not None
    assert "第 1 次尝试失败：批次不存在或已被撤销" in row.error

    # 自动重跑：这一跑知道自己是重跑（is_retry），所以「一行都没删」不再算失败 —— 上次已经把活干完了。
    # 任务判成功，结果里明确写着 already_reverted，而不是假装又删了 0 行。
    jobs.run_job(job_id, db_session)
    row = db_session.get(BackgroundJob, job_id)
    assert row.status == "succeeded", row.error
    assert row.attempts == 2 and row.error is None and row.next_attempt_at is None
    assert row.result["deleted_rows"] == 0 and row.result["already_reverted"] is True
    assert row.result["recalculated"] == [] and row.result["cleared"] == []

    # 审计里两条 batch.revert：第一条真删了行，第二条是重跑的空结果（标了 already_reverted）
    trail = await anon_client.get("/api/v1/admin/audit?action=batch.revert&limit=50", headers=auth_headers)
    assert trail.status_code == 200, trail.text
    entries = [entry for entry in trail.json()["data"] if entry["target_id"] == batch]
    assert len(entries) == 2, entries
    assert entries[0]["detail"]["already_reverted"] is True
    assert entries[0]["detail"]["deleted_rows"] == 0
    assert entries[0]["detail"]["job"] == job_id

    # 已经成功的任务不接受重试（把「撤销」再按一次不是个合理的操作）
    again = await anon_client.post(f"/api/v1/admin/jobs/{job_id}/retry", headers=auth_headers)
    assert again.status_code == 409
    assert "只有失败的任务" in again.json()["detail"]


async def test_manual_retry_appends_budget_keeps_reason_and_is_audited(anon_client, auth_headers,
                                                                       db_session):
    """⑩ 预算用尽后的人工重试：追加一份预算、保留上次的失败原因、留痕，且只认 failed。"""
    from backend import jobs
    from backend.models import BackgroundJob

    def boom(db, payload, context):
        raise RuntimeError("故意失败")

    jobs.HANDLERS["pytest.boom"] = boom
    try:
        # 预算故意只给 1 次：跑一次就判死，正是需要人来点「重试」的状态
        job, created = jobs.submit(db_session, "pytest.boom", {"n": 1}, actor="pytest", max_attempts=1)
        assert created
        jobs.run_job(job.id, db_session)
        row = db_session.get(BackgroundJob, job.id)
        assert row.status == "failed" and row.attempts == 1 and row.next_attempt_at is None
        assert "第 1 次尝试失败：故意失败" in row.error

        retried = await anon_client.post(f"/api/v1/admin/jobs/{job.id}/retry", headers=auth_headers)
        assert retried.status_code == 202, retried.text
        body = retried.json()
        assert body["status"] == "queued"
        # 追加一份预算，编号继续涨（界面显示「第 2/4 次」），而不是把 attempts 归零 ——
        # 归零会让重跑被当成「第一次尝试」，正好丢掉「上次可能已经做完了」这个信息
        assert body["attempts"] == 1 and body["max_attempts"] == 4
        assert "第 1 次尝试失败" in body["error"]  # 上次原因保留，重跑期间界面还能看到

        # 重跑仍然失败，但这次有预算 → 又回到队列
        jobs.run_job(job.id, db_session)
        row = db_session.get(BackgroundJob, job.id)
        assert row.status == "queued" and row.attempts == 2
        assert "第 2 次尝试失败：故意失败" in row.error

        # 排队中的任务再点重试 → 409；未知 id → 404
        assert (await anon_client.post(f"/api/v1/admin/jobs/{job.id}/retry",
                                       headers=auth_headers)).status_code == 409
        assert (await anon_client.post("/api/v1/admin/jobs/99999999/retry",
                                       headers=auth_headers)).status_code == 404

        trail = await anon_client.get("/api/v1/admin/audit?action=job.retry&limit=50", headers=auth_headers)
        assert trail.status_code == 200, trail.text
        entries = [entry for entry in trail.json()["data"] if entry["target_id"] == str(job.id)]
        assert entries and entries[0]["detail"]["kind"] == "pytest.boom"
        assert "故意失败" in entries[0]["detail"]["previous_error"]
    finally:
        jobs.HANDLERS.pop("pytest.boom", None)


async def test_run_job_retries_with_backoff_and_stops_at_budget(anon_client, auth_headers, db_session,
                                                                monkeypatch):
    """⑩ 处理函数抛异常不再等于判死：按预算指数退避重排，用完预算才落 failed。"""
    from backend import jobs
    from backend.models import BackgroundJob

    seen: list[dict] = []

    def boom(db, payload, context):
        seen.append(context)
        raise RuntimeError("故意失败")

    jobs.HANDLERS["pytest.boom"] = boom
    try:
        job, created = jobs.submit(db_session, "pytest.boom", {"n": 1}, actor="pytest", max_attempts=2)
        assert created and job.attempts == 0

        jobs.run_job(job.id, db_session)
        row = db_session.get(BackgroundJob, job.id)
        assert row.status == "queued", row.error  # 还有一次预算 → 放回队列
        assert row.attempts == 1 and row.finished_at is None
        assert row.next_attempt_at is not None
        assert "第 1 次尝试失败：故意失败" in row.error
        assert seen[0]["is_retry"] is False and seen[0]["attempt"] == 1

        jobs.run_job(job.id, db_session)
        row = db_session.get(BackgroundJob, job.id)
        assert row.status == "failed" and row.attempts == 2
        assert row.next_attempt_at is None and row.finished_at is not None
        assert "第 2 次尝试失败：故意失败" in row.error
        # 重跑的那一次带着 is_retry=True：处理函数靠它区分「第一次做」和「上次可能已经做了」
        assert seen[1]["is_retry"] is True and seen[1]["attempt"] == 2

        # 退避：第 1 次重试 2 秒、第 2 次 4 秒……封顶 60 秒（生产路径才真挂定时器，这里换成记录器）
        timers: list[tuple[float, int]] = []
        monkeypatch.setattr(jobs, "SCHEDULER", lambda delay, job_id: timers.append((delay, job_id)))
        row.status = "queued"
        row.max_attempts = 3  # 再给一次预算，才有「第 2 次失败 → 4 秒后重试」这条退避曲线
        db_session.flush()
        assert jobs.mark_failure(db_session, row, RuntimeError("再来一次"),
                                 retry=True, schedule=True) == "queued"
        assert timers == [(4.0, job.id)]
        assert jobs.retry_delay(1) == 2.0 and jobs.retry_delay(2) == 4.0
        assert jobs.retry_delay(6) == 60.0 and jobs.retry_delay(20) == 60.0
    finally:
        jobs.HANDLERS.pop("pytest.boom", None)


# ---------------------------------------------------------------- 结构漂移

async def test_schema_matches_models(require_database):
    """模型加了列却忘了写进 `migrations.COLUMNS` 时，这里就炸，而不是等接口 500。

    这是当初 `financial_alerts.review_status`（`UndefinedColumn`）和 `audit_logs`
    （`relation does not exist`）两次翻车的回归闸门：`create_all()` 只建缺失的**表**，
    不会给已存在的表补**列**，所以补列清单必须手工维护 —— 一旦漏了，本用例把列名点出来。
    索引是同一个坑：`create_all()` 也只在**建表**时建索引，老库上新加的 `index=True` /
    `Index(...)` 必须写进 `migrations.INDEXES`，否则一直在顺序扫而且不报错。
    """
    from backend.migrations import ensure_schema, missing_columns, missing_indexes

    ensure_schema()
    assert missing_columns() == []
    assert missing_indexes() == []


async def test_fresh_database_can_be_built_from_migrations_alone(require_database, db_session):
    """从零建的库（CI、换台机器、新同事）必须也能建全表 —— 本地老库看不出这个坑。

    两件事都只有空库才暴露，而在本地被 `scripts/init_pgvector_db.py` 手工做掉了：

    1. **表要注册进 metadata 才会被 `create_all()` 建出来**。`document_chunks` 定义在
       `backend/rag/pgvector_store.py`，而 `migrations.py` 只 `import backend.models`，
       于是「只导入 migrations」的路径（空库、一次性脚本）少建这张表，症状是启动日志一条 error、
       过一阵 RAG 接口才报 `relation "document_chunks" does not exist`。`AuditLog` 当初是同一个坑。
    2. **`embedding VECTOR(384) NOT NULL` 要求 `vector` 扩展先在**，否则建表直接
       `type "vector" does not exist`，整个 `ensure_schema()` 半途而废（前面 8 张表建了、知识库表没有）。

    这里用一个子进程只 `import backend.migrations`（不靠本文件已导入的其它模块帮忙）证明第 1 条，
    再在真库上核对第 2 条：CI 的 job 会在**空库**上跑整套测试，所以这条断言在本地绿、在空库红是没用的
    —— 它必须两边都成立，靠的是 `migrations.ensure_pgvector()` 自己把扩展装上。
    """
    import subprocess
    import tempfile

    from sqlalchemy import text

    from backend.migrations import ensure_pgvector, has_pgvector
    from backend.models import Base

    probe = (
        "import json, sys\n"
        "import backend.migrations  # noqa: F401  只导入它，看表注册全不全\n"
        "from backend.models import Base\n"
        "json.dump(sorted(Base.metadata.tables), open(sys.argv[1], 'w', encoding='utf-8'))\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        out_path = Path(tmp) / "tables.json"
        completed = subprocess.run([sys.executable, "-c", probe, str(out_path)],
                                   cwd=str(PROJECT_ROOT), capture_output=True, text=True)
        assert completed.returncode == 0, completed.stderr
        tables = json.loads(out_path.read_text(encoding="utf-8"))

    assert "document_chunks" in tables, f"只导入 migrations 时注册到的表：{tables}"
    assert "document_chunks" in Base.metadata.tables

    assert has_pgvector() is True or ensure_pgvector() is True
    assert has_pgvector() is True
    assert db_session.execute(text(
        "SELECT 1 FROM pg_extension WHERE extname = 'vector'")).first() is not None


async def test_required_indexes_are_declared_and_present(require_database, db_session):
    """索引要在三处对齐：模型的 `Index(...)`（新库）、`migrations.INDEXES`（老库）、真库的 `pg_indexes`。

    真跑出来的教训：`ix_financial_alerts_review_status` 与 `ix_financial_transactions_reversal_of_id`
    都是「先有表、后加 `index=True` 列」，`create_all()` 不会补 → 真库上一直没有这两条索引，
    按状态筛预警、按冲销来源找原行都在顺序扫（演示库 8909 行时不明显，数据一多就是全表扫）。
    这里同时卡住列顺序：`(department, transaction_date)` 反了规划器就用不上第二列。
    """
    from sqlalchemy import text

    from backend.migrations import INDEXES, missing_indexes
    from backend.models import FinancialAlert, FinancialTransaction

    declared = {index.name: [column.name for column in index.columns]
                for index in FinancialTransaction.__table__.indexes}
    assert declared["ix_financial_transactions_department_date"] == ["department", "transaction_date"]

    # 手工补索引清单必须包含这三条，且列清单与模型一致（不然老库补出来的索引和模型不是一回事）
    listed = {name: columns for _table, name, columns, _why in INDEXES}
    assert listed["ix_financial_transactions_department_date"] == "department, transaction_date"
    assert "ix_financial_transactions_reversal_of_id" in listed
    assert "ix_financial_alerts_review_status" in listed
    assert FinancialAlert.__table__.indexes, "预警表也得有索引，否则这条清单没有意义"

    definitions = dict(db_session.execute(text(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = current_schema()")).all())
    transaction_definition = definitions.get("ix_financial_transactions_department_date", "")
    assert "department" in transaction_definition and "transaction_date" in transaction_definition
    assert transaction_definition.index("department") < transaction_definition.index("transaction_date")
    assert "ix_financial_transactions_reversal_of_id" in definitions
    assert "ix_financial_alerts_review_status" in definitions
    assert missing_indexes() == []


async def test_transaction_list_period_filter_stays_sargable(anon_client, auth_headers, db_session):
    """明细列表的期间过滤必须发成区间比较，不许退回 `to_char(transaction_date) = '...'`。

    抓接口**实际发出去的 SQL** 来卡形状（不是断言源码里有没有那个词）：函数套在列上，
    `(department, transaction_date)` 的第二列就用不上，而且每行都要算一次 `to_char`。
    最后的期间下拉（`periods_in`）仍然按 `to_char` 分组去重 —— 那是取候选值，不是按期间筛行。
    """
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    _seed_row(db_session, day=6)
    seen: list[str] = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        seen.append(" ".join(statement.split()))

    event.listen(Engine, "before_cursor_execute", record)
    try:
        response = await anon_client.get(f"/api/v1/financial/transactions?period={TX_PERIOD}",
                                         headers=auth_headers)
    finally:
        event.remove(Engine, "before_cursor_execute", record)

    assert response.status_code == 200, response.text
    listing_sql = [sql for sql in seen if "FROM financial_transactions" in sql
                   and "ORDER BY financial_transactions.transaction_date DESC" in sql]
    assert len(listing_sql) == 1, f"没抓到唯一的一条明细列表查询：{listing_sql}"
    assert "to_char" not in listing_sql[0].lower(), listing_sql[0]
    assert "financial_transactions.transaction_date >=" in listing_sql[0], listing_sql[0]
    assert "financial_transactions.transaction_date <" in listing_sql[0], listing_sql[0]


# ---------------------------------------------------------------- 出口配额（导出 / 问答）

async def test_export_quota_returns_429_and_does_not_touch_ai(anon_client, auth_headers, stub_retrieval):
    """导出打满后返回 429 + Retry-After；问答配额是另一本账，不受影响。"""
    from backend import ratelimit

    ratelimit.install_quota("export", 2, 60)
    ratelimit.install_quota("ai", 5, 60)
    try:
        for _ in range(2):
            ok = await anon_client.get("/api/v1/export/metrics", headers=auth_headers)
            assert ok.status_code == 200, ok.text
        blocked = await anon_client.get("/api/v1/export/metrics", headers=auth_headers)
        assert blocked.status_code == 429, blocked.text
        assert "导出过于频繁" in blocked.json()["detail"]
        assert int(blocked.headers["Retry-After"]) >= 1
        # 三个导出接口共用一本账（不能靠换路径绕过）
        assert (await anon_client.get("/api/v1/export/alerts", headers=auth_headers)).status_code == 429
        # 问答走另一本账
        assert (await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                       json={"query": "奖金方案", "top_k": 5})).status_code == 200
        # 计数按账号分桶：本用例只写了当前账号 + 一个配额名，不写审计（429 写审计会被拿来放大）
        assert ratelimit.tracked_buckets(prefix="export:") == 1
        # 被拒的那两次（429）不落库，桶里就是那两次成功的导出
        assert ratelimit.snapshot(prefix="export:")[0]["hits"] == 2
    finally:
        ratelimit.reset_quotas()


async def test_ai_quota_covers_search_answer_and_agent(anon_client, auth_headers, stub_retrieval, monkeypatch):
    """问答配额：/rag/search、/rag/answer、/ai/* 共用一本账，超限同样是 429 + Retry-After。"""
    from backend import ratelimit

    monkeypatch.setattr("backend.api_rag.generate_answer", lambda question, results: ("依据证据的回答", []))
    monkeypatch.setattr("backend.api_ai.rag_answer",
                        lambda question, **kwargs: {"answer": "依据证据的回答", "sources": []})
    ratelimit.install_quota("ai", 3, 60)
    try:
        first = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                       json={"query": "奖金方案", "top_k": 5})
        assert first.status_code == 200, first.text
        second = await anon_client.post("/api/v1/rag/answer", headers=auth_headers,
                                        json={"query": "差旅费标准", "top_k": 5})
        assert second.status_code == 200, second.text
        third = await anon_client.post("/api/v1/ai/rag/chat", headers=auth_headers,
                                       json={"question": "差旅费标准是多少？"})
        assert third.status_code == 200, third.text
        blocked = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                         json={"query": "奖金方案", "top_k": 5})
        assert blocked.status_code == 429, blocked.text
        assert "问答过于频繁" in blocked.json()["detail"]
        assert int(blocked.headers["Retry-After"]) >= 1
        # 导出不受影响
        assert (await anon_client.get("/api/v1/export/metrics", headers=auth_headers)).status_code == 200
    finally:
        ratelimit.reset_quotas()


async def test_rate_limit_counts_live_in_the_database_not_in_memory(require_database):  # noqa: ARG001
    """㉑ 的核心承诺：计数在库里，多个 worker / 重启后共享同一本账。

    这里刻意不走 HTTP：`consume()` 每次都用**自己的会话并立即提交**（和生产里一样），
    所以第三次「另一个进程」读到的仍是同一个窗口 —— 换成进程内的 `deque`，多 worker 下
    每个 worker 各算 10 次、重启清零，这条断言就会红。
    """
    from backend import ratelimit

    key = f"restart_{uuid.uuid4().hex[:8]}@10.1.2.3"
    ratelimit.install_quota("login", 2, 60)
    try:
        assert [ratelimit.consume("login", key).allowed for _ in range(2)] == [True, True]
        third = ratelimit.consume("login", key)
        assert third.allowed is False and third.retry_after >= 1
        # 被拒的那次没落库：窗口里就是那两次尝试
        rows = ratelimit.snapshot(prefix=f"login:{key}")
        assert len(rows) == 1 and rows[0]["hits"] == 2, rows
        # reset 只清这一个桶；purge 按前缀清（真机脚本与用例夹具靠它收拾现场）
        assert ratelimit.reset("login", key) == 2
        assert ratelimit.consume("login", key).allowed is True
        assert ratelimit.tracked_buckets(prefix=f"login:{key}") == 1
        assert ratelimit.purge(bucket_prefix=f"login:{key}") == 1
        assert ratelimit.tracked_buckets(prefix=f"login:{key}") == 0
    finally:
        ratelimit.reset_quotas()
        ratelimit.purge(bucket_prefix=f"login:{key}")


async def test_quota_registry_reports_config_and_rejects_unknown_name():
    """配额登记表：未登记的名字要显式报错，reset 后回到配置里的默认值。"""
    from backend import ratelimit
    from backend.config import get_settings

    ratelimit.reset_quotas()
    try:
        with pytest.raises(KeyError):
            ratelimit.quota("nope")
        with pytest.raises(KeyError):
            ratelimit.install_quota("nope", 1, 1)
        settings = get_settings()
        assert ratelimit.quota("export") == (settings.export_max_per_minute, ratelimit.LIMIT_WINDOW_SECONDS)
        assert ratelimit.quota("ai") == (settings.ai_max_per_minute, ratelimit.LIMIT_WINDOW_SECONDS)
        assert ratelimit.quota("login") == (settings.login_max_attempts, float(settings.login_window_seconds))
        # 覆盖只换「上限 + 窗口」这两条策略，不动库里的计数（计数是事实，不是缓存）
        assert ratelimit.install_quota("export", 1, 30) == (1, 30.0)
        assert ratelimit.quota("export") == (1, 30.0)
        assert ratelimit.all_quotas()["export"] == {"limit": 1, "window_seconds": 30.0}
    finally:
        ratelimit.reset_quotas()
    assert ratelimit.quota("export") == (get_settings().export_max_per_minute, ratelimit.LIMIT_WINDOW_SECONDS)


# ---------------------------------------------------- 季度/年度落库（financial_rollups）

ROLLUP_QUARTER = "2098-Q2"
ROLLUP_MONTHS = ("2098-04", "2098-05", "2098-06")
# 缺月清单的种子：三季度的三个月只播两个月（且两个口径各一行），
# 于是「缺月清单」的内容完全由用例自己决定，不依赖演示库里恰好有一行不完整的 2026 年度。
INCOMPLETE_QUARTER = "2098-Q3"
INCOMPLETE_MONTHS = ("2098-07", "2098-08")
ROLLUP_TEST_YEAR = "2098"


def _seed_department_metric(db, period: str, *, department: str = "部门A") -> None:
    """给某个部门插一行月指标（落库行按 department 分组，缺月清单才有「部门行」那一半）。"""
    from backend.models import FinancialMetric

    revenue, expense = Decimal("100.00"), Decimal("10.00")
    net = revenue - expense
    db.add(FinancialMetric(
        period=period, department=department, revenue_amount=revenue, cost_amount=Decimal("0"),
        expense_amount=expense, gross_profit_amount=revenue, net_profit_amount=net,
        gross_profit_ratio=Decimal("1.0000"),
        net_profit_ratio=(net / revenue).quantize(Decimal("0.0001")),
    ))
    db.commit()


def _seed_company_rows(db, period: str, rows) -> None:
    """按明细播种、部门留空（全公司口径）。

    不能用手写指标的方式播种：`calculate()` 是「先删后建」，它按明细重算，
    手写进去的指标行会被删掉，落库合计就变成 0 了（第一版用例就栽在这里）。
    """
    from backend.models import FinancialTransaction

    for index, (revenue, cost, expense) in enumerate(rows):
        db.add(FinancialTransaction(
            transaction_date=f"{period}-{index + 1:02d}",
            account_name="测试科目", account_code="6001", department=None,
            description="rollup 种子", revenue_amount=Decimal(revenue),
            cost_amount=Decimal(cost), expense_amount=Decimal(expense),
            cash_in_amount=Decimal("0"), cash_out_amount=Decimal("0"),
            import_batch="pytest-seed",
        ))
    db.flush()


def _rollup_rows(db, period: str):
    from backend.models import FinancialRollup

    return list(db.scalars(
        select(FinancialRollup).where(FinancialRollup.period == period)
        .order_by(FinancialRollup.id)).all())


def _drop_rollups(db) -> None:
    """清掉本用例涉及的落库行。

    2098/2099 两个年份不属于别的用例（演示数据是 2026），但落库行是派生数据、
    会跨用例累积，所以每个用例自己清一遍。
    """
    from sqlalchemy import delete

    from backend.models import FinancialRollup

    for prefix in ("2098", "2099"):
        db.execute(delete(FinancialRollup).where(FinancialRollup.period.like(f"{prefix}%")))
    db.commit()


async def test_calculate_refreshes_quarter_rollup_and_year_reports_missing_months(anon_client, auth_headers, db_session):
    """重算某个月必须顺手刷新它所在的季与年，年度合计要如实点名缺了哪几个月。"""
    _drop_rollups(db_session)
    for month in ROLLUP_MONTHS:
        _seed_company_rows(db_session, month, [("1000.00", "0.00", "200.00")])

    for month in ROLLUP_MONTHS:
        response = await anon_client.post(f"/api/v1/analysis/calculate?period={month}", headers=auth_headers)
        assert response.status_code == 200, response.text

    # 落库行是 calculate 自己刷出来的（不需要手工点重算）
    rows = _rollup_rows(db_session, ROLLUP_QUARTER)
    assert [row.period for row in rows] == [ROLLUP_QUARTER]
    assert rows[0].grain == "quarter"
    assert rows[0].department is None              # 全公司行
    assert rows[0].months == list(ROLLUP_MONTHS)
    assert Decimal(rows[0].revenue_amount) == Decimal("3000.00")
    assert rows[0].complete is True                # 二季度的三个月都在

    # 年度行也一起刷了：2098 只有这三个月，必须标成不完整并点名缺月
    years = _rollup_rows(db_session, "2098")
    assert [row.grain for row in years] == ["year"]
    assert years[0].complete is False
    assert years[0].month_count == 3


async def test_quarter_summary_reads_stored_rollup_and_year_is_incomplete(anon_client, auth_headers, db_session):
    """`/analysis/summary` 对季/年读落库行，并把「这一期是不是凑齐」说清楚。"""
    _drop_rollups(db_session)
    for month in ROLLUP_MONTHS:
        _seed_company_rows(db_session, month, [("1000.00", "0.00", "200.00")])
        await anon_client.post(f"/api/v1/analysis/calculate?period={month}", headers=auth_headers)

    quarter = await anon_client.get(f"/api/v1/analysis/summary?period={ROLLUP_QUARTER}", headers=auth_headers)
    assert quarter.status_code == 200, quarter.text
    body = quarter.json()
    assert body["source"] == "stored", body
    assert body["grain"] == "quarter"
    assert body["grain_label"] == "季"
    assert body["period_label"] == "2098 年第 2 季度"
    assert Decimal(str(body["revenue_amount"])) == Decimal("3000.00")
    assert Decimal(str(body["net_profit_amount"])) == Decimal("2400.00")   # 3 × (1000 - 200)
    assert body["months"] == list(ROLLUP_MONTHS)
    assert body["complete"] is True                 # 二季度的三个月都在
    assert body["missing_months"] == []

    year = await anon_client.get("/api/v1/analysis/summary?period=2098", headers=auth_headers)
    assert year.status_code == 200, year.text
    payload = year.json()
    assert payload["source"] == "stored"
    assert payload["grain"] == "year"
    assert payload["complete"] is False
    assert payload["months"] == list(ROLLUP_MONTHS)
    assert payload["covered_months"] == [f"2098-{month:02d}" for month in range(1, 13)]
    assert set(payload["missing_months"]) == {f"2098-{month:02d}" for month in (1, 2, 3, 7, 8, 9, 10, 11, 12)}

    # 月口径不受落库影响，仍读指标表
    month = await anon_client.get("/api/v1/analysis/summary?period=2098-05", headers=auth_headers)
    month_body = month.json()
    assert month_body["source"] == "month", month_body
    assert Decimal(str(month_body["revenue_amount"])) == Decimal("1000.00")
    assert month_body["missing_months"] == []


async def test_rollup_without_monthly_metrics_is_computed_not_invented(anon_client, auth_headers, db_session):
    """落库还没覆盖这一期时按同一算法现算并标 computed，绝不凭空造一个合计。"""
    _drop_rollups(db_session)
    # 只写月指标、不调 calculate：模拟「老库刚升级，季/年还没落库」
    _seed_metric(db_session, "2099-03", revenue="500.00", expense="0.00")

    response = await anon_client.get("/api/v1/analysis/summary?period=2099-Q1", headers=auth_headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["source"] == "computed", body
    assert Decimal(str(body["revenue_amount"])) == Decimal("500.00")
    assert body["months"] == ["2099-03"]
    assert body["missing_months"] == ["2099-01", "2099-02"]
    assert body["complete"] is False
    assert _rollup_rows(db_session, "2099-Q1") == []      # 读路径不写库

    empty = await anon_client.get("/api/v1/analysis/summary?period=2099-Q4", headers=auth_headers)
    assert empty.json()["source"] == "empty"
    assert empty.json()["has_data"] is False


async def test_admin_rollup_rebuild_is_audited_and_admin_only(anon_client, api_app, auth_headers, db_session):
    """管理端重算落库：回填/修数用，写审计；受限账号 403。"""
    from sqlalchemy import delete

    from backend.models import FinancialMetric, FinancialRollup

    _drop_rollups(db_session)
    for month in ROLLUP_MONTHS:
        _seed_metric(db_session, month, revenue="1000.00", expense="200.00")
    # 一个注定「不完整」的季度：三个月只播两个月，且全公司与部门各一行
    for month in INCOMPLETE_MONTHS:
        _seed_metric(db_session, month, revenue="500.00", expense="100.00")
        _seed_department_metric(db_session, month)

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        forbidden = await scoped.post("/api/v1/admin/rollups/rebuild", headers=headers, json={})
        assert forbidden.status_code == 403
    finally:
        await scoped.aclose()

    listing = await anon_client.get("/api/v1/admin/rollups", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    assert [item["value"] for item in listing.json()["grains"]] == ["quarter", "year"]

    rebuilt = await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                     json={"periods": [ROLLUP_QUARTER, INCOMPLETE_QUARTER]})
    assert rebuilt.status_code == 200, rebuilt.text
    payload = rebuilt.json()
    assert payload["rows"] == 3            # 二季度全公司 1 行 + 三季度全公司与部门各 1 行
    assert payload["data"][0]["period"] == ROLLUP_QUARTER
    assert payload["data"][0]["revenue_amount"] == 3000.0
    assert payload["data"][0]["complete"] is True
    assert payload["data"][0]["note"] == "已覆盖全部 3 个月"
    assert ROLLUP_QUARTER in payload["coverage"]["quarters"]

    # 缺月清单要说清「是哪一行缺月」：全公司行与部门行都可能缺月，页面上不能只给期间标签。
    # 只看本用例自己的年份：演示库里本来就有不完整的 2026 年度行，跟着它断言等于没断言
    # （空库上跑这条会直接挂掉——CI 上就是这么发现的）。
    incomplete = [item for item in payload["coverage"]["incomplete"]
                  if item["period"].startswith(ROLLUP_TEST_YEAR)]
    assert {(item["grain"], item["period"], item["department"]) for item in incomplete} == {
        ("quarter", INCOMPLETE_QUARTER, ""), ("quarter", INCOMPLETE_QUARTER, "部门A")}, incomplete
    assert all(item["missing_months"] == ["2098-09"] for item in incomplete), incomplete
    assert all({"department_label", "period_label"} <= set(item) for item in incomplete)
    for key in {(item["grain"], item["period"]) for item in incomplete}:
        group = [item["department"] for item in incomplete if (item["grain"], item["period"]) == key]
        assert group == ["", "部门A"], group      # 全公司行排在同期的部门行前面

    # 期间与 grains 对不上、grains 本身不合法都要 422，而不是静默什么都不算
    bad = await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                 json={"periods": ["2098-04"]})
    assert bad.status_code == 422, bad.text
    assert (await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                   json={"grains": ["month"]})).status_code == 422

    # 撤销/缺数以后重算要把落库行删掉，不能留一个凭空的季度合计
    db_session.execute(delete(FinancialMetric).where(FinancialMetric.period.in_(ROLLUP_MONTHS)))
    db_session.commit()
    again = await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                   json={"periods": [ROLLUP_QUARTER]})
    assert again.json()["rows"] == 0
    assert db_session.scalars(select(FinancialRollup).where(
        FinancialRollup.period == ROLLUP_QUARTER)).all() == []

    rows = await _audit(anon_client, auth_headers, action="rollups.rebuild", limit=10)
    entry = _find(rows["data"], "rollups.rebuild")
    assert entry is not None, rows
    assert ROLLUP_QUARTER in entry["detail"]["periods"]
    assert entry["detail"]["grains"] == ["quarter", "year"]


# ------------------------------------------- 明细单行更正与冲销（⑨）

TX_PERIOD = "2097-12"


def _seed_row(db, *, period: str = TX_PERIOD, day: int = 1, department: str | None = "部门-097",
              revenue: str = "100.00", expense: str = "20.00", batch: str = "pytest-seed",
              account_name: str = "测试科目", description: str = "pytest 种子"):
    """插一行明细，返回 ORM 行（id 已 flush，可用于拼 URL）。"""
    from backend.models import FinancialTransaction

    row = FinancialTransaction(
        transaction_date=datetime.fromisoformat(f"{period}-{day:02d}"), account_name=account_name,
        account_code="6001", department=department, description=description,
        revenue_amount=Decimal(revenue),
        cost_amount=Decimal("0"), expense_amount=Decimal(expense), cash_in_amount=Decimal("0"),
        cash_out_amount=Decimal("0"), import_batch=batch,
    )
    db.add(row)
    db.flush()
    return row


def _company_metric(db, period: str = TX_PERIOD):
    return db.scalars(select(FinancialMetric).where(
        FinancialMetric.period == period, FinancialMetric.department.is_(None))).one()


async def test_transaction_list_is_scoped_and_filtered(anon_client, api_app, auth_headers, db_session):
    mine = _seed_row(db_session, day=2, department="部门-097")
    other = _seed_row(db_session, day=3, department="部门-999", revenue="500.00")
    company = _seed_row(db_session, day=4, department=None, revenue="700.00")

    listing = await anon_client.get(f"/api/v1/financial/transactions?period={TX_PERIOD}",
                                    headers=auth_headers)
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["total"] == 3
    assert {item["id"] for item in body["data"]} == {mine.id, other.id, company.id}
    assert TX_PERIOD in body["periods"]
    assert body["scope"]["unrestricted"] is True
    # 列表里的行要能看出「冲没冲销、改过几次」，前端据此决定按钮状态
    assert all({"reversed", "is_reversal", "corrections", "period"} <= set(item) for item in body["data"])

    paged = await anon_client.get("/api/v1/financial/transactions?query=测试科目&limit=2&offset=0",
                                  headers=auth_headers)
    assert paged.json()["total"] == 3, "total 是筛选后的总数，不是当前页长度"
    assert len(paged.json()["data"]) == 2
    assert (await anon_client.get("/api/v1/financial/transactions?batch=不存在的批次",
                                  headers=auth_headers)).json()["total"] == 0

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门-097")
    try:
        scoped_list = await scoped.get(f"/api/v1/financial/transactions?period={TX_PERIOD}", headers=headers)
        assert scoped_list.status_code == 200, scoped_list.text
        assert [item["id"] for item in scoped_list.json()["data"]] == [mine.id]
        # 越界口径要 403，而不是安静地返回空列表（空列表会被读成「这个部门没数据」）
        forbidden = await scoped.get("/api/v1/financial/transactions?department=部门-999", headers=headers)
        assert forbidden.status_code == 403
        assert "不能查看" in forbidden.json()["detail"]
        # 单行详情同样守范围
        assert (await scoped.get(f"/api/v1/financial/transactions/{other.id}",
                                 headers=headers)).status_code == 403
        detail = await scoped.get(f"/api/v1/financial/transactions/{mine.id}", headers=headers)
        assert detail.status_code == 200, detail.text
        assert detail.json()["data"]["id"] == mine.id
    finally:
        await scoped.aclose()

    assert (await anon_client.get("/api/v1/financial/transactions/99999999",
                                  headers=auth_headers)).status_code == 404


async def test_transaction_correction_updates_metric_and_is_audited(anon_client, auth_headers, db_session):
    row = _seed_row(db_session, day=5, department="部门-097", revenue="100.00", expense="20.00")
    await anon_client.post(f"/api/v1/analysis/calculate?period={TX_PERIOD}", headers=auth_headers)
    assert Decimal(str(_company_metric(db_session).revenue_amount)) == Decimal("100.00")

    fixed = await anon_client.patch(f"/api/v1/financial/transactions/{row.id}", headers=auth_headers,
                                    json={"revenue_amount": "250.50", "description": "更正后"})
    assert fixed.status_code == 200, fixed.text
    body = fixed.json()
    assert body["data"]["revenue_amount"] == 250.5
    assert body["data"]["description"] == "更正后"
    assert body["data"]["corrections"] == 1
    assert body["recalculated"] == [TX_PERIOD], "改完必须重算这一期，否则看板还是旧指标"

    db_session.expire_all()
    assert Decimal(str(_company_metric(db_session).revenue_amount)) == Decimal("250.50")

    rows = await _audit(anon_client, auth_headers, action="transaction.update", limit=10)
    entry = _find(rows["data"], "transaction.update", str(row.id))
    assert entry is not None, rows
    assert entry["detail"]["before"]["revenue_amount"] == "100.00"
    assert entry["detail"]["after"]["revenue_amount"] == "250.50"
    assert entry["detail"]["periods"] == [TX_PERIOD]

    # 把日期改到另一期：原期间没数据了要被清掉，新期间要算出来
    moved = await anon_client.patch(f"/api/v1/financial/transactions/{row.id}", headers=auth_headers,
                                   json={"transaction_date": "2097-11-03"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["data"]["period"] == "2097-11"
    assert sorted(moved.json()["recalculated"]) == ["2097-11"]
    assert moved.json()["cleared"] == [TX_PERIOD]


async def test_transaction_reversal_adds_negative_row_and_keeps_original(anon_client, auth_headers, db_session):
    row = _seed_row(db_session, day=6, department="部门-097", revenue="100.00", expense="20.00")
    await anon_client.post(f"/api/v1/analysis/calculate?period={TX_PERIOD}", headers=auth_headers)
    me = (await anon_client.get("/api/v1/auth/me", headers=auth_headers)).json()

    response = await anon_client.post(f"/api/v1/financial/transactions/{row.id}/reverse",
                                      headers=auth_headers, json={"reason": "凭证重复"})
    assert response.status_code == 200, response.text
    body = response.json()
    original, reversal = body["original"], body["reversal"]
    assert original["id"] == row.id and original["reversed"] is True
    assert original["reversed_by"] == me["username"]
    assert reversal["revenue_amount"] == -100.0
    assert reversal["expense_amount"] == -20.0
    assert reversal["is_reversal"] is True and reversal["reversal_of_id"] == row.id
    assert reversal["import_batch"] == original["import_batch"], "冲销行要留在原批次里，否则撤销批次只收回一半"
    assert reversal["description"] == "冲销：pytest 种子"

    # 原行没有被删掉：两行都在，合计抵消成 0
    listing = await anon_client.get(f"/api/v1/financial/transactions?period={TX_PERIOD}&query=pytest 种子",
                                   headers=auth_headers)
    assert listing.json()["total"] == 2
    db_session.expire_all()
    assert Decimal(str(_company_metric(db_session).revenue_amount)) == Decimal("0.00")

    # 冲销过的行不能改；冲销行不能再冲销；同一行不能重复冲销
    blocked = await anon_client.patch(f"/api/v1/financial/transactions/{row.id}", headers=auth_headers,
                                      json={"revenue_amount": "1.00"})
    assert blocked.status_code == 409, blocked.text
    assert "已经冲销" in blocked.json()["detail"]
    assert (await anon_client.post(f"/api/v1/financial/transactions/{row.id}/reverse",
                                   headers=auth_headers, json={})).status_code == 409
    again = await anon_client.post(f"/api/v1/financial/transactions/{reversal['id']}/reverse",
                                   headers=auth_headers, json={})
    assert again.status_code == 409, again.text
    assert "冲销行" in again.json()["detail"]

    rows = await _audit(anon_client, auth_headers, action="transaction.reverse", limit=10)
    entries = [item for item in rows["data"] if item["target_id"] == str(row.id)]
    assert len(entries) == 1, f"一次冲销只该留一条审计：{entries}"
    assert entries[0]["detail"]["reversal_id"] == reversal["id"]
    assert entries[0]["detail"]["reason"] == "凭证重复"


async def test_transaction_correction_permissions_and_validation(anon_client, api_app, auth_headers, db_session):
    mine = _seed_row(db_session, day=7, department="部门-097")
    other = _seed_row(db_session, day=8, department="部门-999")
    company = _seed_row(db_session, day=9, department=None)

    headers, scoped = await _scoped_headers(anon_client, api_app, auth_headers, "部门-097")
    try:
        # 别人的部门、以及「没有部门的全公司行」，受限账号都不能动（与读路径同一套口径）
        assert (await scoped.patch(f"/api/v1/financial/transactions/{other.id}", headers=headers,
                                   json={"revenue_amount": "1.00"})).status_code == 403
        assert (await scoped.patch(f"/api/v1/financial/transactions/{company.id}", headers=headers,
                                   json={"revenue_amount": "1.00"})).status_code == 403
        assert (await scoped.post(f"/api/v1/financial/transactions/{company.id}/reverse",
                                  headers=headers, json={})).status_code == 403
        allowed = await scoped.patch(f"/api/v1/financial/transactions/{mine.id}", headers=headers,
                                     json={"revenue_amount": "150.00"})
        assert allowed.status_code == 200, allowed.text
    finally:
        await scoped.aclose()

    # 校验类错误一律 422，且消息是给人看的
    base = f"/api/v1/financial/transactions/{company.id}"
    bad_amount = await anon_client.patch(base, headers=auth_headers, json={"revenue_amount": "abc"})
    assert bad_amount.status_code == 422 and "有效金额" in bad_amount.json()["detail"]
    assert (await anon_client.patch(base, headers=auth_headers, json={})).status_code == 422
    assert (await anon_client.patch(base, headers=auth_headers,
                                    json={"transaction_date": "不是日期"})).status_code == 422
    # 批次号是文件哈希，不允许通过更正改掉
    assert (await anon_client.patch(base, headers=auth_headers,
                                    json={"import_batch": "pytest-other"})).status_code == 422
    same = await anon_client.patch(f"/api/v1/financial/transactions/{mine.id}", headers=auth_headers,
                                  json={"revenue_amount": "150.00"})
    assert same.status_code == 422 and "一致" in same.json()["detail"]
    assert (await anon_client.patch("/api/v1/financial/transactions/99999999", headers=auth_headers,
                                    json={"revenue_amount": "1.00"})).status_code == 404


# ------------------------------------------- 并发重算互斥（期间锁，②）

LOCK_SQL = "pg_advisory_xact_lock"


def _capture_locks():
    """抓「发出去的 SQL + 绑定参数」，返回 (seen, stop)。调用方必须在 finally 里 stop()。"""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    seen: list[tuple[str, dict]] = []

    def record(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((" ".join(statement.split()), dict(parameters) if parameters else {}))

    event.listen(Engine, "before_cursor_execute", record)
    return seen, lambda: event.remove(Engine, "before_cursor_execute", record)


def _lock_state(seen, candidates) -> tuple[list[str], list[int]]:
    """把抓到的加锁语句还原成 (按加锁顺序的期间名, 不在候选全集里的 key)。

    锁语句里只有算好的 key，所以要把候选期间（含各自的季/年）算成 key 才能反查回来。
    第二个返回值是「本次锁了候选之外的期间」——出现它说明锁集合与预期不符，用例应当报出来。
    """
    from backend.locks import lock_key, period_lock_set

    names = {lock_key(name): name for name in period_lock_set(candidates)}
    order: list[str] = []
    unknown: list[int] = []
    for statement, params in seen:
        if LOCK_SQL not in statement:
            continue
        key = params.get("key")
        if key in names:
            order.append(names[key])
        else:
            unknown.append(key)
    return order, unknown


async def test_calculate_takes_period_lock_including_quarter_and_year(anon_client, auth_headers, db_session):
    """重算某个月必须在**改数据之前**持有「这个月 + 它所在的季与年」的事务锁。

    断言的是接口真发出去的 `SELECT pg_advisory_xact_lock(:key)`（不是源码里有没有这个词）：
    重算是「读明细 → 删旧指标/预警 → 重写」的读改写，两个并发请求会各写一份少算对方数据的
    结果而且不报错。季与年也要锁，因为这次重算会连带刷新它们的落库行。
    """
    _seed_row(db_session, day=5)
    seen, stop = _capture_locks()
    try:
        response = await anon_client.post(f"/api/v1/analysis/calculate?period={TX_PERIOD}",
                                          headers=auth_headers)
    finally:
        stop()

    assert response.status_code == 200, response.text
    locks_taken, unknown = _lock_state(seen, [TX_PERIOD])
    assert unknown == [], f"锁了预期之外的期间：{unknown}"
    assert set(locks_taken) == {TX_PERIOD, "2097-Q4", "2097"}, locks_taken
    # 加锁必须在「删指标」之前：改完才锁，另一个重算这一期的请求已经读到旧数据了
    first_lock = next(index for index, (sql, _p) in enumerate(seen) if LOCK_SQL in sql)
    first_delete = next(index for index, (sql, _p) in enumerate(seen)
                        if sql.startswith("DELETE FROM financial_metrics"))
    assert first_lock < first_delete, seen


async def test_correction_locks_both_periods_before_touching_the_row(anon_client, auth_headers, db_session):
    """更正把一行搬到别的月份时，新旧两期要**一起**锁上，且都在改行之前。

    只锁旧期（或改完再补锁新期）等于没锁：并发重算旧期的请求仍可能读到改到一半的行集合；
    分两次取锁还会与「反向搬运」的另一个请求互相等成 Postgres deadlock detected。
    """
    row = _seed_row(db_session, day=6)
    seen, stop = _capture_locks()
    try:
        response = await anon_client.patch(
            f"/api/v1/financial/transactions/{row.id}", headers=auth_headers,
            json={"transaction_date": "2097-11-02"})
    finally:
        stop()

    assert response.status_code == 200, response.text
    locks_taken, unknown = _lock_state(seen, [TX_PERIOD, "2097-11"])
    assert unknown == [], f"锁了预期之外的期间：{unknown}"
    assert {"2097-11", "2097-12"} <= set(locks_taken), locks_taken
    first_lock = next(index for index, (sql, _p) in enumerate(seen) if LOCK_SQL in sql)
    first_update = next(index for index, (sql, _p) in enumerate(seen)
                        if sql.startswith("UPDATE financial_transactions"))
    assert first_lock < first_update, seen


async def test_import_takes_all_period_locks_before_inserting(anon_client, auth_headers, tmp_path, monkeypatch):
    """一份文件涉及多期时，要**一次性**把全集锁全，再开始插行。

    一圈循环里逐期加锁是并发版的经典坑：两个多期请求各持一半、又都在等对方剩下的那半，
    Postgres 会掐掉一个并报 deadlock detected。这里卡住顺序：先锁，后 INSERT。
    """
    monkeypatch.setattr("backend.api_financial.get_settings",
                        lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    payload = _csv_bytes("2097-01-03,6001,一月,部门-097,跨期导入,100,0,0,0,0\n"
                         "2097-02-04,6001,二月,部门-097,跨期导入,200,0,0,0,0")
    seen, stop = _capture_locks()
    try:
        response = await anon_client.post("/api/v1/financial/import", headers=auth_headers,
                                          files={"file": ("lock.csv", io.BytesIO(payload), "text/csv")})
    finally:
        stop()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    # 导入会多出一把锁：文件内容哈希（`:79` 的查重锁），两把锁各管一件事，都不是多余的
    locks_taken, unknown = _lock_state(seen, ["2097-01", "2097-02"])
    assert unknown == [int(body["batch"][:15], 16)], f"除了去重锁，不该锁别的：{unknown}"

    # 关键点：这次请求**最早**取的那批期间锁必须是「两期 + 各自的季/年」的全集、且按名字排序。
    # 首个 INSERT 已经在第一期重算里面了（calculate 里 flush 才落行），所以这里看的是
    # 「插行之前的加锁序列以全集开头」，而不是「插行前只有这些锁」——每期重算会再重复锁子集。
    expected = ["2097", "2097-01", "2097-02", "2097-Q1"]
    first_insert = next(index for index, (sql, _p) in enumerate(seen)
                        if sql.startswith("INSERT INTO financial_transactions"))
    before_insert, _unknown = _lock_state(seen[:first_insert], ["2097-01", "2097-02"])
    assert before_insert[:len(expected)] == expected, (
        f"插行前第一批锁必须是全集且按排序取锁：{before_insert}")

    # 之后每个期间的重算只会重复锁已持有的子集，不会冒出新的期间（冒出新的就是「逐期取锁」了）
    assert set(locks_taken) == set(expected), (
        f"出现了全集之外的期间锁，说明锁不是一次取全的：{sorted(set(locks_taken) - set(expected))}")
    # 锁都要在插行之前取到（`seen[:first_insert]` 里已经能取到上面那把全集锁）
    first_lock = next(index for index, (sql, _p) in enumerate(seen) if LOCK_SQL in sql)
    assert first_lock < first_insert, seen


async def test_rollup_rebuild_locks_the_quarter_it_rewrites(anon_client, auth_headers, db_session):
    """管理端重算落库同样是「按期先删后建」，所以它也要锁住那些季/年。

    不锁的话，并发的一次月重算会把刚写好的季度合计覆盖成少算一个月的版本（数字变小、无报错）。
    """
    _drop_rollups(db_session)
    for month in ROLLUP_MONTHS:
        _seed_metric(db_session, month, revenue="1000.00", expense="200.00")

    seen, stop = _capture_locks()
    try:
        response = await anon_client.post("/api/v1/admin/rollups/rebuild", headers=auth_headers,
                                          json={"periods": [ROLLUP_QUARTER]})
    finally:
        stop()

    assert response.status_code == 200, response.text
    locks_taken, unknown = _lock_state(seen, ["2098-04", "2098-05", "2098-06"])
    assert unknown == [], f"锁了预期之外的期间：{unknown}"
    assert locks_taken == [ROLLUP_QUARTER], locks_taken
    first_lock = next(index for index, (sql, _p) in enumerate(seen) if LOCK_SQL in sql)
    first_delete = next(index for index, (sql, _p) in enumerate(seen)
                        if sql.startswith("DELETE FROM financial_rollups"))
    assert first_lock < first_delete, seen


# ---------------------------------------------------------------------------
# 会话版本号与强制下线（③）
# ---------------------------------------------------------------------------


def _claims(token: str) -> dict:
    """解开令牌看声明：`ver` 就是签发那一刻的会话版本号。"""
    from jose import jwt as jose_jwt

    from backend.auth import ALGORITHM, SECRET_KEY

    return jose_jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _legacy_token(user_id: int) -> str:
    """手搓一个「本功能上线之前签发的令牌」：没有 `ver` 声明。

    这类令牌必须继续可用到该账号第一次退出登录为止——否则加这个功能等于把所有人踢下线。
    """
    from datetime import datetime, timedelta

    from jose import jwt as jose_jwt

    from backend.auth import ALGORITHM, SECRET_KEY

    expires = datetime.now(UTC) + timedelta(minutes=30)
    return jose_jwt.encode({"sub": str(user_id), "username": "legacy", "exp": expires},
                           SECRET_KEY, algorithm=ALGORITHM)


async def test_token_carries_session_version_and_logout_revokes_it(anon_client, auth_headers):
    """退出登录要把**所有**已签发的令牌作废，而不只是清浏览器里那一份。

    这里的操作者是**普通账号**（自己注册的），读审计那一步才用 `auth_headers`
    这个管理员夹具：⑳ 起「注册出来的账号」不再是管理员，也不能自己升。
    """
    account = {"username": f"token_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=account)).status_code == 200
    login = await anon_client.post("/api/v1/auth/login", json=account)
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    assert login.json()["token_version"] == 0
    assert _claims(token)["ver"] == 0
    headers = _bearer(token)
    me = await anon_client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200 and me.json()["token_version"] == 0

    # 同一个账号再登录一次（换一台设备）：两份令牌的 ver 都是 0，都能用
    other = (await anon_client.post("/api/v1/auth/login", json=account)).json()["access_token"]
    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(other))).status_code == 200

    logout = await anon_client.post("/api/v1/auth/logout", headers=headers)
    assert logout.status_code == 200, logout.text
    assert logout.json() == {"username": account["username"], "token_version": 1, "logged_out": True}

    # 两台设备上的旧令牌一起失效，且报的是「已在别处退出」而不是含糊的「登录已失效」
    for stale in (token, other):
        denied = await anon_client.get("/api/v1/auth/me", headers=_bearer(stale))
        assert denied.status_code == 401, denied.text
        assert "已在别处退出" in denied.json()["detail"]

    # 重新登录拿到 ver=1 的新令牌，可以正常用
    fresh = (await anon_client.post("/api/v1/auth/login", json=account)).json()["access_token"]
    assert _claims(fresh)["ver"] == 1
    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(fresh))).status_code == 200

    # 留痕：谁在什么时候把自己下线了（审计只有管理员能读，用管理员夹具那副令牌）
    rows = (await _audit(anon_client, auth_headers, action="user.logout"))["data"]
    row = _find(rows, "user.logout")
    assert row is not None, rows
    assert row["actor"] == account["username"]
    assert row["action_label"] == "退出登录"
    # 键名不含 "token"：audit._scrub() 会把含 token 的字段整条丢掉（所以叫 session_version）
    assert row["detail"]["session_version"] == 1


async def test_legacy_token_without_version_claim_stays_valid_until_bumped(anon_client, credentials, db_session):
    """没有 `ver` 声明的老令牌按 0 处理：升级当天不会把所有人踢下线，下线后立刻失效。"""
    from backend.models import User

    response = await anon_client.post("/api/v1/auth/register", json=credentials)
    assert response.status_code == 200, response.text
    user_id = response.json()["id"]

    legacy = _legacy_token(user_id)
    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(legacy))).status_code == 200

    # 有人（自己或管理员）把这个账号的会话版本 +1 之后，老令牌也一起作废
    user = db_session.get(User, user_id)
    user.token_version = 1
    db_session.flush()
    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(legacy))).status_code == 401


async def test_admin_can_force_logout_another_account(anon_client, auth_headers):
    """管理员把人踢下线：目标账号的令牌立刻失效，管理员自己的令牌照旧可用。"""
    # 刻意用与 auth_headers 不同的账号：那是夹具自己注册的账号，再注册同名会 409
    victim = {"username": f"kick_{uuid.uuid4().hex[:8]}", "password": "pytest-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=victim)).status_code == 200
    target_token = (await anon_client.post("/api/v1/auth/login", json=victim)).json()["access_token"]
    target_me = await anon_client.get("/api/v1/auth/me", headers=_bearer(target_token))
    assert target_me.status_code == 200, target_me.text
    target_id = target_me.json()["id"]

    kicked = await anon_client.post(f"/api/v1/admin/users/{target_id}/logout", headers=auth_headers)
    assert kicked.status_code == 200, kicked.text
    assert kicked.json() == {"id": target_id, "username": victim["username"],
                             "token_version": 1, "logged_out": True}

    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(target_token))).status_code == 401
    # 管理员自己不受影响（踢的是别人）
    assert (await anon_client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 200

    # 目标账号重新登录就能继续用（被踢 ≠ 被停用）
    again = (await anon_client.post("/api/v1/auth/login", json=victim)).json()["access_token"]
    assert (await anon_client.get("/api/v1/auth/me", headers=_bearer(again))).status_code == 200

    rows = (await _audit(anon_client, auth_headers, action="user.force_logout"))["data"]
    row = _find(rows, "user.force_logout", target_id)
    assert row is not None, rows
    assert row["action_label"] == "强制下线"
    assert row["detail"]["to_version"] == 1
    # 被踢的账号自己**读不到**审计：⑳ 起审计列表是管理员的口子。以前「没有部门」就等于管理员，
    # 于是每个刚注册的账号都能翻全公司的操作记录 —— 那正是这一版要关掉的洞。
    peek = await anon_client.get("/api/v1/admin/audit?action=user.force_logout", headers=_bearer(again))
    assert peek.status_code == 403, peek.text


async def test_force_logout_guards_self_non_admin_and_unknown(anon_client, auth_headers, db_session):
    """自己踢自己走 /auth/logout；非管理员的踢人请求是 403；不存在的账号 404。"""
    me = await anon_client.get("/api/v1/auth/me", headers=auth_headers)
    my_id = me.json()["id"]
    self_kick = await anon_client.post(f"/api/v1/admin/users/{my_id}/logout", headers=auth_headers)
    assert self_kick.status_code == 400, self_kick.text
    assert "自己" in self_kick.json()["detail"]
    assert (await anon_client.post("/api/v1/admin/users/99999999/logout", headers=auth_headers)).status_code == 404

    scoped = await _plain_headers(anon_client, prefix="scoped")
    # ⑳ 起新注册的账号既不是管理员、也没有部门（以前「没有部门」就等于管理员，这个用例
    # 反而测的正是管理员，会「通过」得毫无意义）。
    denied = await anon_client.post(f"/api/v1/admin/users/{my_id}/logout", headers=scoped)
    assert denied.status_code == 403, denied.text

    # 名单接口带上会话版本号，管理页才能显示「这个账号被下线过几次」
    listing = await anon_client.get("/api/v1/admin/users", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    for item in listing.json()["data"]:
        assert isinstance(item["token_version"], int), item


# ---------------------------------------------------------------------------
# 登录失败锁定、指数退避与弱口令（④）
# ---------------------------------------------------------------------------


async def _wrong_login(client, username: str, times: int = 1):
    """连着用错密码登录 `times` 次，返回最后一次响应（每次都必须还是 401）。"""
    response = None
    for _ in range(times):
        response = await client.post("/api/v1/auth/login",
                                     json={"username": username, "password": "wrong-password-2026"})
        assert response.status_code == 401, response.text
    return response


async def test_lockout_blocks_even_the_correct_password_until_manager_unlocks(
        anon_client, auth_headers, db_session):
    """连续失败到阈值后，**密码正确也进不来**；管理员解锁后立刻恢复。"""
    from backend.models import User

    threshold = get_settings().login_lockout_threshold
    victim = {"username": f"lock_{uuid.uuid4().hex[:8]}", "password": "victim-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=victim)).status_code == 200

    await _wrong_login(anon_client, victim["username"], times=threshold)
    row = db_session.scalar(select(User).where(User.username == victim["username"]))
    db_session.refresh(row)
    assert row.failed_attempts == threshold, row.failed_attempts
    assert row.locked_until is not None

    # 这一步是这个功能的意义所在：口令是对的，但账号被锁着，所以还是 429
    blocked = await anon_client.post("/api/v1/auth/login", json=victim)
    assert blocked.status_code == 429, blocked.text
    assert "登录尝试过于频繁" in blocked.json()["detail"]
    assert blocked.headers.get("Retry-After")
    # 与「限流」同一句文案、且不提锁定与用户名：否则 429/401 的差别就成了账号枚举接口
    detail = blocked.json()["detail"]
    assert "锁" not in detail and victim["username"] not in detail

    # 留痕：失败若干条 + 锁上那一条（不含任何口令字样）
    failed = [r for r in (await _audit(anon_client, auth_headers, action="user.login_failed"))["data"]
              if r["actor"] == victim["username"]]
    assert len(failed) == threshold, len(failed)
    assert all("password" not in json.dumps(r["detail"], ensure_ascii=False).lower() for r in failed)
    locked_rows = [r for r in (await _audit(anon_client, auth_headers, action="user.login_locked"))["data"]
                   if r["actor"] == victim["username"]]
    assert {r["action_label"] for r in locked_rows} == {"账号锁定"}
    reasons = " ".join(r["detail"]["reason"] for r in locked_rows)
    assert "连续失败达到阈值" in reasons and "已锁定" in reasons

    # 管理员解锁：清计数与锁定时间，但**不碰**会话版本（解锁 ≠ 踢人）
    before_version = row.token_version
    unlocked = await anon_client.post(f"/api/v1/admin/users/{row.id}/unlock", headers=auth_headers)
    assert unlocked.status_code == 200, unlocked.text
    body = unlocked.json()
    assert body["failed_attempts"] == 0 and body["locked"] is False and body["locked_until"] is None
    assert body["cleared_attempts"] == threshold, body
    db_session.refresh(row)
    assert row.token_version == before_version

    # 解锁后同一份口令立刻可用，而且不会误报「刚清掉一堆失败」
    back = await anon_client.post("/api/v1/auth/login", json=victim)
    assert back.status_code == 200, back.text
    assert back.json()["failures_cleared"] == 0

    unlock_row = _find((await _audit(anon_client, auth_headers, action="user.unlock"))["data"],
                       "user.unlock", row.id)
    assert unlock_row is not None, "解锁要留痕"
    assert unlock_row["action_label"] == "解除账号锁定"
    assert unlock_row["detail"]["cleared_attempts"] == threshold

    # 名单接口把锁定状态给出来，管理页才分得清「密码错」与「被锁了」
    listing = (await anon_client.get("/api/v1/admin/users", headers=auth_headers)).json()["data"]
    item = next(u for u in listing if u["id"] == row.id)
    assert item["failed_attempts"] == 0 and item["locked"] is False


async def test_lockout_counter_restarts_after_the_silence_window(anon_client, db_session):
    """静默期过后计数从 0 重数：慢慢试的爆破不会被永久累积成一次锁定。"""
    from backend.models import User

    ttl = get_settings().login_failure_ttl_seconds
    victim = {"username": f"silent_{uuid.uuid4().hex[:8]}", "password": "silent-login-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=victim)).status_code == 200
    await _wrong_login(anon_client, victim["username"], times=2)
    row = db_session.scalar(select(User).where(User.username == victim["username"]))
    db_session.refresh(row)
    assert row.failed_attempts == 2

    # 把「上次失败」挪到静默期之外：这一次失败应当算第 1 次，而不是第 3 次
    row.last_failed_at = datetime.now(UTC) - timedelta(seconds=ttl + 60)
    db_session.flush()
    await _wrong_login(anon_client, victim["username"], times=1)
    db_session.refresh(row)
    assert row.failed_attempts == 1, row.failed_attempts


async def test_registration_rejects_weak_passwords_and_leaves_a_trail(anon_client, auth_headers, db_session):
    """弱口令在注册口就被挡住（400）：不建账号，但留一条「注册被拒」的痕。"""
    from backend import audit
    from backend.models import User

    username = f"weak_{uuid.uuid4().hex[:8]}"
    for weak in ("pytest-password-2026", "1234567890", "aaaaaaaaaaa"):
        rejected = await anon_client.post("/api/v1/auth/register",
                                          json={"username": username, "password": weak})
        assert rejected.status_code == 400, f"{weak} 不该被接受：{rejected.text}"
        assert rejected.json()["detail"], rejected.text
        assert db_session.scalar(select(User).where(User.username == username)) is None

    # 用户名没被「占坑」：换成够强的口令就能注册
    strong = await anon_client.post("/api/v1/auth/register",
                                    json={"username": username, "password": "strong-login-2026"})
    assert strong.status_code == 200, strong.text

    rows = [r for r in (await _audit(anon_client, auth_headers, action="user.register_rejected"))["data"]
            if r["actor"] == username]
    assert len(rows) == 3, rows
    assert rows[0]["action_label"] == "注册被拒（弱口令）"
    assert rows[0]["detail"]["problems"], rows[0]
    assert "user.register_rejected" in audit.ACTIONS

    # 已经存在的用户名重注册还是「用户名已存在」：老账号（含演示账号）的弱口令不该在这里被翻出来说
    existing = await anon_client.post("/api/v1/auth/register",
                                      json={"username": username, "password": "pytest-password-2026"})
    assert existing.status_code == 409, existing.text


async def test_unlock_guards_self_non_admin_and_unknown(anon_client, auth_headers):
    """解锁走的是管理接口：自己走 /auth/me、非管理员 403、不存在的账号 404。"""
    me = (await anon_client.get("/api/v1/auth/me", headers=auth_headers)).json()
    self_unlock = await anon_client.post(f"/api/v1/admin/users/{me['id']}/unlock", headers=auth_headers)
    assert self_unlock.status_code == 400, self_unlock.text
    assert "自己" in self_unlock.json()["detail"]
    assert (await anon_client.post("/api/v1/admin/users/99999999/unlock",
                                   headers=auth_headers)).status_code == 404

    scoped = await _plain_headers(anon_client, prefix="scoped")
    denied = await anon_client.post(f"/api/v1/admin/users/{me['id']}/unlock", headers=scoped)
    assert denied.status_code == 403, denied.text


# ---------------------------------------------------------------- ⑫ 安全响应头 / GZip / 请求 id

async def test_security_headers_and_request_id_are_on_every_response(anon_client):
    """每条响应都带安全头与请求 id；404 这类由内层直接返回的响应也不例外。"""
    from backend.http_headers import API_CONTENT_SECURITY_POLICY, REQUEST_ID_HEADER

    for path, code in (("/api/v1/health", 200), ("/api/v1/definitely-not-here", 404)):
        response = await anon_client.get(path)
        assert response.status_code == code, path
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "geolocation=()" in response.headers["Permissions-Policy"]
        assert response.headers["Content-Security-Policy"] == API_CONTENT_SECURITY_POLICY
        # 没带就自己生成：32 位十六进制，正好能在日志里 grep
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) == 32 and all(c in "0123456789abcdef" for c in request_id)
        # 本机是 http://127.0.0.1，下发 HSTS 会把浏览器锁死成「必须 https」
        assert "Strict-Transport-Security" not in response.headers


async def test_request_id_is_echoed_when_sane_and_regenerated_when_not(anon_client):
    """请求 id 只接受 1–128 位的 `[A-Za-z0-9._-]`：带空格或超长就当没带，重新生成。"""
    from backend.http_headers import REQUEST_ID_HEADER

    mine = "trace-abc.123_XYZ"
    echoed = await anon_client.get("/api/v1/health", headers={REQUEST_ID_HEADER: mine})
    assert echoed.headers[REQUEST_ID_HEADER] == mine

    assert len("a" * 128) == 128
    boundary = await anon_client.get("/api/v1/health", headers={REQUEST_ID_HEADER: "a" * 128})
    assert boundary.headers[REQUEST_ID_HEADER] == "a" * 128

    # httpx 连非 ASCII 的请求头都不让发（UnicodeEncodeError），所以这里只测能发出去的几种；
    # 中文/换行那类由 test_units.py 的 request_id_from 单测覆盖
    for bad in ("a" * 129, "id with space", "id;with;semicolon", "id/with/slash"):
        response = await anon_client.get("/api/v1/health", headers={REQUEST_ID_HEADER: bad})
        assert response.headers[REQUEST_ID_HEADER] != bad, bad
        assert len(response.headers[REQUEST_ID_HEADER]) == 32, bad


async def test_csp_is_strict_for_api_and_loose_for_docs(anon_client):
    """API 响应不需要加载任何资源，给最严的；文档页要从 CDN 取资源，只留 frame-ancestors。"""
    from backend.http_headers import API_CONTENT_SECURITY_POLICY, DOCS_CONTENT_SECURITY_POLICY

    api = await anon_client.get("/api/v1/health")
    assert api.headers["Content-Security-Policy"] == API_CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in api.headers["Content-Security-Policy"]

    for path in ("/openapi.json", "/docs", "/redoc"):
        response = await anon_client.get(path)
        assert response.status_code == 200, path
        assert response.headers["Content-Security-Policy"] == DOCS_CONTENT_SECURITY_POLICY, path
        assert "default-src 'none'" not in response.headers["Content-Security-Policy"], path


async def test_hsts_only_when_the_request_came_over_https(anon_client):
    """反代后面 `request.url.scheme` 是 http，只有 `X-Forwarded-Proto` 能说明是不是 https。"""
    from backend.http_headers import HSTS_VALUE

    assert "Strict-Transport-Security" not in (await anon_client.get("/api/v1/health")).headers

    forward = {"X-Forwarded-Proto": "https"}
    assert (await anon_client.get("/api/v1/health",
                                 headers=forward)).headers["Strict-Transport-Security"] == HSTS_VALUE
    # nginx 可能写成 "https, http"（链路多段），取第一段
    chain = {"X-Forwarded-Proto": "https, http"}
    assert (await anon_client.get("/api/v1/health",
                                 headers=chain)).headers["Strict-Transport-Security"] == HSTS_VALUE
    # 明说 http 就不能下发
    plain = {"X-Forwarded-Proto": "http"}
    assert "Strict-Transport-Security" not in (await anon_client.get("/api/v1/health",
                                                                     headers=plain)).headers


async def test_large_json_is_gzipped_and_small_response_is_not(anon_client):
    """`/openapi.json` 有几十 KB，压完明显更小；`/health` 只有 39 字节，压它只白烧 CPU。"""
    gzip_accept = {"Accept-Encoding": "gzip"}

    big = await anon_client.get("/openapi.json", headers=gzip_accept)
    assert big.headers["Content-Encoding"] == "gzip"
    assert big.headers["Vary"] == "Accept-Encoding"
    # httpx 会把 gzip 解回来，所以 content 是解压后的长度，跟压缩后的 Content-Length 一比就知道真压了
    assert int(big.headers["Content-Length"]) < len(big.content)
    plain = await anon_client.get("/openapi.json", headers={"Accept-Encoding": "identity"})
    assert big.json() == plain.json()

    small = await anon_client.get("/api/v1/health", headers=gzip_accept)
    assert "Content-Encoding" not in small.headers


async def test_sse_is_never_gzipped(anon_client):
    """压了就得攒够一块才吐，SSE 的「流式」就没了 —— 所以 text/event-stream 在排除表里。"""
    response = await anon_client.post("/api/v1/chat/stream", json={"message": "gzip 检查"},
                                      headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/event-stream")
    assert "Content-Encoding" not in response.headers
    assert response.text.count("data: ") >= 2


async def test_unhandled_error_carries_request_id_and_security_headers(api_app):
    """500 走的是最外层的 `ServerErrorMiddleware`（不在用户中间件里），所以得自己补头。

    这条同时是「用户报错时怎么对上日志」的证据：响应里的 `request_id` 与日志那一行是同一个。
    """
    from backend.http_headers import REQUEST_ID_HEADER

    async def _boom():
        raise RuntimeError("pytest 故意炸")

    api_app.add_api_route("/api/v1/__pytest_boom", _boom)
    route = api_app.router.routes[-1]
    try:
        transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver",
                                     trust_env=False) as client:
            response = await client.get("/api/v1/__pytest_boom",
                                        headers={REQUEST_ID_HEADER: "trace-from-user"})
    finally:
        api_app.router.routes.remove(route)

    assert response.status_code == 500, response.text
    assert response.json()["request_id"] == "trace-from-user"
    assert response.headers[REQUEST_ID_HEADER] == "trace-from-user"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]


async def test_metrics_endpoint_serves_prometheus_text(anon_client, api_app):
    """⑬ `/metrics`：Prometheus 文本格式（HELP/TYPE + 直方图累计桶），且**不进接口契约**。

    不进 OpenAPI 是刻意的：它是给爬取侧用的文本端点，混进契约会让「契约漂移」比对
    多出一条永远在变的噪音。
    """
    from backend import metrics

    metrics.reset()
    response = await anon_client.get("/metrics")

    assert response.status_code == 200, response.text
    assert response.headers["Content-Type"].startswith("text/plain; version=0.0.4")
    body = response.text
    assert "# HELP finance_http_requests_total" in body
    assert "# TYPE finance_http_requests_total counter" in body
    assert "# TYPE finance_http_request_duration_seconds histogram" in body
    assert 'finance_app_info{version="1.0.0",environment=' in body
    assert "finance_process_start_time_seconds " in body
    # 在途请求数在**响应渲染时**就该是 1（这条请求自己）：卡住时它是唯一还能看的指标。
    assert "finance_http_requests_in_flight 1" in body
    assert "/metrics" not in api_app.openapi()["paths"]


async def test_metrics_labels_use_route_templates(anon_client):
    """按原始路径记标签会让一个 id 一条时间序列；未匹配的路径一律归 `unmatched`。"""
    from backend import metrics

    metrics.reset()
    await anon_client.get("/api/v1/health")
    await anon_client.get("/api/v1/financial/transactions/123")
    await anon_client.get("/no/such/path/xyz")
    body = (await anon_client.get("/metrics")).text

    assert 'finance_http_requests_total{method="GET",route="/api/v1/health",status="200"} 1' in body
    assert 'route="/api/v1/financial/transactions/{row_id}"' in body
    assert 'finance_http_requests_total{method="GET",route="unmatched",status="404"} 1' in body
    assert "/123" not in body


async def test_metrics_token_and_kill_switch(anon_client, monkeypatch):
    """没配 `METRICS_TOKEN` 就开放（爬取侧没法走 JWT 登录）；配了就认 Bearer；`METRICS_ENABLED=false` 直接 404。"""
    monkeypatch.setattr("backend.main.get_settings",
                        lambda: SimpleNamespace(metrics_enabled=True, metrics_token="s3cret"))
    assert (await anon_client.get("/metrics")).status_code == 401
    allowed = await anon_client.get("/metrics", headers={"Authorization": "Bearer s3cret"})
    assert allowed.status_code == 200, allowed.text

    monkeypatch.setattr("backend.main.get_settings",
                        lambda: SimpleNamespace(metrics_enabled=False, metrics_token="s3cret"))
    assert (await anon_client.get("/metrics")).status_code == 404


# ---------------------------------------------------------------------------
# 预警外发通道（⑭）
# ---------------------------------------------------------------------------

# 专用期间：`plan()` 扫的是全库的「待处理预警 × 启用通道」，而接口用例连的就是演示库。
# 挑两个演示数据里没有的期间，再按自己的 channel_id 过滤，断言才不会被既有预警带跑偏。
CHANNEL_PERIOD = "2099-12"
HOOK_PERIOD = "2099-10"


def _outbound_channel_payload(name: str, **overrides) -> dict:
    payload = {"name": name, "kind": "webhook", "target": "http://127.0.0.1:9/hook",
               "min_level": "low", "department": "", "enabled": True, "secret": ""}
    payload.update(overrides)
    return payload


def _outbound_settings(**overrides) -> SimpleNamespace:
    """假 settings：只带 `alerts_outbound` 读到的字段——少一个就是 AttributeError。"""
    values = {"alert_webhook_timeout_seconds": 2.0, "alert_max_attempts": 3,
              "alert_smtp_host": "", "alert_smtp_port": 587, "alert_smtp_user": "",
              "alert_smtp_password": "", "alert_smtp_starttls": True, "alert_mail_from": ""}
    values.update(overrides)
    return SimpleNamespace(**values)


def _stub_webhook(monkeypatch, *, ok=True, status=200, detail=""):
    """接口用例只验链路（建单 → 排队 → 投递 → 台账），真发 HTTP 由单测的本地 sink 负责。"""
    from backend import alerts_outbound

    calls: list[dict] = []

    def fake(channel, payload, *, delivery_id=None, settings=None,
             event=alerts_outbound.WEBHOOK_EVENT):
        calls.append({"channel": channel.name, "metric": payload.get("metric_name"),
                      "delivery_id": delivery_id, "event": event})
        return ok, status, detail

    monkeypatch.setattr(alerts_outbound, "send_webhook", fake)
    return calls


def _run_jobs_inline(db, monkeypatch):
    """把 `jobs.SCHEDULER`（0 秒后的进程内定时器）换成「当场同步跑一遍」。

    用例要断言的正是「排出去的任务跑完之后台账长什么样」；等真定时器就变成
    「睡一会儿再看」，既慢又不稳（而且断言跑在任务前面就会随机红）。
    """
    from backend import jobs

    timers: list[tuple[float, int]] = []

    def inline(delay, job_id):
        timers.append((delay, job_id))
        jobs.run_job(job_id, db=db)

    monkeypatch.setattr(jobs, "SCHEDULER", inline)
    return timers


async def _deliveries_for(client, headers, channel_id: int) -> list[dict]:
    response = await client.get(f"/api/v1/admin/alert-deliveries?channel_id={channel_id}&limit=200",
                                headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def test_alert_channels_crud_and_secret_is_write_only(anon_client, auth_headers, db_session):
    """通道能建能改能删；`secret` 只写不读——响应里连字段都不给，只说「配没配」。"""
    assert db_session is not None  # 用真库的会话夹具，接口的 commit 才落在同一事务里
    name = f"pytest 通道 {uuid.uuid4().hex[:8]}"
    created = await anon_client.post("/api/v1/admin/alert-channels",
                                     json=_outbound_channel_payload(name, secret="topsecret-9f3c",
                                                                    min_level="medium"),
                                     headers=auth_headers)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == name and body["kind"] == "webhook"
    assert body["kind_label"] == "Webhook（HTTP POST）"
    assert body["min_level"] == "medium" and body["min_level_label"] == "中及以上"
    assert body["enabled"] is True and body["has_secret"] is True
    assert "secret" not in body and "topsecret-9f3c" not in created.text
    channel_id = body["id"]

    # 重名 409：两条同名通道在页面上根本分不清
    duplicate = await anon_client.post("/api/v1/admin/alert-channels",
                                       json=_outbound_channel_payload(name), headers=auth_headers)
    assert duplicate.status_code == 409 and "已经有一条叫" in duplicate.json()["detail"]

    # 坏地址/坏级别/坏类型当场挡下：留着它们只会等到第一次真发的时候才炸
    for index, bad in enumerate(({"target": "ftp://example.com/hook"},
                                 {"target": "http://robot:pw@example.com/hook"},
                                 {"min_level": "urgent"}, {"kind": "sms"})):
        response = await anon_client.post("/api/v1/admin/alert-channels",
                                          json=_outbound_channel_payload(f"{name}-坏{index}", **bad),
                                          headers=auth_headers)
        assert response.status_code == 422, (bad, response.text)

    # 邮件通道只做形状校验，并且顺手规范化（多一个空格就会让 SMTP 报 501）
    email_channel = await anon_client.post(
        "/api/v1/admin/alert-channels",
        json=_outbound_channel_payload(f"{name}-邮件", kind="email",
                                       target=" fin@example.com , ops@example.com "),
        headers=auth_headers)
    assert email_channel.status_code == 201, email_channel.text
    assert email_channel.json()["target"] == "fin@example.com, ops@example.com"

    listing = await anon_client.get("/api/v1/admin/alert-channels", headers=auth_headers)
    assert listing.status_code == 200, listing.text
    payload = listing.json()
    item = next(row for row in payload["data"] if row["id"] == channel_id)
    # 新建时台账是空的（新建**不**自动补发旧预警）
    assert item["counts"] == {"pending": 0, "sent": 0, "failed": 0}
    assert set(payload["kinds"]) == {"webhook", "email"} and "medium" in payload["min_levels"]
    assert "high" in payload["levels"]

    # 只传要改的字段：secret 不动（None = 不改），空串 = 清掉密钥
    updated = await anon_client.put(f"/api/v1/admin/alert-channels/{channel_id}",
                                    json={"min_level": "high", "enabled": False}, headers=auth_headers)
    assert updated.status_code == 200, updated.text
    assert updated.json()["min_level"] == "high" and updated.json()["enabled"] is False
    assert updated.json()["has_secret"] is True
    cleared = await anon_client.put(f"/api/v1/admin/alert-channels/{channel_id}",
                                    json={"secret": ""}, headers=auth_headers)
    assert cleared.json()["has_secret"] is False
    assert (await anon_client.put("/api/v1/admin/alert-channels/99999999",
                                  json={"enabled": False}, headers=auth_headers)).status_code == 404

    rows = (await _audit(anon_client, auth_headers, action="alert_channel.create"))["data"]
    entry = _find(rows, "alert_channel.create", channel_id)
    assert entry is not None and entry["detail"]["name"] == name
    assert "topsecret-9f3c" not in json.dumps(entry["detail"], ensure_ascii=False)

    deleted = await anon_client.delete(f"/api/v1/admin/alert-channels/{channel_id}",
                                       headers=auth_headers)
    assert deleted.status_code == 200 and deleted.json()["deleted"] is True
    assert (await anon_client.delete(f"/api/v1/admin/alert-channels/{channel_id}",
                                     headers=auth_headers)).status_code == 404


async def test_alert_channel_test_endpoint_reports_failure_and_skips_the_ledger(
        anon_client, auth_headers, db_session, monkeypatch):
    """测试发送的成功/失败原样回给页面；它**不进投递台账**——它不是某条预警的投递。"""
    from sqlalchemy import func, select

    from backend.models import AlertDelivery

    name = f"pytest 通道测试 {uuid.uuid4().hex[:8]}"
    created = await anon_client.post("/api/v1/admin/alert-channels",
                                     json=_outbound_channel_payload(name), headers=auth_headers)
    channel_id = created.json()["id"]

    calls = _stub_webhook(monkeypatch, ok=False, status=500, detail="HTTP 500：boom")
    failed = await anon_client.post(f"/api/v1/admin/alert-channels/{channel_id}/test",
                                    headers=auth_headers)
    assert failed.status_code == 200, failed.text
    body = failed.json()
    assert body["ok"] is False and body["status"] == 500 and "boom" in body["detail"]
    assert body["channel"]["last_result"] == "failed"
    assert body["channel"]["last_error"] == "HTTP 500：boom"
    assert calls and calls[0]["event"] == "alert.channel_test" and calls[0]["channel"] == name
    # 测试通知不落台账：混进去的话「待发送 N 条」里会多出一条并不存在的预警
    assert db_session.scalar(select(func.count()).select_from(AlertDelivery)
                             .where(AlertDelivery.channel_id == channel_id)) == 0

    _stub_webhook(monkeypatch, ok=True, status=200, detail="")
    ok = await anon_client.post(f"/api/v1/admin/alert-channels/{channel_id}/test",
                                headers=auth_headers)
    assert ok.json()["ok"] is True
    assert ok.json()["channel"]["last_result"] == "ok"
    assert ok.json()["channel"]["last_sent_at"] is not None

    entry = _find((await _audit(anon_client, auth_headers, action="alert_channel.test"))["data"],
                  "alert_channel.test", channel_id)
    assert entry is not None and entry["detail"]["kind"] == "webhook"
    assert "ok" in entry["detail"]


async def test_notify_dedups_by_content_and_manual_retry_resets_attempts(
        anon_client, auth_headers, db_session, monkeypatch):
    """⑭ 主链路：补发建单 → 按内容去重 → 失败留台账（不自动重轰）→ 人工重试归零后当场成功。"""
    from backend import alerts_outbound
    from backend.models import FinancialAlert

    timers = _run_jobs_inline(db_session, monkeypatch)
    # 预算 1：第一次就判失败，任务整体算成功，链路才好接着往下走（见下面的注释）
    monkeypatch.setattr(alerts_outbound, "get_settings",
                        lambda: _outbound_settings(alert_max_attempts=1))

    name = f"pytest 通道台账 {uuid.uuid4().hex[:8]}"
    created = await anon_client.post("/api/v1/admin/alert-channels",
                                     json=_outbound_channel_payload(name), headers=auth_headers)
    channel_id = created.json()["id"]

    db_session.add_all([
        FinancialAlert(period=CHANNEL_PERIOD, department=None, alert_type="成本率异常",
                       description="pytest 外发种子", level="high", metric_name="cost_ratio",
                       current_value=Decimal("0.900000"), threshold_value=Decimal("0.800000"),
                       review_status="pending"),
        # 已处理的不补发：否则新建通道点一下就把人工已经判过的预警又轰一遍
        FinancialAlert(period=CHANNEL_PERIOD, department=None, alert_type="已处理的历史预警",
                       description="pytest 不该外发", level="high", metric_name="settled_ratio",
                       current_value=Decimal("0.900000"), threshold_value=Decimal("0.800000"),
                       review_status="resolved"),
    ])
    db_session.flush()

    failing = _stub_webhook(monkeypatch, ok=False, status=503, detail="HTTP 503：网关维护")
    first = await anon_client.post("/api/v1/admin/alerts/notify", json={"period": CHANNEL_PERIOD},
                                   headers=auth_headers)
    assert first.status_code == 200, first.text
    assert first.json()["created"] == 1 and first.json()["queued"] == 1
    assert first.json()["job"] and timers and timers[-1][0] == 0.0  # 0 秒后立刻发，不阻塞响应

    rows = await _deliveries_for(anon_client, auth_headers, channel_id)
    assert len(rows) == 1
    delivery = rows[0]
    assert delivery["alert_type"] == "成本率异常" and delivery["level_label"] == "高"
    assert delivery["status"] == "failed" and delivery["status_label"] == "已失败"
    assert delivery["attempts"] == 1 and "503" in delivery["last_error"]
    assert delivery["channel_name"] == name and delivery["alert_id"] is not None
    assert len(failing) == 1

    # 再点一次补发：内容级去重不再建单；已判失败的也不会被自动重排（失败就该让人看一眼）
    second = await anon_client.post("/api/v1/admin/alerts/notify", json={"period": CHANNEL_PERIOD},
                                    headers=auth_headers)
    assert second.json()["created"] == 0 and second.json()["queued"] == 0
    assert len(await _deliveries_for(anon_client, auth_headers, channel_id)) == 1

    # 台账筛选与取值
    listed = await anon_client.get(
        f"/api/v1/admin/alert-deliveries?channel_id={channel_id}&period={CHANNEL_PERIOD}"
        f"&status=failed&level=high", headers=auth_headers)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    assert listed.json()["status_labels"]["failed"] == "已失败"
    assert listed.json()["stats"]["failed"] >= 1
    for bad_query in ("status=nonsense", "level=urgent"):
        assert (await anon_client.get(f"/api/v1/admin/alert-deliveries?{bad_query}",
                                      headers=auth_headers)).status_code == 422

    # 通道修好 → 人工重试：attempts 归零，当场发成功
    _stub_webhook(monkeypatch, ok=True, status=200, detail="")
    retried = await anon_client.post(f"/api/v1/admin/alert-deliveries/{delivery['id']}/retry",
                                     headers=auth_headers)
    assert retried.status_code == 200, retried.text
    assert retried.json()["queued"] == 1 and retried.json()["deduplicated"] is False
    assert retried.json()["data"]["status"] == "sent" and retried.json()["data"]["status_label"] == "已送达"
    assert retried.json()["data"]["attempts"] == 1
    assert retried.json()["data"]["response_status"] == 200
    assert (await anon_client.post("/api/v1/admin/alert-deliveries/99999999/retry",
                                   headers=auth_headers)).status_code == 404

    # 留痕：补发点了两次，两次都要有记录（第一次建了 1 条，第二次一条都没建）
    notify_log = (await _audit(anon_client, auth_headers, action="alert.notify"))["data"]
    notify_entries = [row for row in notify_log if row["target_id"] == CHANNEL_PERIOD]
    assert {row["detail"]["created"] for row in notify_entries} == {1, 0}
    retry_entry = _find((await _audit(anon_client, auth_headers, action="alert_delivery.retry"))["data"],
                        "alert_delivery.retry", delivery["id"])
    assert retry_entry is not None and retry_entry["detail"]["channel"] == name

    # 通道上的计数跟着台账走，管理页一眼能看出「这条通道发出去过几条」
    listing = await anon_client.get("/api/v1/admin/alert-channels", headers=auth_headers)
    item = next(row for row in listing.json()["data"] if row["id"] == channel_id)
    assert item["counts"] == {"pending": 0, "sent": 1, "failed": 0}


async def test_calculate_and_import_queue_outbound_deliveries(anon_client, auth_headers, db_session,
                                                             tmp_path, monkeypatch):
    """⑭ 钩子：重算与导入之后要把新预警排进外发队列，并把排出去的条数回给调用方。"""
    from backend import jobs

    timers: list[tuple[float, int]] = []
    monkeypatch.setattr(jobs, "SCHEDULER", lambda delay, job_id: timers.append((delay, job_id)))

    name = f"pytest 通道钩子 {uuid.uuid4().hex[:8]}"
    created = await anon_client.post("/api/v1/admin/alert-channels",
                                     json=_outbound_channel_payload(name), headers=auth_headers)
    channel_id = created.json()["id"]

    # 重算：净利润为负 → 生成预警 → 与重算同事务建投递单，提交之后立刻排队
    _seed_transactions(db_session, HOOK_PERIOD, [("100", "0", "150")])
    calculated = await anon_client.post(f"/api/v1/analysis/calculate?period={HOOK_PERIOD}",
                                        headers=auth_headers)
    assert calculated.status_code == 200, calculated.text
    assert calculated.json()["alerts"] >= 1
    assert calculated.json()["deliveries"] >= 1 and calculated.json()["dispatch_job"]
    assert timers and timers[-1][0] == 0.0

    rows = await _deliveries_for(anon_client, auth_headers, channel_id)
    assert rows and {row["period"] for row in rows} == {HOOK_PERIOD}
    assert {row["status"] for row in rows} == {"pending"}

    # 导入：也是提交之后排程，返回体里同样给出「排了多少条」
    monkeypatch.setattr("backend.api_financial.get_settings",
                        lambda: SimpleNamespace(upload_dir=str(tmp_path)))
    csv = _csv_bytes("2099-10-01,6001,导入行,部门-097,ok,100.005,90,20,0,0")
    imported = await anon_client.post("/api/v1/financial/import", headers=auth_headers,
                                      files={"file": ("import.csv", io.BytesIO(csv), "text/csv")})
    assert imported.status_code == 200, imported.text
    assert imported.json()["deliveries"] >= 1
    assert timers[-1][0] == 0.0
    assert [row for row in await _deliveries_for(anon_client, auth_headers, channel_id)
            if row["period"] == HOOK_PERIOD]


async def test_alert_channel_endpoints_require_admin(anon_client, api_app, auth_headers):
    """通道能往任意地址 POST（含内网）、台账里有业务信息：只给管理员。"""
    for method, path in (("get", "/api/v1/admin/alert-channels"),
                         ("post", "/api/v1/admin/alert-channels"),
                         ("get", "/api/v1/admin/alert-deliveries"),
                         ("post", "/api/v1/admin/alerts/notify")):
        call = getattr(anon_client, method)
        response = await (call(path, json={}) if method == "post" else call(path))
        assert response.status_code == 401, (method, path, response.status_code)

    headers, client = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        denied = await client.get("/api/v1/admin/alert-channels", headers=headers)
        assert denied.status_code == 403 and "需要管理员权限" in denied.json()["detail"]
        assert (await client.post("/api/v1/admin/alerts/notify", json={},
                                  headers=headers)).status_code == 403
    finally:
        await client.aclose()


# ---------------------------------------------------------------- Agent 会话落库与停止生成（⑮）


def _agent_events(body: str) -> list[tuple[str, dict]]:
    """把 SSE 文本拆成 (事件名, 数据)；只服务测试，不追求协议完备。"""
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name, payload = "delta", {}
        for line in block.splitlines():
            if line.startswith("event:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                payload = json.loads(line.split(":", 1)[1].strip())
        events.append((name, payload))
    return events


def _agent_text(events: list[tuple[str, dict]]) -> str:
    return "".join(item.get("text", "") for name, item in events if name == "delta")


def _agent_llm(chunks=("依据", "证据"), tokens=(12, 5), before_second=None) -> SimpleNamespace:
    """假的模型：块内容与用量可指定；`before_second` 用来在第二块之前挂住（测停止）。

    `calls` 记下每轮收到的 `history`（⑰：用来断言模型真的看得见上一轮问答）。
    """
    calls: list = []

    def stream(question, context, usage=None, history=None):
        calls.append({"question": question, "history": list(history or [])})
        if usage is not None and tokens:
            usage.update({"prompt_tokens": tokens[0], "completion_tokens": tokens[1]})
        for index, chunk in enumerate(chunks):
            if before_second is not None and index == 1:
                before_second()
            yield chunk

    return SimpleNamespace(stream=stream, calls=calls)


class _RealAgentDatabase:
    """把整条请求链路切到真库（绕过 conftest 的 savepoint）。

    ⑮ 的流式落库会在 Starlette 的线程池里另开一个会话写回答，savepoint 里尚未提交的占位行
    它看不见——所以流式用例必须让接口与生成器看到同一份真库数据，结束时按用户名精确清理。
    """

    def __init__(self, api_app):
        self.api_app = api_app
        self.username = f"pytest-agent-{uuid.uuid4().hex[:10]}"
        self.password = "pytest-agent-2026"
        self.headers: dict = {}

    async def __aenter__(self):
        from backend.db import SessionLocal, get_db

        def real_get_db():
            db = SessionLocal()
            try:
                yield db
            finally:
                db.close()

        self.SessionLocal = SessionLocal
        self.api_app.dependency_overrides[get_db] = real_get_db
        return self

    async def login(self, client) -> dict:
        registered = await client.post("/api/v1/auth/register",
                                       json={"username": self.username, "password": self.password})
        assert registered.status_code == 200, registered.text
        response = await client.post("/api/v1/auth/login",
                                     json={"username": self.username, "password": self.password})
        assert response.status_code == 200, response.text
        self.headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        return self.headers

    async def __aexit__(self, *exc) -> None:
        from sqlalchemy import delete

        from backend.models import AgentMessage, AgentSession, AuditLog, User

        with self.SessionLocal() as db:
            user = db.execute(select(User).where(User.username == self.username)).scalars().first()
            if user is None:
                return
            db.execute(delete(AgentMessage).where(AgentMessage.user_id == user.id))
            db.execute(delete(AgentSession).where(AgentSession.user_id == user.id))
            db.execute(delete(AuditLog).where(AuditLog.actor == self.username))
            db.delete(user)
            db.commit()


async def test_agent_sessions_crud_ownership_and_audit(anon_client, api_app, auth_headers):
    """会话是本人的私产：能建/列/看/删，别人的既看不见也删不掉，删除要留审计。"""
    created = await anon_client.post("/api/v1/ai/agent/sessions", headers=auth_headers,
                                     json={"title": "  成本率排查  "})
    assert created.status_code == 201, created.text
    session = created.json()                                # 建会话直接返回会话本身
    assert session["title"] == "成本率排查"                 # 首尾空白被压掉
    assert session["answers"] == 0 and session["total_tokens"] == 0
    session_id = session["id"]

    listed = (await anon_client.get("/api/v1/ai/agent/sessions", headers=auth_headers)).json()
    assert any(item["id"] == session_id for item in listed["data"])
    assert listed["untitled"] == "未命名会话"

    detail = (await anon_client.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                    headers=auth_headers)).json()
    assert detail["data"]["id"] == session_id and detail["messages"] == []
    assert detail["status_labels"]["stopped"] == "已停止"

    untitled = await anon_client.post("/api/v1/ai/agent/sessions", headers=auth_headers, json={})
    assert untitled.status_code == 201
    assert untitled.json()["title"] == ""
    assert untitled.json()["display_title"] == "未命名会话"

    headers, stranger = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        assert (await stranger.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                   headers=headers)).status_code == 404
        assert (await stranger.delete(f"/api/v1/ai/agent/sessions/{session_id}",
                                      headers=headers)).status_code == 404
    finally:
        await stranger.aclose()

    removed = await anon_client.delete(f"/api/v1/ai/agent/sessions/{session_id}", headers=auth_headers)
    assert removed.status_code == 200
    assert removed.json() == {"id": session_id, "title": "成本率排查",
                              "deleted_messages": 0, "deleted": True}
    assert (await anon_client.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                  headers=auth_headers)).status_code == 404

    logged = (await _audit(anon_client, auth_headers, action="agent_session.delete"))["data"]
    assert any(row["target_id"] == str(session_id) and row["detail"]["messages"] == 0
               for row in logged)


async def test_agent_stream_persists_session_tokens_and_usage(anon_client, api_app,
                                                              stub_retrieval, monkeypatch):
    """一次真流式问答：会话/提问/回答/token 全部落库，done 事件里的数字与库里一致。"""
    llm = _agent_llm()
    monkeypatch.setattr("backend.api_ai.get_llm", lambda: llm)
    async with _RealAgentDatabase(api_app) as real:
        headers = await real.login(anon_client)
        async with anon_client.stream("POST", "/api/v1/ai/agent/chat/stream", headers=headers,
                                      json={"question": "差旅费标准是多少？"}) as response:
            assert response.status_code == 200
            body = "".join([chunk async for chunk in response.aiter_text()])

        events = _agent_events(body)
        assert events[0][0] == "start" and events[-1][0] == "done"
        start, done = events[0][1], events[-1][1]
        assert start["title"].startswith("差旅费标准")
        assert done["session_id"] == start["session_id"] and done["status"] == "ok"
        assert done["stopped"] is False
        assert done["tokens"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17,
                                  "usage_source": "api"}
        assert _agent_text(events) == "依据证据"
        assert start["history_turns"] == 0 and start["history_messages"] == 0
        assert done["history_tokens"] == 0 and done["history_truncated"] is False
        assert llm.calls[0]["history"] == []                    # 第一轮没有历史可带
        session_id = start["session_id"]

        detail = (await anon_client.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                        headers=headers)).json()
        assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
        answer = detail["messages"][1]
        assert answer["content"] == "依据证据" and answer["status"] == "ok"
        assert answer["usage_source"] == "api" and answer["total_tokens"] == 17
        assert answer["duration_ms"] is not None
        assert detail["data"]["answers"] == 1 and detail["data"]["total_tokens"] == 17

        usage = (await anon_client.get("/api/v1/ai/agent/usage?days=1", headers=headers)).json()
        assert usage["data"]["api_answers"] == 1 and usage["data"]["total_tokens"] == 17
        assert [item["total_tokens"] for item in usage["series"]] == [17]
        assert "by_user" not in usage["data"]                 # 按用户拆开只给管理员端点

        # 追加到同一个会话：第二轮问答进同一个 session，回答数累加
        async with anon_client.stream("POST", f"/api/v1/ai/agent/chat/stream?session_id={session_id}",
                                      headers=headers, json={"question": "住宿标准呢？"}) as response:
            again = _agent_events("".join([chunk async for chunk in response.aiter_text()]))
        assert again[0][1]["session_id"] == session_id
        assert again[0][1]["title"] == start["title"]          # 已有标题不覆盖
        # ⑰：第二轮把上一轮问答当历史交给模型，条数/字数都是可观测的
        assert again[0][1]["history_turns"] == 1 and again[0][1]["history_messages"] == 2
        assert again[0][1]["history_tokens"] > 0
        assert again[0][1]["history_truncated"] is False
        assert llm.calls[1]["history"] == [{"role": "user", "content": "差旅费标准是多少？"},
                                           {"role": "assistant", "content": "依据证据"}]
        detail = (await anon_client.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                        headers=headers)).json()
        assert len(detail["messages"]) == 4 and detail["data"]["answers"] == 2
        assert detail["data"]["total_tokens"] == 34

        # 估算口径也把历史算进 prompt（没开用量上报时，字数估算要反映真实上下文）
        estimated = _agent_llm(chunks=("答",), tokens=())
        monkeypatch.setattr("backend.api_ai.get_llm", lambda: estimated)
        async with anon_client.stream("POST", f"/api/v1/ai/agent/chat/stream?session_id={session_id}",
                                      headers=headers, json={"question": "再核对一遍"}) as response:
            third = _agent_events("".join([chunk async for chunk in response.aiter_text()]))
        assert third[-1][1]["tokens"]["usage_source"] == "estimated"
        # 估算是按"历史 + 本轮问题 + 证据"的字数来的：只算本轮问题的话这里会是个位数
        assert third[-1][1]["tokens"]["prompt_tokens"] >= 30
        assert len(estimated.calls[0]["history"]) == 4          # 前两轮问答（4 条消息）都在

        missing = await anon_client.post("/api/v1/ai/agent/chat/stream?session_id=999999999",
                                         headers=headers, json={"question": "还在吗？"})
        assert missing.status_code == 404


async def _wait_for_streaming_row(real, session_id: int, timeout: float = 10.0):
    """轮询真库，等那条 `streaming` 的占位回答出现（httpx 的 ASGITransport 会把响应体攒完
    再交给调用方，所以不能靠"读到第一个 delta"来判断流已经开始了）。"""
    import asyncio
    import time

    from backend.models import AgentMessage

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with real.SessionLocal() as db:
            row = db.execute(select(AgentMessage).where(
                AgentMessage.session_id == session_id, AgentMessage.role == "assistant",
                AgentMessage.status == "streaming")).scalars().first()
            if row is not None:
                return row.id
        await asyncio.sleep(0.05)
    return None


async def test_agent_stop_marks_the_answer_stopped(anon_client, api_app, stub_retrieval, monkeypatch):
    """停止生成：服务端标志位一置，生成器下一块就收尾，已生成的部分按「已停止」落库。"""
    import asyncio
    import threading

    gate = threading.Event()

    def wait_for_the_test():
        assert gate.wait(timeout=15), "测试没有放开闸门"

    monkeypatch.setattr("backend.api_ai.get_llm", lambda: _agent_llm(
        chunks=("已经写了一半", "这半句不该出现"), tokens=None, before_second=wait_for_the_test))

    async with _RealAgentDatabase(api_app) as real:
        headers = await real.login(anon_client)
        created = await anon_client.post("/api/v1/ai/agent/sessions", headers=headers, json={})
        assert created.status_code == 201
        session_id = created.json()["id"]

        async def drive():
            async with anon_client.stream("POST", f"/api/v1/ai/agent/chat/stream?session_id={session_id}",
                                          headers=headers, json={"question": "成本率怎么了？"}) as response:
                assert response.status_code == 200
                return "".join([chunk async for chunk in response.aiter_text()])

        task = asyncio.create_task(drive())
        try:
            message_id = await _wait_for_streaming_row(real, session_id)
            assert message_id is not None, "占位回答没落库"
            stopped = await anon_client.post(f"/api/v1/ai/agent/sessions/{session_id}/stop",
                                            headers=headers)
            assert stopped.status_code == 200, stopped.text
            assert stopped.json() == {"session_id": session_id, "message_id": message_id,
                                      "stopped": True}
        finally:
            gate.set()                                        # 用例早早失败时别把线程池挂死
        body = await asyncio.wait_for(task, timeout=20)

        events = _agent_events(body)
        done = events[-1][1]
        assert done["status"] == "stopped" and done["stopped"] is True
        assert _agent_text(events) == "已经写了一半"           # 停住之后不再产出

        detail = (await anon_client.get(f"/api/v1/ai/agent/sessions/{session_id}",
                                        headers=headers)).json()
        answer = detail["messages"][-1]
        assert answer["status"] == "stopped" and answer["status_label"] == "已停止"
        assert answer["content"] == "已经写了一半"
        assert answer["usage_source"] == "estimated" and answer["total_tokens"] > 0

        again = await anon_client.post(f"/api/v1/ai/agent/sessions/{session_id}/stop", headers=headers)
        assert again.status_code == 409                       # 已经没有在生成的了


async def test_agent_endpoints_require_token_and_usage_scope(anon_client, api_app, auth_headers):
    """会话与用量都要求登录；跨用户用量只给管理员，部门账号只看得到自己的。"""
    for method, path in (("get", "/api/v1/ai/agent/sessions"),
                         ("post", "/api/v1/ai/agent/sessions"),
                         ("get", "/api/v1/ai/agent/usage"),
                         ("get", "/api/v1/admin/agent-usage")):
        call = getattr(anon_client, method)
        response = await (call(path, json={}) if method == "post" else call(path))
        assert response.status_code == 401, (method, path, response.status_code)

    mine = await anon_client.get("/api/v1/ai/agent/usage?days=7", headers=auth_headers)
    assert mine.status_code == 200
    assert mine.json()["data"]["days"] == 7 and "by_user" not in mine.json()["data"]
    assert len(mine.json()["series"]) == 7

    overview = await anon_client.get("/api/v1/admin/agent-usage", headers=auth_headers)
    assert overview.status_code == 200
    assert "by_user" in overview.json()["data"]               # 无部门账号 = 不受限 = 管理员
    assert overview.json()["status_labels"]["interrupted"] == "已中断"

    # 后台任务式的越界参数要被挡下
    assert (await anon_client.get("/api/v1/admin/agent-usage?days=0",
                                  headers=auth_headers)).status_code == 422

    headers, client = await _scoped_headers(anon_client, api_app, auth_headers, "部门A")
    try:
        denied = await client.get("/api/v1/admin/agent-usage", headers=headers)
        assert denied.status_code == 403 and "需要管理员权限" in denied.json()["detail"]
        assert (await client.get("/api/v1/ai/agent/usage", headers=headers)).status_code == 200
    finally:
        await client.aclose()


