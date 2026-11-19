"""Thirty-minute UTC retrieval and byte-compatible frozen two-hour responses."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from newsdesk.env01_search import (CURRENT_UTC_INDICATOR_PROTOCOL, CURRENT_UTC_SEARCH_PROTOCOL,
    INDICATOR_SEARCH_TOOL, SEARCH_TOOL, LocalIndicatorSearch, LocalNewsSearch, digest,
    indicator_search_tool, news_search_tool)
from newsdesk.tests.test_retrieval_cutoff_gate import article, question, window
from time_gate_agent.local import (CURRENT_RETRIEVAL_POLICY, RETRIEVAL_POLICY, LocalTimeGate)


def current_question():
    case = question()
    case["retrieval_time_policy"] = CURRENT_RETRIEVAL_POLICY
    return case


def morning_question():
    case = current_question()
    case.update(input_date="2026-09-25", target_date="2026-09-26")
    known = window("2026-09-25", "2026-09-24T08:00:00Z", "2026-09-25T07:00:00Z")
    forecast = window("2026-09-26", "2026-09-26T08:00:00Z", "2026-09-27T07:00:00Z")
    case["time_windows"] = {"status": "ready", "known": known, "forecast": forecast,
                            "information_cutoff_utc": known["end_utc"]}
    return case


class ThirtyMinuteRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_0800_start_has_0730_cutoff_and_minute_metadata(self):
        boundary = LocalTimeGate.retrieval_boundary(morning_question())
        self.assertTrue(boundary["allowed"])
        self.assertEqual(boundary["forecast_start_utc"], "2026-09-26T08:00:00Z")
        self.assertEqual(boundary["cutoff_utc"], "2026-09-26T07:30:00Z")
        self.assertEqual(boundary["buffer_minutes"], 30)
        self.assertNotIn("buffer_hours", boundary)
        self.assertIn("30分钟", boundary["message"])

    def test_news_and_indicators_share_inclusive_minute_and_microsecond_boundary(self):
        case = morning_question()
        times = ["2026-09-26T07:29:00Z", "2026-09-26T07:29:59.999999Z", "2026-09-26T07:30:00Z",
                 "2026-09-26T07:30:00.000001Z", "2026-09-26T07:31:00Z"]
        news = LocalNewsSearch(case, [article(str(index), value) for index, value in enumerate(times)], self.root / "news")
        response = news.search("Frozen")
        technical = LocalIndicatorSearch(case,
            [{"available_at": value, "price": index} for index, value in enumerate(times)], self.root / "technical")
        points = technical.call({"indicator": "price", "start_time": "2026-09-26T07:00:00Z"})
        self.assertEqual(response["cutoff_utc"], points["cutoff_utc"])
        self.assertEqual({row["id"] for row in response["articles"]}, {"0", "1", "2"})
        self.assertEqual([row["value"] for row in points["rows"]], [0, 1, 2])
        for result in (response, points):
            self.assertEqual(result["retrieval_time_policy"], CURRENT_RETRIEVAL_POLICY)
            self.assertEqual(result["timezone"], "UTC")
        self.assertEqual(news.summary()["protocol"], CURRENT_UTC_SEARCH_PROTOCOL)
        self.assertEqual(technical.summary()["protocol"], CURRENT_UTC_INDICATOR_PROTOCOL)
        for search in (news, technical):
            receipt = search.summary()["receipts"][0]
            self.assertEqual(receipt["cutoff_utc"], "2026-09-26T07:30:00Z")
            self.assertEqual(receipt["retrieval_time_policy"], CURRENT_RETRIEVAL_POLICY)

    def test_wti_2200_start_releases_2100_known_daily_bar_at_2130_cutoff(self):
        rows = [{"date": "2026-10-28", "available_at": "2026-10-28T21:00:00Z", "price": 70},
                {"date": "2026-10-29", "available_at": "2026-10-29T21:00:00Z", "price": 71}]
        current = LocalIndicatorSearch(current_question(), rows, self.root / "current")
        result = current.call({"indicator": "price", "start_time": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["cutoff_utc"], "2026-10-29T21:30:00Z")
        self.assertEqual([row["value"] for row in result["rows"]], [70, 71])
        old = LocalIndicatorSearch(question(), rows, self.root / "old")
        result = old.call({"indicator": "price", "start_time": "2026-01-01T00:00:00Z"})
        self.assertEqual(result["cutoff_utc"], "2026-10-29T20:00:00Z")
        self.assertEqual([row["value"] for row in result["rows"]], [70])
        self.assertEqual(result["retrieval_time_policy"], RETRIEVAL_POLICY)

    def test_timezones_revisions_and_requested_end_cannot_extend_cutoff(self):
        case = current_question()
        news = LocalTimeGate([
            article("same-instant", "2026-10-30T05:30:00+08:00"),
            article("later-revision", "2026-10-29T21:00:00Z", source_modified_at="2026-10-29T21:30:00.000001Z"),
            article("date-only", "2026-10-29")]).select(case)
        self.assertEqual([row["id"] for row in news["articles"]], ["same-instant"])
        self.assertEqual(news["articles"][0]["published_at"], "2026-10-29T21:30:00Z")
        self.assertEqual(news["audit"]["buffer_minutes"], 30)
        self.assertNotIn("buffer_hours", news["audit"])
        query = LocalTimeGate.validate_indicator_time_query(case, "2026-10-29T20:00:00Z", "2026-10-29T21:30:00.000001Z")
        self.assertFalse(query["allowed"])
        self.assertEqual(query["code"], "future_time_range")
        case["time_windows"]["forecast"]["segments"] = []
        self.assertFalse(LocalTimeGate.retrieval_boundary(case)["allowed"])

    def test_frozen_two_hour_boundary_and_public_response_bytes_do_not_change(self):
        case = question()
        self.assertEqual(LocalTimeGate.retrieval_boundary(case), {
            "allowed": True, "code": "allowed", "cutoff_utc": "2026-10-29T20:00:00Z",
            "forecast_start_utc": "2026-10-29T22:00:00Z", "policy": RETRIEVAL_POLICY,
            "timezone": "UTC", "buffer_hours": 2,
            "message": "检索截止为冻结预测区间开始前2小时，所有时间使用UTC。"})
        response = LocalNewsSearch(case, [article("known", "2026-10-29T20:00:00Z")], self.root / "news").search("Frozen")
        self.assertEqual(digest(response), "57abcf35a076de1fd778587bdc3a7a7cb221d3cf7f4c0329a6048a18f42ef3ad")
        rows = [{"date": "2026-10-28", "available_at": "2026-10-28T21:00:00Z", "price": 70},
                {"date": "2026-10-29", "available_at": "2026-10-29T21:00:00Z", "price": 71}]
        response = LocalIndicatorSearch(case, rows, self.root / "technical").call(
            {"indicator": "price", "start_time": "2026-01-01T00:00:00Z"})
        self.assertEqual(digest(response), "3493a390160ada8c12a3e18098434bb1a710cce8f490639f60548f38fbbd79db")

    def test_case_selects_frozen_tool_description_without_changing_time_schema(self):
        self.assertIn("30 minutes", SEARCH_TOOL["description"])
        self.assertIn("30 minutes", INDICATOR_SEARCH_TOOL["description"])
        self.assertIn("two hours", news_search_tool(question())["description"])
        self.assertIn("two hours", indicator_search_tool(question())["description"])
        self.assertEqual(indicator_search_tool(question())["inputSchema"], INDICATOR_SEARCH_TOOL["inputSchema"])
        self.assertEqual(news_search_tool(question())["inputSchema"], SEARCH_TOOL["inputSchema"])
        self.assertEqual(indicator_search_tool(current_question()), INDICATOR_SEARCH_TOOL)

    def test_mcp_keeps_precise_indicator_tool_for_empty_snapshot(self):
        snapshot = self.root / "news.json"
        snapshot.write_text("[]", encoding="utf-8")
        context = self.root / "context.json"
        context.write_text(json.dumps({"case": current_question(), "news_snapshot_path": str(snapshot),
                                      "news_snapshot_sha256": digest([])}), encoding="utf-8")
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "search_indicators",
                        "arguments": {"indicator": "price", "start_time": "2026-01-01T00:00:00Z"}}}]
        process = subprocess.run([sys.executable, "-m", "newsdesk.env01_search", "--context", str(context),
                                  "--audit-dir", str(self.root / "search")],
                                 input="".join(json.dumps(request) + "\n" for request in requests),
                                 capture_output=True, text=True, cwd=Path(__file__).resolve().parents[2], timeout=10)
        self.assertEqual(process.returncode, 0)
        results = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual({tool["name"] for tool in results[0]["result"]["tools"]}, {"search_news", "search_indicators"})
        response = json.loads(results[1]["result"]["content"][0]["text"])
        self.assertEqual(response["status"], "no_results")
        self.assertEqual(response["cutoff_utc"], "2026-10-29T21:30:00Z")


if __name__ == "__main__":
    unittest.main()
