"""真 PostgreSQL + 真 ASGI 的接口测试。

隔离方式：conftest 的外层事务 + savepoint，所以这里可以放心地让接口真的 commit、
真的写库，用例结束统一回滚（库里不留数据）。
"""

from __future__ import annotations

import io
import sys
import uuid
from datetime import datetime
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


async def test_department_is_server_side_not_from_header(anon_client, auth_headers):
    me = await anon_client.get("/api/v1/auth/me", headers=auth_headers)
    assert me.status_code == 200
    assert me.json()["department"] is None
    assert me.json()["unrestricted"] is True

    # 伪造请求头自称管理员：不影响服务端记录的部门
    forged = await anon_client.get("/api/v1/auth/me", headers={**auth_headers, "X-User-Department": "finance-admin"})
    assert forged.json()["department"] is None

    updated = await anon_client.patch("/api/v1/auth/me", headers=auth_headers, json={"department": " 部门-097 "})
    assert updated.status_code == 200
    assert updated.json()["department"] == "部门-097"
    assert updated.json()["unrestricted"] is False

    cleared = await anon_client.patch("/api/v1/auth/me", headers=auth_headers, json={"department": None})
    assert cleared.json()["department"] is None
    assert cleared.json()["unrestricted"] is True


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


async def test_rag_search_filters_by_department(anon_client, auth_headers, stub_retrieval):
    response = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                      json={"query": "奖金方案", "top_k": 5})
    assert response.status_code == 200, response.text
    assert {item["source"] for item in response.json()["data"]} == {"pytest-stub"}
    contents = [item["content"] for item in response.json()["data"]]
    assert any("公共资料" in item for item in contents)
    assert any("部门A" in item for item in contents)

    await anon_client.patch("/api/v1/auth/me", headers=auth_headers, json={"department": "部门A"})
    scoped = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                    json={"query": "奖金方案", "top_k": 5})
    contents = [item["content"] for item in scoped.json()["data"]]
    assert any("部门A" in item for item in contents)
    assert not any("部门B" in item for item in contents), "不应看到其他部门的资料"

    # 伪造请求头不能提权
    forged = await anon_client.post("/api/v1/rag/search",
                                    headers={**auth_headers, "X-User-Department": "finance-admin"},
                                    json={"query": "奖金方案", "top_k": 5})
    assert not any("部门B" in item["content"] for item in forged.json()["data"])


async def test_rag_search_rejects_mismatched_embedding_model(anon_client, auth_headers, stub_retrieval):
    response = await anon_client.post("/api/v1/rag/search", headers=auth_headers,
                                      json={"query": "奖金", "embedding_model": "other-model",
                                            "embedding": [0.0] * 384})
    assert response.status_code == 422
    assert "模型标识" in response.json()["detail"]


async def test_agent_stream_requires_token_and_streams_sse(anon_client, auth_headers, stub_retrieval, monkeypatch):
    def fake_stream(question, context):
        yield "依据"
        yield "证据"

    monkeypatch.setattr("backend.api_ai.get_llm", lambda: SimpleNamespace(stream=fake_stream))
    async with anon_client.stream("POST", "/api/v1/ai/agent/chat/stream", headers=auth_headers,
                                  json={"question": "差旅费标准是多少？"}) as response:
        assert response.status_code == 200
        body = "".join([chunk async for chunk in response.aiter_text()])
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
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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


async def test_system_status_requires_token(anon_client):
    assert (await anon_client.get("/api/v1/system/status")).status_code == 401


# ---------------------------------------------------------------- 管理员入口

ADMIN_PATHS = ("/api/v1/admin/overview", "/api/v1/admin/users",
               "/api/v1/admin/batches", "/api/v1/admin/knowledge")


async def test_admin_endpoints_require_token(anon_client):
    for path in ADMIN_PATHS:
        assert (await anon_client.get(path)).status_code == 401, path


