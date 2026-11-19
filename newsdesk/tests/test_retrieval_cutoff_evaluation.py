"""Frozen v4/v5 inputs, timestamp provenance and replayed evaluation receipts."""

import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from newsdesk.benchmark import _time_windows
from newsdesk.env01_search import LocalIndicatorSearch, LocalNewsSearch
from newsdesk.evaluation import (AIHubPricePredictor, EVALUATION_PROTOCOL, V4_EVALUATION_PROTOCOL, LEGACY_ACTIVE_PROTOCOL,
    RETRIEVAL_TIME_POLICY, LEGACY_RETRIEVAL_TIME_POLICY, EvaluationManager, _capture_trace, _indicator_receipts, _save_cli_events,
    _search_receipts, case_protocol, digest, environments, prepare_retrieval_cases, read_json,
    retrieval_metadata, technical_snapshot, validate_run_retrieval_policy, write_json)
from newsdesk.tests.test_evaluation import article, forecast, question


CONTRACT = {"familyId": "WTI", "code": "CL.1:NYM", "exchange": "NYMEX"}


def frozen_case(policy=LEGACY_RETRIEVAL_TIME_POLICY):
    case = question("wti-new")
    case.update(input_date="2026-10-29", target_date="2026-10-30", news_cutoff_exclusive="2026-10-30",
                contract_code=CONTRACT["code"], exchange_timezone="America/New_York",
                time_windows=_time_windows(CONTRACT, "2026-10-29", "2026-10-30"),
                retrieval_time_policy=policy)
    return case


def new_context(root, policy=LEGACY_RETRIEVAL_TIME_POLICY):
    case = frozen_case(policy)
    cutoff = retrieval_metadata(case)["cutoff_utc"]
    news = [article("safe", published_at="2026-10-29T19:59:00Z", ingestion={"published_date": "2026-10-29"}),
            article("late", published_at=cutoff[:-1] + ".000001Z", ingestion={"published_date": "2026-10-29"})]
    rows = [{"date": "2026-10-28", "price": 70, "available_at": "2026-10-28T21:00:00Z"},
            {"date": "2026-10-29", "price": 999, "available_at": "2026-10-29T21:00:00Z"}]
    write_json(root / "news-snapshot.json", news)
    write_json(root / "indicator-snapshot.json", rows)
    return {"case": case, "news_snapshot_path": str((root / "news-snapshot.json").resolve()),
            "news_snapshot_sha256": digest(news), "indicator_snapshot_path": str((root / "indicator-snapshot.json").resolve()),
            "indicator_snapshot_sha256": digest(rows)}


