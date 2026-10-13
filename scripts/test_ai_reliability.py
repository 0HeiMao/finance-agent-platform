"""离线回归：python scripts/test_ai_reliability.py；不连接数据库、不调用付费模型。"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import postgresql

from backend.llm import DeepSeekClient
from backend.rag import financial_evidence as evidence
from backend.rag.pgvector_store import search_documents
from backend.rag.question_periods import parse
from backend.rag.rerank import rerank


class ReliabilityTests(unittest.TestCase):
    def test_relative_time_uses_calendar_not_latest_data(self):
        anchor = date(2026, 1, 2)
        cases = {"本月收入": ("2026-01",), "上个月收入": ("2025-12",),
                 "上季度收入": ("2025-10", "2025-11", "2025-12"),
                 "2026年第一季度": ("2026-01", "2026-02", "2026-03"),
                 "2026-Q2": ("2026-04", "2026-05", "2026-06"),
                 "2026年1月至3月": ("2026-01", "2026-02", "2026-03")}
        for question, months in cases.items():
            with self.subTest(question=question):
                self.assertEqual(parse(question, today=anchor).months, months)
        self.assertEqual(len(parse("去年", today=anchor).months), 12)

    def test_ambiguous_and_invalid_time_requires_clarification(self):
        for question in ("3月收入", "第一季度收入", "2026-Q5", "2026-13", "最近三个月", "去年同期",
                         "2026-03至2026-01", "2020-01至2026-01", "本月同比", "2026年上半年",
                         "2026-01至2026-03和2026-05"):
            with self.subTest(question=question), self.assertRaises(ValueError):
                parse(question, today=date(2026, 10, 11))

    def test_quarter_missing_month_is_not_a_complete_quarter(self):
        with patch.object(evidence, "available_periods", return_value=["2026-01", "2026-03"]):
            result = evidence.collect(Mock(), "2026年第一季度收入")
        self.assertEqual(result["missing_periods"], ["2026-02"])

    def test_missing_explicit_month_never_defaults(self):
        for question in ("2025年3月收入", "2026-13收入", "2026-12收入", "2026-10收入"):
            with self.subTest(question=question):
                self.assertIsNone(evidence.pick_period(question, ["2026-01", "2026-03"]))

    def test_latest_only_when_unspecified(self):
        self.assertEqual(evidence.pick_period("收入是多少", ["2026-01", "2026-03"]), "2026-03")
        self.assertIsNone(evidence.pick_period("收入是多少", []))

    def test_two_explicit_months_and_deduplication(self):
        self.assertEqual(evidence.requested_periods("对比2026年10月与2026/12，再看2026-10"),
                         ["2026-10", "2026-12"])

    def test_missing_comparison_endpoint_stops_before_query(self):
        db = Mock()
        with patch.object(evidence, "available_periods", return_value=["2026-01"]):
            result = evidence.collect(db, "对比2026-01与2026-02")
        self.assertFalse(result["has_data"])
        self.assertEqual(result["missing_periods"], ["2026-02"])
        self.assertIn("2026-02", result["unavailable_message"])
        db.scalars.assert_not_called()

    def test_comparison_collects_both_details(self):
        periods = ["2026-01", "2026-02"]
        rows = [SimpleNamespace(period=p) for p in periods]
        db = Mock()
        db.scalars.return_value = rows
        with (patch.object(evidence, "available_periods", return_value=periods),
              patch.object(evidence, "metric_totals", return_value=rows),
              patch.object(evidence, "_summary_lines", return_value=[]),
              patch.object(evidence, "_period_lines", return_value=(["detail"], [])) as detail):
            result = evidence.collect(db, "对比2026-01和2026-02")
        self.assertTrue(result["has_data"])
        self.assertEqual([call.args[1] for call in detail.call_args_list], periods)

    def test_rerank_uses_configured_model_and_changes_order(self):
        chunks = [SimpleNamespace(content=str(i), source="test") for i in range(3)]
        client = Mock()
        client.rank.return_value = '{"order": [3, 1]}'
        with (patch("backend.rag.rerank.get_settings", return_value=SimpleNamespace(rerank_model="rank-model")),
              patch("backend.llm.get_llm", return_value=client)):
            result = rerank("费用", chunks, 2)
        self.assertEqual(result, [chunks[2], chunks[0]])
        self.assertEqual(client.rank.call_args.args[2], "rank-model")
        self.assertIn('{"order":', client.rank.call_args.args[1])

    def test_rerank_failure_falls_back(self):
        chunks = [SimpleNamespace(content=str(i)) for i in range(3)]
        client = Mock()
        client.rank.side_effect = TimeoutError()
        with (patch("backend.rag.rerank.get_settings", return_value=SimpleNamespace(rerank_model="rank-model")),
              patch("backend.llm.get_llm", return_value=client)):
            self.assertEqual(rerank("费用", chunks, 2), chunks[:2])

    def test_rank_sdk_model_and_system_instructions(self):
        client = DeepSeekClient.__new__(DeepSeekClient)
        client.client = Mock()
        client.client.chat.completions.create.return_value = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"order":[2]}'))])
        self.assertEqual(client.rank("question", "rank-only", "rank-model"), '{"order":[2]}')
        arguments = client.client.chat.completions.create.call_args.kwargs
        self.assertEqual(arguments["model"], "rank-model")
        self.assertEqual(arguments["messages"][0], {"role": "system", "content": "rank-only"})

    def test_rerank_distinguishes_empty_from_invalid(self):
        chunks = [SimpleNamespace(content=str(i)) for i in range(3)]
        for output, expected in [('{"order":[]}', []), ('{"order":[3]}', [chunks[2]]),
                                 ('not json 3', chunks[:2]), ('{"order":[99]}', chunks[:2])]:
            with self.subTest(output=output):
                client = Mock()
                client.rank.return_value = output
                with (patch("backend.rag.rerank.get_settings", return_value=SimpleNamespace(rerank_model="rank-model")),
                      patch("backend.llm.get_llm", return_value=client)):
                    self.assertEqual(rerank("费用", chunks, 2), expected)

    def test_visibility_is_in_sql_before_limit(self):
        for departments in (None, (), ("finance",), ("finance", "sales")):
            with self.subTest(departments=departments):
                db = Mock()
                db.execute.return_value.all.return_value = []
                search_documents(db, [1.0] * 384, 1, embedding_model="test", departments=departments)
                statement = db.execute.call_args.args[0]
                compiled = statement.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
                sql = str(compiled)
                where = sql.split("WHERE", 1)[1].split("ORDER BY", 1)[0]
                self.assertEqual("'department'" in where, departments is not None)
                if departments:
                    for department in departments:
                        self.assertIn(department, where)
                self.assertIn("LIMIT 1", sql)


if __name__ == "__main__":
    unittest.main(verbosity=2)
