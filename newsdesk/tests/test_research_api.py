"""Local research API validation without network or model calls."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from newsdesk.server import create_server


class ResearchAPITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(port=0, database=Path(self.temp.name) / "news.sqlite3", seed_demo=False)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, payload=None):
        request = Request(self.base + path,
                          data=json.dumps(payload).encode() if payload is not None else None,
                          headers={"Content-Type": "application/json"})
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_research_catalog_and_empty_question_list(self):
        status, data = self.request("/api/research")
        self.assertEqual(status, 200)
        self.assertEqual(data["benchmark"]["total_questions"], 0)
        self.assertTrue(any(item["id"] == "env01" for item in data["environments"]))
        self.assertTrue(any(item["id"] == "gpt6.1-sol" for item in data["strategies"]))
        self.assertEqual(self.request("/api/benchmark/questions?family=WTI")[1]["items"], [])
        self.assertEqual(self.request("/api/benchmark/questions?family=unknown")[0], 400)

    def test_build_validation_and_freeze_endpoint(self):
        with patch.object(self.server.benchmarks, "build", return_value={"dataset_id": "frozen", "total_questions": 270}) as build:
            self.assertEqual(self.request("/api/benchmark/build", {"days": True})[0], 400)
            self.assertEqual(self.request("/api/benchmark/build", {"days": 30, "unknown": 1})[0], 400)
            self.assertEqual(self.request("/api/benchmark/build", {"days": 30})[0], 201)
            build.assert_called_once_with(days=30, refresh=False, mode="backtest")

    def test_benchmark_modes_are_separate_and_invalid_mode_is_rejected(self):
        self.assertEqual(self.request("/api/benchmark?mode=live")[1]["mode"], "live")
        self.assertEqual(self.request("/api/benchmark?mode=other")[0], 400)
        self.assertEqual(self.request("/api/benchmark/questions?family=WTI&mode=other")[0], 400)
        with patch.object(self.server.benchmarks, "build", return_value={"dataset_id":"live", "mode":"live"}) as build:
            self.assertEqual(self.request("/api/benchmark/build", {"mode":"live"})[0], 201)
            build.assert_called_once_with(days=30, refresh=False, mode="live")

    def test_september_v2_profile_has_independent_read_and_build_routes(self):
        summary = {"dataset_id": "september-v2", "mode": "backtest", "profile": "v2", "total_questions": 57}
        questions = [{"case_id": "september-case", "family": "SC", "input_date": "2026-09-29", "target_date": "2026-09-30"}]
        with patch.object(self.server.benchmarks, "summary", return_value=summary) as read_summary, \
             patch.object(self.server.benchmarks, "questions", return_value=questions) as read_questions, \
             patch.object(self.server.benchmarks, "build", return_value=summary) as build:
            self.assertEqual(self.request("/api/benchmark?mode=backtest&profile=v2"), (200, summary))
            read_summary.assert_called_once_with(mode="backtest", profile="v2")
            result = self.request("/api/benchmark/questions?mode=backtest&profile=v2&family=SC")[1]
            self.assertEqual(result["items"], questions)
            self.assertEqual(result["profile"], "v2")
            read_questions.assert_called_once_with(family="SC", mode="backtest", profile="v2")
            build.assert_not_called()
            self.assertEqual(self.request("/api/benchmark/build", {"mode": "backtest", "profile": "v2"}), (201, summary))
            build.assert_called_once_with(days=30, refresh=False, mode="backtest", profile="v2")

    def test_september_v2_profile_rejects_ambiguous_or_live_selection(self):
        with patch.object(self.server.benchmarks, "build") as build:
            for route in ("/api/benchmark", "/api/benchmark/questions"):
                for query in ("profile=", "profile=v3", "profile=v1&profile=v2", "mode=live&profile=v2",
                              "mode=backtest&mode=live&profile=v2", "mode=live&profile=v2&history=1"):
                    with self.subTest(route=route, query=query):
                        self.assertEqual(self.request(route + "?" + query)[0], 400)
            for payload in ({"profile": "other"}, {"profile": True}, {"profile": {}},
                            {"profile": "v2", "mode": "live"},
                            {"profile": "v2", "start_date": "2026-08-01"}):
                with self.subTest(payload=payload):
                    self.assertEqual(self.request("/api/benchmark/build", payload)[0], 400)
            build.assert_not_called()

    def test_evaluation_start_validates_before_submitting_and_records_conflict(self):
        with patch.object(self.server.evaluations, "start", return_value={"run_id": "test-run", "status": "running"}) as start:
            self.assertEqual(self.request("/api/evaluations", {"workers": 0})[0], 400)
            self.assertEqual(self.request("/api/evaluations", {"workers": True})[0], 400)
            status, result = self.request("/api/evaluations", {"workers": 4})
            self.assertEqual(status, 202)
            self.assertEqual(result["run_id"], "test-run")
            start.assert_called_once_with(strategy_id="gpt6.1-sol", environment_id="env01", dataset_id=None, workers=4, mode="backtest", family=None)
        with patch.object(self.server.evaluations, "start", side_effect=RuntimeError("已有评估运行中")):
            self.assertEqual(self.request("/api/evaluations", {})[0], 409)

    def test_live_history_routes_are_read_only_and_keep_current_routes_separate(self):
        summary = {"mode": "live", "history": True, "total_questions": 2, "history_revision": "revision", "errors": {"bad": {"message": "damaged"}}}
        questions = [{"case_id": "wti", "family": "WTI"}, {"case_id": "sc", "family": "SC"}]
        with patch.object(self.server.benchmarks, "live_history_summary", return_value=summary) as history_summary, \
             patch.object(self.server.benchmarks, "live_history", return_value={"summary": summary, "questions": questions}) as history, \
             patch.object(self.server.benchmarks, "build") as build, \
             patch.object(self.server.benchmarks, "settle_live") as settle, \
             patch.object(self.server.evaluations, "start") as start:
            self.assertEqual(self.request("/api/benchmark?mode=live&history=1"), (200, summary))
            result = self.request("/api/benchmark/questions?mode=live&history=1&family=SC")[1]
            self.assertEqual(result["items"], [questions[1]])
            self.assertEqual(result["history_revision"], "revision")
            self.assertEqual(result["errors"], summary["errors"])
            self.assertEqual(self.request("/api/benchmark/questions?mode=live&history=1")[1]["total"], 2)
            self.assertEqual(self.request("/api/benchmark?mode=live&history=0")[1]["total_questions"], 0)
            self.assertEqual(self.request("/api/benchmark/questions?mode=live")[1]["items"], [])
            history_summary.assert_called_once()
            self.assertEqual(history.call_count, 2)
            build.assert_not_called()
            settle.assert_not_called()
            start.assert_not_called()

    def test_live_history_parameters_reject_ambiguous_or_nonlive_requests(self):
        for route in ("/api/benchmark", "/api/benchmark/questions"):
            for query in ("history=1", "mode=backtest&history=1", "mode=live&history=", "mode=live&history=true", "mode=live&history=2", "mode=live&history=1&history=0", "mode=live&mode=backtest&history=1"):
                with self.subTest(route=route, query=query):
                    self.assertEqual(self.request(route + "?" + query)[0], 400)

    def test_live_prediction_dialog_route_is_read_only_and_public_proxy_compatible(self):
        dataset_id = "qa-" + "a" * 20
        payload = {"mode": "live", "items": [{"model": "gpt-6.1-sol", "status": "pending"}], "total": 1}
        route = "/api/benchmark?mode=live&kind=predictions&dataset_id=" + dataset_id + "&case_id=case-1"
        with patch("newsdesk.live_predictions.live_question_predictions", return_value=payload) as read, \
             patch.object(self.server.benchmarks, "build") as build, \
             patch.object(self.server.benchmarks, "settle_live") as settle, \
             patch.object(self.server.evaluations, "start") as start:
            self.assertEqual(self.request(route), (200, payload))
            read.assert_called_once_with(self.server.benchmarks, self.server.evaluations, dataset_id, "case-1")
            build.assert_not_called()
            settle.assert_not_called()
            start.assert_not_called()
        with patch("newsdesk.live_predictions.live_question_predictions", side_effect=LookupError("missing")):
            self.assertEqual(self.request(route)[0], 404)
        with patch("newsdesk.live_predictions.live_question_predictions", side_effect=ValueError("damaged")):
            self.assertEqual(self.request(route)[0], 503)

    def test_live_prediction_dialog_route_rejects_injection_and_ambiguous_parameters(self):
        dataset_id = "qa-" + "a" * 20
        prefix = "/api/benchmark?kind=predictions&"
        for query in ("mode=backtest&dataset_id=" + dataset_id + "&case_id=case",
                      "mode=live&case_id=case", "mode=live&dataset_id=../../private&case_id=case",
                      "mode=live&dataset_id=" + dataset_id + "&case_id=..%2Fprivate",
                      "mode=live&dataset_id=" + dataset_id + "&case_id=case&case_id=other",
                      "mode=live&mode=backtest&dataset_id=" + dataset_id + "&case_id=case"):
            with self.subTest(query=query):
                self.assertEqual(self.request(prefix + query)[0], 400)

    def test_evaluation_history_and_missing_run(self):
        self.assertEqual(self.request("/api/evaluations")[1]["items"], [])
        self.assertEqual(self.request("/api/evaluations/not-a-run")[0], 404)
        self.assertEqual(self.request("/data/evaluations/labels.json")[0], 404)

    def test_case_trace_is_lazy_and_downloadable(self):
        transcript = {"run_id": "test-run", "case_id": "case-1",
                      "reasoning_capture": "public_summary_only", "conversation": []}
        with patch.object(self.server.evaluations, "get_case_trace", return_value=transcript) as trace:
            self.assertEqual(self.request("/api/evaluations/test-run/cases/case-1/trace"), (200, transcript))
            trace.assert_called_once_with("test-run", "case-1")
            with urlopen(self.base + "/api/evaluations/test-run/cases/case-1/trace?download=1", timeout=5) as response:
                self.assertEqual(json.load(response), transcript)
                self.assertIn('attachment; filename="env01-case-1-trace.json"', response.headers["Content-Disposition"])

    def test_case_trace_rejects_missing_and_malformed_identifiers(self):
        with patch.object(self.server.evaluations, "get_case_trace", side_effect=ValueError("invalid case")):
            self.assertEqual(self.request("/api/evaluations/run/cases/%2E%2E/trace")[0], 404)
        with patch.object(self.server.evaluations, "get_case_trace") as trace:
            self.assertEqual(self.request("/api/evaluations/run/cases/extra/case/trace")[0], 404)
            trace.assert_not_called()

    def test_live_settlement_endpoint_scores_frozen_predictions_without_start(self):
        with patch.object(self.server.evaluations, "settle", return_value={"run_id":"live", "pending":1}) as settle, patch.object(self.server.evaluations, "start") as start:
            self.assertEqual(self.request("/api/evaluations/live/settle", {"refresh":True})[0], 200)
            settle.assert_called_once_with("live", refresh=True)
            start.assert_not_called()
            self.assertEqual(self.request("/api/evaluations/live/settle", {"refresh":"yes"})[0], 400)


if __name__ == "__main__":
    unittest.main()
