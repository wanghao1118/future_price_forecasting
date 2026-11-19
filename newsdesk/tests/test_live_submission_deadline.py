"""Live submission boundaries are enforced without reinterpreting accepted locks."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.benchmark import (BenchmarkStore, LIVE_SUBMISSION_POLICY, LEGACY_SUBMISSION_POLICY,
                                _deadline, _write, live_submission_deadline, project_live_submission)
from newsdesk.evaluation import EvaluationManager, _validate_live_open, read_json
from newsdesk.tests.test_live_history import freeze, question
from newsdesk.tests import test_evaluation as evaluation_fixtures
from newsdesk.tests.test_evaluation import (FakeBenchmark, FakePredictor, FakeNews,
                                           write_legacy_v2_run)


class SubmissionBoundaryTests(unittest.TestCase):
    def test_market_and_dst_submission_cutoffs_are_thirty_minutes_before_session_start(self):
        for family, input_day, target, zone, expected in (
            ("SC", "2026-10-29", "2026-10-30", "Asia/Shanghai", "2026-10-29T12:30:00+00:00"),
            ("SC", "2026-09-30", "2026-10-08", "Asia/Shanghai", "2026-10-08T00:30:00+00:00"),
            ("WTI", "2026-10-29", "2026-10-30", "America/New_York", "2026-10-29T21:30:00+00:00"),
            ("WTI", "2026-11-02", "2026-11-03", "America/New_York", "2026-11-02T22:30:00+00:00"),
            ("Brent", "2026-10-22", "2026-10-23", "Europe/London", "2026-10-22T23:30:00+00:00"),
            ("Brent", "2026-10-29", "2026-10-30", "Europe/London", "2026-10-29T23:30:00+00:00"),
            ("Brent", "2026-11-02", "2026-11-03", "Europe/London", "2026-11-03T00:30:00+00:00"),
            ("Brent", "2026-10-30", "2026-11-02", "Europe/London", "2026-11-01T22:30:00+00:00"),
        ):
            with self.subTest(family=family, target=target):
                value = datetime.fromisoformat(_deadline(family, input_day, target, zone)).astimezone(timezone.utc)
                self.assertEqual(value.isoformat(), expected)

    def test_equality_is_rejected_for_all_markets_even_with_later_frozen_deadline(self):
        for family, zone, input_day, target_day, start in (
            ("SC", "Asia/Shanghai", "2026-11-03", "2026-11-04", "2026-11-03T13:00:00Z"),
            ("WTI", "America/New_York", "2026-11-02", "2026-11-03", "2026-11-02T23:00:00Z"),
            ("Brent", "Europe/London", "2026-11-02", "2026-11-03", "2026-11-03T01:00:00Z"),
        ):
            case = question("case", input_day, target_day, family, deadline=start)
            case["exchange_timezone"] = zone
            case["time_windows"]["forecast"] = {"start_utc": start}
            deadline = live_submission_deadline(case)
            with self.subTest(family=family):
                with self.assertRaisesRegex(ValueError, "截止时间"):
                    _validate_live_open(case, deadline.isoformat())
                self.assertEqual(live_submission_deadline(case, legacy=True), deadline + timedelta(minutes=30))

    def test_legacy_current_and_history_projection_never_rewrites_frozen_files(self):
        with tempfile.TemporaryDirectory() as directory:
            store = BenchmarkStore(directory)
            case = question("old", "2026-11-03", "2026-11-04", deadline="2026-11-03T13:00:00Z")
            case["time_windows"]["forecast"] = {"start_utc": "2026-11-03T13:00:00Z"}
            # This accepted historical publication was timely under its original
            # protocol, even though it arrived after today's revised cutoff.
            manifest = freeze(store, "2026-11-03T12:45:00+00:00", [case])
            _write(store._pointer("live"), {"dataset_id": manifest["dataset_id"]})
            before = {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
            with patch("newsdesk.benchmark._current_time", return_value=datetime(2026, 11, 3, 12, 50, tzinfo=timezone.utc)):
                shown = store.questions(mode="live")[0]
                history = store.live_history()
                self.assertEqual(shown["forecast_deadline"], "2026-11-03T12:30:00+00:00")
                self.assertEqual(shown["frozen_forecast_deadline"], "2026-11-03T13:00:00Z")
                self.assertEqual(shown["submission_deadline_policy"], LIVE_SUBMISSION_POLICY)
                self.assertEqual(shown["live_status"], "waiting_new_completed_bar")
                self.assertEqual(store.summary("live")["ready_questions"], 0)
                self.assertEqual(history["summary"]["total_questions"], 1)
                self.assertEqual(history["questions"][0]["live_status"], "forecast_closed")
                self.assertEqual(history["questions"][0]["forecast_deadline"], shown["forecast_deadline"])
            after = {str(path): path.read_bytes() for path in store.root.rglob("*") if path.is_file()}
            self.assertEqual(before, after)
            self.assertEqual(store.load_dataset(manifest["dataset_id"], mode="live")["questions"][0], case)

    def test_display_windows_work_and_missing_windows_have_an_explicit_safe_fallback(self):
        displayed = {"forecast_deadline": "2026-11-03T13:00:00+00:00", "display_time_windows": {
            "forecast": {"start_utc": "2026-11-03T13:00:00Z"}}}
        projected = project_live_submission(displayed)
        self.assertEqual(projected["submission_deadline_source"], "display_time_windows.forecast.start_utc")
        self.assertFalse(projected["submission_deadline_fallback"])
        fallback = project_live_submission({"forecast_deadline": "2026-11-03T21:00:00+08:00"})
        self.assertEqual(fallback["forecast_deadline"], "2026-11-03T13:00:00+00:00")
        self.assertEqual(fallback["submission_deadline_policy"], LEGACY_SUBMISSION_POLICY)
        self.assertTrue(fallback["submission_deadline_fallback"])
        for bad in ({}, {"forecast_deadline": "2026-11-03T13:00:00"}, {
            "forecast_deadline": "2026-11-03T13:00:00Z", "time_windows": {"forecast": {"start_utc": "2026-11-03T13:00:00"}}}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                live_submission_deadline(bad)


# The manager tests use the same fixed fixture clock as its existing suite.
# Defining them separately avoids changing the integration fixtures themselves.
class SubmissionManagerBoundaryTests(unittest.TestCase):
    setUp = evaluation_fixtures.EvaluationManagerTest.setUp

    def test_start_rejects_at_new_cutoff_before_the_old_open_time(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        cutoff = datetime(2026, 11, 2, 22, 30, tzinfo=timezone.utc)
        class AtDeadline(datetime):
            @classmethod
            def now(cls, tz=None):
                return cutoff.astimezone(tz) if tz is not None else cutoff.replace(tzinfo=None)
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            with patch("newsdesk.evaluation.datetime", AtDeadline), self.assertRaisesRegex(ValueError, "截止时间"):
                manager.start(mode="live", family="WTI")
            self.assertEqual(predictor.calls, [])

    def test_dispatch_rechecks_cutoff_after_a_run_has_been_queued(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            with patch.object(manager, "_launch"):
                run = manager.start(mode="live", family="WTI", workers=1)
            root = Path(directory) / "runs" / run["run_id"]
            frozen = read_json(root / "dataset.json")
            deadline = live_submission_deadline(frozen["questions"][0]).isoformat()
            with patch("newsdesk.evaluation.now", return_value=deadline):
                manager._execute(run["run_id"], 1, False)
            final = manager.get_run(run["run_id"])
            self.assertEqual(final["failed"], 1)
            self.assertIn("截止时间", final["cases"][0]["error"])
            self.assertEqual(predictor.calls, [])
            self.assertEqual(run["submission_deadline_policy"], LIVE_SUBMISSION_POLICY)

    def test_lock_equal_to_cutoff_is_rejected_after_a_timely_dispatch(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            original = predictor.predict
            clock = None
            def finish_at_cutoff(material, work, search_context=None):
                nonlocal clock
                raw = original(material, work, search_context=search_context)
                deadline = live_submission_deadline(material["question"]).isoformat()
                clock = patch("newsdesk.evaluation.now", return_value=deadline)
                clock.start()
                return raw
            predictor.predict = finish_at_cutoff
            try:
                run = manager.start(mode="live", family="WTI", workers=1)
                result = manager.wait(run["run_id"], 5)
                self.assertEqual(result["failed"], 1)
                self.assertIn("截止时间", result["cases"][0]["error"])
                self.assertEqual(len(predictor.calls), 1)
            finally:
                if clock:
                    clock.stop()

    def test_original_accepted_lock_after_new_cutoff_still_settles_under_original_policy(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        case = benchmark.questions[0]
        case.update(forecast_deadline="2026-11-02T08:15:00Z", time_windows={"timezone": "UTC", "status": "ready", "forecast": {
            "start_utc": "2026-11-02T08:15:00Z"}})
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            run = write_legacy_v2_run(directory, benchmark, status="awaiting_settlement")
            path = Path(directory) / "runs" / run["run_id"]
            before = (path / "dataset.json").read_bytes()
            benchmark.publish_truth = True
            result = manager.settle(run["run_id"], refresh=False)
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["succeeded"], 1)
            self.assertEqual(predictor.calls, [])
            self.assertEqual((path / "dataset.json").read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