async def test_admin_rejects_department_scoped_account(anon_client, api_app):
    """部门不为空的账号必须 403，而不是拿到空数据（否则"看不到"会被误读成"没有"）。"""
    credentials = {"username": f"scoped_{uuid.uuid4().hex[:8]}", "password": "pytest-password-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=credentials)).status_code == 200
    token = (await anon_client.post("/api/v1/auth/login", json=credentials)).json()["access_token"]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app),
                                 base_url="http://testserver", trust_env=False) as scoped:
        headers = {"Authorization": f"Bearer {token}"}
        me = await scoped.patch("/api/v1/auth/me", json={"department": "部门A"}, headers=headers)
        assert me.status_code == 200, me.text
        assert me.json()["unrestricted"] is False
        for path in ADMIN_PATHS:
            response = await scoped.get(path, headers=headers)
            assert response.status_code == 403, path
            assert "管理员" in response.json()["detail"]
        assert (await scoped.delete("/api/v1/admin/knowledge?source=x", headers=headers)).status_code == 403


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


async def test_admin_user_department_update_and_self_protection(anon_client, auth_headers, credentials):
    # credentials 夹具已经通过 auth_headers 注册并登录（该账号部门为空 → 管理员）
    target = {"username": f"managed_{uuid.uuid4().hex[:8]}", "password": "pytest-password-2026"}
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
                              "department": "部门B", "unrestricted": False}

    cleared = await anon_client.patch(f"/api/v1/admin/users/{target_id}",
                                      json={"department": "  "}, headers=auth_headers)
    assert cleared.json()["department"] == ""
    assert cleared.json()["unrestricted"] is True

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
    from backend import api_admin
    from backend.rag.pgvector_store import DocumentChunk
    from sqlalchemy import func, select

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


