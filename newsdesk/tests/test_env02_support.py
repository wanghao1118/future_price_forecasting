"""No external calls: env02 search denial, batch preparation and durable audit."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from newsdesk.batch_audit import audit_run
from newsdesk.batch_evaluation import PreparedManager, prepare_experiment, execute
from newsdesk.env01_search import load_context, load_indicator_context
from newsdesk.env02 import cli_settings, instruction
from newsdesk.environment_policy import ENV02_PROTOCOL
from newsdesk.evaluation import AIHubPricePredictor, PRICE_SCHEMA, aggregate_metrics, digest, evaluate_forecast, read_json, write_json
from newsdesk.tests.test_evaluation import FakeBenchmark, forecast, question


class Env02SupportTests(unittest.TestCase):
    def fixture(self, root):
        q = question()
        q.pop("news_cutoff_exclusive")
        material = {"environment_id": "env02", "question": q, "news_policy": "disabled", "network_search": False}
        context = {"environment_id": "env02", "case": q}
        text = instruction(material)
        answer = json.dumps(dict(forecast(), reason="Known bar", public_analysis={"analysis_document": "Known bar and stated uncertainty."}))
        path = root / "runs" / "fixture"
        attempt = path / "cases" / q["case_id"] / "attempt-fixture"
        work = attempt / "model"
        trace = {"protocol": ENV02_PROTOCOL, "environment_id": "env02", "tool_mode": "closed_book_no_tools",
                 "validation_errors": [], "public_analysis": {"analysis_document": "Known bar and stated uncertainty."},
                 "conversation": [{"type": "question_context", **q},
                     {"type": "user_message", "text": text, "sha256": digest(text)},
                     {"type": "assistant_message", "text": answer},
                     {"type": "final_answer", "text": answer}],
                 "search_call_count": 0, "indicator_call_count": 0, "news_count": 0, "technical_count": 0,
                 "api_requests": 1, "tool_call_count": 0}
        result = dict(q, status="success", trace_file=str((work / "conversation.json").relative_to(path)),
                      raw_output=answer, input_sha256=digest(material), **evaluate_forecast(answer, forecast(), q["current_price"]))
        lock = {"input_sha256": digest(material), "predicted": result["predicted"], "raw_output_sha256": digest(answer)}
        result["prediction_lock_sha256"] = digest(lock)
        dataset = {"questions": [q], "labels": {q["case_id"]: forecast()}, "manifest": {"dataset_id": "fixture"}}
        run = {"run_id": "fixture", "model": "deepseek-v4-pro", "status": "completed", "mode": "backtest", "total": 1,
               "environment_id": "env02", "protocol": ENV02_PROTOCOL, "tool_mode": "closed_book_no_tools",
               "dataset_snapshot_sha256": digest(dataset), "news_snapshot_sha256": digest([]), "technical_snapshot_sha256": digest([]),
               "metrics": aggregate_metrics([result], [q], ENV02_PROTOCOL)}
        for target, value in ((path / "dataset.json", dataset), (path / "news-snapshot.json", []),
            (path / "technical-snapshot.json", []), (path / "run.json", run),
            (attempt / "input.json", material), (attempt / "prediction-lock.json", lock),
            (attempt.parent / "result.json", result), (work / "search-context.json", context),
            (work / "prompt.json", {"text": text, "sha256": digest(text)}), (work / "conversation.json", trace)):
            write_json(target, value)
        payload = {"model": run["model"], "tools": [], "tool_choice": "none", "stream": False,
                   "messages": [{"role": "user", "content": text}], "max_tokens": 16000}
        response = {"choices": [{"message": {"content": answer}}]}
        self.jsonl(work / "api-requests.jsonl", {"protocol": "chat", "payload": payload, "sha256": digest(payload)})
        self.jsonl(work / "api-responses.jsonl", {"protocol": "chat", "response": response, "sha256": digest(response)})
        return path, work, run

    @staticmethod
    def jsonl(path, value):
        path.write_text(json.dumps(value) + "\n", encoding="utf-8")

    def test_search_loaders_and_stdio_fail_closed_before_opening_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = {"environment_id": "env02", "case": question(),
                       "news_snapshot_path": str(root / "missing-news.json"), "news_snapshot_sha256": "none",
                       "indicator_snapshot_path": str(root / "missing-indicators.json"), "indicator_snapshot_sha256": "none"}
            write_json(root / "context.json", context)
            with self.assertRaisesRegex(ValueError, "news_search_disabled"):
                load_context(root / "context.json")
            with self.assertRaisesRegex(ValueError, "indicator_search_disabled"):
                load_indicator_context(root / "context.json")
            process = subprocess.run([sys.executable, "-m", "newsdesk.env01_search", "--context", str(root / "context.json"),
                "--audit-dir", str(root / "search")], input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
                capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 1)
            self.assertEqual(process.stdout, "")
            self.assertFalse((root / "search").exists())

    def test_audit_replays_zero_tools_and_rejects_tampering_after_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, work, run = self.fixture(root)
            self.assertTrue(audit_run(root, run["run_id"], run)["complete"])
            audit = read_json(path / "run-validation.json")
            self.assertEqual(next(iter(audit["cases"].values()))["date_gate"], "not_applicable")
            mutations = {
                "input.json": lambda value: {**value, "indicator_search_tool": {"name": "search_indicators"}},
                "prompt.json": lambda value: {**value, "text": value["text"] + "\nInjected context"},
                "search-context.json": lambda value: {**value, "indicator_snapshot_path": "private.json"},
                "conversation.json": lambda value: {**value, "conversation": value["conversation"] + [{"type": "tool_call", "tool": "search_news"}]},
            }
            for filename, mutate in mutations.items():
                with self.subTest(filename=filename):
                    target = work.parent / filename if filename == "input.json" else work / filename
                    original = read_json(target)
                    write_json(target, mutate(copy.deepcopy(original)))
                    with self.assertRaises(AssertionError):
                        audit_run(root, run["run_id"], run)
                    write_json(target, original)
            request_path = work / "api-requests.jsonl"
            request = json.loads(request_path.read_text())
            request["payload"]["tools"] = [{"type": "web_search"}]
            request["sha256"] = digest(request["payload"])
            self.jsonl(request_path, request)
            with self.assertRaisesRegex(AssertionError, "advertises tools"):
                audit_run(root, run["run_id"], run)

    def test_env02_batch_prepare_and_execute_never_instantiate_search_gate(self):
        benchmark = FakeBenchmark()
        benchmark.questions = [question(f"{family}-{index}", family) for family in ("WTI", "Brent", "SC") for index in range(30)]
        benchmark.labels = {q["case_id"]: forecast() for q in benchmark.questions}
        news = mock.Mock()
        news.all.side_effect = AssertionError("env02 cannot read news")
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch("newsdesk.batch_evaluation.BenchmarkStore", return_value=benchmark), \
             mock.patch("newsdesk.batch_evaluation.AIHubPricePredictor"), \
             mock.patch("newsdesk.evaluation.LocalTimeGate", side_effect=AssertionError("No time gate")), \
             mock.patch("newsdesk.batch_evaluation.LocalTimeGate", side_effect=AssertionError("No batch time gate")):
            root = Path(directory)
            batch_path = prepare_experiment(root, "SupportFixture", ["deepseek-v4-pro"], news_store=news, environment_id="env02")
            batch = read_json(batch_path)
            self.assertEqual(batch["protocol"], ENV02_PROTOCOL)
            self.assertNotIn("retrieval_time_policy", batch)
            run_path = root / "runs" / batch["models"]["deepseek-v4-pro"]
            self.assertEqual(read_json(run_path / "news-snapshot.json"), [])
            self.assertEqual(read_json(run_path / "technical-snapshot.json"), [])
            self.assertTrue(all("retrieval_time_policy" not in q for q in read_json(run_path / "dataset.json")["questions"]))
            # No jobs need dispatching: this still traverses scheduler setup and
            # proves it cannot instantiate a time gate for the environment.
            with mock.patch("newsdesk.batch_evaluation.wait"), \
                 mock.patch("newsdesk.batch_evaluation.verify_batch_reference", return_value=read_json(run_path / "run.json")), \
                 mock.patch("newsdesk.batch_evaluation.EvaluationManager._case", side_effect=AssertionError("No model requests")):
                for q in read_json(run_path / "dataset.json")["questions"]:
                    write_json(run_path / "cases" / q["case_id"] / "result.json", {**q, "status": "success", "direction_correct": True,
                        "absolute_errors": {"high": 0, "low": 0, "return_pct": 0}})
                execute(root, batch_path, retries=0)
            news.all.assert_not_called()

    def test_partial_audit_checks_counts_and_aggregate_scores(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, work, run = self.fixture(root)
            dataset = read_json(path / "dataset.json")
            failed_question = dict(dataset["questions"][0], case_id="failed-case")
            dataset["questions"].append(failed_question)
            dataset["labels"]["failed-case"] = forecast()
            failure = dict(failed_question, status="failed", error="Provider unavailable")
            write_json(path / "cases" / "failed-case" / "result.json", failure)
            success = read_json(work.parent.parent / "result.json")
            run.update(status="partial", total=2, succeeded=1, failed=1, completed=2,
                       dataset_snapshot_sha256=digest(dataset),
                       metrics=aggregate_metrics([success, failure], dataset["questions"], ENV02_PROTOCOL))
            write_json(path / "dataset.json", dataset)
            write_json(path / "run.json", run)
            summary = audit_run(root, run["run_id"], run)
            self.assertEqual((summary["succeeded"], summary["failed"], summary["missing"]), (1, 1, 0))
            self.assertFalse(summary["complete"])
            write_json(path / "run.json", {**run, "failed": 0})
            with self.assertRaisesRegex(AssertionError, "run count mismatch"):
                audit_run(root, run["run_id"], run)
            write_json(path / "run.json", {**run, "metrics": {}})
            with self.assertRaisesRegex(AssertionError, "aggregate scoring mismatch"):
                audit_run(root, run["run_id"], run)

    def test_cli_audit_binds_native_messages_output_and_actual_config_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path, work, run = self.fixture(root)
            for name in ("api-requests.jsonl", "api-responses.jsonl"):
                (work / name).unlink()
            trace = read_json(work / "conversation.json")
            answer = trace["conversation"][-1]["text"]
            trace["conversation"][2]["event_id"] = "native-answer"
            write_json(work / "conversation.json", trace)
            (work / "raw-output.json").write_text(answer, encoding="utf-8")
            write_json(work / "schema.json", PRICE_SCHEMA)
            prompt = read_json(work / "prompt.json")
            settings = cli_settings()
            command = ["codex", "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--model", run["model"]]
            for key, value in settings.items():
                command += ["-c", key + "=" + json.dumps(value)]
            runtime = {"command": command, "settings": settings, "prompt_sha256": prompt["sha256"],
                       "schema_sha256": digest(PRICE_SCHEMA)}
            write_json(work / "runtime-config.json", runtime)
            events = [{"type": "item.completed", "item": {"id": "native-answer", "type": "agent_message", "text": answer}},
                      {"type": "turn.completed"}]
            event_text = "".join(json.dumps(event) + "\n" for event in events)
            (work / "cli-events.jsonl").write_text(event_text, encoding="utf-8")
            self.assertTrue(audit_run(root, run["run_id"], run)["complete"])

            bad_events = copy.deepcopy(events)
            bad_events[0]["item"]["text"] = "A completely different native answer"
            (work / "cli-events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in bad_events), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "transcript differs from native"):
                audit_run(root, run["run_id"], run)
            # Even if the transcript is changed to match the tampered native
            # message, it cannot disagree with the prediction's final answer.
            bad_trace = copy.deepcopy(trace)
            bad_trace["conversation"][2]["text"] = bad_events[0]["item"]["text"]
            write_json(work / "conversation.json", bad_trace)
            with self.assertRaises((AssertionError, ValueError)):
                audit_run(root, run["run_id"], run)
            (work / "cli-events.jsonl").write_text(event_text, encoding="utf-8")
            write_json(work / "conversation.json", trace)
            (work / "raw-output.json").write_text(answer + "\n", encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "output file differs"):
                audit_run(root, run["run_id"], run)
            (work / "raw-output.json").write_text(answer, encoding="utf-8")

            changes = [
                (["--config", 'web_search="auto"'], "unrecorded config override"),
                (["-c", 'web_search="auto"'], "duplicate config overrides"),
            ]
            for extra, message in changes:
                with self.subTest(extra=extra):
                    write_json(work / "runtime-config.json", {**runtime, "command": command + extra})
                    with self.assertRaisesRegex(AssertionError, message):
                        audit_run(root, run["run_id"], run)
            modified_command = [arg.replace('web_search="disabled"', 'web_search="auto"') for arg in command]
            write_json(work / "runtime-config.json", {**runtime, "command": modified_command})
            with self.assertRaisesRegex(AssertionError, "command differs from recorded settings"):
                audit_run(root, run["run_id"], run)
            for key in ("features.multi_agent", "features.shell_tool", "features.skip_host_skill_discovery"):
                with self.subTest(missing_safety_key=key):
                    reduced = dict(settings)
                    reduced.pop(key)
                    reduced_command = command[:7]
                    for setting, value in reduced.items():
                        reduced_command += ["-c", setting + "=" + json.dumps(value)]
                    write_json(work / "runtime-config.json", {**runtime, "command": reduced_command, "settings": reduced})
                    with self.assertRaisesRegex(AssertionError, "missing mandatory safety settings"):
                        audit_run(root, run["run_id"], run)

    def test_real_case_pipeline_and_independent_audit_accept_both_api_protocols(self):
        for protocol in ("chat", "responses"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory, \
                 mock.patch("newsdesk.evaluation.LocalTimeGate", side_effect=AssertionError("No time gate")), \
                 mock.patch("newsdesk.evaluation.LocalNewsSearch", side_effect=AssertionError("No news")), \
                 mock.patch("newsdesk.evaluation.LocalIndicatorSearch", side_effect=AssertionError("No indicators")):
                root = Path(directory)
                benchmark = FakeBenchmark()
                benchmark.questions = [question()]
                benchmark.labels = {benchmark.questions[0]["case_id"]: forecast()}
                news = mock.Mock()
                news.all.side_effect = AssertionError("env02 cannot read news")
                manager = PreparedManager(root, benchmark, news)
                run = manager.start(strategy_id="aihub-deepseek-v4-pro", environment_id="env02")
                path = root / "runs" / run["run_id"]
                q = read_json(path / "dataset.json")["questions"][0]
                answer = json.dumps(dict(forecast(), reason="Known bar", public_analysis={"analysis_document": "Known bar, assumptions and uncertainty."}))
                response = ({"output": [{"type": "message", "content": [{"type": "output_text", "text": answer}]}]}
                            if protocol == "responses" else {"choices": [{"message": {"content": answer}}]})
                predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture", api_protocol=protocol)
                with mock.patch.object(predictor, "_request", return_value=(response, "fixture-trace")):
                    result = manager._case(path, q, forecast(), None, predictor)
                self.assertEqual(result["status"], "success", result.get("error"))
                manager._update(path, [q], finish=True)
                run = read_json(path / "run.json")
                self.assertTrue(audit_run(root, run["run_id"], run)["complete"])
                self.assertEqual(result["search_calls"], 0)
                self.assertEqual(result["indicator_calls"], 0)


if __name__ == "__main__":
    unittest.main()
