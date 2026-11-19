"""Offline tests for persisted schedules, overlap prevention and real-run wiring."""

import json
import tempfile
import threading
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from newsdesk.scheduler import (UpdateScheduler, news_worker, source_health,
                               next_update_time, DAILY_CONTRACT_POLICY, contract_publication_retry)


class DailyScheduleTests(unittest.TestCase):
    @staticmethod
    def instant(value):
        return datetime.fromisoformat(value).timestamp()

    def test_each_market_has_its_post_close_slot_and_weekends_are_skipped(self):
        for current, expected in [
            ("2026-10-30T07:34:00+00:00", "2026-10-30T07:35:00+00:00"),
            ("2026-10-30T07:36:00+00:00", "2026-10-30T21:35:00+00:00"),
            ("2026-10-30T21:36:00+00:00", "2026-10-30T23:35:00+00:00"),
            ("2026-10-30T23:36:00+00:00", "2026-11-02T07:35:00+00:00"),
        ]:
            with self.subTest(current=current):
                self.assertEqual(next_update_time("contracts", self.instant(current), 1440), expected)

    def test_uk_and_us_daylight_saving_changes_are_independent(self):
        for current, expected in [
            ("2026-10-23T21:36:00+00:00", "2026-10-23T22:35:00+00:00"),
            ("2026-10-30T21:36:00+00:00", "2026-10-30T23:35:00+00:00"),
            ("2026-11-02T07:36:00+00:00", "2026-11-02T22:35:00+00:00"),
        ]:
            with self.subTest(current=current):
                self.assertEqual(next_update_time("contracts", self.instant(current), 1440), expected)

    def test_legacy_schedule_migrates_once_and_restart_catches_up_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "settings.json"
            now = [self.instant("2026-10-30T07:34:00+00:00")]
            calls = []
            runners = {"news": lambda: {"status": "success"},
                       "contracts": lambda: calls.append("contracts") or {"status": "success"}}
            scheduler = UpdateScheduler(path, runners, clock=lambda: now[0])
            scheduler.configure({"news_interval_minutes": 10080})
            saved = json.loads(path.read_text())
            saved["jobs"]["contracts"].pop("schedule_policy")
            saved["jobs"]["contracts"]["next_run_at"] = "2026-10-30T02:51:00+00:00"
            path.write_text(json.dumps(saved))
            scheduler = UpdateScheduler(path, runners, clock=lambda: now[0])
            job = scheduler.snapshot()["jobs"]["contracts"]
            self.assertEqual(job["schedule_policy"], DAILY_CONTRACT_POLICY)
            self.assertEqual(job["next_run_at"], "2026-10-30T07:35:00+00:00")
            now[0] = self.instant("2026-10-30T21:36:00+00:00")
            scheduler = UpdateScheduler(path, runners, clock=lambda: now[0])
            self.assertEqual(scheduler.snapshot()["jobs"]["contracts"]["next_run_at"], job["next_run_at"])
            scheduler.run_due()
            scheduler._workers["contracts"].join(timeout=2)
            self.assertEqual(calls, ["contracts"])
            self.assertEqual(scheduler.snapshot()["jobs"]["contracts"]["next_run_at"], "2026-10-30T23:35:00+00:00")
            scheduler.run_due()
            self.assertEqual(calls, ["contracts"])
            scheduler.stop()

    def test_custom_interval_can_switch_back_to_market_daily_schedule(self):
        with tempfile.TemporaryDirectory() as temporary:
            now = self.instant("2026-10-30T07:34:00+00:00")
            scheduler = UpdateScheduler(Path(temporary) / "settings.json", {}, clock=lambda: now)
            custom = scheduler.configure({"contract_interval_minutes": 60})
            self.assertEqual(custom["jobs"]["contracts"]["next_run_at"], "2026-10-30T08:34:00+00:00")
            self.assertEqual(custom["contract_update_schedule"]["mode"], "interval")
            daily = scheduler.configure({"contract_interval_minutes": 1440})
            self.assertEqual(daily["jobs"]["contracts"]["next_run_at"], "2026-10-30T07:35:00+00:00")
            self.assertEqual(daily["contract_update_schedule"]["mode"], "exchange_close_daily")
            scheduler.stop()


