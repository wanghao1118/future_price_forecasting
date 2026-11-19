"""Dispatch budgets survive restarts without invoking real model APIs."""

from collections import Counter
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from newsdesk.evaluation import read_json, write_json
from newsdesk.live_prediction_batch import (
    _dispatch_live_case, _live_attempt_budget, execute_live_batch,
)
from newsdesk.tests import test_live_prediction_batch as fixtures


class LiveDispatchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runs" / "model-run"
        self.question = {"case_id": "case-one", "mode": "live"}
        self.case_root = self.path / "cases" / self.question["case_id"]

    def claim(self, maximum=2):
        return _live_attempt_budget(self.path, self.question, maximum, claim=True)

    def test_durable_claim_is_written_before_dispatch_and_restart_does_not_reset_it(self):
        manager = Mock()

        def interrupted(*args):
            saved = read_json(self.case_root / "dispatch-budget.json")
            self.assertEqual(saved["used_attempts"], 1)
            self.assertEqual(len(saved["dispatches"]), 1)
            (self.case_root / "attempt-interrupted").mkdir()
            raise RuntimeError("process interrupted before result")

        manager._case.side_effect = interrupted
        with patch("newsdesk.live_prediction_batch.project_live_submission", side_effect=lambda value: value), \
                patch("newsdesk.live_prediction_batch._validate_live_open"):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                _dispatch_live_case(manager, self.path, self.question, None, None, 2)
        budget, claimed = self.claim()
        self.assertTrue(claimed)
        self.assertEqual(budget["used_attempts"], 2)
        self.assertEqual(budget["dispatches"][0]["attempt"], "attempt-interrupted")
        self.assertFalse(self.claim()[1])
        self.assertFalse(self.claim(4)[1], "A later invocation cannot enlarge the first frozen budget")

    def test_claim_without_attempt_still_reserves_a_slot_after_restart(self):
        self.assertTrue(self.claim()[1])
        self.assertTrue(self.claim()[1])
        self.assertFalse(self.claim()[1])
        self.assertEqual(read_json(self.case_root / "dispatch-budget.json")["used_attempts"], 2)

    def test_old_runner_attempts_and_missing_directories_are_accounted_once(self):
        (self.case_root / "attempt-old-one").mkdir(parents=True)
        (self.case_root / "attempt-old-two").mkdir()
        write_json(self.case_root / "result.json", {"status": "failed", "attempt": "attempt-old-three"})
        budget, claimed = self.claim()
        self.assertFalse(claimed)
        self.assertEqual(budget["used_attempts"], 3)
        (self.case_root / "attempt-old-two").rmdir()
        self.assertEqual(self.claim()[0]["used_attempts"], 3, "Deleting an old attempt cannot reopen its budget")

    def test_result_without_an_attempt_reference_proves_one_legacy_dispatch(self):
        write_json(self.case_root / "result.json", {"status": "failed", "error": "HTTP 503"})
        budget, claimed = self.claim()
        self.assertTrue(claimed)
        self.assertEqual(budget["used_attempts"], 2)
        self.assertFalse(self.claim()[1])

    def test_extra_attempts_from_an_old_runner_do_not_hide_behind_one_claim(self):
        self.claim()
        for name in ("attempt-one", "attempt-two"):
            (self.case_root / name).mkdir()
        budget, claimed = self.claim()
        self.assertFalse(claimed)
        self.assertEqual(budget["used_attempts"], 2)
        self.assertEqual(len(budget["legacy_attempts"]), 1)

    def test_budget_is_independent_for_each_model_and_case(self):
        self.claim();self.claim()
        for path, question in ((self.path.parent / "another-model", self.question),
                               (self.path, {"case_id": "another-case"})):
            budget, claimed = _live_attempt_budget(path, question, 2, claim=True)
            self.assertTrue(claimed)
            self.assertEqual(budget["used_attempts"], 1)

    def test_terminal_403_and_closed_deadline_never_dispatch_or_consume_new_claims(self):
        manager = Mock()
        write_json(self.case_root / "result.json", {"case_id": "case-one", "status": "failed", "error": "AIHub HTTP 403，3001"})
        with patch("newsdesk.live_prediction_batch._validate_live_open") as validate:
            self.assertIsNone(_dispatch_live_case(manager, self.path, self.question, None, None, 2))
        validate.assert_not_called()
        self.assertFalse((self.case_root / "dispatch-budget.json").exists())
        (self.case_root / "result.json").unlink()
        with patch("newsdesk.live_prediction_batch.project_live_submission", side_effect=lambda value: value), \
                patch("newsdesk.live_prediction_batch._validate_live_open", side_effect=ValueError("deadline")):
            self.assertIsNone(_dispatch_live_case(manager, self.path, self.question, None, None, 2))
        manager._case.assert_not_called()
        self.assertFalse((self.case_root / "dispatch-budget.json").exists())

    def test_existing_lock_wins_even_when_the_budget_is_corrupt(self):
        manager = Mock()
        write_json(self.case_root / "dispatch-budget.json", {"broken": True})
        write_json(self.case_root / "result.json", {"case_id": "case-one", "status": "failed", "prediction_lock_sha256": "saved-lock"})
        self.assertIsNone(_dispatch_live_case(manager, self.path, self.question, None, None, 2))
        (self.case_root / "result.json").unlink()
        lock = self.case_root / "attempt-orphan" / "prediction-lock.json"
        lock.parent.mkdir()
        lock.write_text("interrupted partial lock")
        original = lock.read_bytes()
        self.assertIsNone(_dispatch_live_case(manager, self.path, self.question, None, None, 2))
        self.assertEqual(lock.read_bytes(), original)
        self.assertEqual(read_json(self.case_root / "result.json")["recovery_status"], "prediction_lock_requires_audit")
        manager._case.assert_not_called()

    def test_corrupt_budget_fails_closed_before_dispatch(self):
        self.claim()
        path = self.case_root / "dispatch-budget.json"
        value = read_json(path);value["used_attempts"] = 0;write_json(path, value)
        manager = Mock()
        with patch("newsdesk.live_prediction_batch.project_live_submission", side_effect=lambda value: value), \
                patch("newsdesk.live_prediction_batch._validate_live_open"):
            with self.assertRaisesRegex(ValueError, "预算校验"):
                _dispatch_live_case(manager, self.path, self.question, None, None, 2)
        manager._case.assert_not_called()


class LiveBatchRestartTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.LivePredictionBatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.root = self.fixture.root
        self.batch_path = self.fixture.prepare()
        self.batch = read_json(self.batch_path)

    def execute(self, predict, retries=1):
        with ExitStack() as stack:
            stack.enter_context(redirect_stdout(StringIO()))
            stack.enter_context(patch("newsdesk.live_prediction_batch.AIHubPricePredictor"))
            stack.enter_context(patch("newsdesk.evaluation.datetime", fixtures.FixedClock))
            stack.enter_context(patch("newsdesk.live_prediction_batch.audit_live_run", return_value={"valid": False}))
            mocked = stack.enter_context(patch("newsdesk.live_prediction_batch.EvaluationManager._case", side_effect=predict))
            execute_live_batch(self.root, self.batch_path, api_workers=1, retries=retries)
            return mocked.call_count

    @staticmethod
    def fail(path, question, *args):
        case_root = path / "cases" / question["case_id"]
        index = len(list(case_root.glob("attempt-*"))) + 1
        attempt = "attempt-test-" + str(index)
        (case_root / attempt).mkdir(parents=True)
        result = {"case_id": question["case_id"], "family": question["family"], "status": "failed",
                  "attempt": attempt, "error": "fixture HTTP 503"}
        write_json(case_root / "result.json", result)
        return result

    def test_two_failed_calls_per_model_are_the_total_budget_across_restarts(self):
        calls = Counter()

        def fail(path, question, *args):
            calls[path.name] += 1
            return self.fail(path, question)

        self.assertEqual(self.execute(fail), 2 * len(self.batch["models"]))
        self.assertEqual(self.execute(fail), 0)
        self.assertTrue(all(count == 2 for count in calls.values()))
        for identifier in self.batch["models"].values():
            state = read_json(self.root / "runs" / identifier / "cases/s1/dispatch-budget.json")
            self.assertEqual((state["used_attempts"], state["remaining_attempts"]), (2, 0))

    def test_one_legacy_failed_dispatch_leaves_only_one_default_retry(self):
        for identifier in self.batch["models"].values():
            path = self.root / "runs" / identifier
            self.fail(path, {"case_id": "s1", "family": "SC"})
        self.assertEqual(self.execute(self.fail), len(self.batch["models"]))
        self.assertEqual(self.execute(self.fail), 0)

    def test_restarting_with_larger_retry_flag_does_not_expand_the_frozen_budget(self):
        self.assertEqual(self.execute(self.fail, retries=0), len(self.batch["models"]))
        self.assertEqual(self.execute(self.fail, retries=1), 0)

    def test_403_result_is_not_automatically_retried(self):
        for identifier in self.batch["models"].values():
            path = self.root / "runs" / identifier / "cases/s1"
            write_json(path / "result.json", {"case_id": "s1", "family": "SC", "status": "failed", "error": "AIHub HTTP 403，3001"})
        self.assertEqual(self.execute(self.fail), 0)


if __name__ == "__main__":
    unittest.main()
