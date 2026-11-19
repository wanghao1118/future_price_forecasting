import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from newsdesk.evaluation import (AIHubPricePredictor, CodexPricePredictor, EvaluationManager, aggregate_metrics,
                                evaluate_answer, evaluate_forecast, extract_forecast, extract_price, extract_public_analysis, read_json,
                                write_json, digest, input_market_context, _save_cli_events, _capture_trace, PUSH_NEWS_PROTOCOL,
                                STRATEGY_ID)
from newsdesk.env01_search import LocalIndicatorSearch, LocalNewsSearch
from time_gate_agent.local import LocalTimeGate, CURRENT_RETRIEVAL_POLICY


def article(identifier="good", **changes):
    value = {"id": identifier, "is_demo": False, "title": "原油供应公告",
             "body": "产油设施检修减少原油供应。", "assets": ["WTI", "Brent", "SC"],
             "published_at": "2026-10-20T08:00:00+08:00", "collected_at": "2026-10-28T02:00:00+00:00",
             "ingestion": {"published_date": "2026-10-20", "source_id": "fixture"},
             "agent_review": {"relevance": True}}
    value.update(changes)
    return value


def question(identifier="wti-case", family="WTI", current=70, mode="backtest"):
    return {"case_id": identifier, "family": family, "contract_id": family.lower() + "-main",
            "contract_code": "SC0" if family == "SC" else "CL=F", "currency": "CNY" if family == "SC" else "USD",
            "unit": "元 / 桶" if family == "SC" else "美元 / 桶", "input_date": "2026-10-20",
            "target_date": "2026-10-21", "current_price": current,
            "question": "当日收盘价为 {}，预测次日收盘方向、涨跌幅及日内高低点。".format(current),
            "news_cutoff_exclusive": "2026-10-21", "mode": mode, "series_type": "dominant_continuous"}


def forecast(return_pct=1, high=72, low=69):
    return {"direction": "up" if return_pct > 0 else "down" if return_pct < 0 else "flat",
            "return_pct": return_pct, "high": high, "low": low}


class LocalTimeGateTest(unittest.TestCase):
    def test_only_original_pre_input_dates_pass_and_unknowns_are_not_filled(self):
        rows = [article(), article("future", published_at="2026-10-21T12:00:00+00:00", ingestion={"published_date": "2026-10-21"}),
                article("unknown", published_at="", ingestion={"source_id": "fixture"}),
                article("demo", is_demo=True), article("excluded", agent_review={"relevance": False}),
                article("manual", ingestion={}), article("revision", source_modified_at="2026-10-22T00:00:00Z")]
        result = LocalTimeGate(rows).select(question())
        self.assertEqual([row["id"] for row in result["articles"]], ["good"])
        self.assertEqual(result["audit"]["rejected"], 6)
        self.assertFalse(result["audit"]["network_used"])
        self.assertEqual(result["audit"]["historical_version_unverified_count"], 1)

    def test_frozen_snapshot_and_calendar_precision_and_archive_updated_at(self):
        source = article(published_at="", updated_at="2026-10-29T00:00:00Z")
        rows = [source]
        gate = LocalTimeGate(rows)
        source["body"] = "mutated after freeze"
        selected = gate.select(question())["articles"]
        self.assertEqual(len(selected), 1)
        self.assertNotEqual(selected[0]["body"], source["body"])
        self.assertEqual(selected[0]["published_date"], "2026-10-20")

    def test_conflicting_dates_and_naive_timestamps_are_denied(self):
        rows = [article("conflict", ingestion={"published_date": "2026-10-19"}),
                article("naive", published_at="2026-10-20T08:00:00")]
        self.assertEqual(LocalTimeGate(rows).select(question())["articles"], [])

    def test_precise_publication_time_is_converted_to_contract_timezone(self):
        sc = dict(question("sc", "SC"), exchange_timezone="Asia/Shanghai")
        late = article("late", published_at="2026-10-20T23:59:00-04:00", ingestion={"published_date": "2026-10-20"})
        result = LocalTimeGate([late]).select(sc)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["audit"]["decisions"][0]["published_date"], "2026-10-20")
        self.assertEqual(result["audit"]["decisions"][0]["eligibility_date"], "2026-10-21")
        self.assertEqual(result["audit"]["decisions"][0]["reason"], "on_or_after_target_day")
        wti = dict(question(), exchange_timezone="America/New_York")
        early = article("early", published_at="2026-10-21T00:30:00+08:00", ingestion={"published_date": "2026-10-21"})
        result = LocalTimeGate([early]).select(wti)
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["articles"][0]["eligibility_date"], "2026-10-20")

    def test_date_only_news_keeps_source_calendar_date_without_invented_timestamp(self):
        sc = dict(question("sc", "SC"), exchange_timezone="Asia/Shanghai")
        row = article("daily", published_at="", ingestion={"published_date": "2026-10-21"})
        self.assertEqual(LocalTimeGate([row]).select(sc)["articles"], [])

    def test_selection_does_not_return_wrong_family_and_never_exceeds_limit(self):
        rows = [article(str(i)) for i in range(12)] + [article("other", assets=["SC"])]
        selected = LocalTimeGate(rows).select(question())
        self.assertEqual(len(selected["articles"]), 8)
        self.assertNotIn("other", [row["id"] for row in selected["articles"]])


