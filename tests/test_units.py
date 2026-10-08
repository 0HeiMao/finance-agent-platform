"""不依赖数据库与外部模型的纯逻辑测试：校验规则、文档切分、部门可见性、看板计算。"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import check_openapi_snapshot as snapshot_gate  # noqa: E402
from check_coverage import parse_counts, parse_total  # noqa: E402
from check_frontend_coverage import COVERED_MODULES, parse_coverage_table  # noqa: E402

from backend.api_financial import AMOUNT_COLUMNS, validate_row  # noqa: E402
from backend.api_rag import DEPARTMENT_ADMIN, filter_visible, user_department_of  # noqa: E402
from backend.main import app  # noqa: E402
from backend.rag.document_parser import extract_text, split_text  # noqa: E402

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
    visible = [item.metadata_json["department"] for item in filter_visible(chunks, ("部门A",))]
    assert visible == [None, "", "部门A"]


def test_filter_visible_admin_and_unset_see_everything():
    chunks = [_chunk("部门A"), _chunk("部门B")]
    assert len(filter_visible(chunks, None)) == 2
    assert len(filter_visible(chunks, (DEPARTMENT_ADMIN,))) == 2


def test_filter_visible_multi_department_is_a_union():
    """多部门账号看到的是并集：两个部门的资料都可见，第三个部门仍然挡住。"""
    chunks = [_chunk(None), _chunk("部门A"), _chunk("部门B"), _chunk("部门C")]
    visible = [item.metadata_json["department"] for item in filter_visible(chunks, ("部门A", "部门B"))]
    assert visible == [None, "部门A", "部门B"]


def test_user_department_prefers_server_side_record():
    user = SimpleNamespace(department="部门A")
    # 请求头自报 finance-admin 不能提权：部门以服务端记录为准
    assert user_department_of(user, DEPARTMENT_ADMIN) == ("部门A",)
    assert user_department_of(user, "部门B") == ("部门A",)


def test_user_department_reads_multi_department_list():
    """`departments` 是权威值：有列表就按列表，列表为空才回落到老的单值列。"""
    user = SimpleNamespace(departments=["部门A", "部门B"], department="部门A")
    assert user_department_of(user) == ("部门A", "部门B")
    assert user_department_of(user, DEPARTMENT_ADMIN) == ("部门A", "部门B")
    legacy = SimpleNamespace(departments=None, department="部门B")
    assert user_department_of(legacy) == ("部门B",)


# ---------------------------------------------------------------- 预警规则分档


def _rule(alert_type, *, department=None, period=None, threshold="0.8", rule_id=1, level="high"):
    """不碰数据库的规则对象：分档逻辑是纯函数，没必要为了测它建表。"""
    return SimpleNamespace(id=rule_id, alert_type=alert_type, department=department, period=period,
                           metric_name="cost_ratio", comparator="gt", threshold_value=Decimal(threshold),
                           level=level, require_metric=None, description="", enabled=True)


def test_scope_rank_counts_limited_dimensions():
    from backend.alert_rules import scope_rank

    assert scope_rank(_rule("成本率异常"), period="2097-10", department="部门A") == 0
    assert scope_rank(_rule("成本率异常", department="部门A"), period="2097-10", department="部门A") == 1
    # 部门限定了但不匹配 → 这条规则对这个部门根本不适用，不是「不具体」
    assert scope_rank(_rule("成本率异常", department="部门B"), period="2097-10", department="部门A") is None
    assert scope_rank(_rule("成本率异常", period="2097-10"), period="2097-09", department=None) is None
    both = _rule("成本率异常", department="部门A", period="2097-10")
    assert scope_rank(both, period="2097-10", department="部门A") == 2
    # 空部门规则对全公司那一行（department=None）同样适用
    assert scope_rank(_rule("成本率异常", period="2097-10"), period="2097-10", department=None) == 1


def test_resolve_rules_picks_most_specific_and_overrides():
    """同一个 alert_type 只留最具体的一条：覆盖而不是叠加，否则一个部门会同时收到两条同类预警。"""
    from backend.alert_rules import resolve_rules

    default = _rule("成本率异常", rule_id=1)
    by_department = _rule("成本率异常", department="部门A", rule_id=2, threshold="0.85")
    by_period = _rule("成本率异常", period="2097-10", rule_id=3, threshold="0.9")
    both = _rule("成本率异常", department="部门A", period="2097-10", rule_id=4, threshold="0.95")
    other = _rule("利润异常", rule_id=5)
    rules = [default, by_department, by_period, both, other]

    chosen = resolve_rules(rules, period="2097-10", department="部门A")
    assert [(rule.alert_type, rule.id) for rule in chosen] == [("利润异常", 5), ("成本率异常", 4)]
    # 其它部门/期间每个 alert_type 同样只留一条（返回按 alert_type、id 排序）
    assert [rule.id for rule in resolve_rules(rules, period="2097-10", department="部门B")] == [5, 3]
    assert [rule.id for rule in resolve_rules(rules, period="2097-09", department="部门A")] == [5, 2]
    assert [rule.id for rule in resolve_rules(rules, period="2097-09", department=None)] == [5, 1]


def test_resolve_rules_same_specificity_prefers_lowest_id():
    """同级按 id 小的胜出：库里存了重复规则时结果也要稳定、可复现，不能随机挑。"""
    from backend.alert_rules import resolve_rules

    older = _rule("成本率异常", department="部门A", rule_id=7, threshold="0.85")
    newer = _rule("成本率异常", department="部门A", rule_id=9, threshold="0.99")
    chosen = resolve_rules([newer, older], period="2097-10", department="部门A")
    assert [rule.id for rule in chosen] == [7]


def test_rule_scope_label_reads_both_dimensions():
    from backend.alert_rules import rule_scope_label

    assert rule_scope_label(_rule("成本率异常")) == "全公司 · 所有期间"
    assert rule_scope_label(_rule("成本率异常", department="018")) == "018 · 所有期间"
    assert rule_scope_label(_rule("成本率异常", period="2026-10")) == "全公司 · 2026-10"
    assert rule_scope_label(_rule("成本率异常", department="018", period="2026-10")) == "018 · 2026-10"


def test_user_department_ignores_forged_legacy_header():
    user = SimpleNamespace(department=None)
    assert user_department_of(user, "部门B") is None
    # 只有旧用户自己声明 finance-admin 才兼容接受
    assert user_department_of(user, DEPARTMENT_ADMIN) == (DEPARTMENT_ADMIN,)


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


def test_slow_path_alert_threshold_and_log_rate_limit(caplog):
    """超过阈值要主动告警，并且**按路径限流**——否则一个慢接口能把日志刷满。

    这正是「按路径耗时阈值告警」的实现要点：阈值可以按路径覆盖（重接口单独放宽），
    同一条路径在 `log_seconds` 内的后续超阈值请求不逐条上报，但条数要累计进下一次告警。
    """
    import logging

    from backend import observability

    observability.reset()
    observability.set_thresholds(
        default_ms=100, overrides={"/api/v1/unit-heavy": 500}, log_seconds=60)
    try:
        with caplog.at_level(logging.WARNING, logger="finance-api.slow"):
            observability.record_request("/api/v1/unit-fast", 200, 50.0)
            observability.record_request("/api/v1/unit-slow", 200, 150.0)   # 第 1 次告警
            observability.record_request("/api/v1/unit-slow", 200, 800.0)   # 被限流
            observability.record_request("/api/v1/unit-heavy", 200, 400.0)  # 未超覆盖阈值
            observability.record_request("/api/v1/unit-heavy", 200, 900.0)  # 第 2 次告警
            assert observability.threshold_for("/api/v1/unit-heavy") == 500
            assert observability.threshold_for("/api/v1/unit-nothing") == 100

        alerts = [record.getMessage() for record in caplog.records if "慢接口告警" in record.getMessage()]
        assert len(alerts) == 2, alerts
        assert any("path=/api/v1/unit-slow" in text and "累计超阈值=1 次" in text for text in alerts)
        assert any("path=/api/v1/unit-heavy" in text and "threshold_ms=500" in text for text in alerts)

        snapshot = observability.snapshot()
        assert snapshot["slow_default_ms"] == 100
        assert snapshot["slow_overrides"] == {"/api/v1/unit-heavy": 500}
        # 超阈值次数多的排前面；「最慢的接口」不受影响（unit-fast 也在里面）
        assert [row["path"] for row in snapshot["slow_paths"]] == ["/api/v1/unit-slow", "/api/v1/unit-heavy"]
        slow = snapshot["slow_paths"][0]
        assert (slow["threshold_ms"], slow["exceeded"], slow["worst_ms"]) == (100, 2, 800.0)
        assert snapshot["paths"]["/api/v1/unit-fast"]["p95_ms"] == 50.0

        # 限流窗口过去之后，下一批要把被压掉的次数报出来，而不是永远闭嘴
        observability.set_thresholds(log_seconds=0)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="finance-api.slow"):
            observability.record_request("/api/v1/unit-slow", 200, 160.0)
        assert any("累计超阈值=3 次" in record.getMessage()
                   and "另有 1 次被限流" in record.getMessage() for record in caplog.records)
    finally:
        observability.reset()


def test_slow_path_overrides_parser_skips_bad_entries():
    """`SLOW_PATH_OVERRIDES` 是调优旋钮：写坏一项只跳过它，不能让服务或解析崩。"""
    from backend.config import _parse_slow_path_overrides

    assert _parse_slow_path_overrides("/api/v1/rag/search=2000, /api/v1/ai/agent/chat=5000") == {
        "/api/v1/rag/search": 2000, "/api/v1/ai/agent/chat": 5000}
    # 缺等号、值不是数字、值非正、空路径一律忽略
    assert _parse_slow_path_overrides("bad,/a=xyz,/b=0,=100,/c=7") == {"/c": 7}
    assert _parse_slow_path_overrides("") == {}


# ---------------------------------------------------------------- 期间口径与落库


def test_period_grain_conversions_and_labels():
    """月/季/年的规范写法与换算：落库、接口、报告都靠这几个纯函数，错一处就全线错。"""
    from backend import periods

    assert periods.grain_of("2026-10") == "month"
    assert periods.grain_of("2026-Q3") == "quarter"
    assert periods.grain_of("2026") == "year"
    # 认不出来的必须返回 None，让调用方 422，而不是猜一个口径
    assert periods.grain_of("2026-13") is None
    assert periods.grain_of("2026-Q5") is None
    assert periods.grain_of("2026-1") is None
    assert periods.grain_of("") is None

    assert periods.quarter_of("2026-01") == "2026-Q1"
    assert periods.quarter_of("2026-03") == "2026-Q1"
    assert periods.quarter_of("2026-10") == "2026-Q4"
    assert periods.year_of("2026-10") == "2026"
    assert periods.containing_periods("2026-10") == ("2026-Q4", "2026")

    assert periods.months_of("2026-10") == ("2026-10",)
    assert periods.months_of("2026-Q3") == ("2026-07", "2026-08", "2026-09")
    assert periods.months_of("2026") == tuple(f"2026-{month:02d}" for month in range(1, 13))
    assert periods.months_of("随便写的") == ()
    assert periods.months_count("2026-Q3") == 3 and periods.months_count("2026") == 12

    assert periods.period_label("2026-10") == "2026 年 10 月"
    assert periods.period_label("2026-Q3") == "2026 年第 3 季度"
    assert periods.period_label("2026") == "2026 年度"
    assert periods.granularity_label("2026-Q3") == "季"


def test_previous_period_is_same_grain_and_crosses_year():
    """环比要的是同粒度上一期：季度比上季度，跨年要回退到上一年的 12 月 / Q4。

    拿季度跟去年同季比是**同比**，不是这里要的东西，所以不能把 `previous_period` 写成减一年。
    """
    from backend import periods

    assert periods.previous_period("2026-10") == "2026-09"
    assert periods.previous_period("2026-01") == "2025-12"
    assert periods.previous_period("2026-Q3") == "2026-Q2"
    assert periods.previous_period("2026-Q1") == "2025-Q4"
    assert periods.previous_period("2026") == "2025"
    assert periods.previous_period("0001") is None
    assert periods.previous_period("啥也不是") is None


def test_sort_key_keeps_same_grain_in_time_order():
    """同一粒度内必须按时间排：报告的分期与落库的「上一期」都依赖它。"""
    from backend import periods

    months = ["2026-10", "2026-07", "2026-01"]
    assert sorted(months, key=periods.sort_key) == ["2026-01", "2026-07", "2026-10"]
    quarters = ["2026-Q4", "2025-Q1", "2026-Q1"]
    assert sorted(quarters, key=periods.sort_key) == ["2025-Q1", "2026-Q1", "2026-Q4"]
    # 认不出来的期间被排到最早的位置（年份 0），不会混进正常序列中间
    assert periods.sort_key("坏数据")[0] == 0
    assert periods.sort_key("坏数据") < periods.sort_key("2026-01")


def test_period_bounds_are_half_open_and_cross_year():
    """期间区间必须是 `[首日, 次期首日)`：12 月要跨到下一年 1 月，季/年取首月与末月。"""
    from datetime import date

    from backend import periods

    assert periods.month_bounds("2026-10") == (date(2026, 10, 1), date(2026, 11, 1))
    assert periods.month_bounds("2026-12") == (date(2026, 12, 1), date(2027, 1, 1))
    assert periods.period_bounds("2026-10") == (date(2026, 10, 1), date(2026, 11, 1))
    assert periods.period_bounds("2026-Q4") == (date(2026, 10, 1), date(2027, 1, 1))
    assert periods.period_bounds("2026") == (date(2026, 1, 1), date(2027, 1, 1))
    assert periods.month_bounds("2026-13") is None
    assert periods.period_bounds("随便写的") is None


def test_period_range_is_sargable_and_never_uses_to_char():
    """期间条件不许写成 `to_char(transaction_date,'YYYY-MM') = ...`。

    函数套在列上，`(department, transaction_date)` 索引的第二列就用不上，而且每行都要算一次
    `to_char`；这里直接看编译出来的 SQL 形状，谁改回去谁炸。顺带确认区间是前闭后开
    （月末最后一天的零点在区间内），以及认不出来的期间退化成「一行都不匹配」而不是报错。
    """
    from backend import periods
    from backend.models import FinancialTransaction

    column = FinancialTransaction.transaction_date
    sql = str(periods.period_range(column, "2026-10"))
    assert "to_char" not in sql.lower()
    assert "transaction_date >= " in sql and "transaction_date < " in sql

    bounds = periods.period_range(column, "2026-Q4").compile().params
    assert sorted(str(value) for value in bounds.values()) == ["2026-10-01", "2027-01-01"]

    # 「一行都不匹配」：与旧的 to_char 相等比较在非法期间上的行为一致，调用方不用写额外分支
    assert str(periods.period_range(column, "2026-13")).lower() == "false"


def test_combine_sums_amounts_and_recomputes_ratios():
    """季度/年度合计：金额相加、比率用相加后的金额重算。

    比率不能相加也不能平均——部门口径下收入极小、费用很大时比率能冲到 10^6，
    平均一下就是编数字。
    """
    from backend.rollups import _combine, department_single
    from backend.scope import MetricTotals

    def totals(period, revenue, expense, department=None):
        return MetricTotals(
            period=period, departments=(department,) if department else (),
            department=department, revenue_amount=Decimal(revenue),
            cost_amount=Decimal("0"), expense_amount=Decimal(expense),
            gross_profit_amount=Decimal(revenue),
            net_profit_amount=Decimal(revenue) - Decimal(expense),
            gross_profit_ratio=Decimal("100.00"), net_profit_ratio=Decimal("100.00"))

    monthly = [totals("2026-07", "1000.00", "200.00"), totals("2026-08", "1000.00", "800.00")]
    combined = _combine(monthly, "2026-Q3")[0]
    assert combined.period == "2026-Q3"
    assert combined.revenue_amount == Decimal("2000.00")
    assert combined.expense_amount == Decimal("1000.00")
    assert combined.net_profit_amount == Decimal("1000.00")
    # 比率按 1000 / 2000 重算，不是 100 和 100 相加或平均
    assert combined.net_profit_ratio == Decimal("0.5000")
    assert combined.departments == ()

    # 参与合计的只有一个部门时口径仍是那个部门；混了多个部门就变成「不属于任何一个部门」
    assert department_single([totals("2026-07", "1", "1", "018")]) is True
    assert department_single([totals("2026-07", "1", "1", "018"), totals("2026-08", "1", "1", "018")]) is True
    assert department_single([totals("2026-07", "1", "1", "018"), totals("2026-08", "1", "1", "097")]) is False


# ---------------------------------------------------------------- 期间锁（并发重算）
def test_period_lock_key_is_stable_namespaced_and_signed_64_bit():
    """锁 key 必须跨进程/跨重启稳定、不同期间不撞号，并且能塞进 Postgres 的 bigint。

    `pg_advisory_xact_lock` 的 key 是**整个数据库共享**的一个数字空间，所以要用命名空间前缀
    哈希，别拿一个自增序号（两个库/两类用途会撞上同一个数字，表现为毫不相干的请求互相阻塞）。
    """
    from backend import locks

    assert locks.lock_key("2026-07") == locks.lock_key("2026-07")
    assert locks.lock_key("2026-07") != locks.lock_key("2026-08")
    assert locks.lock_key("2026-07") != locks.lock_key("2026")
    assert locks.NAMESPACE, "没有命名空间就等于裸哈希，容易被别处撞上"
    keys = {locks.lock_key(name) for name in ("2026", "2026-Q2", "2026-04", "2026-05", "2026-06")}
    assert len(keys) == 5, "不同期间不能算出同一个 key"
    for key in keys:
        assert -(2 ** 63) <= key < 2 ** 63, "超出 bigint 会直接报错，等于锁不上"


def test_period_lock_set_includes_quarter_and_year():
    """锁的期间全集 = 这些月 + 它们所在的季与年。

    月重算会连带刷新季/年落库行（`rollups.refresh_for_months`），所以季/年也是同一个临界区；
    只锁月份时，两个不同月份的并发重算会各自把季度合计覆盖成少算一个月的版本。
    """
    from backend import locks

    assert locks.period_lock_set(["2026-05"]) == ["2026", "2026-05", "2026-Q2"]
    assert locks.period_lock_set(["2026-12"]) == ["2026", "2026-12", "2026-Q4"]
    # 多个期间合并成一个集合（一次取全，不是逐期分开取）
    assert locks.period_lock_set(["2026-06", "2026-05"]) == ["2026", "2026-05", "2026-06", "2026-Q2"]
    # 重复、空值、季/年与乱写的期间：去重、忽略空串，认不出的只锁它自己
    assert locks.period_lock_set(["2026-05", "2026-05", "", None]) == ["2026", "2026-05", "2026-Q2"]
    assert locks.period_lock_set(["2026-Q2"]) == ["2026-Q2"]


def test_lock_periods_takes_sorted_unique_locks_in_one_batch():
    """`lock_periods()` 按排序去重的顺序逐个加锁，且顺序必须每次都一样。

    顺序本身不重要，**一致**才重要：两个请求以相反顺序去抢同一组锁就是 deadlock。
    这里用一个假 Session 记录发出去的参数，连「重复的期间只锁一次」也一起卡住。
    """
    from backend import locks

    class FakeSession:
        def __init__(self):
            self.keys: list[int] = []

        def execute(self, statement, parameters=None):
            assert "pg_advisory_xact_lock" in str(statement)
            self.keys.append(parameters["key"])

    fake = FakeSession()
    taken = locks.lock_periods(fake, ["2026-Q2", "2026-05", "2026-05", "2026"])
    assert taken == ["2026", "2026-05", "2026-Q2"]
    assert fake.keys == [locks.lock_key(name) for name in taken]
    # 乱序传入也要收敛成同一串 key，否则两个调用方会以相反顺序互抢
    other = FakeSession()
    locks.lock_periods(other, ["2026", "2026-Q2", "2026-05"])
    assert other.keys == fake.keys

    # 补全发生在 lock_periods 内部：只传月名也要连它的季与年一起锁
    # （真机脚本 scripts/verify_recalc_lock.py 第一版就是在这里露馅的：调用方传原始月名，
    #  结果只锁住了月，季/年的落库行没人保护，而且不会报任何错）
    month_only = FakeSession()
    assert locks.lock_periods(month_only, ["2026-05"]) == ["2026", "2026-05", "2026-Q2"]
    assert month_only.keys == [locks.lock_key(name) for name in ("2026", "2026-05", "2026-Q2")]
    assert locks.lock_periods(FakeSession(), ["2026"] + locks.period_lock_set(["2026-05"])) == \
        ["2026", "2026-05", "2026-Q2"], "传已补全的集合要幂等"

    empty = FakeSession()
    assert locks.lock_periods(empty, []) == []
    assert empty.keys == [], "没有期间就不该发语句"


# ------------------------------------------------- 登录失败锁定与弱口令（④）

def _lockout_settings(**overrides):
    """一套确定的锁定参数：单测不该被 `.env` 里的阈值改动带跑。"""
    base = {"login_lockout_threshold": 5, "login_lockout_base_seconds": 60,
            "login_lockout_max_seconds": 900, "login_failure_ttl_seconds": 900,
            "password_min_length": 10}
    base.update(overrides)
    return SimpleNamespace(**base)


def test_lockout_backoff_doubles_from_threshold_and_caps(monkeypatch):
    """退避序列：阈值以下不锁，到阈值从 60s 起翻倍，封顶 900s。"""
    from backend import lockout

    monkeypatch.setattr(lockout, "get_settings", lambda: _lockout_settings())
    assert [lockout.lock_seconds(n) for n in (1, 4)] == [0, 0]
    assert [lockout.lock_seconds(n) for n in (5, 6, 7, 8)] == [60, 120, 240, 480]
    assert lockout.lock_seconds(9) == 900, "60×2^4=960 要封顶到 900"
    assert lockout.lock_seconds(200) == 900, "指数不能溢出成天文数字"


def test_lockout_counts_failures_locks_then_restarts_after_silence(monkeypatch):
    """累计失败到阈值就上锁；静默期一过，计数从 0 重新数（慢慢试的爆破不被永久累积）。"""
    from backend import lockout
    from backend.models import User

    monkeypatch.setattr(lockout, "get_settings", lambda: _lockout_settings())
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    user = User(username="lock-probe")  # 没落库：failed_attempts 还是 None，必须按 0 处理
    assert user.failed_attempts is None

    states = [lockout.register_failure(user, now=now) for _ in range(5)]
    assert [s["failed_attempts"] for s in states] == [1, 2, 3, 4, 5]
    assert [s["locked_for"] for s in states[:4]] == [0, 0, 0, 0]
    assert states[-1]["locked_for"] == 60
    assert lockout.locked_for(user, now=now) == 60
    assert lockout.is_locked(user, now=now) is True

    # 静默期之外重新开始：这一串失败与被锁的那一串无关
    later = now + timedelta(seconds=901)
    fresh = lockout.register_failure(user, now=later)
    assert fresh["failed_attempts"] == 1
    assert fresh["locked_for"] == 0
    assert lockout.locked_for(user, now=later) == 0
    assert user.locked_until is None, "没到阈值就不该留下一个「锁到过去」的时间"


def test_locked_for_accepts_naive_datetime_and_clears_nothing(monkeypatch):
    """数据库/手写数据可能是 naive 时间：按 UTC 处理，不能因为缺 tzinfo 就当成已解锁。"""
    from backend import lockout
    from backend.models import User

    naive = datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=120)
    user = User(username="naive-probe", locked_until=naive, failed_attempts=6)
    assert 115 <= lockout.locked_for(user) <= 120
    assert lockout.is_locked(user) is True
    assert lockout.locked_for(User(username="clean")) == 0


def test_unlock_clears_counter_and_reports_previous_state():
    """管理员解锁：计数与锁定时间清空，返回值里带上「清掉了几次、当时还剩多久」。"""
    from backend import lockout
    from backend.models import User

    now = datetime.now(UTC)
    user = User(username="unlock-probe", failed_attempts=7,
                locked_until=now + timedelta(seconds=300), last_failed_at=now)
    info = lockout.unlock(user)
    assert info["cleared_attempts"] == 7
    assert info["was_locked_for"] >= 295 and info["unlocked"] is True
    assert user.failed_attempts == 0 and user.locked_until is None and user.last_failed_at is None
    assert lockout.locked_for(user) == 0


def test_password_rules_reject_weak_and_accept_strong():
    """弱口令判据：黑名单、l33t 变体、键盘序、纯数字、同字符重复、长度、含用户名。"""
    from backend import passwords

    for weak in ("pytest-password-2026", "P@ssword-2026", "1234567890", "aaaaaaaaaa",
                 "qwertyuiop12", "Abcdefghij", "short", "短密码", "secret"):
        assert passwords.problems(weak), f"{weak} 应该被拒"
    for strong in ("correct-horse-battery", "TierSmoke#2026", "verify-session-2026", "pytest-login-2026"):
        assert passwords.problems(strong) == [], f"{strong} 应该被接受"
    # 口令里含自己的用户名：改成大小写/加数字都不算换了
    assert passwords.problems("demo_analyst-2026", "demo_analyst")
    assert any("10" in problem for problem in passwords.problems("nine-char"))


def test_password_normalize_undoes_leet_and_trailing_digits():
    """归一化是判据的第一步：去大小写、还原 l33t、去非字母数字、去掉末尾纯数字。"""
    from backend import passwords

    assert passwords.normalize("P@SSW0RD") == "password"
    assert passwords.normalize("Passw0rd") == "password"
    assert passwords.normalize("Demo_Analyst") == "demoanalyst"
    assert passwords.normalize("password99") == "password"
    assert passwords.normalize("") == ""


# ---------------------------------------------------------------- 覆盖率门槛（⑥）

# node 20 的覆盖表：一行一个完整相对路径，行首是 `# `（同一份代码在 node 22+ 换成了下面的树形）。
COVERAGE_FLAT_NODE20 = """# start of coverage report
# -------------------------------------------------------------------------------------------
# file                               | line % | branch % | funcs % | uncovered lines
# -------------------------------------------------------------------------------------------
# src/api/client.js                  |  71.74 |    60.71 |   60.00 | 12-16 18-20
# src/components/AgentChat.vue       |  96.13 |    83.33 |   90.00 | 133-134 149
# src/utils/markdown.js              | 100.00 |   100.00 |  100.00 |
# -------------------------------------------------------------------------------------------
# all files                          |  97.95 |    88.03 |   87.45 |
# -------------------------------------------------------------------------------------------
# end of coverage report
"""

# node 22+ 的树形覆盖表：分组与文件分三行，靠缩进表示层级，行首是 `ℹ `（本机 node 24 的真实输出）。
COVERAGE_TREE_NODE24 = """\u2139 start of coverage report
\u2139 ------------------------------------------------------------------------------
\u2139 file                  | line % | branch % | funcs % | uncovered lines
\u2139 ------------------------------------------------------------------------------
\u2139 src                   |        |          |         |
\u2139  api                  |        |          |         |
\u2139   client.js           |  71.74 |    60.71 |   60.00 | 12-16 18-20
\u2139  components           |        |          |         |
\u2139   AgentChat.vue       |  96.13 |    83.33 |   90.00 | 133-134 149
\u2139  utils                |        |          |         |
\u2139   markdown.js         | 100.00 |   100.00 |  100.00 |
\u2139 all files             |  97.00 |    85.58 |   84.52 |
\u2139 ------------------------------------------------------------------------------
\u2139 end of coverage report
"""


def test_frontend_coverage_table_parses_both_node_layouts():
    """两种覆盖表格式必须解析成同一组路径 —— 少了哪一种，门槛在某台机器上就读不到数。"""
    flat = parse_coverage_table(COVERAGE_FLAT_NODE20)
    tree = parse_coverage_table(COVERAGE_TREE_NODE24)
    for rows in (flat, tree):
        assert rows["src/api/client.js"] == (71.74, 60.71, 60.00)
        assert rows["src/components/AgentChat.vue"] == (96.13, 83.33, 90.00)
        assert rows["src/utils/markdown.js"] == (100.00, 100.00, 100.00)
        assert "all files" in rows
    # `all files` 的总分在两个 node 版本里不一样（测试文件算不算进去），所以门槛只算固定名单的平均值。
    assert flat["all files"][0] == 97.95 and tree["all files"][0] == 97.00
    assert len(COVERED_MODULES) == 12 and all("/" in module for module in COVERED_MODULES)


def test_backend_coverage_gate_parses_pytest_output():
    """pytest-cov 的 TOTAL 行与用例计数：解析一旦偏了，门槛就会把低覆盖率读成高覆盖率。"""
    output = """\
