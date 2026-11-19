import tempfile
from pathlib import Path
import unittest
from unittest import mock

from newsdesk.batch_evaluation import execute, verify_baseline, verify_batch_reference, prepare_experiment
from newsdesk.evaluation import digest, read_json, write_json


class FrozenBatchTests(unittest.TestCase):
    def test_v2_freezes_actual_family_counts_and_validates_full_reference(self):
        from newsdesk.tests.test_evaluation import FakeBenchmark, question, forecast
        benchmark = FakeBenchmark()
        counts = {"WTI":41, "Brent":42, "SC":41}
        benchmark.questions = [question(f"{family}-{i}", family) for family, count in counts.items() for i in range(count)]
        benchmark.labels = {q["case_id"]:forecast() for q in benchmark.questions}
        dataset = benchmark.load_dataset()
        dataset["manifest"].update(profile="v2", expected_questions=124,
                                   families=[{"id":family, "count":count} for family,count in counts.items()])
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("newsdesk.batch_evaluation.BenchmarkStore", return_value=benchmark), \
             mock.patch.object(benchmark, "load_dataset", return_value=dataset), \
             mock.patch("newsdesk.batch_evaluation.AIHubPricePredictor"):
            root = Path(directory)
            batch = read_json(prepare_experiment(root, "Exp02", ["deepseek-v4-pro", "gpt-6.1-sol"], environment_id="env02"))
            self.assertEqual(batch["total_per_model"],124)
            self.assertEqual(batch["expected_total"],248)
            self.assertEqual(batch["family_question_counts"],counts)
            for rid in batch["models"].values():
                self.assertEqual(verify_baseline(root,rid,require_completed=False)["total"],124)
            rid=batch["snapshot_run_id"]
            run=read_json(root / "runs" / rid / "run.json")
            run["total"]=123
            write_json(root / "runs" / rid / "run.json",run)
            with self.assertRaisesRegex(ValueError,"完整回测"):
                verify_baseline(root,rid,require_completed=False)

    def test_new_experiment_reference_can_be_unscored_but_remains_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            run = read_json(root / "runs/baseline/run.json")
            run.update(status="queued", experiment_id="Exp01")
            write_json(root / "runs/baseline/run.json", run)
            with self.assertRaises(ValueError):
                verify_baseline(root, "baseline")
            batch = {"snapshot_run_id":"baseline", "experiment_id":"Exp01"}
            self.assertEqual(verify_batch_reference(root, batch)["run_id"], "baseline")
            batch["experiment_id"] = "Exp02"
            with self.assertRaisesRegex(ValueError, "归属"):
                verify_batch_reference(root, batch)

    def test_named_experiment_freezes_inputs_once_and_refuses_duplicate_creation(self):
        from newsdesk.tests.test_evaluation import FakeBenchmark, FakeNews, question, forecast
        benchmark = FakeBenchmark()
        benchmark.questions = [question("{}-{}".format(f,i),f) for f in ("WTI","Brent","SC") for i in range(30)]
        benchmark.labels = {q["case_id"]:forecast() for q in benchmark.questions}
        news = FakeNews()
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("newsdesk.batch_evaluation.BenchmarkStore", return_value=benchmark), \
             mock.patch("newsdesk.batch_evaluation.AIHubPricePredictor"), \
             mock.patch.object(news, "all", wraps=news.all) as read_news:
            root = Path(directory)
            path = prepare_experiment(root,"Exp01",["deepseek-v4-pro","gpt-6.1-sol"],news_store=news)
            batch = read_json(path)
            self.assertEqual(batch["expected_total"],180)
            read_news.assert_called_once()
            runs = [read_json(root / "runs" / rid / "run.json") for rid in batch["models"].values()]
            self.assertTrue(all(r["total"] == 90 and r["experiment_id"] == "Exp01" for r in runs))
            self.assertEqual(runs[0]["news_snapshot_sha256"],runs[1]["news_snapshot_sha256"])
            self.assertEqual(runs[0]["dataset_snapshot_sha256"],runs[1]["dataset_snapshot_sha256"])
            self.assertTrue((Path(batch["source_root"]) / "newsdesk/batch_watch.py").is_file())
            with self.assertRaisesRegex(ValueError,"已存在"):
                prepare_experiment(root,"Exp01",["deepseek-v4-pro"],news_store=news)

    def fixture(self, root):
        questions = [{"case_id": "case-{}".format(i), "family": "WTI", "input_date": "2026-10-29"}
                     for i in range(90)]
        values = {"dataset.json": {"questions": questions, "labels": {q["case_id"]: {} for q in questions}},
                  "news-snapshot.json": [], "technical-snapshot.json": []}
        run = {"run_id": "baseline", "status": "completed", "mode": "backtest", "total": 90,
               "dataset_snapshot_sha256": digest(values["dataset.json"]),
               "news_snapshot_sha256": digest([]), "technical_snapshot_sha256": digest([])}
        for name, value in values.items():
            write_json(root / "runs/baseline" / name, value)
            write_json(root / "runs/model-run" / name, value)
        write_json(root / "runs/baseline/run.json", run)
        write_json(root / "runs/model-run/run.json", dict(run, run_id="model-run", succeeded=89, status="queued"))
        batch = {"baseline_run_id": "baseline", "models": {"gpt-6.1-sol": "model-run"},
                 "generation": {"client_timeout_seconds": 300, "gateway_timeout_seconds": 240,
                                "max_output_tokens": 16000, "max_rounds": 18}}
        write_json(root / "batch.json", batch)
        return values

    def test_snapshot_tampering_stops_before_predictor_construction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            write_json(root / "runs/model-run/news-snapshot.json", [{"unexpected": "future article"}])
            with mock.patch("newsdesk.batch_evaluation.AIHubPricePredictor") as predictor:
                with self.assertRaisesRegex(ValueError, "快照校验失败"):
                    execute(root, root / "batch.json", retries=0)
                predictor.assert_not_called()
            write_json(root / "runs/baseline/technical-snapshot.json", [{"unexpected": "changed data"}])
            with self.assertRaisesRegex(ValueError, "基准快照校验失败"):
                verify_baseline(root, "baseline")

    def test_resume_retries_only_failure_and_preserves_all_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            values = self.fixture(root)
            for q in values["dataset.json"]["questions"]:
                write_json(root / "runs/model-run/cases" / q["case_id"] / "result.json",
                           {"case_id": q["case_id"], "status": "failed" if q["case_id"] == "case-89" else "success"})
            original = {p: p.read_bytes() for p in (root / "runs/model-run/cases").glob("*/result.json")
                        if p.parent.name != "case-89"}
            executed = []
            class Manager:
                def __init__(self, *args):
                    pass
                def _case(self, path, question, label, gate, predictor):
                    executed.append(question["case_id"])
                    result = {"case_id": question["case_id"], "status": "success"}
                    write_json(path / "cases" / question["case_id"] / "result.json", result)
                    return result
                def _update(self, path, questions, finish=False):
                    row = read_json(path / "run.json")
                    row.update(succeeded=90, status="completed" if finish else "running")
                    write_json(path / "run.json", row)
            with mock.patch("newsdesk.batch_evaluation.EvaluationManager", Manager), \
                 mock.patch("newsdesk.batch_evaluation.AIHubPricePredictor"):
                execute(root, root / "batch.json", retries=0, case_workers=2, api_workers=1)
            self.assertEqual(executed, ["case-89"])
            self.assertTrue(all(p.read_bytes() == content for p, content in original.items()))
            self.assertEqual(read_json(root / "runs/model-run/run.json")["status"], "completed")


if __name__ == "__main__":
    unittest.main()