class EvaluationAgentTest(unittest.TestCase):
    def test_visible_final_document_survives_receipt_validation_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_context(root)
            predictor = AIHubPricePredictor("hy3", api_key="fixture-key")
            answer = json.dumps(dict(forecast(), public_analysis={"analysis_document": "完整公开分析文档"}))
            responses = [
                ({"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "type": "function",
                    "function": {"name": "search_news", "arguments": '{"keywords":"原油"}'}}]}}]}, "trace"),
                ({"choices": [{"message": {"content": answer}}]}, "trace"),
            ]
            with mock.patch.object(predictor, "_request", side_effect=responses), \
                 mock.patch.object(predictor, "_write_trace", side_effect=ValueError("receipt validation failed")):
                with self.assertRaisesRegex(ValueError, "receipt validation"):
                    predictor.predict({"question": question()}, root / "model", context)
            trace = read_json(root / "model/conversation.partial.json")
            self.assertEqual(trace["conversation"][-1]["text"], answer)
            self.assertEqual(trace["public_analysis"]["analysis_document"], "完整公开分析文档")
            self.assertEqual(read_json(root / "model/visible-final-output.json")["text"], answer)

    def test_many_quota_rejections_are_audited_without_extra_news_execution(self):
        from newsdesk.evaluation import _search_receipts
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_context(root)
            work = root / "model"
            write_json(work / "search-context.json", context)
            search = LocalNewsSearch(context["case"], read_json(context["news_snapshot_path"]), work / "search")
            for _ in range(24):
                response = search.search("原油")
            self.assertEqual(search.summary()["execution_count"], 8)
            self.assertEqual(response["remaining_calls"], 0)
            self.assertFalse(response["error"]["retryable"])
            calls = _search_receipts(work)
            self.assertEqual(len(calls), 24)
            self.assertTrue(all(not call["response"]["articles"] for call in calls[8:]))

    def test_provider_internal_error_obeys_single_request_retry(self):
        predictor = AIHubPricePredictor("hy4-preview", api_key="fixture-key")
        error = RuntimeError("AIHub 返回错误（HTTP 500，3006）")
        with mock.patch.object(predictor, "_request_impl", side_effect=error) as request, \
             mock.patch("newsdesk.evaluation.time.sleep") as sleep:
            with self.assertRaises(RuntimeError):
                predictor._request({"model": predictor.model})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(5)

    def test_response_read_timeout_retries_same_request_without_restart(self):
        predictor = AIHubPricePredictor("glm-5.3", api_key="fixture-key")
        payload = {"model": "glm-5.3", "messages": []}
        with tempfile.TemporaryDirectory() as directory:
            predictor._request_context.work = Path(directory)
            with mock.patch.object(predictor, "_request_impl", side_effect=[TimeoutError("timed out"), ({"ok": True}, "trace")]) as request, \
                 mock.patch("newsdesk.evaluation.time.sleep") as sleep:
                self.assertEqual(predictor._request(payload), ({"ok": True}, "trace"))
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args_list[0], request.call_args_list[1])
            sleep.assert_called_once_with(5)
            error_path = Path(directory) / "api-errors.jsonl"
            self.assertEqual(error_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(error_path.read_text())["retry"], 0)

    def test_fenced_public_document_after_prose_keeps_all_text_and_rejects_ambiguity(self):
        document = "市场公开分析\n" * 4000
        answer = dict(forecast(), public_analysis={"analysis_document": document})
        block = "```json\n" + json.dumps(answer, ensure_ascii=False) + "\n```"
        output = "以下是分析与最终预测：\n" + block
        self.assertEqual(extract_public_analysis(output)["analysis_document"], document)
        self.assertEqual(extract_forecast(output), forecast())
        with self.assertRaises(ValueError):
            extract_forecast(block + "\n" + block)
        with self.assertRaises(ValueError):
            extract_forecast(output[:-20])

    def test_invalid_json_is_returned_to_model_for_bounded_format_correction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_context(root)
            predictor = AIHubPricePredictor("claude-haiku-5-5", api_key="fixture-key")
            responses = [
                {"choices": [{"message": {"content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "search_news", "arguments": '{"keywords":"原油"}'}}]}}]},
                {"choices": [{"message": {"content": '{"direction":"up", broken JSON'}}]},
                {"choices": [{"message": {"content": json.dumps(dict(forecast(), public_analysis={"analysis_document": "完整公开分析"}))}}]},
            ]
            payloads = []
            def request(payload, protocol="chat"):
                payloads.append(copy.deepcopy(payload))
                return responses[len(payloads) - 1], "trace"
            with mock.patch.object(predictor, "_request", side_effect=request):
                output = predictor.predict({"question": question()}, root / "model", context)
            self.assertEqual(extract_forecast(output), forecast())
            self.assertIn("Escape quotations", payloads[-1]["messages"][-1]["content"])
            self.assertNotIn("actual", payloads[-1]["messages"][-1]["content"])
            trace = read_json(root / "model/conversation.json")
            self.assertTrue(any(item.get("source") == "format_validation" for item in trace["conversation"]))
            self.assertTrue(any("broken JSON" in item.get("text", "") for item in trace["conversation"]))

    def test_aihub_gpt_uses_responses_function_call_loop_without_persisting_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            context = make_context(root)
            predictor = AIHubPricePredictor("gpt-6.1-sol", api_key="fixture-key")
            calls = []
            first = {"id": "resp-1", "output": [
                {"type": "reasoning", "encrypted_content": "opaque-secret"},
                {"type": "function_call", "call_id": "call-1", "name": "search_news",
                 "arguments": '{"keywords":"原油","limit":1}'}],
                "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14}}
            second = {"output": [{"type": "message", "content": [{"type": "output_text",
                "text": json.dumps(dict(forecast(), reason="公开依据", public_analysis={
                    "summary": "供给偏紧，需求边际稳定。", "steps": ["核对供应新闻", "比较反方需求风险"],
                    "supporting_evidence": [{"article_id": "n1", "signal": "检修", "impact": "偏多"}],
                    "contrary_evidence": ["需求走弱仍可能压制涨幅"], "uncertainty": ["库存数据尚未完全确认"],
                    "forecast_mapping": "供给偏紧支持方向向上，但不确定性限制幅度和高点。"}), ensure_ascii=False)}]}],
                "usage": {"input_tokens": 20, "output_tokens": 8, "total_tokens": 28}}
            def fake_request(payload, protocol="chat"):
                calls.append((payload, protocol))
                return (first if len(calls) == 1 else second), "trace"
            with mock.patch.object(predictor, "_request", side_effect=fake_request):
                raw = predictor.predict({"question": question()}, root / "model", search_context=context)
            self.assertEqual(extract_forecast(raw)["return_pct"], 1)
            self.assertEqual([protocol for _, protocol in calls], ["responses", "responses"])
            self.assertEqual(calls[0][0]["tools"][0]["type"], "function")
            self.assertNotIn("messages", calls[0][0])
            self.assertTrue(any(item.get("type") == "function_call_output" for item in calls[1][0]["input"]))
            self.assertEqual(calls[1][0]["previous_response_id"], "resp-1")
            transcript = (root / "model" / "conversation.json").read_text(encoding="utf-8")
            self.assertIn("tool_result", transcript)
            self.assertIn("public_analysis", transcript)
            self.assertNotIn("opaque-secret", transcript)

    def test_numeric_json_and_unambiguous_text_extraction(self):
        for raw in ({"predicted_price": 72.5, "reason": "供应变化"}, '{"predicted_price":72.5,"reason":""}',
                    '```json\n{"predicted_price":72.5}\n```', "预测价格：72.5 美元/桶", 72.5):
            self.assertEqual(extract_price(raw), 72.5)
        self.assertEqual(evaluate_answer({"predicted_price": -20}, -23)["absolute_error"], 3)

    def test_public_analysis_is_bounded_and_excludes_hidden_reasoning(self):
        raw = {"direction": "up", "return_pct": 1, "high": 72, "low": 69,
               "reason": "供应收紧", "public_analysis": {
                   "summary": "公开结论", "steps": ["先看供应", "再看需求"],
                   "analysis_document": "市场背景\n证据链\n风险与决策" * 1200,
                   "supporting_evidence": [{"article_id": "n1", "signal": "检修", "impact": "偏多"}],
                   "reasoning_content": "hidden should not be selected",
               }, "reasoning_content": "opaque hidden reasoning"}
        analysis = extract_public_analysis(raw)
        self.assertEqual(analysis["summary"], "公开结论")
        self.assertLessEqual(len(analysis["analysis_document"]), 18000)
        self.assertNotIn("reasoning_content", analysis)

    def test_ambiguous_missing_nonfinite_outputs_never_fall_back_to_current_price(self):
        for raw in ("今天70，明天72", {}, {"predicted_price": True}, {"predicted_price": "72"},
                    float("nan"), float("inf"), "not a price"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                extract_price(raw)
        with self.assertRaises(ValueError):
            evaluate_answer({"predicted_price": 1e308}, -1e308)

    def test_mae_is_per_family_and_only_complete_categories_are_rankable(self):
        questions = [question("w1"), question("w2"), question("s1", "SC", 500)]
        rows = [{"case_id": "w1", "status": "success", "direction_correct": True,
                 "absolute_errors": {"return_pct": 2, "high": 3, "low": 4}},
                {"case_id": "w2", "status": "failed", "absolute_errors": None},
                {"case_id": "s1", "status": "success", "direction_correct": False,
                 "absolute_errors": {"return_pct": 1, "high": 10, "low": 20}}]
        metrics = {row["family"]: row for row in aggregate_metrics(rows, questions)}
        self.assertEqual(metrics["WTI"]["direction_accuracy"], 1)
        self.assertEqual(metrics["WTI"]["return_pct_mae"], 2)
        self.assertNotIn("mae", metrics["WTI"])
        self.assertFalse(metrics["WTI"]["eligible_for_ranking"])
        self.assertEqual(metrics["SC"]["direction_accuracy"], 0)
        self.assertEqual(metrics["SC"]["high_mae"], 10)
        self.assertTrue(metrics["SC"]["eligible_for_ranking"])
        self.assertNotEqual(metrics["WTI"]["currency"], metrics["SC"]["currency"])

    def test_cli_disables_external_tools_and_uses_only_env01_mcp(self):
        owner = self
        class Process:
            returncode = 0
            def __init__(self, command, **kwargs):
                self.command = command
                self.result_path = Path(command[command.index("--output-last-message") + 1])
            def communicate(self, prompt, timeout):
                owner.assertIn("actively call env01.search_news", prompt)
                owner.assertNotIn("news-snapshot.json", prompt)
                context = read_json(self.result_path.parent / "search-context.json")
                events, _ = search_events(context, self.result_path.parent)
                self.result_path.write_text(json.dumps(dict(forecast(), reason="local input only")))
                return "\n".join(json.dumps(event) for event in events), ""
        with tempfile.TemporaryDirectory() as directory, mock.patch("newsdesk.evaluation.subprocess.Popen", side_effect=Process) as popen:
            context = make_context(Path(directory))
            client = CodexPricePredictor(executable="/fixture/codex")
            output = client.predict({"question": question()}, Path(directory) / "model", search_context=context)
            command = popen.call_args.args[0]
            for setting in ('--ignore-user-config', '--ephemeral', 'web_search="disabled"',
                            'features.shell_tool=false', 'features.plugins=false',
                            'mcp_servers.env01.enabled_tools=["search_news"]',
                            'mcp_servers.env01.required=true', 'model_reasoning_summary="detailed"'):
                self.assertIn(setting, command)
            self.assertEqual(extract_forecast(output)["return_pct"], 1)
            trace = read_json(Path(directory) / "model" / "conversation.json")
            self.assertEqual(trace["reasoning_capture"], "public_summary_only")
            self.assertEqual(trace["search_call_count"], 1)
            self.assertEqual(trace["news_count"], 1)
            self.assertEqual(trace["conversation"][0]["type"], "user_message")
            self.assertNotIn("news_snapshot_path", trace["conversation"][0]["text"])

    def test_claimed_search_without_real_mcp_receipt_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            _save_cli_events(work, [{"type": "item.completed", "item": {"id": "text", "type": "agent_message",
                             "text": "I searched local news"}}, {"type": "turn.completed"}])
            with self.assertRaisesRegex(RuntimeError, "至少一次"):
                _capture_trace(work, dict(forecast(), reason="claimed search"))
            trace = read_json(work / "conversation.json")
            self.assertEqual(trace["reasoning_summary_status"], "none_available")

    def test_exact_response_binding_forbidden_tools_and_safe_public_summary(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "model"; work.mkdir()
            context = make_context(Path(directory))
            write_json(work / "search-context.json", context)
            events, response = search_events(context, work)
            events.insert(0, {"type": "item.completed", "encrypted_content": "opaque-secret",
                             "authorization": "Bearer credential", "item": {"id": "r", "type": "reasoning",
                             "text": "供给新闻提供了公开依据。", "hidden_reasoning": "never public"}})
            _save_cli_events(work, events)
            trace = _capture_trace(work, dict(forecast(), reason="fixture"))
            self.assertEqual(trace["visible_reasoning_summary_count"], 1)
            public = (work / "cli-events.jsonl").read_text()
            self.assertNotIn("opaque-secret", public)
            self.assertNotIn("credential", public)
            self.assertNotIn("never public", public)
            self.assertEqual([row for row in trace["conversation"] if row["type"] == "tool_result"][0]["result"], response)
            completed = next(event for event in events if event.get("item", {}).get("type") == "mcp_tool_call" and event["type"] == "item.completed")
            completed["item"]["result"]["content"][0]["text"] = json.dumps(dict(response, returned_count=99))
            _save_cli_events(work, events)
            with self.assertRaisesRegex(RuntimeError, "不匹配"):
                _capture_trace(work, forecast())
            completed["item"]["server"] = "external"
            _save_cli_events(work, events)
            with self.assertRaisesRegex(RuntimeError, "禁止"):
                _capture_trace(work, forecast())

    def test_zero_result_search_counts_but_receipt_tampering_does_not(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory) / "model"; work.mkdir()
            context = make_context(Path(directory))
            write_json(work / "search-context.json", context)
            events, _ = search_events(context, work, keywords="无匹配特殊字符串")
            _save_cli_events(work, events)
            trace = _capture_trace(work, forecast())
            self.assertEqual((trace["search_call_count"], trace["news_count"]), (1, 0))
            receipt = read_json(work / "search" / "summary.json")["receipts"][0]
            Path(receipt["files"]["response"]["path"]).write_text('{"tampered":true}')
            with self.assertRaisesRegex(RuntimeError, "凭证"):
                _capture_trace(work, forecast())

    def test_denied_recall_content_stays_out_of_model_visible_conversation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); work = root / "model"; work.mkdir()
            rows = [article(), article("future-private", body="拒绝新闻正文秘密", published_at="2026-10-22T10:00:00+08:00",
                    ingestion={"published_date": "2026-10-22"})]
            snapshot = root / "snapshot.json"; write_json(snapshot, rows)
            context = {"case": question(), "news_snapshot_path": str(snapshot), "news_snapshot_sha256": digest(rows)}
            write_json(work / "search-context.json", context)
            events, _ = search_events(context, work)
            _save_cli_events(work, events)
            trace = _capture_trace(work, forecast())
            self.assertNotIn("拒绝新闻正文秘密", json.dumps(trace, ensure_ascii=False))
            self.assertNotIn("future-private", json.dumps(trace))
            self.assertEqual(trace["search_calls"][0]["time_gate"]["rejected"], 1)
            self.assertEqual(trace["search_calls"][0]["time_gate"]["news_cutoff_exclusive"], "2026-10-21")

    def test_four_field_validation_and_percentage_point_errors(self):
        output = forecast(1.2, 72, 69)
        actual = forecast(2.4, 73, 68)
        result = evaluate_forecast(output, actual, current_price=70)
        self.assertTrue(result["direction_correct"])
        self.assertEqual(result["absolute_errors"], {"return_pct": 1.2, "high": 1.0, "low": 1.0})
        self.assertEqual(extract_forecast('```json\n'+json.dumps(output)+'\n```'), output)
        for bad in ({"direction": "up"}, dict(output, direction="down"), dict(output, high=60),
                    dict(output, low=float("nan")), dict(output, return_pct=True), "1.2 72 69"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                extract_forecast(bad, 70)
        with self.assertRaises(ValueError):
            extract_forecast(forecast(20, 72, 69), 70)

    def test_flat_direction_is_scored_explicitly_and_pending_has_no_zero_scores(self):
        result = evaluate_forecast(forecast(0, 71, 69), forecast(0, 72, 68), 70)
        self.assertTrue(result["direction_correct"])
        pending = [{"case_id": "wti-case", "status": "predicted_pending"}]
        metric = aggregate_metrics(pending, [question(mode="live")])[0]
        self.assertEqual(metric["pending"], 1)
        self.assertIsNone(metric["direction_accuracy"])
        self.assertIsNone(metric["return_pct_mae"])
        self.assertFalse(metric["eligible_for_ranking"])


def make_context(root, case=None):
    snapshot = Path(root) / "news-snapshot.json"
    write_json(snapshot, [article()])
    return {"case": case or question(), "news_snapshot_path": str(snapshot.resolve()),
            "news_snapshot_sha256": digest([article()])}


def search_events(context, work, keywords="原油"):
    work = Path(work); work.mkdir(parents=True, exist_ok=True)
    search = LocalNewsSearch(context["case"], read_json(context["news_snapshot_path"]), work / "search")
    response = search.search(keywords)
    item = {"id": "search-1", "type": "mcp_tool_call", "server": "env01", "tool": "search_news",
            "arguments": {"keywords": keywords}, "status": "in_progress"}
    completed = dict(item, status="completed", result={"content": [{"type": "text", "text": json.dumps(response, ensure_ascii=False)}]}, error=None)
    return [{"type": "item.started", "item": item}, {"type": "item.completed", "item": completed},
            {"type": "turn.completed"}], response


class FakeBenchmark:
    def __init__(self, mode="backtest"):
        self.mode = mode
        self.questions = [question("w1", mode=mode), question("s1", "SC", 500, mode=mode)]
        for row in self.questions:
            row["contract_code"] = "SC0" if row["family"] == "SC" else "CL.1:NYM"
            row["exchange_timezone"] = "Asia/Shanghai" if row["family"] == "SC" else "America/New_York"
        self.truth = {"w1": forecast(2, 73, 68), "s1": forecast(2, 512, 497)}
        self.labels = copy.deepcopy(self.truth) if mode == "backtest" else {}
        self.settlement_calls = 0
        self.publish_truth = False
        if mode == "live":
            for row in self.questions:
                zone = ZoneInfo("Asia/Shanghai" if row["family"] == "SC" else "America/New_York")
                today = datetime.now(zone).date()
                row["input_date"] = today.isoformat()
                row["target_date"] = (today + timedelta(days=1)).isoformat()
                row["news_cutoff_exclusive"] = row["target_date"]
                row["input_is_today"] = True
                row["forecast_deadline"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                row["live_status"] = "ready"
    def load_dataset(self, dataset_id=None, mode="backtest"):
        contracts = {"WTI": {"familyId": "WTI", "code": "CL.1:NYM", "exchange": "NYMEX"},
                     "Brent": {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"},
                     "SC": {"familyId": "SC", "code": "SC0", "exchange": "INE"}}
        fingerprints = {}
        for row in self.questions:
            previous = datetime.fromisoformat(row["input_date"]) - timedelta(days=1)
            while previous.weekday() >= 5:
                previous -= timedelta(days=1)
            bars = [{"trading_date": day, "open": row["current_price"], "close": row["current_price"],
                     "high": row["current_price"] + 2, "low": row["current_price"] - 2, "volume": 100}
                    for day in (previous.date().isoformat(), row["input_date"])]
            fingerprints[row["family"]] = {"contract": contracts[row["family"]], "history_bars": bars}
        return {"manifest": {"dataset_id": "dataset-fixture", "sha256": "a" * 64,
                             "mode": self.mode, "series_type": "dominant_continuous",
                             "source_fingerprints": fingerprints},
                "questions": copy.deepcopy(self.questions), "labels": copy.deepcopy(self.labels)}
    def settle_live(self, dataset_id=None, refresh=True):
        self.settlement_calls += 1
        if self.publish_truth:
            self.labels = copy.deepcopy(self.truth)
        return {"status": "settled" if self.labels else "pending"}


class FakeNews:
    def __init__(self):
        self.items = [article()]
    def all(self):
        return copy.deepcopy(self.items)


class FakePredictor:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
    def predict(self, material, work, search_context=None):
        self.calls.append(copy.deepcopy(material))
        events, _ = search_events(search_context, work)
        if search_context.get("indicator_snapshot_path"):
            search = LocalIndicatorSearch(search_context["case"], read_json(search_context["indicator_snapshot_path"]),
                                          Path(work) / "indicators")
            if search_context["case"].get("retrieval_time_policy"):
                arguments = {"indicator": "price", "start_time": "2026-01-01T00:00:00Z"}
            else:
                arguments = {"indicator": "price", "start_date": "2026-01-01",
                             "end_date": search_context["case"]["input_date"]}
            response = search.call(arguments)
            item = {"id": "indicator-1", "type": "mcp_tool_call", "server": "env01", "tool": "search_indicators",
                    "arguments": arguments, "status": "in_progress"}
            completed = dict(item, status="completed", result={"content": [{"type": "text",
                             "text": json.dumps(response, ensure_ascii=False)}]}, error=None)
            events[-1:-1] = [{"type": "item.started", "item": item}, {"type": "item.completed", "item": completed}]
        _save_cli_events(work, events)
        key = material["question"]["case_id"]
        if key == self.fail:
            return "invalid output"
        return dict(forecast(1, 72, 69) if key == "w1" else forecast(1, 508, 495), reason="fixture")


_MANAGER_FIXTURE_TIME = datetime(2026, 11, 2, 8, tzinfo=timezone.utc)


class FixtureDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _MANAGER_FIXTURE_TIME.astimezone(tz) if tz is not None else _MANAGER_FIXTURE_TIME.replace(tzinfo=None)


def write_legacy_v2_run(root, benchmark, status="queued"):
    """Persist a historical fixture with its original inputs and prediction lock."""
    dataset = benchmark.load_dataset(mode=benchmark.mode)
    dataset["questions"] = dataset["questions"][:1]
    case = dataset["questions"][0]
    dataset["labels"] = {key: value for key, value in dataset["labels"].items() if key == case["case_id"]}
    stamp = datetime.now(timezone.utc).isoformat()
    run_id = "legacy-v2-fixture"
    path = Path(root) / "runs" / run_id
    run = {"run_id": run_id, "dataset_id": "dataset-fixture", "dataset_snapshot_sha256": digest(dataset),
           "strategy_id": STRATEGY_ID, "environment_id": "env01", "mode": benchmark.mode,
           "family": None, "protocol": PUSH_NEWS_PROTOCOL, "status": status,
           "series_type": "dominant_continuous", "started_at": stamp, "total": 1,
           "completed": 0, "succeeded": 0, "failed": 0, "pending": 0, "metrics": []}
    write_json(path / "dataset.json", dataset)
    write_json(path / "run.json", run)
    if status == "awaiting_settlement":
        output = dict(forecast(), reason="historical fixed-news prediction")
        material = {"environment_id": "env01", "question": case, "local_news": [article()], "network_search": False}
        lock = {"case_id": case["case_id"], "submitted_at": stamp, "prediction_locked_at": stamp,
                "predicted": forecast(), "input_sha256": digest(material), "raw_output_sha256": digest(output)}
        attempt = "attempt-original"
        write_json(path / "cases" / case["case_id"] / attempt / "input.json", material)
        write_json(path / "cases" / case["case_id"] / attempt / "prediction-lock.json", lock)
        write_json(path / "cases" / case["case_id"] / "result.json",
                   dict(case, status="predicted_pending", attempt=attempt, predicted=forecast(), raw_output=output,
                        prediction_lock_sha256=digest(lock)))
    return run


class EvaluationManagerTest(unittest.TestCase):
    def setUp(self):
        # Fixed verified 2026 trading dates keep these clock-sensitive fixtures
        # independent of weekends, holidays and the machine's current year.
        for target in (__name__ + ".datetime", "newsdesk.evaluation.datetime"):
            patch = mock.patch(target, FixtureDateTime)
            patch.start()
            self.addCleanup(patch.stop)

    def test_historical_case_time_annotation_is_read_only_and_uses_frozen_venue(self):
        case = dict(question("brent-history", "Brent"), contract_code="IB.1:IEU",
                    input_date="2026-10-29", target_date="2026-10-30")
        dataset = {"manifest": {"source_fingerprints": {"Brent": {"contract": {
            "familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}}}}, "questions": [case]}
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, FakeBenchmark(), FakeNews())
            root = Path(directory) / "runs" / "fixture"
            write_json(root / "run.json", {"run_id": "fixture", "dataset_snapshot_sha256": digest(dataset)})
            write_json(root / "dataset.json", dataset)
            result_path = root / "cases" / case["case_id"] / "result.json"
            write_json(result_path, dict(case, predicted=forecast(), status="completed"))
            files = [root / "run.json", root / "dataset.json", result_path]
            before = [path.read_bytes() for path in files]
            shown = manager.get_run("fixture")["cases"][0]
            self.assertNotIn("time_windows", shown)
            self.assertEqual(shown["question"], case["question"])
            self.assertEqual(shown["predicted"], forecast())
            windows = shown["display_time_windows"]
            self.assertTrue(windows["display_only"])
            self.assertEqual(windows["forecast"]["start_utc"], "2026-10-30T00:00:00Z")
            self.assertEqual(windows["forecast"]["end_utc"], "2026-10-30T22:00:00Z")
            self.assertEqual([path.read_bytes() for path in files], before)

    def test_new_case_passes_frozen_time_windows_to_predictor_without_labels(self):
        from newsdesk.benchmark import display_time_windows
        benchmark, predictor = FakeBenchmark(), FakePredictor()
        for case in benchmark.questions:
            windows = display_time_windows(case)
            windows.pop("display_only", None)
            case["time_windows"] = windows
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = manager.start(workers=1)
            final = manager.wait(row["run_id"], timeout=5)
            self.assertEqual(final["succeeded"], 2)
            for material in predictor.calls:
                public = material["question"]
                self.assertEqual(public["retrieval_time_policy"], CURRENT_RETRIEVAL_POLICY)
                self.assertEqual(public["time_windows"], next(case["time_windows"] for case in benchmark.questions
                                 if case["case_id"] == public["case_id"]))
                self.assertNotIn("actual", public)
                self.assertNotIn("labels", public)

    def test_input_market_uses_previous_trading_close_and_verified_snapshot(self):
        dataset = {"manifest": {"source_fingerprints": {"WTI": {"history_bars": [
            {"trading_date": "2026-09-18", "close": 100},
            {"trading_date": "2026-09-21", "close": 105},
            {"trading_date": "2026-09-22", "close": 999},
        ]}}}, "questions": [{"case_id": "case-1", "family": "WTI", "input_date": "2026-09-21",
            "target_date": "2026-09-22", "current_bar": {"open": 102, "close": 105, "high": 108, "low": 99}}]}
        reference = input_market_context(dataset)["case-1"]
        self.assertEqual(reference["date"], "2026-09-21")
        self.assertEqual(reference["previous_date"], "2026-09-18")
        self.assertEqual(reference["return_pct"], 5)
        self.assertEqual(reference["open"], 102)
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, FakeBenchmark(), FakeNews())
            root = Path(directory) / "runs" / "fixture"
            write_json(root / "run.json", {"run_id": "fixture", "dataset_snapshot_sha256": digest(dataset)})
            write_json(root / "dataset.json", dataset)
            before = (root / "dataset.json").read_bytes()
            self.assertEqual(manager.get_run("fixture")["input_market"]["case-1"], reference)
            self.assertEqual((root / "dataset.json").read_bytes(), before)
            dataset["questions"][0]["current_bar"]["close"] = 1
            write_json(root / "dataset.json", dataset)
            self.assertEqual(manager.get_run("fixture")["input_market"], {})
            self.assertIn("input_market_error", manager.get_run("fixture"))
        for previous_close in (None, 0):
            dataset["manifest"]["source_fingerprints"]["WTI"]["history_bars"][0]["close"] = previous_close
            self.assertIsNone(input_market_context(dataset)["case-1"]["return_pct"])

    def test_persistent_run_scores_without_sending_labels_and_snapshot_is_frozen(self):
        with tempfile.TemporaryDirectory() as directory:
            news, predictor = FakeNews(), FakePredictor()
            manager = EvaluationManager(directory, FakeBenchmark(), news, predictor=predictor)
            row = manager.start(workers=2)
            news.items[0]["body"] = "modified archive"
            final = manager.wait(row["run_id"], timeout=5)
            self.assertEqual(final["status"], "completed")
            self.assertEqual((final["succeeded"], final["failed"]), (2, 0))
            self.assertEqual({item["family"]: item["return_pct_mae"] for item in final["metrics"]}, {"WTI": 1.0, "SC": 1.0})
            self.assertEqual(len(predictor.calls), 2)
            for material in predictor.calls:
                self.assertNotIn("actual_price", json.dumps(material))
                self.assertNotIn("labels", json.dumps(material))
                self.assertFalse(material["network_search"])
                self.assertNotIn("local_news", material)
                self.assertEqual(material["search_tool"]["name"], "env01.search_news")
            self.assertEqual(final["search_calls"], 2)
            self.assertEqual(final["search_used_cases"], 2)
            self.assertEqual(final["indicator_calls"], 2)
            self.assertEqual(final["indicator_used_cases"], 2)
            trace = manager.get_case_trace(row["run_id"], "w1")
            returned = next(event for event in trace["conversation"] if event["type"] == "tool_result")
            self.assertNotEqual(returned["result"]["articles"][0]["body"], "modified archive")
            self.assertNotIn("labels", json.dumps(trace))
            reloaded = EvaluationManager(directory, FakeBenchmark(), news)
            self.assertEqual(reloaded.get_run(row["run_id"])["status"], "completed")
            self.assertEqual(len(reloaded.runs()), 1)

    def test_resume_retries_only_failed_cases_and_preserves_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            first = FakePredictor(fail="s1")
            manager = EvaluationManager(directory, FakeBenchmark(), FakeNews(), predictor=first)
            row = manager.start(workers=1)
            result = manager.wait(row["run_id"], timeout=5)
            self.assertEqual(result["status"], "partial")
            second = FakePredictor()
            resumed = EvaluationManager(directory, FakeBenchmark(), FakeNews(), predictor=second)
            resumed.resume(row["run_id"], workers=1, retry_failed=True)
            result = resumed.wait(row["run_id"], timeout=5)
            self.assertEqual(result["status"], "completed")
            self.assertEqual([item["question"]["case_id"] for item in second.calls], ["s1"])

    def test_active_foreign_manager_reads_do_not_mark_running_as_interrupted(self):
        waiting = threading.Event()
        release = threading.Event()
        class WaitingPredictor(FakePredictor):
            def predict(self, material, work, search_context=None):
                waiting.set()
                release.wait(3)
                return super().predict(material, work, search_context=search_context)
        with tempfile.TemporaryDirectory() as directory:
            first = EvaluationManager(directory, FakeBenchmark(), FakeNews(), predictor=WaitingPredictor())
            row = first.start(workers=1)
            self.assertTrue(waiting.wait(2))
            second = EvaluationManager(directory, FakeBenchmark(), FakeNews(), predictor=FakePredictor())
            try:
                self.assertEqual(second.get_run(row["run_id"])["status"], "running")
                self.assertEqual(second.resume(row["run_id"])["status"], "running")
                self.assertFalse(second._threads)
            finally:
                release.set()
            self.assertEqual(first.wait(row["run_id"], 5)["status"], "completed")

    def test_live_prediction_is_frozen_then_settled_without_another_model_call(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = manager.start(mode="live", workers=2)
            final = manager.wait(row["run_id"], timeout=5)
            self.assertEqual(final["status"], "awaiting_settlement")
            self.assertEqual((final["completed"], final["succeeded"], final["pending"]), (2, 0, 2))
            self.assertTrue(all(item["status"] == "predicted_pending" for item in final["cases"]))
            for metric in final["metrics"]:
                self.assertIsNone(metric["direction_accuracy"])
                self.assertIsNone(metric["high_mae"])
            still_waiting = manager.settle(row["run_id"], refresh=False)
            self.assertEqual(still_waiting["status"], "awaiting_settlement")
            benchmark.publish_truth = True
            settled = manager.settle(row["run_id"], refresh=False)
            self.assertEqual(settled["status"], "completed")
            self.assertEqual((settled["succeeded"], settled["pending"]), (2, 0))
            self.assertEqual(len(predictor.calls), 2)
            self.assertTrue(all(item["direction_correct"] for item in settled["cases"]))

    def test_v2_live_locked_outputs_can_settle_without_protocol_relabeling(self):
        benchmark = FakeBenchmark("live")
        with tempfile.TemporaryDirectory() as directory:
            predictor = FakePredictor()
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = write_legacy_v2_run(directory, benchmark, status="awaiting_settlement")
            dataset_path = Path(directory) / "runs" / row["run_id"] / "dataset.json"
            frozen_bytes = dataset_path.read_bytes()
            frozen = read_json(dataset_path)
            self.assertTrue(all("retrieval_time_policy" not in case for case in frozen["questions"]))
            benchmark.publish_truth = True
            final = manager.settle(row["run_id"], refresh=False)
            self.assertEqual(final["status"], "completed")
            self.assertEqual(final["protocol"], PUSH_NEWS_PROTOCOL)
            self.assertTrue(all(item["protocol"] == PUSH_NEWS_PROTOCOL for item in final["metrics"]))
            self.assertEqual(dataset_path.read_bytes(), frozen_bytes)
            self.assertEqual(predictor.calls, [])

    def test_daily_settlement_passes_only_the_supplied_snapshots_without_model_calls(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = manager.start(mode="live", workers=2)
            manager.wait(row["run_id"], timeout=5)
            calls = len(predictor.calls)
            snapshots = {"SC": {"source_stale": False}}
            with mock.patch.object(benchmark, "settle_live", return_value={}) as settle:
                result = manager.settle(row["run_id"], refresh=False, snapshots=snapshots)
            settle.assert_called_once_with(dataset_id="dataset-fixture", refresh=False, snapshots=snapshots)
            self.assertEqual(result["status"], "awaiting_settlement")
            self.assertEqual(len(predictor.calls), calls)

    def test_trace_endpoint_rejects_path_escape_and_private_file_pointer(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, FakeBenchmark(), FakeNews(), predictor=FakePredictor())
            row = manager.start(workers=1)
            result = manager.wait(row["run_id"], 5)
            for invalid in ("..", "../w1", "/w1", "x/y"):
                with self.subTest(case_id=invalid), self.assertRaises(ValueError):
                    manager.get_case_trace(row["run_id"], invalid)
            result_path = Path(directory) / "runs" / row["run_id"] / "cases" / "w1" / "result.json"
            case = read_json(result_path); case["trace_file"] = "dataset.json"; write_json(result_path, case)
            with self.assertRaises(ValueError):
                manager.get_case_trace(row["run_id"], "w1")
            self.assertEqual(result["reasoning_capture"], "public_summary_only")

    def test_v2_queued_run_is_not_resumed_as_new_active_search_protocol(self):
        with tempfile.TemporaryDirectory() as directory:
            benchmark = FakeBenchmark()
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=FakePredictor())
            row = write_legacy_v2_run(directory, benchmark)
            with mock.patch.object(manager, "_launch"):
                second = manager.start(workers=1)
            self.assertNotEqual(second["run_id"], row["run_id"])
            self.assertNotEqual(second["protocol"], PUSH_NEWS_PROTOCOL)
            with self.assertRaisesRegex(ValueError, "旧协议"):
                manager.resume(row["run_id"])

    def test_live_late_or_stale_cases_never_call_the_model(self):
        for kind in ("late", "stale", "already_settled"):
            benchmark, predictor = FakeBenchmark("live"), FakePredictor()
            if kind == "late":
                benchmark.questions[0]["forecast_deadline"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
                benchmark.questions[0]["time_windows"] = {"timezone": "UTC", "status": "ready", "forecast": {
                    "start_utc": (datetime.now(timezone.utc) + timedelta(minutes=29)).isoformat()}}
            elif kind == "stale":
                benchmark.questions[0]["live_status"] = "waiting_new_completed_bar"
            else:
                benchmark.labels = copy.deepcopy(benchmark.truth)
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
                with self.assertRaises(ValueError):
                    manager.start(mode="live", family="WTI")
                self.assertEqual(predictor.calls, [])

    def test_global_live_submits_only_ready_family_and_records_others_unavailable(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        wti = benchmark.questions[0]
        wti.update(live_status="waiting_new_completed_bar", input_is_today=False)
        brent = dict(wti, case_id="b1", family="Brent", contract_id="brent-main", contract_code="IB.1:IEU")
        benchmark.questions.append(brent)
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = manager.start(mode="live", workers=2)
            final = manager.wait(row["run_id"], 5)
            self.assertEqual(final["status"], "awaiting_settlement")
            self.assertEqual((final["total"], final["requested_total"], final["pending"]), (1, 3, 1))
            self.assertEqual(final["unavailable_families"], ["Brent", "WTI"])
            self.assertEqual(len(final["skipped_families"]), 2)
            self.assertEqual([item["question"]["family"] for item in predictor.calls], ["SC"])
            self.assertEqual([item["family"] for item in final["cases"]], ["SC"])
            self.assertEqual([item["family"] for item in final["metrics"]], ["SC"])

    def test_global_live_with_zero_ready_families_refuses_without_model_submission(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        for row in benchmark.questions:
            row["input_is_today"] = False
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            with self.assertRaisesRegex(ValueError, "没有可提交"):
                manager.start(mode="live")
            self.assertEqual(predictor.calls, [])

    def test_frozen_ready_flag_cannot_allow_a_previous_input_date(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        benchmark.questions[0]["input_date"] = "2026-10-20"
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            with self.assertRaisesRegex(ValueError, "不是.*今天"):
                manager.start(mode="live", family="WTI")
            self.assertEqual(predictor.calls, [])

    def test_tampered_live_prediction_lock_fails_before_reading_any_future_label(self):
        benchmark, predictor = FakeBenchmark("live"), FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            row = manager.start(mode="live", family="WTI", workers=1)
            result = manager.wait(row["run_id"], 5)["cases"][0]
            lock = Path(directory) / "runs" / row["run_id"] / "cases" / "w1" / result["attempt"] / "prediction-lock.json"
            value = read_json(lock);value["predicted"]["return_pct"] = 50;lock.write_text(json.dumps(value))
            benchmark.publish_truth = True
            settled = manager.settle(row["run_id"], refresh=False)
            self.assertEqual(settled["failed"], 1)
            self.assertEqual(benchmark.settlement_calls, 0)
            self.assertFalse(settled["metrics"][0]["eligible_for_ranking"])

    def test_live_model_finishing_after_deadline_cannot_be_scored(self):
        benchmark = FakeBenchmark("live")
        deadline = datetime.now(timezone.utc) + timedelta(hours=1)
        benchmark.questions[0]["forecast_deadline"] = deadline.isoformat()
        from newsdesk.benchmark import display_time_windows, live_submission_deadline
        windows = display_time_windows(benchmark.questions[0], benchmark.load_dataset()["manifest"])
        benchmark.questions[0]["time_windows"] = windows
        deadline = live_submission_deadline(benchmark.questions[0])
        # Advance only the post-response lock time, not the wall clock that
        # validates the initial submission.
        predictor = FakePredictor()
        with tempfile.TemporaryDirectory() as directory:
            manager = EvaluationManager(directory, benchmark, FakeNews(), predictor=predictor)
            original = predictor.predict
            def late(material, work, search_context=None):
                output = original(material, work, search_context=search_context)
                self.clock_patch = mock.patch("newsdesk.evaluation.now", return_value=(deadline + timedelta(seconds=1)).isoformat())
                self.clock_patch.start()
                return output
            predictor.predict = late
            try:
                row = manager.start(mode="live", family="WTI", workers=1)
                final = manager.wait(row["run_id"], 5)
                self.assertEqual(final["failed"], 1)
                self.assertEqual(final["succeeded"], 0)
                self.assertIn("截止时间", final["cases"][0]["error"])
            finally:
                self.clock_patch.stop()