class RetrievalEvaluationTests(unittest.TestCase):
    def test_new_inputs_use_frozen_forecast_start_and_leave_historical_case_untouched(self):
        old = frozen_case()
        old.pop("retrieval_time_policy")
        old.pop("time_windows")
        original = copy.deepcopy(old)
        manifest = {"source_fingerprints": {"WTI": {"contract": CONTRACT}}}
        case = prepare_retrieval_cases([old], manifest)[0]
        self.assertEqual(old, original)
        self.assertNotIn("display_only", case["time_windows"])
        self.assertEqual(retrieval_metadata(case)["cutoff_utc"], "2026-10-29T21:30:00Z")
        self.assertEqual(case_protocol(old), LEGACY_ACTIVE_PROTOCOL)
        self.assertEqual(case_protocol(case), EVALUATION_PROTOCOL)
        with self.assertRaisesRegex(ValueError, "冻结题缺少"):
            prepare_retrieval_cases([old], {})

    def test_start_persists_new_policy_boundaries_without_changing_source_dataset(self):
        case = frozen_case()
        case.pop("retrieval_time_policy")
        dataset = {"manifest": {"dataset_id": "qa-fixture", "mode": "backtest", "series_type": "dominant_continuous",
                    "sha256": "a" * 64, "source_fingerprints": {"WTI": {"contract": CONTRACT}}},
                   "questions": [case], "labels": {case["case_id"]: forecast()}}
        class Benchmark:
            def load_dataset(self, *args, **kwargs):
                return copy.deepcopy(dataset)
        class News:
            def all(self):
                return []
        class Prepared(EvaluationManager):
            def _launch(self, *args, **kwargs):
                pass
        with tempfile.TemporaryDirectory() as directory:
            manager = Prepared(directory, Benchmark(), News())
            run = manager.start(workers=1)
            saved = read_json(Path(directory) / "runs" / run["run_id"] / "dataset.json")
            self.assertEqual(run["protocol"], EVALUATION_PROTOCOL)
            self.assertEqual(run["retrieval_boundaries"][case["case_id"]]["cutoff_utc"], "2026-10-29T21:30:00Z")
            self.assertEqual(run["retrieval_buffer_minutes"], 30)
            self.assertNotIn("retrieval_buffer_hours", run)
            validate_run_retrieval_policy(run, saved)
            self.assertNotIn("retrieval_time_policy", dataset["questions"][0])
            bad = copy.deepcopy(saved)
            bad["questions"][0].pop("retrieval_time_policy")
            with self.assertRaisesRegex(ValueError, "缺少冻结检索策略"):
                validate_run_retrieval_policy(run, bad)

    def test_daily_snapshot_has_session_end_provenance_and_no_midnight_invention(self):
        bars = [{"trading_date": "2026-10-{}".format(day), "open": 70, "high": 72, "low": 69,
                 "close": 71, "volume": 5} for day in (27, 28, 29, 30)]
        manifest = {"source_fingerprints": {"WTI": {"contract": CONTRACT, "history_bars": bars}}}
        rows = technical_snapshot(manifest, [frozen_case()])[0]["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["available_at"], "2026-10-29T21:00:00Z")
        self.assertEqual(rows[-1]["available_at_basis"], "verified_exchange_session_end")
        self.assertIn("source_url", rows[-1]["availability_source"])
        self.assertFalse(rows[-1]["availability_source"]["supplier_aggregation_verified"])
        self.assertTrue(all("T00:00" not in row["available_at"] for row in rows))
        from time_gate_agent.local import LocalTimeGate
        gated = LocalTimeGate.select_indicator_time_rows(frozen_case(), rows, "2026-01-01T00:00:00Z")
        self.assertEqual([row["date"] for row in gated["rows"]], ["2026-10-27", "2026-10-28"])

    def test_snapshot_preserves_microseconds_and_propagates_late_source_revision(self):
        bars = [{"trading_date": "2026-10-27", "open": 70, "high": 72, "low": 69, "close": 71,
                 "available_at": "2026-10-27T21:00:00Z", "timestamp": "2026-10-27T21:00:00.123456Z",
                 "source_modified_at": "2026-10-29T20:00:00.000001Z"},
                {"trading_date": "2026-10-28", "open": 70, "high": 72, "low": 69, "close": 71}]
        manifest = {"source_fingerprints": {"WTI": {"contract": CONTRACT, "history_bars": bars}}}
        rows = technical_snapshot(manifest, [frozen_case()])[0]["rows"]
        self.assertEqual(rows[0]["timestamp"], bars[0]["timestamp"])
        self.assertEqual(rows[0]["source_available_at"], bars[0]["available_at"])
        self.assertEqual(rows[0]["available_at"], "2026-10-29T20:00:00.000001Z")
        self.assertEqual(rows[1]["available_at"], rows[0]["available_at"])

    def test_unknown_source_timestamp_or_session_never_becomes_a_verified_instant(self):
        for extra in ({"available_at": "2026-10-28"}, {"available_at": "2026-10-29T20:00:00.0000001Z"}):
            bar = {"trading_date": "2026-10-28", "open": 70, "high": 72, "low": 69, "close": 71, **extra}
            manifest = {"source_fingerprints": {"WTI": {"contract": CONTRACT, "history_bars": [bar]}}}
            row = technical_snapshot(manifest, [frozen_case()])[0]["rows"][0]
            self.assertIsNone(row["available_at"])
        bar = {"trading_date": "2026-10-28", "open": 70, "high": 72, "low": 69, "close": 71}
        manifest = {"source_fingerprints": {"WTI": {"contract": {}, "history_bars": [bar]}}}
        row = technical_snapshot(manifest, [frozen_case()])[0]["rows"][0]
        self.assertIsNone(row["available_at"])

    def test_receipts_replay_exact_frozen_responses_even_after_hashes_are_recomputed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = new_context(root)
            work = root / "model"
            write_json(work / "search-context.json", context)
            news = LocalNewsSearch(context["case"], read_json(context["news_snapshot_path"]), work / "search")
            news.search("原油")
            calls = _search_receipts(work)
            self.assertEqual([row["id"] for row in calls[0]["response"]["articles"]], ["safe"])
            technical = LocalIndicatorSearch(context["case"], read_json(context["indicator_snapshot_path"]), work / "indicators")
            technical.call({"indicator": "price", "start_time": "2026-01-01T00:00:00Z"})
            self.assertEqual(_indicator_receipts(work)[0]["response"]["rows"][0]["value"], 70)
            summary_path = work / "indicators/summary.json"
            summary = read_json(summary_path)
            receipt_path = Path(summary["receipts"][0]["receipt_path"])
            receipt = read_json(receipt_path)
            response_path = Path(receipt["files"]["response"]["path"])
            response = read_json(response_path)
            response["rows"][0]["value"] = 123456
            write_json(response_path, response)
            receipt["response_sha256"] = receipt["files"]["response"]["sha256"] = digest(response)
            write_json(receipt_path, receipt)
            summary["receipts"][0]["receipt_sha256"] = digest(receipt)
            write_json(summary_path, summary)
            with self.assertRaisesRegex(ValueError, "重放校验"):
                _indicator_receipts(work)

    def test_codex_trace_accepts_timestamp_tool_parameters_and_records_host_boundary(self):
        self._check_codex_trace(LEGACY_RETRIEVAL_TIME_POLICY)

    def test_30m_codex_trace_uses_new_protocol_and_includes_latest_known_bar(self):
        self._check_codex_trace(RETRIEVAL_TIME_POLICY)

    def _check_codex_trace(self, policy):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = new_context(root, policy)
            work = root / "model"
            write_json(work / "search-context.json", context)
            write_json(work / "prompt.json", {"text": "fixture prompt", "sha256": digest("fixture prompt")})
            news = LocalNewsSearch(context["case"], read_json(context["news_snapshot_path"]), work / "search")
            technical = LocalIndicatorSearch(context["case"], read_json(context["indicator_snapshot_path"]), work / "indicators")
            calls = [("search_news", {"keywords": "原油"}, news.search("原油")),
                     ("search_indicators", {"indicator": "price", "start_time": "2026-01-01T00:00:00Z"},
                      technical.call({"indicator": "price", "start_time": "2026-01-01T00:00:00Z"}))]
            events = [{"type": "item.completed", "item": {"id": str(index), "type": "mcp_tool_call", "server": "env01",
                       "tool": name, "arguments": arguments, "status": "completed",
                       "result": {"content": [{"type": "text", "text": json.dumps(response)}]}}}
                      for index, (name, arguments, response) in enumerate(calls)]
            _save_cli_events(work, events + [{"type": "turn.completed"}])
            trace = _capture_trace(work, forecast())
            self.assertEqual(trace["protocol"], case_protocol(context["case"]))
            self.assertEqual(trace["cutoff_utc"], retrieval_metadata(context["case"])["cutoff_utc"])
            self.assertEqual((trace["search_call_count"], trace["indicator_call_count"]), (1, 1))
            result = next(item["result"] for item in trace["conversation"] if item.get("type") == "tool_result" and item.get("tool") == "search_indicators")
            if policy == RETRIEVAL_TIME_POLICY:
                self.assertEqual(result["end_time"], "2026-10-29T21:30:00Z")
                self.assertEqual(result["rows"][-1]["available_at"], "2026-10-29T21:00:00Z")
                self.assertEqual(trace["buffer_minutes"], 30)
                self.assertNotIn("buffer_hours", trace)
            else:
                self.assertEqual(trace["protocol"], V4_EVALUATION_PROTOCOL)
                self.assertEqual(result["rows"][-1]["available_at"], "2026-10-28T21:00:00Z")
                self.assertEqual(trace["buffer_hours"], 2)
                self.assertNotIn("buffer_minutes", trace)

    def test_both_aihub_transports_use_timestamp_schema_and_default_host_end_time(self):
        self._check_aihub_transports(LEGACY_RETRIEVAL_TIME_POLICY)

    def test_30m_both_aihub_transports_allow_latest_completed_bar(self):
        self._check_aihub_transports(RETRIEVAL_TIME_POLICY)

    def _check_aihub_transports(self, policy):
        for model, transport in (("hy3", "chat"), ("gpt-6.1-sol", "responses")):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                context = new_context(root, policy)
                arguments = {"indicator": "price", "start_time": "2026-01-01T00:00:00Z"}
                answer = json.dumps(dict(forecast(), reason="公开依据", public_analysis={"analysis_document": "公开完整分析"}))
                functions = [("search_news", {"keywords": "原油"}), ("search_indicators", arguments)]
                if transport == "chat":
                    responses = [{"choices": [{"message": {"content": None, "tool_calls": [
                        {"id": str(index), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                        for index, (name, args) in enumerate(functions)]}}]},
                        {"choices": [{"message": {"content": answer}}]}]
                else:
                    responses = [{"id": "response-1", "output": [{"type": "function_call", "call_id": str(index),
                        "name": name, "arguments": json.dumps(args)} for index, (name, args) in enumerate(functions)]},
                        {"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]}]
                payloads = []
                def request(payload, protocol="chat"):
                    payloads.append(copy.deepcopy(payload))
                    return responses[len(payloads) - 1], "fixture-trace"
                predictor = AIHubPricePredictor(model, api_key="fixture-key")
                with mock.patch.object(predictor, "_request", side_effect=request):
                    predictor.predict({"question": context["case"]}, root / "model", context)
                tool = payloads[0]["tools"][1]
                schema = tool["function"]["parameters"] if transport == "chat" else tool["parameters"]
                self.assertIn("start_time", schema["properties"])
                self.assertNotIn("start_date", schema["properties"])
                self.assertNotIn("end_time", schema["required"])
                trace = read_json(root / "model/conversation.json")
                self.assertEqual(trace["protocol"], case_protocol(context["case"]))
                self.assertEqual(trace["indicator_calls"][0]["time_gate"]["cutoff_utc"], retrieval_metadata(context["case"])["cutoff_utc"])
                tool_result = next(item["result"] for item in trace["conversation"] if item.get("type") == "tool_result" and item.get("tool") == "search_indicators")
                if policy == RETRIEVAL_TIME_POLICY:
                    self.assertEqual(tool_result["end_time"], "2026-10-29T21:30:00Z")
                    self.assertEqual(tool_result["rows"][-1]["available_at"], "2026-10-29T21:00:00Z")
                    self.assertIn("exactly 30 minutes before", read_json(root / "model/prompt.json")["text"])
                else:
                    self.assertEqual(tool_result["end_time"], "2026-10-29T20:00:00Z")
                    self.assertEqual(tool_result["rows"][-1]["available_at"], "2026-10-28T21:00:00Z")
                    self.assertIn("exactly two hours before", read_json(root / "model/prompt.json")["text"])

    def test_environment_metadata_lists_both_tools_and_utc_policy(self):
        environment = environments()[0]
        self.assertEqual(environment["search_tools"], ["env01.search_news", "env01.search_indicators"])
        self.assertEqual(environment["retrieval_time_policy"], RETRIEVAL_TIME_POLICY)
        self.assertEqual(environment["retrieval_buffer_minutes"], 30)
        self.assertNotIn("retrieval_buffer_hours", environment)

    def test_v4_and_v5_validation_preserves_frozen_protocol_and_metadata_shape(self):
        for policy, protocol, buffer_key, value in (
            (LEGACY_RETRIEVAL_TIME_POLICY, V4_EVALUATION_PROTOCOL, "buffer_hours", 2),
            (RETRIEVAL_TIME_POLICY, EVALUATION_PROTOCOL, "buffer_minutes", 30)):
            case = frozen_case(policy)
            metadata = retrieval_metadata(case)
            self.assertEqual(metadata[buffer_key], value)
            other = "buffer_minutes" if buffer_key == "buffer_hours" else "buffer_hours"
            self.assertNotIn(other, metadata)
            run = {"protocol": protocol, "retrieval_time_policy": policy,
                   "retrieval_boundaries": {case["case_id"]: metadata}}
            validate_run_retrieval_policy(run, {"questions": [case]})
            run["protocol"] = EVALUATION_PROTOCOL if protocol == V4_EVALUATION_PROTOCOL else V4_EVALUATION_PROTOCOL
            with self.assertRaisesRegex(ValueError, "缺少冻结检索策略"):
                validate_run_retrieval_policy(run, {"questions": [case]})


if __name__ == "__main__":
    unittest.main()
