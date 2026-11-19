"""Independently reconcile memory scores with question-only requests and CLI verdicts."""

import argparse
from collections import Counter
import json
from pathlib import Path

from newsdesk.memory_evaluation import MemoryEvaluationManager, read_json, write_json, now
from newsdesk.memory_model import JUDGE_INSTRUCTION
from newsdesk.evaluation import AIHubPricePredictor
from time_gate_agent.closed_book import ClosedBookGate, canonical, sha256, CLOSED_BOOK_INSTRUCTION


def _inside(root, relative):
    path = (root / relative).resolve()
    if root not in path.parents:
        raise ValueError("审计材料路径越界")
    return path


def verify_case(root, run, question, case):
    directory = root / "runs" / run["run_id"] / "cases" / case["case_id"]
    prediction = read_json(directory / "prediction.json")
    if case["model_answer"] != prediction["answer"]:
        raise ValueError("页面答案与锁定回答不一致")
    matched_prediction = None
    for path in sorted((directory / "attempts").glob("prediction-*")):
        receipt_path = path / "gate-receipt.json"
        if not receipt_path.is_file():
            continue
        receipt = read_json(receipt_path)
        if receipt.get("passed") and receipt.get("response_sha256") == prediction["response_sha256"]:
            matched_prediction = path
    if matched_prediction is None:
        raise ValueError("找不到锁定回答对应的实际权限回执")
    prompt = read_json(matched_prediction / "prompt.json")
    if prompt != {"instruction": CLOSED_BOOK_INSTRUCTION, "question": question["question"]}:
        raise ValueError("模型输入超出同一固定指令与题干")
    if sha256(prompt) != prediction["prompt_sha256"]:
        raise ValueError("模型题干指纹不一致")
    payload = read_json(matched_prediction / "request.json")
    config = read_json(matched_prediction / "generation-config.json")
    gate = ClosedBookGate(question["question"])
    gate.validate_payload(payload, config["api_protocol"], run["model"])
    receipt = read_json(matched_prediction / "gate-receipt.json")
    if gate.request_sha256 != receipt["request_sha256"]:
        raise ValueError("实际请求指纹与回执不一致")
    raw = (matched_prediction / "raw-output.txt").read_text(encoding="utf-8")
    if sha256(raw) != prediction["response_sha256"] or raw.strip() != prediction["answer"]:
        raise ValueError("原始回答与锁定回答不一致")
    visible = read_json(matched_prediction / "response.json")
    gate.validate_response(visible)
    visible_answer = (AIHubPricePredictor._responses_text(visible) if config["api_protocol"] == "responses"
                      else AIHubPricePredictor._assistant_message(visible)[1])
    if visible_answer != raw:
        raise ValueError("供应商可见响应与原始回答不一致")
    if any(receipt.get(key) != 0 for key in ("tool_call_count", "search_call_count", "indicator_call_count")):
        raise ValueError("闭卷执行出现了工具或检索")
    if receipt.get("tool_calls") != [] or receipt.get("denied_attempts"):
        raise ValueError("闭卷执行的工具审计未通过")

    verdict = read_json(directory / "verdict.json")
    judge = _inside(root, verdict["work"])
    material = read_json(judge / "evaluator-request.json")
    if material.get("instruction") != JUDGE_INSTRUCTION:
        raise ValueError("判分指令与冻结协议不一致")
    expected_item = {"case_id": question["case_id"], "question": question["question"],
                     "answer": question["answer"], "answer_aliases": question["answer_aliases"],
                     "model_answer": prediction["answer"]}
    items = [item for item in material["items"] if item["case_id"] == case["case_id"]]
    if items != [expected_item]:
        raise ValueError("evaluator 输入与冻结题、答案或锁定回答不一致")
    result = read_json(judge / "response.json")
    accepted = read_json(judge / "verdicts.json")
    matched = [item for item in accepted if item["case_id"] == case["case_id"]]
    if len(matched) != 1 or matched[0] not in result["verdicts"]:
        raise ValueError("判分不是实际 CLI 返回的唯一结果")
    if type(verdict["correct"]) is not bool or any(verdict[key] != matched[0][key] for key in ("case_id", "correct", "reason")):
        raise ValueError("锁定判分与实际 CLI 判分不一致")
    if case["correct"] != verdict["correct"] or case["reason"] != verdict["reason"]:
        raise ValueError("页面逐题判分不一致")
    judge_receipt = read_json(judge / "gate-receipt.json")
    runtime = read_json(judge / "runtime-config.json")
    stdout = (judge / "stdout.jsonl").read_text(encoding="utf-8")
    stderr = (judge / "stderr.txt").read_text(encoding="utf-8")
    expected_hashes = {"prompt_sha256": sha256(canonical(material)), "schema_sha256": sha256(read_json(judge / "schema.json")),
                       "runtime_config_sha256": sha256(runtime), "stdout_sha256": sha256(stdout),
                       "stderr_sha256": sha256(stderr), "response_sha256": sha256(result), "verdicts_sha256": sha256(accepted)}
    if not judge_receipt.get("passed") or judge_receipt.get("evaluator") != "codex_cli" or not judge_receipt.get("independent"):
        raise ValueError("缺少独立 CLI 判分审计")
    if any(judge_receipt.get(key) != value for key, value in expected_hashes.items()):
        raise ValueError("实际 CLI 判分材料指纹不一致")
    settings = runtime["settings"]
    if settings.get("web_search") != "disabled" or settings.get("mcp_servers") != {}:
        raise ValueError("evaluator 开放了搜索或 MCP")
    events = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    ClosedBookGate("Audit: ____").validate_cli_events(events)
    if not any(event.get("type") == "turn.completed" for event in events):
        raise ValueError("CLI 未实际完成判分")
    return True


