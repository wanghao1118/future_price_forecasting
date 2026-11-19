"""Live model sessions share API capacity and stop at their UTC deadline."""

from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from newsdesk.evaluation import AIHubPricePredictor, LiveSubmissionDeadlineExceeded, read_json, write_json
from newsdesk.live_prediction_batch import execute_live_batch
from newsdesk.tests import test_live_prediction_batch as fixtures


class LiveRequestDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.clock = [100.0]
        timer = patch("newsdesk.evaluation.time.time", side_effect=lambda: self.clock[0])
        timer.start();self.addCleanup(timer.stop)
        self.predictor = AIHubPricePredictor("hy3", api_key="fixture-key", timeout=600, gateway_timeout=540)
        self.predictor._request_context.deadline = 115.0

    def test_expired_case_never_requests_or_sleeps(self):
        self.clock[0] = 115
        with patch.object(self.predictor, "_request_impl") as request, patch("newsdesk.evaluation.time.sleep") as sleep:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        request.assert_not_called();sleep.assert_not_called()

    def test_capacity_wait_is_bounded_and_unacquired_slot_is_not_released(self):
        slots = Mock();slots.acquire.return_value = False
        self.predictor.request_slots = slots
        with patch.object(self.predictor, "_request_impl") as request:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        slots.acquire.assert_called_once_with(timeout=15.0)
        slots.release.assert_not_called();request.assert_not_called()

    def test_capacity_acquired_after_deadline_is_released_without_a_request(self):
        slots = Mock()

        def acquire(**kwargs):
            self.clock[0] = 116
            return True

        slots.acquire.side_effect = acquire
        self.predictor.request_slots = slots
        with patch.object(self.predictor, "_request_impl") as request:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        slots.release.assert_called_once();request.assert_not_called()

    def test_reply_at_deadline_is_rejected_without_retry(self):
        def reply(*args):
            self.clock[0] = 115
            return {"ok": True}, "trace"

        with patch.object(self.predictor, "_request_impl", side_effect=reply) as request:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        self.assertEqual(request.call_count, 1)

    def test_insufficient_backoff_budget_stops_without_another_provider_call(self):
        with patch.object(self.predictor, "_request_impl", side_effect=RuntimeError("HTTP 429")) as request, \
                patch("newsdesk.evaluation.time.sleep") as sleep:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        self.assertEqual(request.call_count, 1);sleep.assert_not_called()

    def test_backoff_releases_api_capacity_and_rechecks_elapsed_time(self):
        self.predictor._request_context.deadline = 200
        self.predictor.request_slots = threading.BoundedSemaphore(1)

        def sleep(delay):
            self.assertTrue(self.predictor.request_slots.acquire(blocking=False), "Backoff must not monopolize API capacity")
            self.predictor.request_slots.release()
            self.clock[0] += delay

        with patch.object(self.predictor, "_request_impl", side_effect=[RuntimeError("HTTP 429"), ({"ok": True}, "trace")]) as request, \
                patch("newsdesk.evaluation.time.sleep", side_effect=sleep):
            self.assertEqual(self.predictor._request({}), ({"ok": True}, "trace"))
        self.assertEqual(request.call_count, 2)
        self.assertEqual(self.clock[0], 160)

    def test_clock_passes_deadline_while_backing_off(self):
        def sleep(delay):
            self.clock[0] = 116

        with patch.object(self.predictor, "_request_impl", side_effect=RuntimeError("HTTP 502")) as request, \
                patch("newsdesk.evaluation.time.sleep", side_effect=sleep) as pause:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request({})
        self.assertEqual(request.call_count, 1);pause.assert_called_once_with(5)

    def response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.code = 200;response.read.return_value = b'{"ok":true}'
        response.headers.get.return_value = None
        return response

    def test_client_and_gateway_timeouts_shrink_without_changing_frozen_profile(self):
        self.predictor._request_context.deadline = 112.75
        opener = Mock();opener.open.return_value = self.response()
        with patch("newsdesk.evaluation.build_opener", return_value=opener):
            self.assertEqual(self.predictor._request_impl({})[0], {"ok": True})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 12.75)
        auth = opener.open.call_args.args[0].get_header("Authorization")
        self.assertIn("?timeout=12&", auth)
        self.assertEqual((self.predictor.timeout, self.predictor.gateway_timeout), (600, 540))

    def test_backtest_keeps_original_timeout_and_retry_behavior(self):
        self.predictor._request_context.deadline = None
        opener = Mock();opener.open.return_value = self.response()
        with patch("newsdesk.evaluation.build_opener", return_value=opener):
            self.predictor._request_impl({})
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 600)
        self.assertIn("?timeout=540&", opener.open.call_args.args[0].get_header("Authorization"))
        with patch.object(self.predictor, "_request_impl", side_effect=[TimeoutError("timed out"), ({"ok": True}, None)]) as request, \
                patch("newsdesk.evaluation.time.sleep") as sleep:
            self.assertEqual(self.predictor._request({}), ({"ok": True}, None))
        self.assertEqual(request.call_count, 2);sleep.assert_called_once_with(5)

    def test_subsecond_window_does_not_send_gateway_zero_timeout(self):
        self.predictor._request_context.deadline = 100.5
        with patch("newsdesk.evaluation.build_opener") as opener:
            with self.assertRaises(LiveSubmissionDeadlineExceeded):
                self.predictor._request_impl({})
        opener.assert_not_called()

    def test_predict_sets_live_deadline_and_clears_it_for_a_later_backtest(self):
        case = {"mode": "live", "forecast_deadline": "2026-11-06T12:30:00Z"}
        context = {"environment_id": "env02", "case": case}
        with tempfile.TemporaryDirectory() as folder, patch("newsdesk.env02.predict_aihub", return_value="answer"):
            self.predictor.predict({"question": case}, Path(folder) / "live", context)
            self.assertEqual(self.predictor._request_context.deadline, 1793968200.0)
            context["case"] = {"mode": "backtest"}
            self.predictor.predict({}, Path(folder) / "backtest", context)
            self.assertIsNone(self.predictor._request_context.deadline)


class LiveSessionConcurrencyTests(unittest.TestCase):
    def test_all_model_sessions_start_while_sharing_one_external_api_slot_pool(self):
        fixture = fixtures.LivePredictionBatchTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        path = fixture.prepare();batch = read_json(path)
        slots = threading.BoundedSemaphore(1)
        barrier = threading.Barrier(len(batch["models"]))

        def predict(run_path, question, *args):
            # With a one-thread conversation executor this fails: the second
            # model cannot start while the first waits or backs off.
            barrier.wait(timeout=2)
            result = {"case_id": question["case_id"], "family": question["family"],
                      "status": "failed", "error": "fixture HTTP 403"}
            write_json(run_path / "cases" / question["case_id"] / "result.json", result)
            return result

        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            constructors = stack.enter_context(patch("newsdesk.live_prediction_batch.AIHubPricePredictor"))
            stack.enter_context(patch("newsdesk.evaluation.datetime", fixtures.FixedClock))
            stack.enter_context(patch("newsdesk.live_prediction_batch.audit_live_run", return_value={"valid": False}))
            model = stack.enter_context(patch("newsdesk.live_prediction_batch.EvaluationManager._case", side_effect=predict))
            execute_live_batch(fixture.root, path, api_workers=1, retries=0, request_slots=slots)
        self.assertEqual(model.call_count, len(batch["models"]))
        self.assertTrue(all(call.kwargs["request_slots"] is slots for call in constructors.call_args_list))


if __name__ == "__main__":
    unittest.main()