---------- coverage: platform win32, python 3.11.9-final-0 ----------
Name                             Stmts   Miss Branch BrPart  Cover   Missing
--------------------------------------------------------------------------
backend\\ai_service.py               60     42     12      3    30%
--------------------------------------------------------------------------
TOTAL                             3628    438    900    113    86%
7 files skipped due to complete coverage.

171 passed in 84.42s (0:01:24)
"""
    assert parse_total(output) == (3628, 438, 900, 113, 86)
    counts = parse_counts(output)
    assert counts == {"passed": 171}
    # 「N files skipped due to complete coverage」不能被当成用例被跳过（门槛里跳过数必须是 0）。
    assert "skipped" not in counts
    assert parse_total("没有覆盖表") is None


# ---------------------------------------------------------------- 接口契约快照（⑧）

def _snapshot() -> dict:
    return json.loads(snapshot_gate.SNAPSHOT_PATH.read_text(encoding="utf-8"))


def test_openapi_snapshot_file_matches_live_schema():
    """快照必须与当前代码生成的接口一致；不一致时把「改了哪些接口」写在断言里，省一轮定位。"""
    schema = snapshot_gate.build_schema()
    saved = snapshot_gate.SNAPSHOT_PATH.read_text(encoding="utf-8")
    changes = snapshot_gate.describe_changes(json.loads(saved), schema)
    assert saved == snapshot_gate.dump_text(schema), (
        "接口与 docs/openapi.json 不一致（接口改动是有意的就跑 "
        "`python scripts/check_openapi_snapshot.py --update` 并一起提交）：\n  " + "\n  ".join(changes))


def test_openapi_snapshot_lists_exactly_the_live_routes():
    """双向核对：漏掉一个接口的快照没有价值，留着一个已删接口的快照会把前端带沟里。"""

    def live_routes(routes) -> set[str]:
        found: set[str] = set()
        for route in routes:
            path = getattr(route, "path", None)
            if path and path.startswith("/api/") and getattr(route, "include_in_schema", True):
                found |= {f"{method.upper()} {path}" for method in getattr(route, "methods", None) or ()}
            # `app.include_router()` 挂上来的是嵌套对象：新版 FastAPI 存成 `_IncludedRouter`
            # （原 router 在 `.original_router` 里），老版直接把子路由放在 `.routes` 上。
            nested = getattr(route, "routes", None) or getattr(
                getattr(route, "original_router", None), "routes", None)
            if nested:
                found |= live_routes(nested)
        return found

    snapshot_routes = {f"{method} {path}" for method, path in snapshot_gate.operations(_snapshot())}
    declared = live_routes(app.routes)
    assert declared, "路由都收不到，这条检查就没意义了"
    assert declared - snapshot_routes == set(), "这些真实路由不在快照里"
    assert snapshot_routes - declared == set(), "快照里这些路由已经不存在了"


def test_contract_doc_only_names_real_endpoints():
    """`docs/api-contract.md` 表格点名的接口必须真实存在 —— 手写契约同样不许漂。"""
    drift = snapshot_gate.contract_drift(snapshot_gate.build_schema())
    assert drift == [], "接口表里写了代码里没有的接口：" + "、".join(drift)


def test_every_operation_in_the_contract_has_a_summary():
    """接口光秃秃地出现在契约里就是文档债（FastAPI 的 summary 来自路由函数 docstring）。"""
    missing = snapshot_gate.undocumented(snapshot_gate.build_schema())
    assert missing == [], "这些接口没有一句说明：" + "、".join(missing)


def test_snapshot_diff_names_operations_and_models():
    """差异要打印成人话：131 KB 的 JSON diff 没人看得懂，等于把问题丢给下一个人。"""
    old = {"openapi": "3.1.0", "info": {"version": "1.0.0"},
           "paths": {"/api/v1/old": {"get": {"summary": "旧接口"}}},
           "components": {"schemas": {"OldModel": {"type": "object"}}}}
    new = {"openapi": "3.1.0", "info": {"version": "1.0.0"},
           "paths": {"/api/v1/new": {"post": {"summary": "新接口"}}},
           "components": {"schemas": {"NewModel": {"type": "object"}}}}
    lines = snapshot_gate.describe_changes(old, new)
    assert "+ 新增接口 POST /api/v1/new" in lines
    assert "- 删除接口 GET /api/v1/old" in lines
    assert "+ 新增模型 NewModel" in lines
    assert "- 删除模型 OldModel" in lines
    assert snapshot_gate.describe_changes(old, json.loads(json.dumps(old))) == []

    changed = json.loads(json.dumps(old))
    changed["paths"]["/api/v1/old"]["get"]["summary"] = "改了说明"
    assert "~ 接口改动 GET /api/v1/old（参数/请求体/响应变了）" in snapshot_gate.describe_changes(old, changed)
    # 版本与文档信息的变化也要点名（否则「换了 fastapi 版本」会被当成接口改动）。
    bumped = json.loads(json.dumps(old))
    bumped["info"]["version"] = "1.1.0"
    assert any(line.startswith("~ 文档信息") for line in snapshot_gate.describe_changes(old, bumped))


def test_snapshot_path_normalization_treats_parameter_names_as_the_same_route():
    """`{id}` 与 `{transaction_id}` 是同一个路由 —— 否则契约文档里的写法会误报成漂移。"""
    assert snapshot_gate.normalize_path("/api/v1/financial/transactions/{id}") == \
        snapshot_gate.normalize_path("/api/v1/financial/transactions/{transaction_id}")
    assert snapshot_gate.normalize_path("/api/v1/health/") == "/api/v1/health"
