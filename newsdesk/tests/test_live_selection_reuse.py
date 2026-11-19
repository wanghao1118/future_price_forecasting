"""Queued live runs are reusable only for the same intact frozen selection."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.batch_evaluation import PreparedManager
from newsdesk.evaluation import EVALUATION_PROTOCOL, read_json, write_json
from newsdesk.live_prediction_batch import prepare_live_batch
from newsdesk.tests.test_evaluation import FakeBenchmark, FakeNews


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        point = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)
        return point.astimezone(tz) if tz else point.replace(tzinfo=None)


class SelectedBenchmark:
    def __init__(self, dataset):
        self.dataset = dataset

    def load_dataset(self, *args, **kwargs):
        return copy.deepcopy(self.dataset)


class LiveSelectionReuseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "evaluations"
        for target in ("newsdesk.tests.test_evaluation.datetime", "newsdesk.evaluation.datetime"):
            clock = patch(target, FixedClock)
            clock.start()
            self.addCleanup(clock.stop)
        predictor = patch("newsdesk.live_prediction_batch.AIHubPricePredictor")
        self.predictor = predictor.start()
        self.addCleanup(predictor.stop)
        self.store, self.news = FakeBenchmark("live"), FakeNews()
        self.model = "gpt-6-luna"
        self.reference = self.root / "batches" / "Exp01.json"
        write_json(self.root / "runs" / "reference" / "run.json",
                   {"run_id": "reference", "model": self.model, "experiment_id": "Exp01", "mode": "backtest"})
        write_json(self.reference, {"experiment_id": "Exp01", "environment_id": "env01", "protocol": EVALUATION_PROTOCOL,
                   "models": {self.model: "reference"}, "generation": {"max_output_tokens": 16000, "max_rounds": 18,
                   "client_timeout_seconds": 300, "gateway_timeout_seconds": 240}})

    def prepare(self, case_id):
        path = prepare_live_batch(self.root, self.reference, self.store, self.news, case_ids=[case_id])
        return path, read_json(path)

    def manager_start(self, dataset):
        manager = PreparedManager(self.root, SelectedBenchmark(dataset), self.news)
        return manager.start(strategy_id="aihub-" + self.model, mode="live", experiment_id="Exp01", workers=1)

    def test_different_queued_selections_in_one_dataset_keep_distinct_runs_and_batch_ownership(self):
        _, sc = self.prepare("s1")
        sc_run_id = sc["models"][self.model]
        sc_path = self.root / "runs" / sc_run_id
        before = (sc_path / "run.json").read_bytes()
        frozen_before = (sc_path / "dataset.json").read_bytes()
        _, wti = self.prepare("w1")
        wti_run_id = wti["models"][self.model]
        self.assertNotEqual(sc_run_id, wti_run_id)
        self.assertEqual(sc["dataset_id"], wti["dataset_id"])
        self.assertEqual((sc_path / "run.json").read_bytes(), before)
        self.assertEqual((sc_path / "dataset.json").read_bytes(), frozen_before)
        self.assertEqual(read_json(sc_path / "run.json")["comparison_batch_id"], sc["batch_id"])
        self.assertEqual(read_json(self.root / "runs" / wti_run_id / "run.json")["comparison_batch_id"], wti["batch_id"])
        self.assertEqual([q["case_id"] for q in read_json(sc_path / "dataset.json")["questions"]], ["s1"])
        self.assertEqual([q["case_id"] for q in read_json(self.root / "runs" / wti_run_id / "dataset.json")["questions"]], ["w1"])
        self.predictor.return_value.predict.assert_not_called()

    def test_same_intact_selection_can_reuse_queued_run_and_prepared_batch(self):
        path, batch = self.prepare("s1")
        run_id = batch["models"][self.model]
        frozen = read_json(self.root / "runs" / run_id / "dataset.json")
        self.assertEqual(self.manager_start(frozen)["run_id"], run_id)
        again, same_batch = self.prepare("s1")
        self.assertEqual(again, path)
        self.assertEqual(same_batch["models"], batch["models"])
        self.assertEqual(read_json(self.root / "runs" / run_id / "run.json")["comparison_batch_id"], batch["batch_id"])
        self.assertEqual(len(list((self.root / "runs").glob("*/dataset.json"))), 1)

    def test_same_economic_question_with_different_exact_case_is_not_reused(self):
        _, batch = self.prepare("s1")
        original = batch["models"][self.model]
        selected = read_json(self.root / "runs" / original / "dataset.json")
        selected["questions"][0]["case_id"] = "s1-revision"
        second = self.manager_start(selected)
        self.assertNotEqual(second["run_id"], original)
        self.assertEqual(read_json(self.root / "runs" / original / "dataset.json")["questions"][0]["case_id"], "s1")

    def test_corrupt_candidate_freeze_fails_explicitly_before_creating_another_run(self):
        _, batch = self.prepare("s1")
        run_id = batch["models"][self.model]
        path = self.root / "runs" / run_id / "dataset.json"
        frozen = read_json(path)
        frozen["questions"][0]["current_price"] += 10
        write_json(path, frozen)
        with self.assertRaisesRegex(ValueError, "冻结测试集校验失败"):
            self.prepare("w1")
        self.assertEqual(len(list((self.root / "runs").glob("*/dataset.json"))), 1)


if __name__ == "__main__":
    unittest.main()
