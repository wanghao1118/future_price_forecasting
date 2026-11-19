"""Publication-driven dispatch without duplicate models or late submissions."""

import copy
import fcntl
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import ANY, Mock, patch

from newsdesk.evaluation import EVALUATION_PROTOCOL, digest, read_json, write_json
from newsdesk.live_prediction_auto import (AutomaticLivePredictions, STATE_NAME,
    dispatch_once, prediction_plan, runnable_jobs)


def question(family="SC", suffix="one"):
    return {"family": family, "contract_id": family.lower() + "-main",
            "input_date": "2026-11-06", "target_date": "2026-11-09",
            "case_id": family + "-" + suffix, "live_status": "ready",
            "forecast_deadline": "2026-11-06T12:30:00+00:00"}


class Publications:
    def __init__(self, *questions):
        self.datasets = {}
        self.published = []
        for index, item in enumerate(questions):
            self.add("qa-" + str(index), item)

    def add(self, identifier, item, actual=None):
        self.datasets[identifier] = {"questions": [copy.deepcopy(item)], "labels": {}}
        self.published.append({**item, "dataset_id": identifier, "actual": actual})

    def load_dataset(self, identifier=None, mode=None):
        return copy.deepcopy(self.datasets[identifier])

    def live_history(self):
        return {"questions": copy.deepcopy(self.published), "summary": {"errors": {}}}


class AutomaticPredictionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "evaluations"
        validator = patch("newsdesk.live_prediction_auto._validate_live_open")
        self.validate = validator.start()
        self.addCleanup(validator.stop)
        self.store = Publications(question())

    def batch(self, questions, phase="prepared", name="existing"):
        path = self.root / "live-batches" / ("live-Exp01-" + name + ".json")
        frozen = {"questions": questions, "labels": {}}
        write_json(self.root / "runs" / name / "dataset.json", frozen)
        write_json(path, {"mode": "live", "experiment_id": "Exp01", "environment_id": "env01",
                         "protocol": EVALUATION_PROTOCOL, "questions": questions, "models": {"model": name},
                         "snapshot_run_id": name, "dataset_snapshot_sha256": digest(frozen), "phase": phase,
                         "dataset_id": "qa-original"})
        return path

    def test_ready_set_changes_do_not_redispatch_a_question_in_combined_batch(self):
        self.batch([question(), question("WTI")], "partial")
        self.assertEqual(prediction_plan(self.root, self.store)[0], [])
        self.store.add("qa-brent", question("Brent"))
        jobs, errors = prediction_plan(self.root, self.store)
        self.assertFalse(errors)
        self.assertEqual([job["question_key"][0] for job in jobs], ["Brent"])

    def test_changed_dataset_and_case_id_resume_the_original_frozen_batch(self):
        path = self.batch([question()])
        store = Publications(question(suffix="new-version"))
        jobs, _ = prediction_plan(self.root, store)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["batch_path"], str(path))

    def test_combined_interrupted_batch_is_scheduled_once(self):
        self.batch([question(), question("WTI")], "running")
        jobs, _ = prediction_plan(self.root, Publications(question(), question("WTI")))
        self.assertEqual(len(jobs), 1)

    def test_duplicate_claims_are_reported_without_starting_another_batch(self):
        self.batch([question()], name="first")
        self.batch([question()], name="second")
        jobs, errors = prediction_plan(self.root, self.store)
        self.assertFalse(jobs)
        self.assertEqual(errors[0]["type"], "DuplicateLiveBatch")

    def test_settled_and_closed_cases_are_never_dispatched(self):
        self.store.published[0]["actual"] = {"direction": "up"}
        self.assertFalse(prediction_plan(self.root, self.store)[0])
        self.store.published[0]["actual"] = None
        self.validate.side_effect = ValueError("deadline")
        self.assertFalse(prediction_plan(self.root, self.store)[0])

    def test_corrupt_existing_freeze_is_quarantined(self):
        path = self.batch([question()])
        batch = read_json(path)
        batch["dataset_snapshot_sha256"] = "changed"
        write_json(path, batch)
        jobs, errors = prediction_plan(self.root, self.store)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["batch_path"])
        self.assertEqual(errors[0]["type"], "CorruptLiveBatch")
        self.assertFalse(path.exists())

    def test_missing_frozen_run_is_quarantined_and_open_question_can_be_rebuilt(self):
        path = self.batch([question()], "partial")
        batch = read_json(path)
        batch["snapshot_run_id"] = "missing-frozen-run"
        write_json(path, batch)
        jobs, errors = prediction_plan(self.root, self.store)
        self.assertEqual(len(jobs), 1)
        self.assertIsNone(jobs[0]["batch_path"])
        self.assertEqual(errors[0]["type"], "CorruptLiveBatch")
        self.assertFalse(path.exists())
        quarantined = list((self.root / "live-batches" / "quarantine").glob(path.name + ".corrupt-*"))
        self.assertEqual(len(quarantined), 1)

    def test_each_new_question_prepares_all_models_as_an_independent_batch(self):
        self.store.add("qa-brent", question("Brent"))
        prepared = []

        def prepare(root, **kwargs):
            prepared.append(kwargs)
            path = self.batch([question("Brent") if kwargs["case_ids"] == ["Brent-one"] else question()],
                              name=kwargs["case_ids"][0])
            return path

        def execute(root, path, **kwargs):
            batch = read_json(path)
            batch["phase"] = "awaiting_settlement"
            write_json(path, batch)
            return batch

        with patch("newsdesk.live_prediction_auto.prepare_live_batch", side_effect=prepare), \
                patch("newsdesk.live_prediction_auto.execute_live_batch", side_effect=execute) as run:
            result = dispatch_once(self.root, self.store, Mock())
            self.assertEqual(len(prepared), 2)
            self.assertTrue(all(len(item["case_ids"]) == 1 for item in prepared))
            self.assertEqual(run.call_count, 2)
            dispatch_once(self.root, self.store, Mock())
            self.assertEqual(run.call_count, 2)
        self.assertEqual(result["status"], "idle")

    def test_startup_adopts_interrupted_batch_without_preparing_new_inputs(self):
        path = self.batch([question()], "running")
        with patch("newsdesk.live_prediction_auto.prepare_live_batch") as prepare, \
                patch("newsdesk.live_prediction_auto.execute_live_batch", return_value={"phase": "partial"}) as run:
            state = dispatch_once(self.root, self.store)
        prepare.assert_not_called()
        run.assert_called_once_with(self.root.resolve(), path.resolve(), api_workers=4, retries=1, request_slots=ANY)
        self.assertEqual(state["status"], "partial")

    def test_interrupted_combined_preparation_retains_its_entire_original_selection(self):
        members = [question(), question("WTI")]
        path = self.batch(members, "preparing")
        self.store.datasets["qa-original"] = {"questions": members, "labels": {}}
        with patch("newsdesk.live_prediction_auto.prepare_live_batch", return_value=path) as prepare, \
                patch("newsdesk.live_prediction_auto.execute_live_batch", return_value={"phase": "partial"}):
            dispatch_once(self.root, self.store)
        self.assertEqual(prepare.call_args.kwargs["dataset_id"], "qa-original")
        self.assertEqual(prepare.call_args.kwargs["case_ids"], ["SC-one", "WTI-one"])

    def test_interrupted_combined_preparation_with_a_closed_member_never_creates_replacement(self):
        members = [question(), question("WTI")]
        self.batch(members, "preparing")
        self.store.datasets["qa-original"] = {"questions": members, "labels": {}}

        def validate(item):
            if item["family"] == "WTI":
                raise ValueError("closed")

        self.validate.side_effect = validate
        with patch("newsdesk.live_prediction_auto.prepare_live_batch") as prepare, \
                patch("newsdesk.live_prediction_auto.execute_live_batch") as run:
            state = dispatch_once(self.root, self.store)
        prepare.assert_not_called()
        run.assert_not_called()
        self.assertEqual(state["status"], "partial")

    def test_dispatcher_process_lock_prevents_overlap(self):
        folder = self.root / "live-batches"
        folder.mkdir(parents=True)
        with (folder / "automatic-dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("newsdesk.live_prediction_auto.prepare_live_batch") as prepare:
                self.assertEqual(dispatch_once(self.root, self.store)["status"], "already_running")
            prepare.assert_not_called()

    def test_new_question_starts_while_the_previous_question_is_still_predicting(self):
        from concurrent.futures import wait as real_wait
        first_started, second_started = threading.Event(), threading.Event()
        slots = []

        def prepare(root, **kwargs):
            family = "Brent" if kwargs["case_ids"] == ["Brent-one"] else "SC"
            return self.batch([question(family)], name=family)

        def execute(root, path, **kwargs):
            slots.append(kwargs["request_slots"])
            if path.stem.endswith("SC"):
                first_started.set()
                self.assertTrue(second_started.wait(3), "A slow model session must not block the next publication")
            else:
                self.assertTrue(first_started.is_set())
                second_started.set()
            return {"phase": "awaiting_settlement"}

        def discover(futures, **kwargs):
            if first_started.is_set() and len(self.store.published) == 1:
                self.store.add("qa-brent", question("Brent"))
            return real_wait(futures, timeout=0.01, return_when=kwargs["return_when"])

        with patch("newsdesk.live_prediction_auto.prepare_live_batch", side_effect=prepare), \
                patch("newsdesk.live_prediction_auto.execute_live_batch", side_effect=execute), \
                patch("newsdesk.live_prediction_auto.wait", side_effect=discover):
            state = dispatch_once(self.root, self.store)
        self.assertTrue(second_started.is_set())
        self.assertEqual(len(slots), 2)
        self.assertIs(slots[0], slots[1], "Concurrent markets must share the API request cap")
        self.assertEqual(state["status"], "idle")

    def test_infrastructure_failures_have_persistent_retry_limit_and_delay(self):
        with patch("newsdesk.live_prediction_auto.prepare_live_batch", side_effect=OSError("unavailable")) as prepare:
            for attempt in range(3):
                result = dispatch_once(self.root, self.store)
                record = next(iter(result["jobs"].values()))
                self.assertEqual(record["dispatch_attempts"], attempt + 1)
                self.assertFalse(runnable_jobs(prediction_plan(self.root, self.store)[0], result))
                record["retry_after"] = 0
                write_json(self.root / "live-batches" / STATE_NAME, result)
            dispatch_once(self.root, self.store)
            self.assertEqual(prepare.call_count, 3)

    def test_monitor_uses_detached_subprocess_and_leaves_inflight_work_on_restart(self):
        monitor = AutomaticLivePredictions(self.root, self.store, self.root.parent / "news.sqlite3")
        write_json(self.root / "batches" / "Exp01.json", {})
        process = Mock()
        process.poll.return_value = None
        with patch("newsdesk.live_prediction_auto.subprocess.Popen", return_value=process) as spawn:
            monitor.check()
            monitor.check()
            self.assertEqual(spawn.call_count, 1)
            self.assertTrue(spawn.call_args.kwargs["start_new_session"])
            monitor.stop()
        process.terminate.assert_not_called()
        self.assertTrue(monitor.snapshot()["worker_running"])

    def test_restarted_monitor_adopts_previous_dispatcher_without_spawning_another(self):
        folder = self.root / "live-batches"
        folder.mkdir(parents=True)
        monitor = AutomaticLivePredictions(self.root, self.store, self.root.parent / "news.sqlite3")
        with (folder / "automatic-dispatch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch("newsdesk.live_prediction_auto.subprocess.Popen") as spawn:
                monitor.check()
            spawn.assert_not_called()
            self.assertTrue(monitor.snapshot()["worker_running"])


if __name__ == "__main__":
    unittest.main()
