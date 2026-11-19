"""Live batch isolation, exact model profiles and deadline-safe resumability."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from newsdesk.evaluation import EVALUATION_PROTOCOL, read_json, write_json
from newsdesk.live_prediction_batch import execute_live_batch, prepare_live_batch, profiles_from_reference
from newsdesk.tests.test_evaluation import FakeBenchmark, FakeNews


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        point = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
        return point.astimezone(tz) if tz else point.replace(tzinfo=None)


class LivePredictionBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "evaluations"
        with patch("newsdesk.tests.test_evaluation.datetime", FixedClock):
            self.store = FakeBenchmark("live")
        self.store.questions = [q for q in self.store.questions if q["family"] == "SC"]
        self.news = FakeNews()
        self.profiles = {
            "deepseek-v4-pro": {"max_output_tokens": 32768, "max_rounds": 18,
                                "client_timeout_seconds": 600, "gateway_timeout_seconds": 540},
            "gpt-6-luna": {"api_protocol": "chat", "reasoning_effort": "none", "provider": "azure"},
        }
        models = {}
        for index, (model, profile) in enumerate(self.profiles.items()):
            run_id = "reference-" + str(index)
            write_json(self.root / "runs" / run_id / "run.json",
                       {"run_id": run_id, "mode": "backtest", "model": model,
                        "experiment_id": "Exp01", "generation_config": profile})
            models[model] = run_id
        self.reference = self.root / "batches" / "Exp01.json"
        write_json(self.reference, {"experiment_id": "Exp01", "environment_id": "env01",
                   "protocol": EVALUATION_PROTOCOL, "models": models,
                   "generation": {"max_output_tokens": 16000, "max_rounds": 18,
                                  "client_timeout_seconds": 300, "gateway_timeout_seconds": 240}})

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, **kwargs):
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"), \
                patch("newsdesk.evaluation.datetime", FixedClock):
            return prepare_live_batch(self.root, self.reference, self.store, self.news, **kwargs)

    def historical_catalog(self):
        with patch("newsdesk.tests.test_evaluation.datetime", FixedClock):
            source = FakeBenchmark("live").load_dataset(mode="live")
        source_id, current_id = "qa-" + "a" * 20, "qa-" + "b" * 20
        source["manifest"]["dataset_id"] = source_id
        current = copy.deepcopy(source)
        current["manifest"]["dataset_id"] = current_id
        current["questions"] = [dict(current["questions"][-1], case_id="current-sc")]
        class Catalog:
            def __init__(self):
                self.datasets, self.calls = {source_id: source, current_id: current}, []

            def load_dataset(self, dataset_id=None, mode="backtest"):
                self.calls.append((dataset_id, mode))
                return copy.deepcopy(self.datasets[dataset_id or current_id])
        self.store = Catalog()
        return source_id, source

    def test_uses_current_live_inputs_and_exact_reference_profiles_without_launching(self):
        before = self.reference.read_bytes()
        with patch.object(self.news, "all", wraps=self.news.all) as news_read, \
                patch("newsdesk.evaluation.EvaluationManager._launch") as launch:
            path = self.prepare()
        news_read.assert_called_once()
        launch.assert_not_called()
        batch = read_json(path)
        self.assertEqual(batch["mode"], "live")
        self.assertEqual(batch["expected_total"], 2)
        self.assertEqual(len(batch["models"]), 2)
        records = [read_json(self.root / "runs" / identifier / "run.json") for identifier in batch["models"].values()]
        for key in ("dataset_snapshot_sha256", "news_snapshot_sha256", "technical_snapshot_sha256"):
            self.assertEqual(len({row[key] for row in records}), 1)
        for row in records:
            self.assertEqual(row["experiment_id"], "Exp01")
            self.assertEqual(row["mode"], "live")
            self.assertEqual(row["total"], 1)
            self.assertEqual(row["generation_config"], batch["profiles"][row["model"]])
        self.assertEqual(records[0]["generation_config"]["max_output_tokens"], 32768)
        self.assertEqual(records[1]["generation_config"]["reasoning_effort"], "none")
        self.assertEqual(self.reference.read_bytes(), before)
        self.assertEqual(self.prepare(), path)
        self.assertEqual(read_json(path)["models"], batch["models"], "Repeated preparation reuses the same runs")

    def test_live_batches_exclude_claude_while_reference_exp01_keeps_it(self):
        claude = "claude-haiku-5-5"
        claude_run = self.root / "runs" / "reference-claude" / "run.json"
        write_json(claude_run, {"run_id": "reference-claude", "mode": "backtest", "model": claude,
                                "experiment_id": "Exp01", "generation_config": self.profiles["gpt-6-luna"]})
        reference = read_json(self.reference)
        reference["models"][claude] = "reference-claude"
        write_json(self.reference, reference)
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"):
            path = self.prepare()
        batch = read_json(path)
        self.assertNotIn(claude, batch["models"])
        self.assertNotIn(claude, batch["profiles"])
        self.assertEqual(set(batch["models"]), set(self.profiles))

    def test_missing_reference_run_uses_surviving_exp01_batch_generation(self):
        source = read_json(self.reference)
        source["models"]["deepseek-v4-pro"] = "missing-reference-run"
        write_json(self.reference, source)
        reference, profiles = profiles_from_reference(self.root, self.reference)
        self.assertEqual(profiles["deepseek-v4-pro"], reference["generation"])
        self.assertIn("deepseek-v4-pro", profiles)

    def test_only_failed_unlocked_cases_can_be_retried(self):
        path = self.prepare()
        batch = read_json(path)
        retained = {}
        for index, identifier in enumerate(batch["models"].values()):
            result_file = self.root / "runs" / identifier / "cases" / "s1" / "result.json"
            write_json(result_file, {"case_id": "s1", "family": "SC",
                                    "status": "predicted_pending" if index == 0 else "failed",
                                    "prediction_lock_sha256": "already-locked"})
            retained[result_file] = result_file.read_bytes()
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"), \
                patch("newsdesk.live_prediction_batch.EvaluationManager._case") as predict, \
                patch("newsdesk.live_prediction_batch.audit_live_run", return_value={"valid": True}):
            execute_live_batch(self.root, path, api_workers=1, retries=1)
        predict.assert_not_called()
        self.assertTrue(all(p.read_bytes() == old for p, old in retained.items()))

    def test_past_deadline_never_dispatches_a_model(self):
        path = self.prepare()
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"), \
                patch("newsdesk.live_prediction_batch._validate_live_open", side_effect=ValueError("deadline")), \
                patch("newsdesk.live_prediction_batch.EvaluationManager._case") as predict, \
                patch("newsdesk.live_prediction_batch.audit_live_run", return_value={"valid": False}):
            result = execute_live_batch(self.root, path, api_workers=1, retries=1)
        predict.assert_not_called()
        self.assertEqual(result["phase"], "partial")

    def test_mutated_snapshot_prevents_prediction(self):
        path = self.prepare()
        batch = read_json(path)
        identifier = next(iter(batch["models"].values()))
        write_json(self.root / "runs" / identifier / "news-snapshot.json", [{"unexpected": "changed news"}])
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"), \
                patch("newsdesk.live_prediction_batch.EvaluationManager._case") as predict:
            with self.assertRaisesRegex(ValueError, "冻结快照"):
                execute_live_batch(self.root, path)
        predict.assert_not_called()

    def test_reference_profile_change_does_not_replace_an_existing_live_batch(self):
        self.prepare()
        source = read_json(self.reference)
        source["generation"]["max_rounds"] = 19
        write_json(self.reference, source)
        with self.assertRaisesRegex(ValueError, "禁止静默替换"):
            self.prepare()

    def test_interrupted_preparation_reuses_frozen_inputs_and_existing_run_ids(self):
        path = self.prepare()
        original = read_json(path)
        pending = {**original, "phase": "preparing", "models": dict(list(original["models"].items())[:1])}
        write_json(path, pending)
        self.news.items.append({"changed_after_freeze": True})
        self.store.questions[0]["current_price"] += 100
        self.prepare()
        resumed = read_json(path)
        self.assertEqual(resumed["models"], original["models"])
        for key in ("dataset_snapshot_sha256", "news_snapshot_sha256", "technical_snapshot_sha256"):
            self.assertEqual(resumed[key], original[key])

    def test_explicit_historical_dataset_selects_only_requested_case_and_filters_labels(self):
        source_id, source = self.historical_catalog()
        source["labels"]["w1"] = {"direction": "up", "return_pct": 2, "high": 73, "low": 68}
        # The unrelated case must not even enter retrieval preparation.
        source["questions"][0]["time_windows"] = {"timezone": "UTC", "status": "unconfirmed"}
        before = copy.deepcopy(source)
        path = self.prepare(dataset_id=source_id, case_ids=["s1"])
        batch = read_json(path)
        self.assertEqual(self.store.calls, [(source_id, "live")])
        self.assertEqual(batch["dataset_id"], source_id)
        self.assertEqual(batch["total_per_model"], 1)
        self.assertEqual([question["case_id"] for question in batch["questions"]], ["s1"])
        for run_id in batch["models"].values():
            root = self.root / "runs" / run_id
            run, frozen = read_json(root / "run.json"), read_json(root / "dataset.json")
            self.assertEqual(run["dataset_id"], source_id)
            self.assertEqual((run["total"], run["requested_total"]), (1, 1))
            self.assertEqual([question["case_id"] for question in frozen["questions"]], ["s1"])
            self.assertEqual(frozen["labels"], {})
            self.assertEqual(frozen["manifest"], source["manifest"])
        self.assertEqual(source, before)

    def test_invalid_case_selections_fail_before_creating_queued_runs(self):
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor") as predictor, \
                patch("newsdesk.evaluation.datetime", FixedClock):
            for selection in ([], set(), "s1", [None], [""], ["s1", "s1"], {"case": "s1"}, ["missing"], ["s1", "missing"]):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    prepare_live_batch(self.root, self.reference, self.store, self.news, case_ids=selection)
        predictor.assert_not_called()
        self.assertEqual(sorted(path.name for path in (self.root / "runs").iterdir()), ["reference-0", "reference-1"])
        self.assertFalse((self.root / "live-batches").exists())

    def test_selected_settled_case_is_not_replaced_by_another_ready_question(self):
        source_id, source = self.historical_catalog()
        source["labels"]["s1"] = {"direction": "up", "return_pct": 2, "high": 512, "low": 497}
        with self.assertRaisesRegex(ValueError, "未结算"):
            self.prepare(dataset_id=source_id, case_ids={"s1"})
        self.assertFalse((self.root / "live-batches").exists())
        path = self.prepare(dataset_id=source_id, case_ids={"s1", "w1"})
        self.assertEqual([question["case_id"] for question in read_json(path)["questions"]], ["w1"])

    def test_selected_case_equal_to_deadline_cannot_fall_through_to_other_ready_family(self):
        source_id, _ = self.historical_catalog()
        class AtSCDeadline(datetime):
            @classmethod
            def now(cls, tz=None):
                point = datetime(2026, 11, 2, 12, 30, tzinfo=timezone.utc)
                return point.astimezone(tz) if tz else point.replace(tzinfo=None)
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor") as predictor, \
                patch("newsdesk.evaluation.datetime", AtSCDeadline), self.assertRaisesRegex(ValueError, "未截止"):
            prepare_live_batch(self.root, self.reference, self.store, self.news, dataset_id=source_id, case_ids=["s1"])
        predictor.assert_not_called()
        self.assertFalse((self.root / "live-batches").exists())

    def test_explicit_selection_batch_identity_stays_economic_across_source_versions(self):
        source_id, source = self.historical_catalog()
        path = self.prepare(dataset_id=source_id, case_ids=["s1"])
        original = read_json(path)
        revision = copy.deepcopy(source)
        revision_id = "qa-" + "c" * 20
        revision["manifest"]["dataset_id"] = revision_id
        revision["questions"][-1]["case_id"] = "revised-sc"
        revision["questions"][-1]["current_price"] += 1
        self.store.datasets[revision_id] = revision
        self.assertEqual(self.prepare(dataset_id=revision_id, case_ids=["revised-sc"]), path)
        self.assertEqual(read_json(path)["models"], original["models"])
        self.assertEqual(read_json(path)["dataset_id"], source_id)

    def test_interrupted_historical_selection_keeps_filtered_frozen_snapshot(self):
        source_id, source = self.historical_catalog()
        path = self.prepare(dataset_id=source_id, case_ids=["s1"])
        original = read_json(path)
        write_json(path, {**original, "phase": "preparing", "models": dict(list(original["models"].items())[:1])})
        source["questions"][-1]["current_price"] += 10
        self.news.items.append({"changed_after_freeze": True})
        self.prepare(dataset_id=source_id, case_ids=["s1"])
        recovered = read_json(path)
        self.assertEqual(recovered["models"], original["models"])
        self.assertEqual(recovered["dataset_snapshot_sha256"], original["dataset_snapshot_sha256"])
        for run_id in recovered["models"].values():
            frozen = read_json(self.root / "runs" / run_id / "dataset.json")
            self.assertEqual([question["case_id"] for question in frozen["questions"]], ["s1"])
            self.assertEqual(frozen["labels"], {})

    def test_profile_mutation_after_prepare_is_rejected_before_prediction(self):
        path = self.prepare()
        batch = read_json(path)
        identifier = next(iter(batch["models"].values()))
        record_path = self.root / "runs" / identifier / "run.json"
        record = read_json(record_path)
        record["generation_config"]["max_output_tokens"] = 42
        write_json(record_path, record)
        with patch("newsdesk.live_prediction_batch.EvaluationManager._case") as predict:
            with self.assertRaisesRegex(ValueError, "冻结模型设置"):
                execute_live_batch(self.root, path)
        predict.assert_not_called()

    def test_orphan_lock_without_result_prevents_model_redispatch(self):
        path = self.prepare()
        batch = read_json(path)
        originals = {}
        for index, identifier in enumerate(batch["models"].values()):
            lock_path = self.root / "runs" / identifier / "cases/s1/attempt-interrupted/prediction-lock.json"
            if index == 0:
                write_json(lock_path, {"case_id": "s1", "predicted": {"direction": "up"},
                                      "submitted_at": "2026-11-03T09:00:00Z",
                                      "prediction_locked_at": "2026-11-03T09:01:00Z"})
            else:
                lock_path.parent.mkdir(parents=True)
                lock_path.write_text("interrupted malformed lock")
            originals[lock_path] = lock_path.read_bytes()
        with patch("newsdesk.live_prediction_batch.AIHubPricePredictor"), \
                patch("newsdesk.live_prediction_batch.EvaluationManager._case") as predict, \
                patch("newsdesk.live_prediction_batch.audit_live_run", return_value={"valid": False}):
            execute_live_batch(self.root, path, api_workers=1, retries=1)
        predict.assert_not_called()
        self.assertTrue(all(p.read_bytes() == old for p, old in originals.items()))
        for identifier in batch["models"].values():
            result = read_json(self.root / "runs" / identifier / "cases/s1/result.json")
            self.assertEqual(result["recovery_status"], "prediction_lock_requires_audit")
            self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main()
