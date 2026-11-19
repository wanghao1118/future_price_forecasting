"""UTC-minute retrieval, independent post-recall checks and transport contracts."""

import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.env01_search import LocalNewsSearch, LocalIndicatorSearch, MAX_RESPONSE_BYTES, canonical, digest, serve
from newsdesk.tests.test_retrieval_cutoff_gate import question, article, window
from time_gate_agent.local import LocalTimeGate


class RetrievalSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = question()

    def news(self, rows):
        return LocalNewsSearch(self.case, [dict(row, title="原油供应") for row in rows], self.root / "news")

    def indicators(self, rows):
        return LocalIndicatorSearch(self.case, rows, self.root / "indicators")

    def test_user_example_0800_start_has_0600_cutoff_in_both_interfaces(self):
        self.case.update(input_date="2026-09-25", target_date="2026-09-26")
        known = window("2026-09-25", "2026-09-24T08:00:00Z", "2026-09-25T07:00:00Z")
        forecast = window("2026-09-26", "2026-09-26T08:00:00Z", "2026-09-27T07:00:00Z")
        self.case["time_windows"] = {"status": "ready", "known": known, "forecast": forecast,
                                     "information_cutoff_utc": known["end_utc"]}
        times = ["2026-09-26T05:59:00Z", "2026-09-26T06:00:00Z", "2026-09-26T06:01:00Z"]
        news = self.news([article(str(i), t) for i, t in enumerate(times)]).search("原油")
        technical = self.indicators([{"date": "2026-09-26", "available_at": t, "price": i} for i, t in enumerate(times)])
        result = technical.search("price", start_time="2026-09-26T05:00:00Z")
        self.assertEqual(news["cutoff_utc"], result["cutoff_utc"], "both tools must bind the same cutoff")
        self.assertEqual(result["cutoff_utc"], "2026-09-26T06:00:00Z")
        self.assertEqual({row["id"] for row in news["articles"]}, {"0", "1"})
        self.assertEqual([row["value"] for row in result["rows"]], [0, 1])

    def test_news_retrieval_filters_time_before_independent_gate_and_limit(self):
        search = self.news([article("future", "2026-10-29T20:01:00Z"), article("safe"),
                            article("date-only", "2026-10-29"),
                            article("revision", "2026-10-29T19:00:00Z", source_modified_at="2026-10-29T20:01:00Z")])
        original, observed = LocalTimeGate.select, []

        def gate(instance, case):
            observed.extend(row["id"] for row in instance.articles)
            return original(instance, case)

        with patch.object(LocalTimeGate, "select", gate):
            result = search.search("原油", limit=1)
        self.assertEqual([r["id"] for r in result["articles"]], ["safe"])
        self.assertEqual(set(observed), {"safe", "revision"})
        receipt = search.summary()["receipts"][0]
        request = json.loads(Path(receipt["files"]["request"]["path"]).read_text())
        self.assertEqual(request["arguments"], {"keywords": "原油", "limit": 1})
        self.assertEqual(request["retrieval_boundary"]["cutoff_utc"], result["cutoff_utc"])
        self.assertEqual(receipt["cutoff_utc"], result["cutoff_utc"])
        self.assertEqual(receipt["response_sha256"], digest(result))

    def test_indicator_precise_range_filter_then_source_revision_validation(self):
        rows = [{"available_at": "2026-10-29T19:59:00Z", "price": 1},
                {"available_at": "2026-10-29T22:00:00+02:00", "price": 2},
                {"available_at": "2026-10-29T20:01:00Z", "price": 3},
                {"date": "2026-10-29", "price": 4},
                {"available_at": "2026-10-29T19:58:00Z", "price": 5,
                 "source_modified_at": "2026-10-29T20:01:00Z"}]
        search = self.indicators(rows)
        result = search.call({"indicator": "price", "start_time": "2026-10-29T19:59:00Z"})
        self.assertEqual([r["value"] for r in result["rows"]], [1, 2])
        self.assertEqual(result["end_time"], "2026-10-29T20:00:00Z")
        self.assertEqual(result["rows"][-1]["available_at"], "2026-10-29T20:00:00Z")
        receipt = search.summary()["receipts"][0]
        request = json.loads(Path(receipt["files"]["request"]["path"]).read_text())
        self.assertNotIn("end_time", request["arguments"])
        self.assertEqual(request["arguments"]["limit"], 366)
        self.assertEqual(receipt["cutoff_utc"], result["cutoff_utc"])

    def test_indicator_rejection_can_be_corrected_without_consuming_success_budget(self):
        search = self.indicators([{"available_at": "2026-10-29T20:00:00Z", "price": 1}])
        bad = search.call({"indicator": "price", "start_time": "2026-10-29T19:00:00Z", "end_time": "2026-10-29T20:01:00Z"})
        self.assertEqual(bad["status"], "rejected")
        self.assertEqual(bad["error"]["allowed_end_time"], "2026-10-29T20:00:00Z")
        good = search.call({"indicator": "price", "start_time": "2026-10-29T19:00:00Z", "end_time": bad["error"]["allowed_end_time"]})
        self.assertEqual(good["rows"][0]["value"], 1)
        self.assertEqual([r["status"] for r in search.summary()["receipts"]], ["rejected", "ok"])

    def test_mcp_schema_and_override_rejection(self):
        search, technical = self.news([article()]), self.indicators([])
        messages = [{"jsonrpc": "2.0", "id": 0, "method": "tools/list"}]
        for i, (name, arguments) in enumerate([
                ("search_news", {"keywords": "原油", "cutoff_utc": "2030-01-01T00:00:00Z"}),
                ("search_indicators", {"indicator": "price", "start_time": "2026-01-01T00:00:00Z", "cutoff_utc": "2030-01-01T00:00:00Z"}),
                ("search_indicators", {"indicator": "price", "start_date": "2026-01-01", "end_date": "2026-10-29"}),
                ("search_indicators", {"indicator": "price", "start_time": "2026-01-01T00:00:00Z", "end_time": None})], 1):
            messages.append({"jsonrpc": "2.0", "id": i, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
        output = io.StringIO()
        serve(search, technical, io.StringIO("\n".join(json.dumps(m) for m in messages)), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        schema = replies[0]["result"]["tools"][1]["inputSchema"]
        self.assertEqual(schema["required"], ["indicator", "start_time"])
        self.assertNotIn("cutoff_utc", schema["properties"])
        for reply in replies[1:]:
            result = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual(result.get("rows", result.get("articles")), [])
            self.assertIn(result["status"], ("rejected", "unavailable"))

    def test_unknown_boundary_fails_closed_and_context_cannot_change_on_restart(self):
        search = self.indicators([])
        self.case["time_windows"]["forecast"]["start_utc"] = "2026-10-29T23:00:00Z"
        self.case["time_windows"]["forecast"]["segments"][0]["start_utc"] = "2026-10-29T23:00:00Z"
        with self.assertRaisesRegex(ValueError, "indicator_audit_context_mismatch"):
            self.indicators([])
        self.case["time_windows"] = None
        with self.assertRaises(ValueError):
            self.news([])

    def test_fractional_boundary_is_not_rounded_down_and_revision_can_enter_range(self):
        news = self.news([article("equal"), article("late", "2026-10-29T20:00:00.000001Z"),
                          article("unsupported", "2026-10-29T20:00:00.0000001Z")]).search("原油")
        self.assertEqual([row["id"] for row in news["articles"]], ["equal"])
        result = self.indicators([
            {"available_at": "2026-10-29T20:00:00.000001Z", "price": 999},
            {"available_at": "2026-10-29T20:00:00.0000001Z", "price": 998},
            {"available_at": "2026-10-29T18:00:00Z", "source_modified_at": "2026-10-29T19:30:00Z", "price": 70}
        ]).search("price", start_time="2026-10-29T19:00:00Z")
        self.assertEqual([row["value"] for row in result["rows"]], [70])
        self.assertEqual(result["rows"][0]["available_at"], "2026-10-29T19:30:00Z")

    def test_new_response_metadata_is_included_in_news_byte_budget(self):
        rows = [article(str(i), title="原油" + "长" * 1800, body="原油" * 4000) for i in range(8)]
        result = LocalNewsSearch(self.case, rows, self.root / "news").search("原油", 8)
        self.assertGreater(result["returned_count"], 0)
        self.assertLessEqual(len(canonical(result).encode("utf-8")), MAX_RESPONSE_BYTES)
        self.assertEqual(result["cutoff_utc"], "2026-10-29T20:00:00Z")

    def test_unknown_derived_dependency_cannot_fall_back_to_observation_timestamp(self):
        rows = [{"timestamp": "2026-10-29T19:00:00Z", "available_at": None,
                 "availability_dependency_unverified": True, "price_trend": 123}]
        result = self.indicators(rows).call({"indicator": "price_trend", "start_time": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["rows"], [])
        self.assertEqual(LocalTimeGate.select_indicator_time_rows(self.case, rows, "2026-01-01T00:00:00Z")["rows"], [])

    def test_benchmark_freezes_source_timestamps_without_using_chart_epoch_anchor(self):
        from newsdesk.benchmark import BenchmarkStore
        from newsdesk.tests.test_benchmark import FakeContinuous
        original = {"trading_date": "2026-10-28", "open": 70, "high": 72, "low": 69, "close": 71,
                    "available_at": "2026-10-28T21:30:00Z", "source_modified_at": "2026-10-29T18:00:00Z", "time": 1234}
        store = BenchmarkStore(self.root / "benchmark", continuous_market=FakeContinuous([original]))
        bar = store._snapshot("WTI", False)["ordered"][0][1]
        self.assertEqual(bar["available_at"], original["available_at"])
        self.assertEqual(bar["source_modified_at"], original["source_modified_at"])
        self.assertNotIn("time", bar)

    def test_real_stdio_exposes_time_tool_even_when_indicator_snapshot_is_empty(self):
        news_path, indicators_path, context_path = [self.root / name for name in ("news.json", "indicators.json", "context.json")]
        news_path.write_text("[]")
        indicators_path.write_text("[]")
        context_path.write_text(canonical({"case": self.case,
            "news_snapshot_path": str(news_path), "news_snapshot_sha256": digest([]),
            "indicator_snapshot_path": str(indicators_path), "indicator_snapshot_sha256": digest([])}))
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
                        "name": "search_indicators", "arguments": {"indicator": "price", "start_time": "2026-10-29T19:00:00Z"}}}]
        completed = subprocess.run([sys.executable, "-m", "newsdesk.env01_search", "--context", str(context_path),
            "--audit-dir", str(self.root / "search")], input="\n".join(canonical(m) for m in messages) + "\n",
            capture_output=True, text=True, timeout=15, cwd=Path(__file__).resolve().parents[2])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([tool["name"] for tool in replies[0]["result"]["tools"]], ["search_news", "search_indicators"])
        result = json.loads(replies[1]["result"]["content"][0]["text"])
        self.assertEqual(result["status"], "no_results")
        self.assertEqual(result["cutoff_utc"], "2026-10-29T20:00:00Z")


if __name__ == "__main__":
    unittest.main()
