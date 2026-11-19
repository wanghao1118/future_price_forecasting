import copy
from datetime import date, timedelta
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.env01_search import LocalIndicatorSearch, LocalNewsSearch, digest, serve
from newsdesk.evaluation import AIHubPricePredictor, technical_snapshot, write_json, _indicator_receipts
from newsdesk.tests.test_evaluation import question, make_context, forecast
from newsdesk.tests.test_benchmark import FakeContinuous
from newsdesk.benchmark import BenchmarkStore
from time_gate_agent.local import LocalTimeGate


class IndicatorTimeGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.case = question()

    def search(self, rows=None, **kwargs):
        return LocalIndicatorSearch(self.case, rows or [{"date": "2026-10-20", "price": 70}],
                                    self.root / "indicators", **kwargs)

    def test_rejected_future_query_then_successful_retry_has_both_receipts(self):
        search = self.search(max_calls=1)
        result = search.search("price", "2026-10-01", "2026-10-21")
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["error"]["allowed_end_date"], "2026-10-20")
        self.assertIn("重新调用", result["error"]["message"])
        result = search.search("price", "2026-10-01", result["error"]["allowed_end_date"])
        self.assertEqual(result["rows"][0]["value"], 70)
        self.assertEqual(search.summary()["call_count"], 2)
        self.assertEqual([r["status"] for r in search.summary()["receipts"]], ["rejected", "ok"])

    def test_invalid_calendar_missing_reversed_and_before_history_start(self):
        for start, end, code in [("2026-02-30", "2026-03-01", "invalid_date"),
                                 (None, "2026-10-20", "invalid_date"),
                                 ("2026-10-20", "2026-10-01", "reversed_date_range"),
                                 ("2025-12-31", "2026-10-20", "before_history_start")]:
            with self.subTest(code=code):
                result = LocalTimeGate.validate_indicator_query(self.case, start, end)
                self.assertFalse(result["allowed"])
                self.assertEqual(result["code"], code)

    def test_weekend_target_still_cannot_query_after_input_day(self):
        case = dict(self.case, input_date="2026-10-16", target_date="2026-10-19")
        result = LocalTimeGate.validate_indicator_query(case, "2026-10-01", "2026-10-18")
        self.assertFalse(result["allowed"])
        self.assertEqual(result["allowed_end_date"], "2026-10-16")

    def test_post_recall_gate_and_no_silent_thirty_row_limit(self):
        rows = [{"date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(), "price": i} for i in range(90)]
        rows += [{"date": "2026-10-21", "price": 999}, {"date": "2026-02-30", "price": 999}]
        result = self.search(rows).search("price", "2026-01-01", "2026-10-20")
        self.assertEqual(result["returned_count"], 90)
        self.assertFalse(result["truncated"])
        self.assertNotIn(999, [r["value"] for r in result["rows"]])

    def test_frozen_full_history_not_just_thirty_question_window_and_prefix_only(self):
        store = BenchmarkStore(self.root / "benchmark", continuous_market=FakeContinuous())
        store.build(days=1)
        dataset = store.load_dataset()
        source = dataset["manifest"]["source_fingerprints"]["WTI"]
        self.assertEqual(len(source["selected_bars"]), 2)
        self.assertEqual(len(source["history_bars"]), 40)
        q = next(q for q in dataset["questions"] if q["family"] == "WTI")
        before = technical_snapshot(dataset["manifest"], [q])[0]
        self.assertEqual(len(before["rows"]), 39)
        source["history_bars"][-1].update(high=999999, low=-99999, close=12345, volume=77777)
        self.assertEqual(before, technical_snapshot(dataset["manifest"], [q])[0])
        self.assertTrue(all(r["date"] < q["target_date"] for r in before["rows"]))

    def test_mcp_rejection_is_delivered_as_retryable_tool_result(self):
        search = self.search()
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "search_indicators", "arguments": {"indicator": "price", "start_date": "2026-10-01", "end_date": "2026-10-21"}}}
        output = io.StringIO()
        serve(LocalNewsSearch(self.case, [], self.root / "search"), search,
              io.StringIO(json.dumps(request) + "\n"), output)
        body = json.loads(output.getvalue())["result"]
        self.assertFalse(body["isError"])
        self.assertEqual(json.loads(body["content"][0]["text"])["status"], "rejected")

    def test_aihub_model_receives_rejection_retries_and_saves_full_results(self):
        context = make_context(self.root)
        rows = [{"date": "2026-10-20", "price": 70}]
        path = self.root / "snapshot.json"
        write_json(path, rows)
        context.update(indicator_snapshot_path=str(path), indicator_snapshot_sha256=digest(rows))
        predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture-key")
        requests = []

        def tool(name, arguments, identifier):
            return {"id": identifier, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}

        def request(payload, protocol="chat"):
            requests.append(copy.deepcopy(payload))
            if len(requests) == 1:
                message = {"role": "assistant", "tool_calls": [
                    tool("search_news", {"keywords": "原油"}, "news"),
                    tool("search_indicators", {"indicator": "price", "start_date": "2026-10-01", "end_date": "2026-10-21"}, "bad")]}
            elif len(requests) == 2:
                feedback = json.loads(payload["messages"][-1]["content"])
                self.assertEqual(feedback["status"], "rejected")
                message = {"role": "assistant", "tool_calls": [tool("search_indicators", {
                    "indicator": "price", "start_date": "2026-10-01", "end_date": feedback["error"]["allowed_end_date"]}, "retry")]}
            else:
                message = {"role": "assistant", "content": json.dumps(dict(forecast(), public_analysis={"analysis_document": "依据召回的价格与新闻形成公开分析。"}))}
            return {"choices": [{"message": message}]}, "fixture-trace"

        work = self.root / "model"
        with patch.object(predictor, "_request", side_effect=request):
            predictor.predict({"question": self.case}, work, search_context=context)
        trace = json.loads((work / "conversation.json").read_text())
        self.assertEqual(trace["validation_errors"], [])
        self.assertEqual(trace["conversation"][0]["prediction_date"], "2026-10-21")
        returns = [e["result"] for e in trace["conversation"] if e["type"] == "tool_result" and e["tool"] == "search_indicators"]
        self.assertEqual([r["status"] for r in returns], ["rejected", "ok"])
        self.assertEqual(returns[-1]["rows"][0]["value"], 70)
        receipt = _indicator_receipts(work)[0]
        summary = json.loads((work / "indicators" / "summary.json").read_text())
        request_path = Path(summary["receipts"][0]["files"]["request"]["path"])
        write_json(request_path, {"indicator": "pcr"})
        with self.assertRaisesRegex(ValueError, "查询参数校验失败"):
            _indicator_receipts(work)

    def test_deepseek_can_finish_after_sequential_news_and_indicator_queries(self):
        context = make_context(self.root)
        rows = [{"date": "2026-10-20", "price": 70}]
        path = self.root / "snapshot.json"
        write_json(path, rows)
        context.update(indicator_snapshot_path=str(path), indicator_snapshot_sha256=digest(rows))
        predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture-key")
        request_count = 0

        def request(payload, protocol="chat"):
            nonlocal request_count
            request_count += 1
            if request_count <= 9:
                name = "search_news" if request_count <= 8 else "search_indicators"
                arguments = {"keywords": "原油"} if name == "search_news" else {
                    "indicator": "price", "start_date": "2026-10-01", "end_date": "2026-10-20"}
                message = {"role": "assistant", "tool_calls": [{"id": str(request_count),
                    "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            else:
                message = {"role": "assistant", "content": json.dumps(dict(forecast(),
                    public_analysis={"analysis_document": "完成合法的逐次查询后，根据新闻与技术指标作出决策。"}))}
            return {"choices": [{"message": message}]}, "fixture-trace"

        with patch.object(predictor, "_request", side_effect=request):
            predictor.predict({"question": self.case}, self.root / "model", search_context=context)
        trace = json.loads((self.root / "model" / "conversation.json").read_text())
        self.assertEqual(request_count, 10)
        self.assertEqual(trace["search_call_count"], 8)
        self.assertEqual(trace["indicator_call_count"], 1)
        self.assertEqual(trace["validation_errors"], [])


if __name__ == "__main__":
    unittest.main()