class DailyPublicationRetryTests(unittest.TestCase):
    @staticmethod
    def instant(value):
        return datetime.fromisoformat(value).timestamp()

    @staticmethod
    def report(family="SC", completed="2026-10-29", needs_retry=True):
        return {"status": "partial" if needs_retry else "success", "continuous": {"series": [
            {"family": family, "latest_completed_date": completed, "needs_daily_retry": needs_retry}]}}

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.now = [self.instant("2026-10-30T07:34:00+00:00")]
        self.calls = []
        self.result = self.report()
        self.runners = {"news": lambda: self.calls.append("news") or {"status": "success"},
                        "contracts": self.contracts}
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])

    def contracts(self):
        self.calls.append("contracts")
        return self.result

    def tearDown(self):
        self.scheduler.stop()
        self.temp.cleanup()

    def finish(self):
        self.scheduler._workers["contracts"].join(timeout=2)
        self.assertFalse(self.scheduler.snapshot()["jobs"]["contracts"]["running"])
        return self.scheduler.snapshot()["jobs"]["contracts"]

    def first_missing_sc(self):
        self.now[0] = self.instant("2026-10-30T07:35:00+00:00")
        self.scheduler.run_due()
        return self.finish()

    def test_missing_sc_bar_retries_then_ready_returns_to_regular_plan(self):
        news_before = self.scheduler.snapshot()["jobs"]["news"]
        job = self.first_missing_sc()
        self.assertEqual(job["next_run_at"], "2026-10-30T07:40:00+00:00")
        self.assertEqual(job["publication_retry"]["attempts"], 1)
        self.assertEqual(job["publication_retry"]["regular_next_run_at"], "2026-10-30T21:35:00+00:00")
        self.scheduler.run_due()
        self.assertEqual(self.calls, ["contracts"])
        self.result = self.report(completed="2026-10-30", needs_retry=False)
        self.now[0] = self.instant("2026-10-30T07:40:00+00:00")
        self.scheduler.run_due()
        job = self.finish()
        self.assertEqual(job["status"], "success")
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T21:35:00+00:00")
        self.assertEqual(self.scheduler.snapshot()["jobs"]["news"], news_before)
        self.assertEqual(self.scheduler.snapshot()["contract_interval_minutes"], 1440)
        self.assertEqual(self.scheduler.snapshot()["news_interval_minutes"], 30)

    def test_restart_keeps_retry_count_and_original_nonrolling_window(self):
        job = self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T07:36:00+00:00")
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])
        self.assertEqual(self.scheduler.snapshot()["jobs"]["contracts"], job)
        self.now[0] = self.instant("2026-10-30T07:40:00+00:00")
        self.scheduler.run_due()
        job = self.finish()
        self.assertEqual(job["publication_retry"]["attempts"], 2)
        self.assertEqual(job["publication_retry"]["window_started_at"], "2026-10-30T07:35:00+00:00")
        self.assertEqual(job["publication_retry"]["expires_at"], "2026-10-30T08:35:00+00:00")

    def test_restart_expires_retry_without_running_outside_window(self):
        self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T08:40:00+00:00")
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])
        job = self.scheduler.snapshot()["jobs"]["contracts"]
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T21:35:00+00:00")
        self.scheduler.run_due()
        self.assertEqual(self.calls.count("contracts"), 1)

    def test_interrupted_retry_restarts_once_inside_original_window(self):
        self.first_missing_sc()
        saved = json.loads(self.path.read_text())
        job = saved["jobs"]["contracts"]
        job.update(running=True, status="running", next_run_at="2026-10-30T21:35:00+00:00")
        self.path.write_text(json.dumps(saved))
        self.now[0] = self.instant("2026-10-30T07:42:00+00:00")
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])
        job = self.scheduler.snapshot()["jobs"]["contracts"]
        self.assertEqual(job["status"], "interrupted")
        self.assertEqual(job["next_run_at"], "2026-10-30T07:40:00+00:00")
        self.scheduler.run_due()
        job = self.finish()
        self.assertEqual(job["next_run_at"], "2026-10-30T07:47:00+00:00")
        self.assertEqual(job["publication_retry"]["expires_at"], "2026-10-30T08:35:00+00:00")
        self.scheduler.run_due()
        self.assertEqual(self.calls.count("contracts"), 2)

    def test_graceful_shutdown_keeps_completed_retry_plan_for_restart(self):
        self.first_missing_sc()
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(timeout=2)
            return self.report()
        self.scheduler.runners["contracts"] = blocked
        self.now[0] = self.instant("2026-10-30T07:40:00+00:00")
        self.assertTrue(self.scheduler.trigger("contracts"))
        self.assertTrue(entered.wait(timeout=1))
        self.scheduler._stop.set()
        self.assertFalse(self.scheduler.trigger("contracts"))
        self.now[0] = self.instant("2026-10-30T07:41:00+00:00")
        release.set()
        job = self.finish()
        self.assertEqual(job["publication_retry"]["attempts"], 2)
        self.assertEqual(job["publication_retry"]["expires_at"], "2026-10-30T08:35:00+00:00")
        self.assertEqual(job["next_run_at"], "2026-10-30T07:46:00+00:00")
        self.now[0] = self.instant("2026-10-30T07:42:00+00:00")
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])
        self.assertEqual(self.scheduler.snapshot()["jobs"]["contracts"], job)

    def test_expired_retry_does_not_discard_overdue_regular_catchup(self):
        self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T22:05:00+00:00")
        self.scheduler = UpdateScheduler(self.path, self.runners, clock=lambda: self.now[0])
        self.assertEqual(self.scheduler.snapshot()["jobs"]["contracts"]["next_run_at"], "2026-10-30T21:35:00+00:00")
        self.scheduler.run_due()
        job = self.finish()
        self.assertEqual(job["next_run_at"], "2026-10-30T23:35:00+00:00")
        self.scheduler.run_due()
        self.assertEqual(self.calls.count("contracts"), 2)

    def test_same_process_clock_jump_expires_retry_before_selecting_due(self):
        self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T12:00:00+00:00")
        self.scheduler.run_due()
        self.scheduler._workers["news"].join(timeout=2)
        job = self.scheduler.snapshot()["jobs"]["contracts"]
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T21:35:00+00:00")
        self.assertEqual(self.calls.count("contracts"), 1)
        self.assertEqual(json.loads(self.path.read_text())["jobs"]["contracts"], job)
        with patch.object(self.scheduler, "_persist", wraps=self.scheduler._persist) as persist:
            self.scheduler.run_due()
            persist.assert_not_called()

    def test_same_process_expiration_preserves_one_overdue_regular_catchup(self):
        self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T22:05:00+00:00")
        self.scheduler.run_due()
        job = self.finish()
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T23:35:00+00:00")
        self.scheduler.run_due()
        self.assertEqual(self.calls.count("contracts"), 2)

    def test_expiration_save_failure_rolls_back_and_next_pass_can_recover(self):
        before = self.first_missing_sc()
        self.now[0] = self.instant("2026-10-30T12:00:00+00:00")
        with patch.object(self.scheduler, "_persist", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.scheduler.run_due()
        self.assertEqual(self.scheduler.snapshot()["jobs"]["contracts"], before)
        self.scheduler.run_due()
        self.assertIsNone(self.scheduler.snapshot()["jobs"]["contracts"]["publication_retry"])
        self.assertEqual(self.calls.count("contracts"), 1)

    def test_custom_interval_cancels_retry_and_missing_bar_does_not_reenable_it(self):
        self.first_missing_sc()
        self.scheduler.configure({"contract_interval_minutes": 60})
        self.assertIsNone(self.scheduler.snapshot()["jobs"]["contracts"]["publication_retry"])
        self.assertTrue(self.scheduler.trigger("contracts"))
        job = self.finish()
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T08:35:00+00:00")

    def test_current_2204_manual_refresh_keeps_next_daily_slot(self):
        self.now[0] = self.instant("2026-10-30T14:04:00+00:00")
        self.result = {"status": "partial", "continuous": {"series": [
            self.report(family)["continuous"]["series"][0] for family in ("WTI", "Brent", "SC")]}}
        self.assertTrue(self.scheduler.trigger("contracts"))
        job = self.finish()
        self.assertIsNone(job["publication_retry"])
        self.assertEqual(job["next_run_at"], "2026-10-30T21:35:00+00:00")

    def test_noncurrent_market_old_gap_and_completed_today_do_not_loop(self):
        at = self.instant("2026-10-30T07:35:00+00:00")
        self.assertIsNone(contract_publication_retry(self.report("WTI", "2026-10-16"), at))
        self.assertIsNone(contract_publication_retry(self.report(completed="2026-10-30"), at))
        self.assertIsNone(contract_publication_retry(self.report(needs_retry=False), at))
        self.assertIsNone(contract_publication_retry({"continuous": {"series": None}}, at))

    def test_markets_published_after_the_thirty_minute_cutoff_do_not_retry(self):
        for family, local in [
            ("WTI", "2026-10-29T17:35:00-04:00"),
            ("Brent", "2026-10-29T23:35:00+00:00"),
        ]:
            with self.subTest(family=family, local=local):
                self.assertIsNone(contract_publication_retry(
                    self.report(family, "2026-10-01"), self.instant(local)))

    def test_publication_window_and_local_midnight_bound_remaining_retries(self):
        for family, local, expires in [
            ("Brent", "2026-10-22T23:35:00+01:00", "2026-10-22T23:00:00+00:00"),
            ("WTI", "2026-10-30T17:35:00-04:00", "2026-10-30T22:35:00+00:00"),
            ("Brent", "2026-11-02T23:35:00+00:00", "2026-11-03T00:00:00+00:00"),
        ]:
            with self.subTest(family=family, local=local):
                report = self.report(family, "2026-10-01")
                first = contract_publication_retry(report, self.instant(local))
                self.assertEqual(first["expires_at"], expires)
                final = contract_publication_retry(report, self.instant(expires) - 4*60, first)
                self.assertIsNone(final["next_run_at"])
                self.assertEqual(final["status"], "exhausted")

    def test_attempt_cap_survives_restart_and_does_not_roll_window(self):
        at = self.instant("2026-10-30T07:35:00+00:00")
        previous = {"family": "SC", "trading_date": "2026-10-30", "attempts": 12}
        retry = contract_publication_retry(self.report(), at, json.loads(json.dumps(previous)))
        self.assertEqual(retry["attempts"], 13)
        self.assertIsNone(retry["next_run_at"])
        self.assertEqual(retry["expires_at"], "2026-10-30T08:35:00+00:00")


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "settings.json"
        self.now = 1793200000.0
        self.calls = []
        self.scheduler = UpdateScheduler(self.path, {
            "news": lambda: self.calls.append("news") or {"status": "success", "written": 2},
            "contracts": lambda: self.calls.append("contracts") or {"status": "success", "updated": 18},
        }, clock=lambda: self.now)

    def tearDown(self):
        self.scheduler.stop()
        self.temp.cleanup()

    def wait_finished(self, name="news"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = self.scheduler.snapshot()["jobs"][name]
            if not state["running"]:
                return state
            time.sleep(0.005)
        self.fail("offline update did not finish")

    def test_defaults_are_persisted_and_not_due_on_first_start(self):
        state = self.scheduler.snapshot()
        self.assertEqual((state["news_interval_minutes"], state["contract_interval_minutes"]), (30, 1440))
        self.scheduler.run_due()
        self.assertEqual(self.calls, [])
        self.assertEqual(json.loads(self.path.read_text())["news_interval_minutes"], 30)

    def test_interval_changes_persist_and_rebase_only_changed_job(self):
        before = self.scheduler.snapshot()
        self.now += 20
        after = self.scheduler.configure({"news_interval_minutes": 15, "contract_interval_minutes": 1440})
        self.assertNotEqual(before["jobs"]["news"]["next_run_at"], after["jobs"]["news"]["next_run_at"])
        self.assertEqual(before["jobs"]["contracts"]["next_run_at"], after["jobs"]["contracts"]["next_run_at"])
        reopened = UpdateScheduler(self.path, self.scheduler.runners, clock=lambda: self.now)
        self.assertEqual(reopened.snapshot()["news_interval_minutes"], 15)
        self.assertEqual(reopened.snapshot()["jobs"], after["jobs"])

    def test_invalid_interval_cannot_change_saved_config(self):
        original = self.path.read_bytes()
        for value in [0, -1, 10081, 1.5, True, "30", None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.scheduler.configure({"news_interval_minutes": value})
        with self.assertRaises(ValueError):
            self.scheduler.configure({"unknown": 10})
        self.assertEqual(original, self.path.read_bytes())

    def test_due_job_runs_and_persists_result_without_triggering_other_job(self):
        self.now += 1801
        self.scheduler.run_due()
        result = self.wait_finished()
        self.assertEqual(self.calls, ["news"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["summary"]["written"], 2)
        self.assertIsNotNone(result["last_success_at"])
        self.scheduler.run_due()
        self.assertEqual(self.calls, ["news"])
        self.assertEqual(json.loads(self.path.read_text())["jobs"]["news"]["status"], "success")

    def test_overlap_and_long_job_backlog_are_prevented(self):
        entered, release = threading.Event(), threading.Event()
        def blocked():
            entered.set()
            release.wait(timeout=3)
            return {"status": "success"}
        self.scheduler.runners["news"] = blocked
        try:
            self.assertTrue(self.scheduler.trigger("news"))
            self.assertTrue(entered.wait(timeout=1))
            self.assertFalse(self.scheduler.trigger("news"))
            self.scheduler.configure({"news_interval_minutes": 1})
            self.now += 600
            self.scheduler.run_due()
            self.assertFalse(self.scheduler.trigger("news"))
        finally:
            release.set()
        result = self.wait_finished()
        self.assertEqual(result["status"], "success")
        self.scheduler.run_due()
        self.assertEqual(self.scheduler.snapshot()["jobs"]["news"]["status"], "success")

    def test_failed_run_can_recover_and_partial_is_not_success(self):
        def broken():
            raise RuntimeError("provider unavailable")
        self.scheduler.runners["news"] = broken
        self.scheduler.trigger("news")
        failed = self.wait_finished()
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "provider unavailable")
        self.assertIsNone(failed["last_success_at"])
        self.scheduler.runners["news"] = lambda: {"status": "partial", "error": "one source failed"}
        self.scheduler.trigger("news")
        partial = self.wait_finished()
        self.assertEqual(partial["status"], "partial")
        self.assertIsNone(partial["last_success_at"])
        self.scheduler.runners["news"] = lambda: {"status": "success"}
        self.scheduler.trigger("news")
        self.assertEqual(self.wait_finished()["status"], "success")

    def test_restart_marks_interrupted_job_and_catches_up_once(self):
        saved = json.loads(self.path.read_text())
        saved["jobs"]["news"].update(running=True, status="running")
        self.path.write_text(json.dumps(saved))
        self.now += 1801
        reopened = UpdateScheduler(self.path, self.scheduler.runners, clock=lambda: self.now)
        self.scheduler = reopened
        self.assertEqual(reopened.snapshot()["jobs"]["news"]["status"], "interrupted")
        reopened.run_due()
        self.assertEqual(self.wait_finished()["status"], "success")
        reopened.run_due()
        self.assertEqual(self.calls, ["news"])

    def test_write_failure_rolls_back_in_memory_settings(self):
        before = self.scheduler.snapshot()
        with patch("newsdesk.scheduler.atomic_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.scheduler.configure({"news_interval_minutes": 1})
        self.assertEqual(self.scheduler.snapshot(), before)

    def test_bad_settings_file_is_reported_without_overwriting_it(self):
        self.path.write_text("{not json")
        with self.assertRaisesRegex(ValueError, "设置文件"):
            UpdateScheduler(self.path, {}, clock=lambda: self.now)
        self.assertEqual(self.path.read_text(), "{not json")


class NewsWorkerTests(unittest.TestCase):
    def test_only_unverified_times_are_filtered_as_zero_new_events(self):
        result = self.run_worker_fixture('ok', {'status':'complete', 'candidate_count':0,
            'created':0, 'updated':0, 'written':0, 'invalid_count':0, 'time_skipped_count':12})
        self.assertEqual((result['status'], result['created'], result['error']), ('success',0,''))
        self.assertEqual(result['time_skipped_count'],12)

    def run_worker_fixture(self, source_status, pipeline):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source_registry.json").write_text(json.dumps({"sources": [{"id": "a", "enabled": True}]}))
            (root / 'run').mkdir()
            (root / 'run/candidates.jsonl').write_text('')
            with patch("newsdesk.scheduler.ROOT", root), patch("newsdesk.ingest.collect") as collect, patch("newsdesk.ingest.process") as process:
                collect.return_value = {"sources": [{"source_id": "a", "status": source_status}]}
                process.return_value = dict(pipeline)
                result = news_worker(root / "news.sqlite3", root / "ingestion", root / "run")
                self.assertEqual(json.loads((root / "run/scheduled-summary.json").read_text()), result)
                return result

    def test_no_new_relevant_news_is_successful_zero_event_update(self):
        for candidates, relevant in [(0, 0), (5, 0), (5, 5)]:
            with self.subTest(candidates=candidates, relevant=relevant):
                result = self.run_worker_fixture("ok", {"status": "complete", "candidate_count": candidates,
                    "relevant_count": relevant, "created": 0, "updated": 0, "reactivated": 0,
                    "written": 0, "unchanged": relevant, "invalid_count": 0})
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["created"], 0)
                self.assertEqual(result["error"], "")

    def test_partial_source_keeps_actual_new_and_existing_counts(self):
        result = self.run_worker_fixture("partial", {"status": "complete", "candidate_count": 10,
            "created": 3, "updated": 2, "reactivated": 1, "written": 6, "invalid_count": 1})
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["created"], 3)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["reactivated"], 1)
        self.assertEqual(result["sources_partial"], 1)

    def test_invalid_content_is_skipped_but_all_invalid_is_not_empty_success(self):
        for candidate_count, expected in [(5, "success"), (0, "partial")]:
            with self.subTest(candidate_count=candidate_count):
                result = self.run_worker_fixture("ok", {"status": "complete", "candidate_count": candidate_count,
                    "created": 0, "updated": 0, "reactivated": 0, "written": 0, "invalid_count": 2})
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["invalid_count"], 2)

    def test_all_sources_failed_is_not_reported_as_no_new_news(self):
        result = self.run_worker_fixture("failed", {"status": "complete", "candidate_count": 0,
            "created": 0, "updated": 0, "reactivated": 0, "written": 0, "invalid_count": 0})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["sources_failed"], 1)

    def test_health_includes_missing_sources_and_partial_supplement(self):
        health = source_health({"sources": [
            {"source_id": "a", "status": "ok"}, {"source_id": "b", "status": "error"}],
            "supplement": [{"source_id": "a", "status": "partial"}]}, ["a", "b", "c"])
        self.assertEqual(health["sources_total"], 3)
        self.assertEqual(health["failed_sources"], ["b", "c"])
        self.assertEqual(health["partial_sources"], ["a"])

    def test_worker_collects_all_and_only_enabled_sources_then_uses_existing_agent_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "source_registry.json").write_text(json.dumps({"sources": [
                {"id": "a", "enabled": True}, {"id": "b", "enabled": False}, {"id": "c", "enabled": True}]}))
            run_dir = root / "run"
            run_dir.mkdir()
            (run_dir / 'candidates.jsonl').write_text('')
            with patch("newsdesk.scheduler.ROOT", root), patch("newsdesk.ingest.collect") as collect, patch("newsdesk.ingest.process") as process:
                collect.return_value = {"sources": [{"source_id": "a", "status": "ok"}, {"source_id": "c", "status": "empty"}]}
                process.return_value = {"status": "complete", "written": 2, "reviewed_count": 6}
                result = news_worker(root / "news.sqlite3", root / "ingestion", run_dir)
                collect.assert_called_once_with(run_dir, source_ids=["a", "c"])
                process.assert_called_once_with(run_dir / "candidates.jsonl", output=root / "ingestion", database=root / "news.sqlite3")
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["sources_total"], 2)
                self.assertEqual(json.loads((run_dir / "scheduled-summary.json").read_text())["written"], 2)


if __name__ == "__main__":
    unittest.main()