def audit_experiment(root, experiment_id="Exp00-extend"):
    root = Path(root).resolve()
    manager = MemoryEvaluationManager(root)
    batch = read_json(root / "experiments" / experiment_id / "batch.json")
    manager._verify(batch)
    dataset = read_json(root / "experiments" / experiment_id / "reference-dataset.json")
    questions = {q["case_id"]: q for q in dataset["questions"]}
    records, all_errors = [], []
    for model, run_id in batch["models"].items():
        run = manager.get_run(run_id)
        verified, errors, counts, graded_months = 0, [], Counter(), Counter()
        for case in run["cases"]:
            if case["status"] != "graded":
                continue
            try:
                verify_case(root, run, questions[case["case_id"]], case)
                verified += 1
                counts["correct"] += int(case["correct"])
                counts[case["month"]] += int(case["correct"])
                graded_months[case["month"]] += 1
            except (OSError, ValueError, KeyError, TypeError) as error:
                errors.append({"case_id": case["case_id"], "error": str(error)})
        overall = run["metrics"]["overall"]
        if not errors and (overall["graded"] != verified or overall["correct"] != counts["correct"]):
            errors.append({"error": "总准确率计数未对齐"})
        if not errors and overall["accuracy"] != counts["correct"] / len(questions):
            errors.append({"error": "总准确率分母未对齐"})
        for month in run["metrics"]["months"]:
            expected_total = sum(q["month"] == month["month"] for q in questions.values())
            resolved = sum(q["month"] == month["month"] and q["status"] in {"graded", "prediction_failed", "judge_failed", "tool_violation"} for q in run["cases"])
            if not errors and (month["correct"] != counts[month["month"]]
                               or month["graded"] != graded_months[month["month"]]
                               or month["total"] != expected_total
                               or month["accuracy"] != counts[month["month"]] / expected_total
                               or month["completion"] != resolved / expected_total):
                errors.append({"error": "分月准确率计数或固定分母未对齐", "month": month["month"]})
        record = {"run_id": run_id, "model": model, "total": len(questions), "graded": overall["graded"],
                  "verified": verified, "correct": counts["correct"], "accuracy": overall["accuracy"],
                  "complete": verified == len(questions) and not errors, "errors": errors}
        records.append(record)
        all_errors.extend({"model": model, **error} for error in errors)
        write_json(root / "runs" / run_id / "audit.json", record)
    report = {"experiment_id": experiment_id, "dataset_id": batch["dataset_id"], "audited_at": now(),
              "expected_total": batch["expected_total"], "verified": sum(r["verified"] for r in records),
              "complete": all(r["complete"] for r in records), "runs": records, "errors": all_errors}
    write_json(root / "experiments" / experiment_id / "audit.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description="核对闭卷输入、零检索回执、真实CLI判分和准确率")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent / "data" / "memory-evaluations")
    parser.add_argument("--experiment", default="Exp00-extend")
    args = parser.parse_args()
    report = audit_experiment(args.root, args.experiment)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
