"""Daily futures integration without upstream or model calls."""

from datetime import datetime
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from newsdesk.benchmark import BenchmarkStore
from newsdesk.daily_update import _continuous_summary, run_futures_update
from newsdesk.scheduler import UpdateScheduler, contract_publication_retry
from newsdesk.server import create_server


def month_report(status="success"):
    updated = 18 if status == "success" else 17 if status == "partial" else 0
    return {"status": status, "series_total": 18, "updated": updated, "failed": 18 - updated,
            "contracts_total": 9, "series": [], "failures": []}


class ContinuousFixture:
    def __init__(self, stale=(), failed=(), missing_sources=(), current_sc=True):
        self.stale = set(stale)
        self.failed = set(failed)
        self.missing_sources = set(missing_sources)
        self.current_sc = current_sc
        self.fetched_at = "2026-10-29T08:00:00+00:00"
        self.day_sets = {}
        self.unfinished = set()
        self.calls = []

    def get(self, family, refresh=False):
        self.calls.append((family, refresh))
        if family in self.failed:
            raise RuntimeError("fixture provider failed")
        zone = {"WTI": "America/New_York", "Brent": "Europe/London", "SC": "Asia/Shanghai"}[family]
        days = ["2026-10-27", "2026-10-28"]
        if family == "SC" and self.current_sc:
            days.append("2026-10-29")
        days = self.day_sets.get(family, days)
        return {"period": "1d", "contract": {"id": family.lower() + "-main", "familyId": family,
                "code": {"WTI": "CL.1:NYM", "Brent": "IB.1:IEU", "SC": "SC0"}[family],
                "exchange": {"WTI": "NYMEX", "Brent": "ICE", "SC": "INE"}[family],
                "currency": "CNY" if family == "SC" else "USD", "unit": "桶",
                "series_type": "dominant_continuous"},
                "source": {"name": "" if family in self.missing_sources else "fixture real series",
                           "timezone": zone, "series_type": "dominant_continuous"},
                "fetched_at": self.fetched_at, "data_as_of": days[-1],
                "bars": [{"trading_date": day, "open": 70, "high": 72, "low": 69, "close": 71,
                          "is_complete": (family, day) not in self.unfinished} for day in days],
                "cache": {"stale": family in self.stale}}


class DailyFuturesUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.provider = ContinuousFixture()
        self.benchmarks = BenchmarkStore(self.root / "benchmark", continuous_market=self.provider)
        self.market = Mock()
        self.market.refresh_all.return_value = month_report()
        self.time = patch("newsdesk.benchmark._current_time", side_effect=lambda zone:
                          datetime(2026, 10, 29, 16, tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(ZoneInfo(zone)))
        self.time.start()

    def tearDown(self):
        self.time.stop()
        self.temp.cleanup()

    def test_updates_prices_and_live_only_and_preserves_frozen_history(self):
        self.benchmarks.build(days=1, mode="backtest")
        self.benchmarks.build(mode="live")
        before = {str(path.relative_to(self.root)): path.read_bytes()
                  for path in self.root.glob("benchmark/datasets/**/*") if path.is_file()}
        backtest_pointer = (self.root / "benchmark/current-backtest.json").read_bytes()
        self.provider.calls.clear()
        result = run_futures_update(self.market, self.benchmarks)
        self.market.refresh_all.assert_called_once_with()
        self.assertEqual(sorted(self.provider.calls), [("Brent", True), ("SC", True), ("WTI", True)])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["updated"], 18)
        self.assertEqual(result["continuous"]["updated"], 3)
        self.assertEqual(result["live_benchmark"]["ready_questions"], 1)
        self.assertEqual(result["live_benchmark"]["unavailable_families"], ["WTI", "Brent"])
        self.assertEqual((self.root / "benchmark/current-backtest.json").read_bytes(), backtest_pointer)
        for relative, data in before.items():
            self.assertEqual((self.root / relative).read_bytes(), data)

    def test_month_failure_does_not_block_live_questions(self):
        self.market.refresh_all.side_effect = RuntimeError("months unavailable")
        result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["month_contracts"]["status"], "failed")
        self.assertEqual(result["continuous"]["updated"], 3)
        self.assertEqual(result["live_benchmark"]["ready_questions"], 1)
        self.assertIn("months unavailable", result["error"])

    def test_publication_notifies_predictions_before_settlement(self):
        notify = Mock(return_value={"status": "scheduled", "experiment_id": "Exp01"})
        from newsdesk.daily_update import _settle_history

        def settle(*args, **kwargs):
            notify.assert_called_once_with()
            return _settle_history(*args, **kwargs)

        with patch("newsdesk.daily_update._settle_history", side_effect=settle):
            result = run_futures_update(self.market, self.benchmarks, on_live_published=notify)
        self.assertEqual(result["live_benchmark"]["model_predictions"]["status"], "scheduled")

    def test_prediction_notification_failure_does_not_undo_publication_or_settlement(self):
        result = run_futures_update(self.market, self.benchmarks,
                                    on_live_published=Mock(side_effect=OSError("dispatcher unavailable")))
        self.assertEqual(result["live_benchmark"]["ready_questions"], 1)
        self.assertEqual(result["live_benchmark"]["model_predictions"]["status"], "failed")
        self.assertEqual(result["live_settlement"]["status"], "success")

    def test_stale_and_failed_series_are_not_counted_as_fresh(self):
        self.provider.stale.add("WTI")
        self.provider.failed.add("Brent")
        result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["continuous"]["updated"], 1)
        self.assertEqual(result["continuous"]["failed"], 2)
        self.assertEqual(result["live_benchmark"]["status"], "partial")
        self.assertEqual(result["live_benchmark"]["ready_questions"], 1)
        self.assertIn("Brent", result["error"])
        self.assertIn("WTI", result["error"])

    def test_fresh_but_unfinished_trading_day_is_waiting_not_failed(self):
        self.provider.current_sc = False
        self.provider.fetched_at = "2026-10-29T06:00:00+00:00"
        with self.at_capture():
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["continuous"]["updated"], 3)
        self.assertEqual(result["live_benchmark"]["status"], "waiting")
        self.assertEqual(result["live_benchmark"]["ready_questions"], 0)
        self.assertEqual(result["live_benchmark"]["unavailable_families"], ["WTI", "Brent", "SC"])

    def at_capture(self):
        return patch("newsdesk.benchmark._current_time", side_effect=lambda zone:
                     datetime.fromisoformat(self.provider.fetched_at).astimezone(ZoneInfo(zone)))

    def set_source_days(self, foreign, sc, fetched_at, unfinished_foreign=None):
        self.provider.fetched_at = fetched_at
        self.provider.day_sets = {"WTI": foreign, "Brent": foreign, "SC": sc}
        self.provider.unfinished = {(family, unfinished_foreign) for family in ("WTI", "Brent")
                                    if unfinished_foreign}

    def test_latest_running_quote_does_not_hide_old_completed_bars(self):
        self.set_source_days(["2026-10-16", "2026-10-30"], ["2026-10-29", "2026-10-30"],
                             "2026-10-30T14:04:35+00:00", "2026-10-30")
        with self.at_capture():
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        continuous = result["continuous"]
        self.assertEqual((continuous["updated"], continuous["completed_updated"], continuous["waiting"], continuous["failed"]), (3, 1, 2, 0))
        for row in continuous["series"][:2]:
            self.assertEqual(row["data_as_of"], "2026-10-30")
            self.assertEqual(row["latest_completed_date"], "2026-10-16")
            self.assertEqual(row["fetched_at"], self.provider.fetched_at)
            self.assertEqual(row["status"], "waiting")
            self.assertEqual(row["completeness"], "recent_gap_unverified")
            self.assertEqual(row["recent_unverified_weekday_gaps"], [
                "2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23",
                "2026-10-26", "2026-10-27", "2026-10-28", "2026-10-29"])
            self.assertTrue(row["needs_daily_retry"])
        sc = continuous["series"][2]
        self.assertEqual(sc["latest_completed_date"], "2026-10-30")
        self.assertFalse(sc["needs_daily_retry"])
        self.assertEqual(result["live_benchmark"]["ready_questions"], 0)
        self.assertEqual(result["live_benchmark"]["status"], "partial")
        self.assertIn("完整主连日K仅到 2026-10-16", result["error"])

    def test_intraday_previous_complete_bar_is_current_without_promoting_today(self):
        self.set_source_days(["2026-10-29", "2026-10-30"], ["2026-10-29", "2026-10-30"],
                             "2026-10-30T14:00:00+00:00", "2026-10-30")
        with self.at_capture():
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["continuous"]["completed_updated"], 3)
        self.assertEqual(result["continuous"]["waiting"], 0)
        for row in result["continuous"]["series"][:2]:
            self.assertEqual(row["latest_completed_date"], "2026-10-29")
            self.assertEqual(row["recent_unverified_weekday_gaps"], [])
            self.assertFalse(row["needs_daily_retry"])

    def test_weekend_and_distant_unverified_holiday_do_not_make_update_partial(self):
        self.set_source_days(["2026-10-21", "2026-10-23", "2026-10-26"],
                             ["2026-10-21", "2026-10-23", "2026-10-26"],
                             "2026-10-26T12:00:00+00:00", "2026-10-26")
        with self.at_capture():
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "success")
        for row in result["continuous"]["series"]:
            self.assertGreater(row["historical_unverified_gap_count"], 0)
            self.assertEqual(row["recent_unverified_weekday_gaps"], [])
            self.assertFalse(row["needs_daily_retry"])

    def test_post_close_missing_today_is_waiting_for_publication(self):
        self.set_source_days(["2026-10-28", "2026-10-29"], ["2026-10-28", "2026-10-29"],
                             "2026-10-30T07:35:00+00:00")
        with self.at_capture():
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["continuous"]["completed_updated"], 2)
        sc = result["continuous"]["series"][2]
        self.assertEqual(sc["recent_unverified_weekday_gaps"], ["2026-10-30"])
        self.assertEqual(sc["status"], "waiting")
        self.assertTrue(sc["needs_daily_retry"])

    def test_absent_completed_bar_or_capture_metadata_is_reported_unknown(self):
        manifest = {"families": [{"id": family, "contracts": [{"source_name": "fixture", "data_as_of": "2026-10-30"}]} for family in ("WTI", "Brent", "SC")]}
        result = _continuous_summary(manifest)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["completed_updated"], 0)
        self.assertEqual(result["waiting"], 3)
        self.assertTrue(all(row["completeness"] == "no_completed_bar" for row in result["series"]))
        manifest["source_fingerprints"] = {family: {"selected_bars": [{"trading_date": "2026-10-29"}]} for family in ("WTI", "Brent", "SC")}
        result = _continuous_summary(manifest)
        self.assertEqual(result["status"], "partial")
        self.assertTrue(all(row["completeness"] == "unknown" and row["needs_daily_retry"] for row in result["series"]))

    def test_live_build_failure_still_returns_successful_month_result(self):
        with patch.object(self.benchmarks, "build", side_effect=OSError("dataset write failed")):
            result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["updated"], 18)
        self.assertEqual(result["live_benchmark"]["status"], "failed")
        self.assertIn("dataset write failed", result["error"])

    def test_both_stages_failing_is_failed(self):
        self.market.refresh_all.return_value = month_report("failed")
        self.provider.failed.update(["WTI", "Brent", "SC"])
        result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["live_benchmark"]["ready_questions"], 0)

    def test_all_sources_failing_preserves_previously_published_live_questions(self):
        self.benchmarks.build(mode="live")
        pointer = self.root / "benchmark/current-live.json"
        before = pointer.read_bytes()
        previous_questions = self.benchmarks.questions(mode="live")
        self.provider.failed.update(["WTI", "Brent", "SC"])
        result = run_futures_update(self.market, self.benchmarks)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["continuous"]["updated"], 0)
        self.assertEqual(result["live_benchmark"]["status"], "failed")
        self.assertEqual(pointer.read_bytes(), before)
        self.assertEqual(self.benchmarks.questions(mode="live"), previous_questions)
        self.assertIn("保留上次题目", result["error"])

    def test_all_stale_caches_or_mixed_stale_and_failed_preserve_live_pointer(self):
        self.benchmarks.build(mode="live")
        pointer = self.root / "benchmark/current-live.json"
        before = pointer.read_bytes()
        for failed in [set(), {"Brent"}]:
            with self.subTest(failed=failed):
                self.provider.failed = failed
                self.provider.stale = {"WTI", "Brent", "SC"} - failed
                result = run_futures_update(self.market, self.benchmarks)
                self.assertEqual(result["status"], "partial")
                self.assertEqual(result["continuous"]["updated"], 0)
                self.assertEqual(result["live_benchmark"]["status"], "failed")
                self.assertEqual(pointer.read_bytes(), before)

    def test_preserved_live_dataset_failure_keeps_due_publication_retry_identifiable(self):
        self.benchmarks.build(mode="live")
        pointer = self.root / "benchmark/current-live.json"
        before = pointer.read_bytes()
        for failure in ("failed", "stale"):
            with self.subTest(failure=failure):
                self.provider.failed = set(("WTI", "Brent", "SC")) if failure == "failed" else set()
                self.provider.stale = set(("WTI", "Brent", "SC")) if failure == "stale" else set()
                self.provider.fetched_at = "2026-10-30T07:35:00+00:00"
                with self.at_capture():
                    result = run_futures_update(self.market, self.benchmarks)
                self.assertEqual(pointer.read_bytes(), before)
                self.assertEqual(result["continuous"]["updated"], 0)
                self.assertEqual(result["continuous"]["failed"], 3)
                self.assertEqual(result["live_benchmark"]["status"], "failed")
                self.assertIn("保留上次题目", result["error"])
                for row in result["continuous"]["series"]:
                    self.assertEqual(row["status"], "failed")
                    self.assertIsNone(row["latest_completed_date"])
                    self.assertEqual(row["completeness"], "unavailable")
                    self.assertTrue(row["needs_daily_retry"])
                    self.assertIn("保留上次题目", row["error"])
                retry = contract_publication_retry(result, datetime(2026, 10, 30, 15, 35, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp())
                self.assertEqual(retry["family"], "SC")
                self.assertEqual(retry["status"], "waiting")
                self.assertEqual(retry["attempts"], 1)
                self.assertEqual(retry["next_run_at"], "2026-10-30T07:40:00+00:00")
                self.assertIsNone(contract_publication_retry(result, datetime(2026, 10, 30, 22, 4, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()))

    def test_live_publishes_while_month_requests_are_still_blocked(self):
        entered, release, published = threading.Event(), threading.Event(), threading.Event()
        original_build = self.benchmarks.build
        def block_months():
            entered.set()
            release.wait(timeout=3)
            return month_report()
        def publish(**kwargs):
            result = original_build(**kwargs)
            published.set()
            return result
        self.market.refresh_all.side_effect = block_months
        with patch.object(self.benchmarks, "build", side_effect=publish):
            thread = threading.Thread(target=run_futures_update, args=(self.market, self.benchmarks))
            thread.start()
            try:
                self.assertTrue(entered.wait(timeout=1))
                self.assertTrue(published.wait(timeout=1))
                self.assertTrue(thread.is_alive())
            finally:
                release.set()
                thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def publish_sc_history(self, duplicate=False):
        """Publish a real pre-deadline SC fixture, with no later truth yet."""
        self.set_source_days(["2026-10-28", "2026-10-29"], ["2026-10-28", "2026-10-29"],
                             "2026-10-29T08:00:00+00:00", "2026-10-29")
        with self.at_capture(), patch("newsdesk.benchmark._now", return_value=self.provider.fetched_at):
            first = self.benchmarks.build(mode="live")
        if duplicate:
            self.provider.fetched_at = "2026-10-29T08:10:00+00:00"
            with self.at_capture(), patch("newsdesk.benchmark._now", return_value=self.provider.fetched_at):
                second = self.benchmarks.build(mode="live")
            return [first["dataset_id"], second["dataset_id"]]
        return [first["dataset_id"]]

    def next_sc_update(self, evaluations=None):
        with self.at_capture(), patch("newsdesk.benchmark._now", return_value=self.provider.fetched_at):
            return run_futures_update(self.market, self.benchmarks, evaluations)

    def test_daily_close_settles_prior_publications_and_preserves_new_pending_question(self):
        versions = self.publish_sc_history(duplicate=True)
        immutable = {str(path): path.read_bytes() for identifier in versions
                     for path in (self.benchmarks.root / "datasets" / identifier).glob("*.json")}
        # A missed Friday run is caught up on Monday. The Friday bar, rather
        # than Monday's close, settles Thursday's next-session question.
        self.set_source_days(["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"],
                             ["2026-10-28", "2026-10-29", "2026-10-30", "2026-11-02"],
                             "2026-11-02T08:00:00+00:00", "2026-11-02")
        self.provider.calls.clear()
        report = self.next_sc_update()
        self.assertEqual(report["live_settlement"]["newly_settled_questions"], 1)
        self.assertEqual(report["live_settlement"]["settled_versions"], 2)
        self.assertEqual(sorted(self.provider.calls), [("Brent", True), ("SC", True), ("WTI", True)])
        for identifier in versions:
            dataset = self.benchmarks.load_dataset(identifier, mode="live")
            sc = next(item for item in dataset["questions"] if item["family"] == "SC")
            self.assertEqual(dataset["labels"][sc["case_id"]]["target_date"], "2026-10-30")
            self.assertEqual(set(dataset["labels"]), {sc["case_id"]}, "Unpublished foreign previews are not auto-settled")
        for path, original in immutable.items():
            self.assertEqual(Path(path).read_bytes(), original)
        current = self.benchmarks.load_dataset(mode="live")
        self.assertEqual(current["labels"], {})
        sc = next(item for item in self.benchmarks.questions(mode="live") if item["family"] == "SC")
        self.assertEqual((sc["input_date"], sc["target_date"], sc["settlement_status"]), ("2026-11-02", "2026-11-03", "pending"))

    def test_incomplete_or_failed_close_cannot_add_historical_truth(self):
        identifiers = self.publish_sc_history()
        self.set_source_days(["2026-10-28", "2026-10-29"], ["2026-10-28", "2026-10-29", "2026-10-30"],
                             "2026-10-30T07:35:00+00:00")
        for cause in ("unfinished", "failed", "stale"):
            with self.subTest(cause=cause):
                self.provider.unfinished = {("SC", "2026-10-30")} if cause == "unfinished" else set()
                self.provider.failed = {"SC"} if cause == "failed" else set()
                self.provider.stale = {"SC"} if cause == "stale" else set()
                self.provider.calls.clear()
                report = self.next_sc_update()
                self.assertEqual(report["live_settlement"]["settled_versions"], 0)
                self.assertEqual(self.benchmarks.load_dataset(identifiers[0], mode="live")["labels"], {})
                self.assertTrue(all(refresh for _, refresh in self.provider.calls), "Settlement must not fetch another source")

    def test_unverified_missing_next_session_stays_pending(self):
        identifiers = self.publish_sc_history()
        self.set_source_days(["2026-10-29", "2026-11-02"], ["2026-10-29", "2026-11-02"],
                             "2026-11-02T08:00:00+00:00", "2026-11-02")
        report = self.next_sc_update()
        self.assertEqual(report["live_settlement"]["newly_settled_questions"], 0)
        self.assertEqual(report["live_settlement"]["status"], "partial")
        self.assertIn("data_gap_unverified", report["error"])
        self.assertEqual(self.benchmarks.load_dataset(identifiers[0], mode="live")["labels"], {})
        self.assertEqual(report["live_benchmark"]["ready_questions"], 1, "A settlement gap must not hide a freshly published question")

    def test_settlement_failure_is_reported_without_discarding_new_publication(self):
        self.publish_sc_history()
        self.set_source_days(["2026-10-29", "2026-10-30"], ["2026-10-29", "2026-10-30"],
                             "2026-10-30T07:35:00+00:00", "2026-10-30")
        with patch.object(self.benchmarks, "settle_live", side_effect=OSError("settlement write failed")):
            report = self.next_sc_update()
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["live_benchmark"]["ready_questions"], 1)
        self.assertIn("settlement write failed", report["error"])

    def test_only_pending_inactive_live_runs_are_scored_without_model_calls(self):
        self.publish_sc_history()
        self.set_source_days(["2026-10-29", "2026-10-30"], ["2026-10-29", "2026-10-30"],
                             "2026-10-30T07:35:00+00:00", "2026-10-30")
        evaluations = Mock()
        evaluations.runs.return_value = [
            {"run_id": "locked", "mode": "live", "pending": 1, "status": "awaiting_settlement", "succeeded": 0},
            {"run_id": "running", "mode": "live", "pending": 1, "status": "running"},
            {"run_id": "finished", "mode": "live", "pending": 0, "status": "completed"},
            {"run_id": "backtest", "mode": "backtest", "pending": 1, "status": "completed"},
        ]
        evaluations.settle.return_value = {"succeeded": 1}
        report = self.next_sc_update(evaluations)
        self.assertEqual(report["live_settlement"]["runs_updated"], 1)
        evaluations.settle.assert_called_once()
        args, kwargs = evaluations.settle.call_args
        self.assertEqual(args, ("locked",))
        self.assertFalse(kwargs["refresh"])
        self.assertIn("SC", kwargs["snapshots"])
        evaluations.start.assert_not_called()
        evaluations.settle.reset_mock()
        evaluations.runs.return_value = [{"run_id": "complete", "mode": "live", "pending": 0}]
        self.next_sc_update(evaluations)
        evaluations.settle.assert_not_called()
        evaluations.start.assert_not_called()

    def test_combined_report_is_persisted_by_scheduler(self):
        scheduler = UpdateScheduler(self.root / "settings.json", {
            "news": lambda: {"status": "success"},
            "contracts": lambda: run_futures_update(self.market, self.benchmarks)})
        try:
            self.assertTrue(scheduler.trigger("contracts"))
            deadline = time.monotonic() + 3
            while scheduler.snapshot()["jobs"]["contracts"]["running"] and time.monotonic() < deadline:
                time.sleep(0.005)
            saved = json.loads((self.root / "settings.json").read_text())["jobs"]["contracts"]
            self.assertEqual(saved["status"], "success")
            self.assertEqual(saved["summary"]["live_benchmark"]["ready_questions"], 1)
            self.assertEqual(saved["summary"]["continuous"]["updated"], 3)
        finally:
            scheduler.stop()

    def test_server_contract_job_is_wired_to_combined_pipeline(self):
        with patch("newsdesk.daily_update.run_futures_update", return_value={"status": "success"}) as run:
            server = create_server(port=0, database=self.root / "server-news.sqlite3", seed_demo=False)
            try:
                server.updates.runners["contracts"]()
                run.assert_called_once_with(server.market_data, server.benchmarks, server.evaluations,
                                            on_live_published=server.live_predictions.notify)
            finally:
                server.server_close()


if __name__ == "__main__":
    unittest.main()
