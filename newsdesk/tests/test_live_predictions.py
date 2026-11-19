"""Prediction dialogs bind each model output and truth to its exact live version."""

import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from newsdesk.benchmark import BenchmarkStore, LIVE_SUBMISSION_POLICY
from newsdesk.evaluation import PUSH_NEWS_PROTOCOL, digest, read_json, write_json
from newsdesk.live_prediction_batch import LIVE_PREDICTION_MODELS
from newsdesk.live_predictions import live_question_predictions
from newsdesk.tests.test_live_history import freeze, question, settle


def save_run(root, manifest, case, run_id, *, stamp="2026-11-02T08:10:00+00:00",
             model="gpt-6.1-sol", experiment="Exp01", status="predicted_pending", new_policy=False):
    path = root / "runs" / run_id
    frozen = {"manifest": manifest, "questions": [copy.deepcopy(case)], "labels": {}}
    run = {"run_id": run_id, "dataset_id": manifest["dataset_id"], "dataset_snapshot_sha256": digest(frozen),
           "mode": "live", "protocol": PUSH_NEWS_PROTOCOL, "model": model,
           "experiment_id": experiment, "experiment_name": experiment, "started_at": stamp, "status": status}
    if new_policy:
        run["submission_deadline_policy"] = LIVE_SUBMISSION_POLICY
    write_json(path / "run.json", run)
    write_json(path / "dataset.json", frozen)
    if status in ("queued", "running"):
        return path
    record = dict(case, status=status, attempt="attempt-fixture", finished_at=stamp)
    if status == "failed":
        record["error"] = "fixture failure"
    else:
        predicted = {"direction": "up", "return_pct": 1, "high": case["current_price"] + 3, "low": case["current_price"] - 2}
        raw = dict(predicted, reason="fixture")
        material = {"question": case, "network_search": False}
        lock = {"case_id": case["case_id"], "submitted_at": stamp, "prediction_locked_at": stamp,
                "predicted": predicted, "input_sha256": digest(material), "raw_output_sha256": digest(raw)}
        if new_policy:
            lock["submission_deadline_policy"] = LIVE_SUBMISSION_POLICY
        attempt = path / "cases" / case["case_id"] / record["attempt"]
        write_json(attempt / "input.json", material)
        write_json(attempt / "prediction-lock.json", lock)
        record.update(predicted=predicted, raw_output=raw, prediction_lock_sha256=digest(lock), input_sha256=digest(material))
    write_json(path / "cases" / case["case_id"] / "result.json", record)
    return path


class LivePredictionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = BenchmarkStore(root / "benchmark")
        self.evaluations = SimpleNamespace(root=root / "evaluations")
        self.case = question("canonical")
        self.manifest = freeze(self.store, "2026-11-02T08:00:00+00:00", [self.case])

    def tearDown(self):
        self.temp.cleanup()

    def query(self, manifest=None, case=None):
        return live_question_predictions(self.store, self.evaluations,
                                         (manifest or self.manifest)["dataset_id"], (case or self.case)["case_id"])

    def test_missing_predictions_show_exp01_models_and_query_is_read_only(self):
        before = {str(path): path.read_bytes() for path in self.store.root.rglob("*") if path.is_file()}
        with patch.object(self.store, "build", side_effect=AssertionError("must not build")), \
             patch.object(self.store, "settle_live", side_effect=AssertionError("must not settle")):
            result = self.query()
        self.assertEqual([row["model"] for row in result["items"]], list(LIVE_PREDICTION_MODELS))
        self.assertTrue(all(row["status"] == "pending" and row["experiment_id"] == "Exp01" for row in result["items"]))
        self.assertEqual(result["summary"]["waiting"], 15)
        after = {str(path): path.read_bytes() for path in self.store.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        with self.assertRaises(ValueError):
            live_question_predictions(self.store, self.evaluations, "../../private", self.case["case_id"])
        with self.assertRaises(ValueError):
            live_question_predictions(self.store, self.evaluations, self.manifest["dataset_id"], "../private")
        with self.assertRaises(LookupError):
            live_question_predictions(self.store, self.evaluations, self.manifest["dataset_id"], "other-case")

    def test_latest_locked_version_uses_its_own_baseline_truth_and_keeps_experiments_separate(self):
        revised = question("revised", price=80)
        revised_manifest = freeze(self.store, "2026-11-02T09:00:00+00:00", [revised], marker="revision")
        settle(self.store, self.manifest, self.case["case_id"], return_pct=2)
        own_truth = settle(self.store, revised_manifest, revised["case_id"], return_pct=8)
        save_run(self.evaluations.root, self.manifest, self.case, "first")
        save_run(self.evaluations.root, revised_manifest, revised, "second", stamp="2026-11-02T09:10:00+00:00")
        save_run(self.evaluations.root, self.manifest, self.case, "control", experiment="Exp00")
        result = self.query()
        selected = next(row for row in result["items"] if row["model"] == "gpt-6.1-sol" and row["experiment_id"] == "Exp01")
        self.assertEqual(selected["run_id"], "second")
        self.assertEqual(selected["actual"], own_truth)
        self.assertEqual(selected["current_price"], 80)
        self.assertEqual(selected["absolute_errors"]["return_pct"], 7)
        self.assertTrue(selected["time_audit"]["integrity_verified"])
        self.assertEqual(result["question"]["actual"]["return_pct"], 2)
        self.assertEqual(result["total"], 16)
        self.assertEqual(self.query(revised_manifest, revised)["items"], result["items"])

    def test_waiting_roster_comes_from_the_exp01_reference_models(self):
        write_json(self.evaluations.root / "batches" / "Exp01.json", {"models": {"hy3": "old-run", "gpt-6.1-sol": "reference-run"}})
        result = self.query()
        self.assertEqual(result["summary"]["expected_models"], 2)
        self.assertEqual({row["model"] for row in result["items"]}, {"hy3", "gpt-6.1-sol"})

    def test_failed_newer_attempt_does_not_hide_good_locked_prediction(self):
        save_run(self.evaluations.root, self.manifest, self.case, "good")
        save_run(self.evaluations.root, self.manifest, self.case, "later-failed", stamp="2026-11-02T10:00:00+00:00", status="failed")
        row = next(row for row in self.query()["items"] if row["run_id"])
        self.assertEqual(row["run_id"], "good")
        self.assertEqual(row["status"], "predicted_pending")
        self.assertEqual(row["latest_attempt"]["run_id"], "later-failed")
        self.assertEqual(row["latest_attempt"]["error"], "fixture failure")

    def test_tampered_dataset_or_lock_and_attempt_traversal_never_publish_predictions(self):
        for kind in ("dataset", "rebound-baseline", "rebound-deadline", "lock", "attempt"):
            with self.subTest(kind=kind):
                path = save_run(self.evaluations.root, self.manifest, self.case, kind)
                if kind in ("dataset", "rebound-baseline", "rebound-deadline"):
                    value = read_json(path / "dataset.json")
                    if kind == "rebound-deadline":
                        value["questions"][0]["forecast_deadline"] = "2026-11-02T23:00:00Z"
                    else:
                        value["questions"][0]["current_price"] = 1000
                    write_json(path / "dataset.json", value)
                    if kind in ("rebound-baseline", "rebound-deadline"):
                        run = read_json(path / "run.json")
                        run["dataset_snapshot_sha256"] = digest(value)
                        write_json(path / "run.json", run)
                elif kind == "lock":
                    lock_file = path / "cases" / self.case["case_id"] / "attempt-fixture" / "prediction-lock.json"
                    lock = read_json(lock_file)
                    lock["predicted"]["return_pct"] = 99
                    write_json(lock_file, lock)
                else:
                    result_file = path / "cases" / self.case["case_id"] / "result.json"
                    record = read_json(result_file)
                    record["attempt"] = "../../other"
                    write_json(result_file, record)
        result = self.query()
        row = next(row for row in result["items"] if row["run_id"])
        self.assertEqual(row["status"], "failed")
        self.assertIsNone(row["predicted"])
        self.assertFalse(row["time_audit"]["integrity_verified"])
        self.assertEqual({error["run_id"] for error in result["errors"]}, {"dataset", "rebound-baseline", "rebound-deadline", "lock", "attempt"})

    def test_run_for_another_case_in_same_dataset_is_not_attached_to_requested_question(self):
        other = question("wti", family="WTI")
        manifest = freeze(self.store, "2026-11-02T09:00:00+00:00", [self.case, other], marker="two-families")
        save_run(self.evaluations.root, manifest, other, "other-family")
        result = self.query()
        self.assertTrue(all(row["run_id"] is None for row in result["items"]))
        self.assertEqual(result["errors"], [])

    def test_queued_rows_have_no_prediction_and_legacy_locks_keep_original_deadline(self):
        save_run(self.evaluations.root, self.manifest, self.case, "queued", model="hy3", status="queued")
        legacy = question("legacy", deadline="2026-11-02T08:15:00Z")
        legacy["time_windows"]["forecast"] = {"start_utc": "2026-11-02T08:15:00Z"}
        manifest = freeze(self.store, "2026-11-02T08:00:00+00:00", [legacy], marker="legacy")
        save_run(self.evaluations.root, manifest, legacy, "accepted-before-policy", stamp="2026-11-02T08:10:00+00:00")
        save_run(self.evaluations.root, manifest, legacy, "new-policy-late", model="hy4-preview", stamp="2026-11-02T08:10:00+00:00", new_policy=True)
        result = self.query()
        accepted = next(row for row in result["items"] if row["model"] == "gpt-6.1-sol")
        queued = next(row for row in result["items"] if row["model"] == "hy3")
        late = next(row for row in result["items"] if row["model"] == "hy4-preview")
        self.assertEqual(accepted["status"], "predicted_pending")
        self.assertTrue(accepted["time_audit"]["legacy_protocol"])
        self.assertEqual(queued["status"], "queued")
        self.assertIsNone(queued["predicted"])
        self.assertEqual(late["status"], "failed")
        self.assertTrue(any(error["run_id"] == "new-policy-late" for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
