"""Enforce the env02 capability boundary on actual transport and CLI paths."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from newsdesk.batch_audit import audit_run
from newsdesk.batch_evaluation import PreparedManager
from newsdesk.env02 import validate_response_no_tools
from newsdesk.environment_policy import ENV02_PROTOCOL, allowed_tools
from newsdesk.evaluation import (AIHubPricePredictor, CodexPricePredictor, environments,
                                read_json, validate_run_retrieval_policy, write_json)
from newsdesk.tests.test_evaluation import FakeBenchmark, forecast, question


class NoToolsTests(unittest.TestCase):
    def material(self):
        case = question()
        case.pop("news_cutoff_exclusive")
        return ({"environment_id": "env02", "question": case, "news_policy": "disabled", "network_search": False},
                {"environment_id": "env02", "case": case})

    def answer(self):
        return json.dumps(dict(forecast(), reason="Known bar and uncertainty", public_analysis={
            "analysis_document": "Analysis using the supplied bar; forecasts are uncertain."}))

    def response(self, protocol, text):
        return ({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
                if protocol == "responses" else {"choices": [{"message": {"content": text}}]})

    def test_environment_has_one_evaluator_and_no_interfaces(self):
        env = next(e for e in environments() if e["id"] == "env02")
        self.assertEqual([a["name"] for a in env["agents"]], ["evaluator agent"])
        self.assertEqual(env["tools"], [])
        self.assertEqual(allowed_tools({"environment_id": "env02"}), ())
        self.assertEqual(env["search_tools"], [])
        self.assertNotIn("retrieval_time_policy", env)

    def test_all_provider_tool_variants_are_denied_without_search_execution(self):
        attempts = [
            {"choices": [{"message": {"tool_calls": [{"function": {"name": tool, "arguments": "{}"}}]}}]}
            for tool in ("search_news", "search_indicators", "web_search", "shell")]
        attempts += [{"output": [{"type": tool}]} for tool in
                     ("function_call", "web_search_call", "file_search_call", "computer_call", "custom_tool_call")]
        attempts += [{"choices": [{"message": {"function_call": {"name": "search_news"}}}]},
                     {"choices": [{"message": {"content": [{"type": "tool_use", "name": "search_news"}]}}]},
                     {"choices": [{"message": {"content": self.answer()}, "finish_reason": "tool_calls"}]}]
        for data in attempts:
            with self.subTest(data=data), tempfile.TemporaryDirectory() as directory:
                protocol = "responses" if "output" in data else "chat"
                material, context = self.material()
                predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture", api_protocol=protocol)
                work = Path(directory) / "model"
                with mock.patch.object(predictor, "_request", return_value=(data, "fixture")) as request, \
                     mock.patch("newsdesk.evaluation.LocalNewsSearch", side_effect=AssertionError("No news")), \
                     mock.patch("newsdesk.evaluation.LocalIndicatorSearch", side_effect=AssertionError("No indicators")):
                    with self.assertRaisesRegex(RuntimeError, "env02 禁止"):
                        predictor.predict(material, work, context)
                self.assertEqual(request.call_count, 1)
                payload = request.call_args.args[0]
                self.assertEqual(payload["tools"], [])
                self.assertEqual(payload["tool_choice"], "none")
                self.assertEqual(read_json(work / "conversation.json")["status"], "failed")
                self.assertFalse((work / "search").exists())
                self.assertFalse((work / "indicators").exists())

    def test_context_cannot_inject_snapshots_tools_or_reference_answers(self):
        for extra in ({"news_snapshot_path": "/private/news"}, {"indicator_snapshot_path": "/private/indicators"},
                      {"actual": forecast()}, {"tools": ["search_news"]}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                material, context = self.material()
                predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture")
                with mock.patch.object(predictor, "_request") as request:
                    with self.assertRaisesRegex(ValueError, "env02"):
                        predictor.predict(material, Path(directory), {**context, **extra})
                request.assert_not_called()

    def test_format_repair_remains_tool_free_and_auditable_in_both_protocols(self):
        for protocol in ("chat", "responses"):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                material, context = self.material()
                work = Path(directory)
                predictor = AIHubPricePredictor("deepseek-v4-pro", api_key="fixture", api_protocol=protocol)
                responses = [self.response(protocol, "broken JSON"), self.response(protocol, self.answer())]
                with mock.patch.object(predictor, "_request", side_effect=[(r, "fixture") for r in responses]) as request:
                    self.assertEqual(predictor.predict(material, work, context), self.answer())
                self.assertEqual(request.call_count, 2)
                for call in request.call_args_list:
                    self.assertEqual(call.args[0]["tools"], [])
                    self.assertEqual(call.args[0]["tool_choice"], "none")
                saved = [json.loads(line) for line in (work / "api-requests.jsonl").read_text().splitlines()]
                history = saved[-1]["payload"]["input" if protocol == "responses" else "messages"]
                self.assertEqual(len(history), 3)
                self.assertIn("correct only JSON", history[-1]["content"])
                self.assertFalse(read_json(work / "conversation.json")["validation_errors"])

    def test_cli_has_no_mcp_and_rejects_all_non_message_events_before_scoring(self):
        owner = self
        for event_type in (None, "mcp_tool_call", "web_search", "command_execution", "custom_tool_call"):
            with self.subTest(event_type=event_type), tempfile.TemporaryDirectory() as directory:
                material, context = self.material()
                answer = self.answer()

                class Process:
                    returncode = 0
                    def __init__(self, command, **kwargs):
                        self.output = Path(command[command.index("--output-last-message") + 1])
                        self.command = command
                    def communicate(self, prompt, timeout):
                        owner.assertIn("No tools or data interfaces are available", prompt)
                        self.output.write_text(answer, encoding="utf-8")
                        events = [{"type": "item.completed", "item": {"id": "answer", "type": "agent_message", "text": answer}},
                                  {"type": "turn.completed"}]
                        if event_type:
                            events.insert(0, {"type": "item.completed", "item": {"id": "bad", "type": event_type}})
                        return "\n".join(json.dumps(e) for e in events), ""

                client = CodexPricePredictor(executable="/fixture/codex")
                with mock.patch("newsdesk.evaluation.subprocess.Popen", side_effect=Process):
                    if event_type:
                        with self.assertRaisesRegex(RuntimeError, "env02"):
                            client.predict(material, Path(directory) / "model", context)
                    else:
                        self.assertEqual(client.predict(material, Path(directory) / "model", context), answer)
                runtime = read_json(Path(directory) / "model/runtime-config.json")
                self.assertEqual(runtime["settings"]["mcp_servers"], {})
                self.assertIn("--ignore-rules", runtime["command"])
                self.assertFalse(any("mcp_servers." in arg for arg in runtime["command"]))
                trace = read_json(Path(directory) / "model/conversation.json")
                self.assertEqual(trace["conversation"][0]["type"], "question_context")
                self.assertEqual(bool(trace["validation_errors"]), bool(event_type))

    def test_protocol_validation_rejects_wrong_environment_and_retrieval_policies(self):
        run = {"environment_id": "env02", "protocol": ENV02_PROTOCOL, "tool_mode": "closed_book_no_tools"}
        data = {"questions": [self.material()[1]["case"]]}
        validate_run_retrieval_policy(run, data)
        for mutation in ({"environment_id": "env01"}, {"protocol": "env01-legacy"},
                         {"retrieval_time_policy": "forecast-start-minus-30m-utc-v1"},
                         {"retrieval_boundaries": {"case": "cutoff"}}, {"tool_mode": "agent_initiated_local_search"}):
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "env02"):
                validate_run_retrieval_policy({**run, **mutation}, data)

    def test_cli_case_locks_prediction_and_passes_independent_audit(self):
        for with_document in (True, False):
            with self.subTest(with_document=with_document), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                benchmark = FakeBenchmark()
                benchmark.questions = [question()]
                benchmark.labels = {benchmark.questions[0]["case_id"]: forecast()}
                news = mock.Mock()
                news.all.side_effect = AssertionError("No news")
                manager = PreparedManager(root, benchmark, news)
                answer = self.answer() if with_document else json.dumps(dict(forecast(), reason="No analysis"))

                class Process:
                    returncode = 0
                    def __init__(self, command, **kwargs):
                        self.output = Path(command[command.index("--output-last-message") + 1])
                    def communicate(self, prompt, timeout):
                        self.output.write_text(answer, encoding="utf-8")
                        return "\n".join(json.dumps(e) for e in [
                            {"type": "item.completed", "item": {"id": "final", "type": "agent_message", "text": answer}},
                            {"type": "turn.completed"}]), ""

                with mock.patch("newsdesk.evaluation.LocalTimeGate", side_effect=AssertionError("No time gate")), \
                     mock.patch("newsdesk.evaluation.subprocess.Popen", side_effect=Process):
                    run = manager.start(environment_id="env02")
                    path = root / "runs" / run["run_id"]
                    case = read_json(path / "dataset.json")["questions"][0]
                    result = manager._case(path, case, forecast(), None, CodexPricePredictor(executable="/fixture/codex"))
                if not with_document:
                    self.assertEqual(result["status"], "failed")
                    self.assertIn("公开分析文档", result["error"])
                    self.assertFalse((path / "cases" / case["case_id"] / result["attempt"] / "prediction-lock.json").exists())
                else:
                    self.assertEqual(result["status"], "success", result.get("error"))
                    manager._update(path, [case], finish=True)
                    self.assertTrue(audit_run(root, run["run_id"], read_json(path / "run.json"))["complete"])


if __name__ == "__main__":
    unittest.main()
