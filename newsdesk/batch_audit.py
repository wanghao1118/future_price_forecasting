"""Verify saved multi-model benchmark documents, receipts, dates and scores."""

import argparse
import json
from pathlib import Path

from newsdesk.evaluation import (DEFAULT_ROOT, _indicator_receipts, _search_receipts,
                                EVALUATION_PROTOCOL, V4_EVALUATION_PROTOCOL, LEGACY_ACTIVE_PROTOCOL, PUBLIC_CASE_FIELDS,
                                ENV03_PROTOCOL, ENV04_PROTOCOL, skill_snapshot,
                                aggregate_metrics, digest, evaluate_forecast,
                                now, read_json, write_json, case_protocol, retrieval_metadata, validate_run_retrieval_policy,
                                structured_output)
from newsdesk.benchmark import project_evaluator_question


from newsdesk.environment_policy import ENV00_PROTOCOL, ENV02_PROTOCOL, NEWS_SEARCH_ENVIRONMENTS, allowed_tools, is_news_environment

AUDIT_VERSION = "frozen-retrieval-boundary-replay-v4-env02"


def _audit_no_tools_input(work, question, result, trace, model):
    """Bind the closed-book input and transcript without invoking a search gate."""
    from newsdesk.env02 import cli_settings, instruction, validate_response_no_tools, validate_cli_no_tools
    context = read_json(work / "search-context.json")
    context_case = context.get("case") if isinstance(context, dict) else None
    expected_fields = context_case.keys() if isinstance(context_case, dict) else PUBLIC_CASE_FIELDS
    public = {key: question[key] for key in expected_fields
              if key in question and key not in ("retrieval_time_policy", "news_cutoff_exclusive")}
    assert context == {"environment_id": "env02", "case": public}, "env02 context contains extra data or interfaces"
    material = read_json(work.parent / "input.json")
    assert material == {"environment_id": "env02", "question": public,
                        "news_policy": "disabled", "network_search": False}, "env02 model input differs from frozen question"
    assert digest(material) == result["input_sha256"], "env02 input hash differs"
    prompt = read_json(work / "prompt.json")
    text = prompt.get("text")
    assert isinstance(text, str) and digest(text) == prompt.get("sha256"), "env02 prompt hash differs"
    assert text == instruction(material), "env02 prompt does not bind model input"
    assert any(event.get("type") == "user_message" and event.get("text") == text
               and event.get("sha256") == prompt["sha256"] for event in trace["conversation"]), "env02 transcript prompt differs"
    assert trace.get("environment_id") == "env02" and trace.get("protocol") == ENV02_PROTOCOL
    assert trace.get("tool_mode") == "closed_book_no_tools"
    assert not (work / "indicator-snapshot.json").exists(), "env02 contains an indicator interface snapshot"
    assert not list((work / "search").glob("**/receipt.json")), "env02 contains news receipts"
    assert not list((work / "indicators").glob("**/receipt.json")), "env02 contains indicator receipts"
    for key in ("news_count", "technical_count", "search_call_count", "indicator_call_count", "tool_call_count"):
        assert trace.get(key, 0) == 0, "env02 reports retrieval activity"
    assert trace.get("tools", []) == [] and trace.get("network_search", False) is False, "env02 reports enabled interfaces"
    for key in ("news_count", "technical_count", "search_calls", "indicator_calls"):
        assert result.get(key, 0) == 0, "env02 result reports retrieval activity"
    assert not trace.get("search_calls") and not trace.get("indicator_calls"), "env02 reports tool receipts"
    permitted = {"question_context", "user_message", "assistant_message", "visible_reasoning_summary", "public_analysis", "final_answer"}
    assert all(event.get("type") in permitted for event in trace["conversation"]), "env02 transcript contains a tool or unknown event"
    finals = [event for event in trace["conversation"] if event.get("type") == "final_answer"]
    assert len(finals) == 1 and finals[0].get("text") == result["raw_output"], "env02 final answer differs"
    if (work / "api-requests.jsonl").exists():
        requests = [json.loads(line) for line in (work / "api-requests.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        responses = [json.loads(line) for line in (work / "api-responses.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        assert requests and len(requests) == len(responses), "env02 missing API request or response evidence"
        assert trace.get("api_requests") == len(requests), "env02 API request count differs"
        transcript_messages = [{"role": "user" if event["type"] == "user_message" else "assistant", "content": event["text"]}
                               for event in trace["conversation"] if event["type"] in ("user_message", "assistant_message")]
        for request, response in zip(requests, responses):
            payload, protocol = request["payload"], request["protocol"]
            assert protocol in ("chat", "responses") and response["protocol"] == protocol
            assert digest(payload) == request["sha256"], "env02 API request hash differs"
            allowed_payload = {"model", "tools", "tool_choice", "stream"} | (
                {"input", "max_output_tokens", "store", "reasoning"} if protocol == "responses" else
                {"messages", "max_tokens", "max_completion_tokens", "reasoning_effort"})
            assert not set(payload) - allowed_payload and payload.get("model") == model, "env02 API exposes extra capabilities or changes model"
            assert payload.get("tools") == [] and payload.get("tool_choice") == "none", "env02 API advertises tools"
            assert payload.get("stream") is False and payload.get("store", False) is False
            messages = payload["input" if protocol == "responses" else "messages"]
            assert messages and messages[0] == {"role": "user", "content": text}, "env02 API prompt differs"
            assert len(messages) % 2 == 1 and messages == transcript_messages[:len(messages)], "env02 API history differs from transcript"
            assert not payload.get("previous_response_id"), "env02 API reuses external conversation"
            assert digest(response["response"]) == response["sha256"], "env02 API response hash differs"
            validate_response_no_tools(response["response"])
        final_response = responses[-1]["response"]
        if responses[-1]["protocol"] == "responses":
            from newsdesk.evaluation import AIHubPricePredictor
            output = AIHubPricePredictor._responses_text(final_response)
        else:
            from newsdesk.evaluation import AIHubPricePredictor
            output = AIHubPricePredictor._assistant_message(final_response)[1]
        assert output == result["raw_output"], "env02 saved API answer differs"
    else:
        runtime = read_json(work / "runtime-config.json")
        settings, command = runtime["settings"], runtime["command"]
        mandatory_settings = cli_settings()
        assert all(key in settings and type(settings[key]) is type(value) and settings[key] == value
                   for key, value in mandatory_settings.items()), "env02 CLI is missing mandatory safety settings"
        options = {}
        for index, argument in enumerate(command):
            assert argument != "--config" and not argument.startswith("--config="), "env02 CLI contains an unrecorded config override"
            assert not (argument.startswith("-c") and argument != "-c"), "env02 CLI contains an unrecorded config override"
            if argument != "-c":
                continue
            assert index + 1 < len(command) and "=" in command[index + 1], "env02 CLI config is malformed"
            key, value = command[index + 1].split("=", 1)
            assert key not in options, "env02 CLI contains duplicate config overrides"
            options[key] = json.loads(value)
        assert options == settings, "env02 CLI command differs from recorded settings"
        assert settings.get("mcp_servers") == {} and settings.get("web_search") == "disabled", "env02 CLI enables retrieval"
        assert settings.get("features.skip_host_skill_discovery") is True, "env02 CLI discovers external skills"
        assert all(value is False for key, value in settings.items() if key.startswith("features.")
                   and key != "features.skip_host_skill_discovery"), "env02 CLI enables tools"
        assert all(flag in command for flag in ("--ignore-user-config", "--ignore-rules", "--ephemeral")), "env02 CLI inherits external context"
        assert command[command.index("--model") + 1] == model, "env02 CLI model differs"
        assert not any("mcp_servers." in arg for arg in command), "env02 CLI registers a tool server"
        assert runtime["prompt_sha256"] == prompt["sha256"]
        assert runtime["schema_sha256"] == digest(read_json(work / "schema.json"))
        events = [json.loads(line) for line in (work / "cli-events.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
        validate_cli_no_tools(events)
        assert any(event.get("type") == "turn.completed" for event in events), "env02 CLI turn is incomplete"
        assert (work / "raw-output.json").read_text(encoding="utf-8") == result["raw_output"], "env02 CLI output file differs"
        native_messages, completed_messages, seen = [], [], set()
        for event in events:
            item = event.get("item", {})
            if item.get("type") != "agent_message" or event.get("type") not in ("item.updated", "item.completed"):
                continue
            text_value = item.get("text")
            if not isinstance(text_value, str) or not text_value:
                continue
            identifier = item.get("id") or digest(item)
            if event["type"] == "item.completed":
                completed_messages.append(text_value)
            if (identifier, text_value) not in seen:
                seen.add((identifier, text_value))
                native_messages.append({"type": "assistant_message", "text": text_value, "event_id": identifier})
        visible_messages = [{key: event.get(key) for key in ("type", "text", "event_id")}
                            for event in trace["conversation"] if event.get("type") == "assistant_message"]
        assert native_messages == visible_messages, "env02 CLI transcript differs from native messages"
        assert completed_messages and structured_output(completed_messages[-1]) == structured_output(result["raw_output"]), "env02 CLI native final answer differs"


def audit_run(root, run_id, baseline):
    path = root / "runs" / run_id
    run, dataset = read_json(path / "run.json"), read_json(path / "dataset.json")
    assert run.get("protocol", LEGACY_ACTIVE_PROTOCOL) == baseline.get("protocol", LEGACY_ACTIVE_PROTOCOL), "mixed evaluation protocols"
    validate_run_retrieval_policy(run, dataset)
    environment = run.get("environment_id", "env01")
    assert environment == baseline.get("environment_id", "env01"), "mixed environments"
    if environment in ("env00", "env02"):
        assert read_json(path / "news-snapshot.json") == [], environment + " contains news"
    if environment in NEWS_SEARCH_ENVIRONMENTS:
        skill = skill_snapshot(environment)
        assert (path / "skill.md").is_file() == (skill is not None), environment + " skill snapshot missing"
        if skill:
            assert digest((path / "skill.md").read_text(encoding="utf-8")) == run.get("skill_sha256"), environment + " skill hash mismatch"
    if environment == "env02":
        assert read_json(path / "technical-snapshot.json") == [], "env02 contains technical indicators"
    for filename, key in (("dataset.json", "dataset_snapshot_sha256"),
                          ("news-snapshot.json", "news_snapshot_sha256"),
                          ("technical-snapshot.json", "technical_snapshot_sha256")):
        assert digest(read_json(path / filename)) == run[key] == baseline[key], filename
    audit_path = path / "run-validation.json"
    audit = read_json(audit_path) if audit_path.exists() else {"run_id": run_id, "cases": {}}
    questions = {question["case_id"]: question for question in dataset["questions"]}
    technical_snapshot = {entry["case_id"]: entry for entry in read_json(path / "technical-snapshot.json")}
    results = []
    for result_path in sorted(path.glob("cases/*/result.json")):
        result = read_json(result_path)
        results.append(result)
        if result["status"] != "success":
            continue
        fingerprint = digest(result)
        cached = audit["cases"].get(result["case_id"], {})
        # UTC protocols replay saved source/receipt content even when the result file
        # is unchanged; a response could have been modified independently.
        if (run.get("protocol") not in (V4_EVALUATION_PROTOCOL, EVALUATION_PROTOCOL, ENV00_PROTOCOL, ENV02_PROTOCOL, ENV03_PROTOCOL, ENV04_PROTOCOL) and cached.get("result_sha256") == fingerprint
                and cached.get("audit_version") == AUDIT_VERSION):
            continue
        trace_path = path / result["trace_file"]
        trace = read_json(trace_path)
        question = questions[result["case_id"]]
        if run.get("protocol") in (EVALUATION_PROTOCOL, ENV00_PROTOCOL, ENV02_PROTOCOL, ENV03_PROTOCOL, ENV04_PROTOCOL):
            question = project_evaluator_question(question)
        boundary = {} if environment == "env02" else retrieval_metadata(question)
        if environment == "env02":
            _audit_no_tools_input(trace_path.parent, question, result, trace, run["model"])
        if boundary:
            context = read_json(trace_path.parent / "search-context.json")
            assert context["case"] == {key: question[key] for key in PUBLIC_CASE_FIELDS if key in question}
            if is_news_environment(environment):
                assert Path(context["news_snapshot_path"]).resolve() == (path / "news-snapshot.json").resolve()
                assert context["news_snapshot_sha256"] == run["news_snapshot_sha256"]
            else:
                assert context.get("environment_id") == "env00"
                assert "news_snapshot_path" not in context and "news_snapshot_sha256" not in context
            expected_rows = technical_snapshot[result["case_id"]]["rows"]
            assert context["indicator_snapshot_sha256"] == digest(expected_rows)
            assert digest(read_json(context["indicator_snapshot_path"])) == digest(expected_rows)
            assert trace["protocol"] == run["protocol"] == case_protocol(question, environment)
            for key, value in boundary.items():
                assert trace.get(key) == value and result.get(key) == value
        news, technical = _search_receipts(trace_path.parent), _indicator_receipts(trace_path.parent)
        first = trace["conversation"][0]
        assert first["type"] == "question_context"
        assert first["question"] == result["question"] and first["target_date"] == result["target_date"]
        assert not trace["validation_errors"]
        if environment == "env02":
            assert not news and not technical, "env02 contains search receipts"
        else:
            assert technical
        if is_news_environment(environment):
            assert news
        else:
            assert not news and trace.get("news_count", 0) == 0
            for event in trace["conversation"]:
                if event["type"] in ("tool_call", "tool_result"):
                    assert event.get("server") == environment and event.get("tool") in allowed_tools(run)
        if not boundary:
            for call in technical:
                for row in call["response"].get("rows", []):
                    assert "2026-01-01" <= row["date"] <= result["input_date"] < result["target_date"]
            for call in news:
                for article in call["response"].get("articles", []):
                    assert article["eligibility_date"] <= result["input_date"] < result["target_date"]
        for tool, calls in (("search_news", news), ("search_indicators", technical)):
            assert sum(c["type"] == "tool_result" and c.get("tool") == tool
                       for c in trace["conversation"]) == len(calls)
        if environment == "env00":
            returned = [event["result"] for event in trace["conversation"]
                        if event["type"] == "tool_result" and event.get("tool") == "search_indicators"]
            assert sorted(digest(value) for value in returned) == sorted(digest(call["response"]) for call in technical), "transcript differs from actual indicator returns"
        analysis = trace.get("public_analysis") or {}
        document = next((analysis[key] for key in ("analysis_document", "decision_document", "long_analysis", "reasoning_report")
                         if isinstance(analysis.get(key), str) and analysis[key].strip()), "")
        assert document, "Missing analysis document: " + result["case_id"]
        score = evaluate_forecast(result["predicted"], dataset["labels"][result["case_id"]], result["current_price"])
        assert score["direction_correct"] == result["direction_correct"]
        assert score["absolute_errors"] == result["absolute_errors"]
        lock = read_json(trace_path.parent.parent / "prediction-lock.json")
        assert digest(lock) == result["prediction_lock_sha256"]
        assert lock["predicted"] == result["predicted"] and lock["raw_output_sha256"] == digest(result["raw_output"])
        if environment == "env02":
            assert lock["input_sha256"] == result["input_sha256"], "env02 prediction lock input differs"
        audit["cases"][result["case_id"]] = {
            "result_sha256": fingerprint, "family": result["family"], "news_calls": len(news),
            "indicator_calls": len(technical), "analysis_chars": len(document), "date_gate": "not_applicable" if environment == "env02" else "passed",
            "receipt_integrity": "passed", "scoring": "passed", "trace_sha256": digest(trace),
            "prediction_lock": "passed"}
        audit["cases"][result["case_id"]].update(audit_version=AUDIT_VERSION,
            protocol=run.get("protocol", LEGACY_ACTIVE_PROTOCOL), **boundary)
    result_ids = [result["case_id"] for result in results]
    assert len(set(result_ids)) == len(result_ids) and set(result_ids) <= set(questions), "unexpected or duplicate results"
    succeeded = sum(result["status"] == "success" for result in results)
    failed = sum(result["status"] == "failed" for result in results)
    missing = len(questions) - len(results)
    successful_ids = {result["case_id"] for result in results if result["status"] == "success"}
    audit["cases"] = {case_id: value for case_id, value in audit["cases"].items() if case_id in successful_ids}
    audit.update(audit_version=AUDIT_VERSION, protocol=run.get("protocol", LEGACY_ACTIVE_PROTOCOL),
                 expected_cases=run["total"], verified_cases=len(audit["cases"]), checked_at=now(),
                 succeeded=succeeded, failed=failed, missing=missing,
                 complete=len(audit["cases"]) == run["total"])
    # Active runners may have committed another result after the run.json read.
    # Compare aggregates only once the process has finished writing the run.
    if run["status"] in ("completed", "partial", "failed"):
        assert run["total"] == len(questions), "run total differs from frozen questions"
        for key, value in (("succeeded", succeeded), ("failed", failed), ("completed", len(results))):
            assert run.get(key, value) == value, "run count mismatch: " + key
        assert aggregate_metrics(results, dataset["questions"], run["protocol"]) == run["metrics"], "aggregate scoring mismatch"
    write_json(audit_path, audit)
    return {"model": run["model"], "run_id": run_id, "verified": audit["verified_cases"],
            "total": run["total"], "succeeded": succeeded, "failed": failed, "missing": missing,
            "complete": audit["complete"], "status": run["status"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    batch = read_json(args.batch)
    baseline = read_json(args.root / "runs" / (batch.get("snapshot_run_id") or batch["baseline_run_id"]) / "run.json")
    rows = [audit_run(args.root, rid, baseline) for rid in batch["models"].values()]
    summary = {"batch_id": batch["batch_id"], "checked_at": now(), "models": rows,
               "complete": all(row["complete"] for row in rows)}
    write_json(args.batch.with_name(args.batch.stem + "-validation.json"), summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