async def _scoped_headers(anon_client, api_app, department: str) -> tuple[dict, httpx.AsyncClient]:
    """注册一个账号、把部门写进服务端记录，返回带 token 的**独立**客户端。

    注意这个客户端已经在 PATCH /auth/me 时被用过，httpx 不允许再 `async with` 它
    （RuntimeError: Cannot open a client instance more than once），用完 `await aclose()`。
    """
    credentials = {"username": f"scoped_{uuid.uuid4().hex[:8]}", "password": "pytest-password-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=credentials)).status_code == 200
    token = (await anon_client.post("/api/v1/auth/login", json=credentials)).json()["access_token"]
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app),
                               base_url="http://testserver", trust_env=False)
    headers = {"Authorization": f"Bearer {token}"}
    me = await client.patch("/api/v1/auth/me", json={"department": department}, headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["unrestricted"] is False
    return headers, client


async def test_scoped_account_reads_only_its_own_department(anon_client, api_app, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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

    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
    try:
        assert (await scoped.get("/api/v1/admin/departments", headers=headers)).status_code == 403
    finally:
        await scoped.aclose()


# ---------------------------------------------------------------- 登录限流与占位密钥

async def test_login_is_rate_limited_by_username_and_source(anon_client):
    """连续失败到阈值后返回 429，并带 Retry-After；换一个用户名不受影响。"""
    from backend.ratelimit import SlidingWindowLimiter, get_login_limiter, reset_login_limiter

    username = f"nobody_{uuid.uuid4().hex[:8]}"
    reset_login_limiter(SlidingWindowLimiter(limit=2, window_seconds=60))
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
        # 限速是按「用户名 + 来源」分桶的，不能把别人一起锁住
        other = await anon_client.post("/api/v1/auth/login",
                                       json={"username": f"other_{uuid.uuid4().hex[:6]}",
                                             "password": "wrong-password-2026"})
        assert other.status_code == 401
    finally:
        reset_login_limiter()
    assert get_login_limiter().limit == get_settings().login_max_attempts


async def test_login_limiter_resets_after_success(anon_client, credentials):  # noqa: ARG001
    """成功后清掉计数：否则正常用户偶尔敲错几次就会被自己的失败次数锁在门外。"""
    from backend.ratelimit import SlidingWindowLimiter, reset_login_limiter

    username = f"reset_{uuid.uuid4().hex[:8]}"
    password = "pytest-password-2026"
    assert (await anon_client.post("/api/v1/auth/register",
                                   json={"username": username, "password": password})).status_code == 200
    limiter = SlidingWindowLimiter(limit=3, window_seconds=60)
    reset_login_limiter(limiter)
    try:
        for _ in range(2):
            wrong = await anon_client.post("/api/v1/auth/login",
                                           json={"username": username, "password": "wrong-pass-2026"})
            assert wrong.status_code == 401
        ok = await anon_client.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert ok.status_code == 200, ok.text
        assert limiter.tracked_keys == 0
    finally:
        reset_login_limiter()


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
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
    try:
        listing = await scoped.get("/api/v1/analysis/departments", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["switchable"] is False
        assert [item["name"] for item in listing.json()["departments"]] == ["部门A"]

        for path in (f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门B",
                     f"/api/v1/dashboard?period=month&department=部门B",
                     f"/api/v1/reports/monthly?period={SCOPE_PERIOD}&department=部门B",
                     f"/api/v1/reports/pptx/outline?period={SCOPE_PERIOD}&department=部门B",
                     f"/api/v1/export/transactions?department=部门B"):
            response = await scoped.get(path, headers=headers)
            assert response.status_code == 403, f"{path} -> {response.status_code}"
            assert "不能查看" in response.json()["detail"]

        # 填自己那个部门是允许的（前端可能把当前口径带在 URL 上）
        own = await scoped.get(f"/api/v1/analysis/summary?period={SCOPE_PERIOD}&department=部门A", headers=headers)
        assert own.status_code == 200 and Decimal(own.json()["revenue_amount"]) == Decimal("300.00")
    finally:
        await scoped.aclose()


# ---------------------------------------------------------------- 数据导出

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
    assert lines[0].startswith("交易日期,科目编码,科目名称,部门,摘要")
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
    assert header[0] == "交易日期" and "导入批次" in header
    assert sheet.max_row == 3
    revenue_column = header.index("收入金额") + 1
    revenues = [sheet.cell(row=row, column=revenue_column).value for row in range(2, sheet.max_row + 1)]
    assert all(isinstance(value, (int, float, Decimal)) for value in revenues), revenues
    assert {Decimal(str(value)) for value in revenues} == {Decimal("300.00"), Decimal("700.00")}
    department_column = header.index("部门") + 1
    departments = {sheet.cell(row=row, column=department_column).value for row in range(2, sheet.max_row + 1)}
    assert departments == {"部门A", "部门B"}


async def test_export_only_contains_callers_scope(anon_client, api_app, auth_headers, db_session):
    """导出是最容易漏权限的出口：受限账号导出的明细里不能出现别的部门。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
    try:
        response = await scoped.get(f"/api/v1/export/transactions?format=csv", headers=headers)
        assert response.status_code == 200, response.text
        body = response.content.decode("utf-8-sig")
        assert "部门A" in body and "部门B" not in body
        metrics = await scoped.get("/api/v1/export/metrics?format=csv", headers=headers)
        metric_text = metrics.content.decode("utf-8-sig")
        assert "部门 部门A" in metric_text and "全公司" not in metric_text
        assert "1000.00" not in metric_text  # 全公司口径的收入不能出现在部门导出里
    finally:
        await scoped.aclose()


async def test_export_metrics_does_not_add_department_rows_together(anon_client, auth_headers, db_session):
    """管理员导出指标时只能拿到全公司行：两套行混在一起是这套权限模型最危险的失效方式。"""
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    response = await anon_client.get(f"/api/v1/export/metrics?format=csv", headers=auth_headers)
    assert response.status_code == 200
    text = response.content.decode("utf-8-sig")
    rows = [line for line in text.strip().splitlines() if line][1:]
    matching = [row for row in rows if SCOPE_PERIOD in row]
    assert len(matching) == 1, matching
    assert "全公司" in matching[0] and "1000.00" in matching[0]

    scoped_export = await anon_client.get(
        f"/api/v1/export/metrics?format=csv&department=部门B", headers=auth_headers)
    scoped_rows = [line for line in scoped_export.content.decode("utf-8-sig").strip().splitlines() if line][1:]
    scoped_matching = [row for row in scoped_rows if SCOPE_PERIOD in row]
    assert len(scoped_matching) == 1 and "700.00" in scoped_matching[0]


async def test_export_alerts_and_scope_helper_require_token(anon_client, auth_headers, db_session):
    _seed_scoped_period(db_session, SCOPE_PERIOD)
    alerts = await anon_client.get(f"/api/v1/export/alerts?period={SCOPE_PERIOD}&format=csv",
                                   headers=auth_headers)
    assert alerts.status_code == 200
    text = alerts.content.decode("utf-8-sig")
    assert text.startswith("期间,口径,预警类型")
    assert "需人工复核" in text.splitlines()[0]
    assert "全公司口径测试预警" in text and "部门B口径测试预警" not in text

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

    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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
    from backend.models import AlertRule, FinancialTransaction, User
    from backend.rag.pgvector_store import DocumentChunk  # chunk 表在 RAG 模块里，不在 models.py

    # 1) 改预警规则（旧值/新值都要在 detail 里）
    rule = await anon_client.put("/api/v1/admin/alert-rules/利润异常",
                                 json={"threshold_value": "-1", "level": "medium"}, headers=auth_headers)
    assert rule.status_code == 200, rule.text

    # 2) 改他人部门 + 3) 删账号（另外造一个账号，避免动到自己）
    victim = {"username": f"audit_{uuid.uuid4().hex[:8]}", "password": "audit-password-2026"}
    assert (await anon_client.post("/api/v1/auth/register", json=victim)).status_code == 200
    victim_id = db_session.scalar(select(User.id).where(User.username == victim["username"]))
    changed = await anon_client.patch(f"/api/v1/admin/users/{victim_id}",
                                      json={"department": "审计测试部"}, headers=auth_headers)
    assert changed.status_code == 200, changed.text
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
    for action in ("alert_rule.update", "user.department_change", "user.delete", "knowledge.delete",
                   "metrics.calculate", "alert.review", "data.export"):
        assert _find(rows, action) is not None, f"缺少审计：{action}"

    rule_row = _find(rows, "alert_rule.update", "利润异常")
    assert rule_row["detail"]["before"]["threshold_value"] == "0.0000"
    assert rule_row["detail"]["after"]["threshold_value"] == "-1.0000"
    assert rule_row["detail"]["after"]["level"] == "medium"

    department_row = _find(rows, "user.department_change", victim_id)
    assert department_row["detail"]["username"] == victim["username"]
    assert department_row["detail"]["from"] == "不受限"
    assert department_row["detail"]["to"] == "审计测试部"

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
    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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

    # 只读：审计接口只有 GET，没有删除/清空入口（管理员也清不掉）
    methods = set(api_app.openapi()["paths"]["/api/v1/admin/audit"].keys())
    assert methods == {"get"}, methods


def test_audit_scrub_drops_sensitive_keys():
    """脱敏规则单测：含 password/token/secret 的键整条丢掉，超长字符串截断。"""
    from backend import audit

    cleaned = audit._scrub({
        "password": "pytest-password-2026",
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


async def test_jobs_list_filters_and_restart_reaping(anon_client, api_app, auth_headers, db_session):
    """任务列表的筛选/分页与"进程重启收尸"，以及受限账号不得查看后台任务。"""
    from backend import jobs
    from backend.models import BackgroundJob

    queued = jobs.submit(db_session, "batch.revert", {"batch": "pytest-queued"}, actor="pytest")
    running = jobs.submit(db_session, "batch.revert", {"batch": "pytest-running"}, actor="pytest")
    running.status = "running"
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

    # 进程重启：还挂在 queued/running 的记录标记为失败（否则界面永远显示"运行中"）
    reaped = jobs.reap_stale_jobs(db_session)
    assert reaped >= 2
    for job_id in (queued.id, running.id):
        fetched = await anon_client.get(f"/api/v1/admin/jobs/{job_id}", headers=auth_headers)
        assert fetched.json()["status"] == "failed"
        assert fetched.json()["error"] == "进程重启，任务未完成"

    headers, scoped = await _scoped_headers(anon_client, api_app, "部门A")
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


# ---------------------------------------------------------------- 结构漂移

async def test_schema_matches_models(require_database):
    """模型加了列却忘了写进 `migrations.COLUMNS` 时，这里就炸，而不是等接口 500。

    这是当初 `financial_alerts.review_status`（`UndefinedColumn`）和 `audit_logs`
    （`relation does not exist`）两次翻车的回归闸门：`create_all()` 只建缺失的**表**，
    不会给已存在的表补**列**，所以补列清单必须手工维护 —— 一旦漏了，本用例把列名点出来。
    """
    from backend.migrations import ensure_schema, missing_columns

    ensure_schema()
    assert missing_columns() == []
