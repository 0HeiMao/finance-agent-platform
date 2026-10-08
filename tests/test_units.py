"""不依赖数据库与外部模型的纯逻辑测试：校验规则、文档切分、部门可见性、看板计算。"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.api_financial import AMOUNT_COLUMNS, validate_row
from backend.api_rag import DEPARTMENT_ADMIN, filter_visible, user_department_of
from backend.rag.document_parser import extract_text, split_text


# ---------------------------------------------------------------- 导入校验

def test_validate_row_rounds_amount_half_up():
    row = {"交易日期": "2099-01-01", "科目名称": "测试", "收入金额": "100.005", "成本金额": "90", "费用金额": "20"}
    date, amounts = validate_row(row)
    assert amounts["收入金额"] == Decimal("100.01")
    assert amounts["成本金额"] == Decimal("90.00")
    assert amounts["费用金额"] == Decimal("20.00")
    assert date.year == 2099


def test_validate_row_accepts_thousands_separator():
    row = {"交易日期": "2099-01-01", "科目名称": "测试", "收入金额": "1,234.5"}
    _, amounts = validate_row(row)
    assert amounts["收入金额"] == Decimal("1234.50")


def test_validate_row_rejects_timezone_aware_date():
    row = {"交易日期": "2099-01-01T00:00:00+08:00", "科目名称": "测试"}
    with pytest.raises(ValueError, match="时区"):
        validate_row(row)


def test_validate_row_requires_account_name():
    with pytest.raises(ValueError, match="科目名称"):
        validate_row({"交易日期": "2099-01-01", "科目名称": "  "})


def test_validate_row_rejects_out_of_range_amount():
    row = {"交易日期": "2099-01-01", "科目名称": "测试", "收入金额": "10000000000000000"}
    with pytest.raises(ValueError, match="超出范围"):
        validate_row(row)


def test_validate_row_reports_column_and_value_for_bad_amount():
    """错误信息必须能定位到列和原值：原来会抛 decimal.InvalidOperation 的内部文本。"""
    row = {"交易日期": "2099-01-01", "科目名称": "测试", "费用金额": "1.2.3"}
    with pytest.raises(ValueError) as excinfo:
        validate_row(row)
    assert "费用金额" in str(excinfo.value)
    assert "1.2.3" in str(excinfo.value)


def test_validate_row_reports_unparsable_date():
    with pytest.raises(ValueError) as excinfo:
        validate_row({"交易日期": "不是日期", "科目名称": "测试"})
    assert "交易日期" in str(excinfo.value)
    assert "不是日期" in str(excinfo.value)


def test_amount_columns_match_import_template():
    assert AMOUNT_COLUMNS == ["收入金额", "成本金额", "费用金额", "现金流入", "现金流出"]


# ---------------------------------------------------------------- 文档解析

def test_split_text_normalizes_whitespace_and_respects_chunk_size():
    text = "第一段。\n\n  第二段   有空格。\t第三段。"
    chunks = split_text(text, chunk_size=12, overlap=4)
    assert chunks, "切分结果不应为空"
    assert all("  " not in chunk for chunk in chunks), "连续空白应被归一"
    assert all(len(chunk) <= 12 for chunk in chunks), "每片不应超过 chunk_size"
    assert chunks[0].startswith("第一段。")
    assert chunks[-1].endswith("第三段。"), "结尾内容不应丢失"
    # 重叠设计为：后一片以前一片末尾 overlap 个字符开头
    assert chunks[1].startswith(chunks[0][-4:])


def test_split_text_single_short_chunk():
    assert split_text("只有一句话。", chunk_size=100, overlap=10) == ["只有一句话。"]


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-8", "gb18030"])
def test_extract_text_decodes_known_encodings(tmp_path, encoding):
    target = tmp_path / "note.txt"
    target.write_bytes("差旅费报销标准为每晚500元。".encode(encoding))
    assert "差旅费" in extract_text(str(target))


def test_extract_text_rejects_unknown_encoding(tmp_path):
    target = tmp_path / "note.txt"
    target.write_bytes(b"\xff\xfe\x00\x01\x02\x03")
    with pytest.raises(ValueError):
        extract_text(str(target))


def test_extract_text_rejects_unsupported_suffix(tmp_path):
    target = tmp_path / "note.xlsx"
    target.write_bytes(b"anything")
    with pytest.raises(ValueError):
        extract_text(str(target))


# ---------------------------------------------------------------- 部门可见性
def _chunk(department):
    return SimpleNamespace(metadata_json={"department": department})


def test_filter_visible_keeps_public_and_own_department():
    chunks = [_chunk(None), _chunk(""), _chunk("部门A"), _chunk("部门B")]
    visible = [item.metadata_json["department"] for item in filter_visible(chunks, "部门A")]
    assert visible == [None, "", "部门A"]


def test_filter_visible_admin_and_unset_see_everything():
    chunks = [_chunk("部门A"), _chunk("部门B")]
    assert len(filter_visible(chunks, None)) == 2
    assert len(filter_visible(chunks, DEPARTMENT_ADMIN)) == 2


def test_user_department_prefers_server_side_record():
    user = SimpleNamespace(department="部门A")
    # 请求头自报 finance-admin 不能提权：部门以服务端记录为准
    assert user_department_of(user, DEPARTMENT_ADMIN) == "部门A"
    assert user_department_of(user, "部门B") == "部门A"


def test_user_department_ignores_forged_legacy_header():
    user = SimpleNamespace(department=None)
    assert user_department_of(user, "部门B") is None
    # 只有旧用户自己声明 finance-admin 才兼容接受
    assert user_department_of(user, DEPARTMENT_ADMIN) == DEPARTMENT_ADMIN


# ---------------------------------------------------------------- 看板计算

def test_previous_months_month_and_quarter():
    from backend.api_dashboard import _previous_months

    assert _previous_months(["2026-04"], "month") == ["2026-03"]
    assert _previous_months(["2026-01"], "month") == ["2025-12"]
    assert _previous_months(["2026-04", "2026-05", "2026-06"], "quarter") == ["2026-01", "2026-02", "2026-03"]


def test_previous_months_year_shifts_back_twelve_months():
    from backend.api_dashboard import _previous_months

    previous = _previous_months(["2026-04"], "year")
    assert len(previous) == 12
    assert previous[-1] == "2026-03"
    assert previous[0] == "2025-04"


def test_kpi_without_previous_reports_no_comparison():
    from backend.api_dashboard import _kpi

    kpi = _kpi("费用金额", Decimal("5886320931394.83"), "元", "icon", Decimal("0.8"))
    assert kpi["value"] == "5,886,320,931,394.83"
    assert kpi["change"] is None
    assert kpi["dir"] == "flat"
    assert kpi["note"] == "无可比期间"


def test_kpi_computes_change_and_direction():
    from backend.api_dashboard import _kpi

    down = _kpi("费用金额", Decimal("50"), "元", "icon", Decimal("0.8"), previous=Decimal("100"))
    assert down["change"] == "50.00%"
    assert down["dir"] == "down"

    up = _kpi("营业收入", Decimal("150"), "元", "icon", Decimal("0"), previous=Decimal("100"))
    assert up["change"] == "50.00%"
    assert up["dir"] == "up"


def test_kpi_with_zero_previous_says_not_comparable():
    from backend.api_dashboard import _kpi

    kpi = _kpi("成本金额", Decimal("0"), "元", "icon", Decimal("0.8"), previous=Decimal("0"), compare_label="上期")
    assert kpi["change"] is None
    assert "无法计算" in kpi["note"]
    assert "上期" in kpi["note"]


def test_observability_reports_duration_per_path():
    """可观测性要回答「哪个接口慢」：全局 P95 之外，必须有按路径的采样。"""
    from backend.observability import record_request, snapshot

    for duration in (10.0, 20.0, 30.0):
        record_request("/api/v1/unit-slow", 200, duration)
    record_request("/api/v1/unit-slow", 500, 1200.0)
    record_request("/api/v1/unit-fast", 200, 3.0)

    observed = snapshot()["paths"]
    slow = observed["/api/v1/unit-slow"]
    assert slow["count"] == 4
    assert slow["p95_ms"] == 1200.0
    assert slow["max_ms"] == 1200.0
    assert observed["/api/v1/unit-fast"]["p95_ms"] == 3.0
    # 状态码也分桶：500 那次不该混进 200 的计数
    assert snapshot()["requests"]["200:/api/v1/unit-slow"] == 3
    assert snapshot()["requests"]["500:/api/v1/unit-slow"] == 1
