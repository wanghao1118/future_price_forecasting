"""Sequential recursive self-improvement for the Exp06 search skill.

The learner runs one GPT-6.1-Sol forecast at a time on the frozen June/July
daily learning set.  After each forecast it reveals the settled next-day
answer, asks the same model to inspect the current skill's missing boundary
conditions, and replaces the skill with the returned full Markdown document.
The final skill is then copied into ``search-strategy/s02/SKILL.md`` and can be
used as the frozen skill for the normal Exp06 v2 benchmark batch.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

from newsdesk.benchmark import display_time_windows
from newsdesk.environment_policy import ENV04_PROTOCOL, ENV04_TOOL_MODE
from newsdesk.evaluation import (
    AIHubPricePredictor,
    EvaluationManager,
    REASONING_CAPTURE,
    RETRIEVAL_TIME_POLICY,
    aggregate_metrics,
    digest,
    evaluate_forecast,
    extract_forecast,
    now,
    technical_snapshot,
    write_json,
)
from newsdesk.server import NewsStore
from time_gate_agent.local import LocalTimeGate


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MARKET_ROOT = PROJECT_ROOT / "newsdesk" / "data" / "market"
SKILL_ROOT = PROJECT_ROOT / "search-strategy"
EVALUATION_ROOT = PROJECT_ROOT / "newsdesk" / "data" / "evaluations"
LEARNING_FAMILIES = ("WTI", "Brent", "SC")
MARKET_FILES = {"WTI": "wti-202702-1d.json", "Brent": "brent-202702-1d.json", "SC": "sc-202702-1d.json"}


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _contract_source(family):
    payload = json.loads((MARKET_ROOT / MARKET_FILES[family]).read_text(encoding="utf-8"))
    normalized = payload["normalized"]
    return normalized["contract"], normalized["bars"], normalized.get("source", {})


def _question_text(family, contract, current, target, windows):
    known = windows.get("known", {})
    forecast = windows.get("forecast", {})
    return (
        f"已知 UTC+0 {known.get('label', current['trading_date'])} 对应的{family} "
        f"{contract.get('code')}完整日线：开盘 {current['open']}、最高 {current['high']}、"
        f"最低 {current['low']}、收盘 {current['close']} {contract.get('unit')}。请预测 "
        f"UTC+0 {forecast.get('label', target['trading_date'])} 下一交易日日线相对上述收盘价的 "
        "涨跌方向（up/down/flat）、涨跌幅（百分数，1.2 表示 1.2%）、当日最高价及最低价，"
        "返回这四项具体结果。只能使用输入日收盘时或之前的信息。"
    )


def build_learning_set():
    """Build the 130 chronological June/July cases and host indicator archive."""
    questions = []
    labels = {}
    sources = {}
    for family in LEARNING_FAMILIES:
        contract, bars, source = _contract_source(family)
        bars = sorted([dict(row) for row in bars], key=lambda row: row.get("trading_date", ""))
        sources[family] = {"contract": contract, "history_bars": bars, "source": source}
        for index, current in enumerate(bars[:-1]):
            input_date = str(current.get("trading_date", ""))
            if not ("2026-06-01" <= input_date <= "2026-07-31"):
                continue
            target = bars[index + 1]
            target_date = str(target.get("trading_date", ""))
            current_close = float(current["close"])
            target_close = float(target["close"])
            return_pct = (target_close / current_close - 1.0) * 100.0
            direction = "up" if target_close > current_close else "down" if target_close < current_close else "flat"
            case_id = f"learning__{family.lower()}-202702__{input_date}__{target_date}"
            question = {
                "case_id": case_id,
                "continuous_method": "provider_fixed_contract_daily",
                "contract_code": contract.get("code"),
                "contract_id": contract.get("id"),
                "currency": contract.get("currency"),
                "current_bar": {key: current.get(key) for key in ("open", "high", "low", "close")},
                "current_price": current_close,
                "exchange": contract.get("exchange"),
                "exchange_timezone": (current.get("session") or {}).get("exchange_timezone", "UTC"),
                "family": family,
                "input_date": input_date,
                "mode": "backtest",
                "news_cutoff_exclusive": target_date,
                "price_basis": "provider_fixed_contract_unadjusted_daily_ohlc",
                "question": "",
                "series_type": "fixed_contract_daily",
                "target_date": target_date,
                "unit": contract.get("unit"),
            }
            windows = display_time_windows(question, {"source_fingerprints": sources})
            if not isinstance(windows, dict):
                raise ValueError(f"无法构造交易时段：{case_id}")
            windows.pop("display_only", None)
            question["time_windows"] = windows
            # Some historical exchange holidays have no verified forecast
            # session. Keep those cases in the learning set and use the
            # legacy date-bounded local gate instead of inventing a precise
            # UTC session boundary.
            if windows.get("forecast", {}).get("status") == "regular_schedule":
                question["retrieval_time_policy"] = RETRIEVAL_TIME_POLICY
            question["question"] = _question_text(family, contract, current, target, windows)
            questions.append(question)
            labels[case_id] = {
                "direction": direction,
                "return_pct": return_pct,
                "high": float(target["high"]),
                "low": float(target["low"]),
                "close": target_close,
                "target_date": target_date,
            }
    questions.sort(key=lambda row: (row["input_date"], LEARNING_FAMILIES.index(row["family"])))
    if len(questions) != 130:
        raise ValueError(f"学习集题数应为 130，实际为 {len(questions)}")
    manifest = {
        "dataset_id": "recursive-s02-learning-20261110",
        "profile": "recursive_self_improvement_v1",
        "mode": "backtest",
        "series_type": "fixed_contract_daily",
        "learning_window": {"start": "2026-06-01", "end": "2026-07-31", "frequency": "1d"},
        "expected_questions": len(questions),
        "families": [{"id": family, "count": sum(row["family"] == family for row in questions)}
                     for family in LEARNING_FAMILIES],
        "source_fingerprints": sources,
    }
    indicators = technical_snapshot(manifest, questions)
    return manifest, questions, labels, indicators


def _strip_json_fence(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(text):
    text = _strip_json_fence(text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start:end + 1])
        raise


def _trace_summary(trace):
    result = {
        "search_calls": [],
        "indicator_calls": [],
        "public_analysis": trace.get("public_analysis"),
    }
    for call in trace.get("search_calls", []):
        result["search_calls"].append({
            "query": call.get("query"),
            "returned_article_ids": call.get("returned_article_ids", []),
            "returned_count": call.get("returned_count"),
        })
    for call in trace.get("indicator_calls", []):
        result["indicator_calls"].append({
            "indicator": call.get("indicator"),
            "start_time": call.get("start_time"),
            "end_time": call.get("end_time"),
            "returned_count": call.get("returned_count"),
        })
    return result


def reflect_skill(predictor, current_skill, question, result, trace, actual, work):
    """Ask GPT-6.1-Sol for a complete next skill version after one settled case."""
    work = Path(work)
    work.mkdir(parents=True, exist_ok=True, mode=0o700)
    prediction = result.get("predicted")
    errors = result.get("absolute_errors")
    case_packet = {
        "case_id": question["case_id"],
        "family": question["family"],
        "input_date": question["input_date"],
        "target_date": question["target_date"],
        "question": question["question"],
        "current_bar": question["current_bar"],
        "prediction": prediction,
        "actual_answer": actual,
        "direction_correct": result.get("direction_correct"),
        "absolute_errors": errors,
        "trace": _trace_summary(trace),
    }
    prompt = (
        "你正在执行一个递归自我改进的搜索策略学习步骤。当前 s02 skill 最初复制自 s01，"
        "现在已经用当前版本完成了一个 2026 年 6—7 月日频 case，并且答案已经结算。\n\n"
        "请完成三件事：\n"
        "1. 判断当前 skill 在这个 case 中哪些地方有效、哪些地方不足；如果答对，也要检查是否只是偶然，"
        "以及哪些边界条件、反例、数据缺失和停止条件还没有写清楚。\n"
        "2. 根据可观察证据修改 skill。修改必须是可迁移的搜索规则，不得记忆本题的日期、价格、新闻 ID、"
        "实际答案或任何只对本题成立的结论。\n"
        "3. 返回下一版本的完整 Markdown skill 文档，保留有效内容并加入必要的边界条件。文档应能直接给下一题的模型使用，"
        "长度控制在 4,000—18,000 个字符。\n\n"
        "要求：不要输出隐藏思维链；只输出公开的事实、规则、反证、未知项、停止条件和决策流程。"
        "不要把本题答案写进规则。即使预测失败，也要区分模型执行失败与策略判断失败。"
        "必须保留主动搜索、时间边界、事实/推断/反证/未知项分离，以及最终结构化预测约束。\n\n"
        "当前 skill：\n--- BEGIN CURRENT SKILL ---\n" + current_skill +
        "\n--- END CURRENT SKILL ---\n\n本题与结算结果：\n" + _canonical(case_packet) +
        "\n\n只返回一个 JSON 对象，字段为：\n"
        "{\"reflection_summary\":\"本次反思摘要\",\"changes\":[\"改动1\",\"改动2\"],"
        "\"skill_markdown\":\"完整的新 Markdown skill 文档\"}"
    )
    write_json(work / "prompt.json", {"role": "user", "text": prompt, "sha256": digest(prompt)})
    predictor._request_context.work = work
    predictor._request_context.deadline = None
    payload = {
        "model": predictor.model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "response_format": {"type": "json_object"},
        "max_completion_tokens": min(predictor.max_output_tokens, 16000),
    }
    try:
        data, trace_id = predictor._request(payload, protocol="chat")
    except RuntimeError as error:
        # Some AIHub gateways expose JSON mode under a different capability
        # flag. Retry the same reflection prompt without response_format; the
        # parser below still rejects anything that is not one JSON object.
        if "HTTP 400" not in str(error):
            raise
        payload.pop("response_format", None)
        data, trace_id = predictor._request(payload, protocol="chat")
    message, content, _calls = predictor._assistant_message(data)
    write_json(work / "response.json", {
        "trace_id": trace_id,
        "content": content,
        "usage": data.get("usage", {}),
        "model": data.get("model"),
    })
    parsed = _parse_json(content)
    skill = parsed.get("skill_markdown") if isinstance(parsed, dict) else None
    if not isinstance(skill, str) or not (4000 <= len(skill) <= 30000):
        raise ValueError("反思返回的 skill_markdown 长度无效")
    if "search" not in skill.lower() or "counter" not in skill.lower():
        raise ValueError("反思返回的 skill 缺少搜索或反证规则")
    if question["case_id"] in skill or question["input_date"] in skill or question["target_date"] in skill:
        raise ValueError("反思返回的 skill 泄漏了本题标识或日期")
    return parsed


def _write_status(path, run, questions, cases, index=None, phase="learning"):
    good = [row for row in cases if row.get("status") == "success"]
    failed = [row for row in cases if row.get("status") == "failed"]
    run.update(status="running", phase=phase, completed=len(good) + len(failed), succeeded=len(good),
               failed=len(failed), total=len(questions), updated_at=now(),
               learning_index=index, metrics=aggregate_metrics(good + failed, questions, protocol=ENV04_PROTOCOL))
    write_json(path / "run.json", run)
    write_json(path / "status.json", {
        "phase": phase, "completed": len(good) + len(failed), "succeeded": len(good), "failed": len(failed),
        "total": len(questions), "learning_index": index, "skill_sha256": run.get("skill_sha256"),
        "updated_at": run["updated_at"],
    })


def run_learning(artifact_root, resume=None, model="gpt-6.1-sol", case_retries=2):
    artifact_root = Path(artifact_root).resolve()
    artifact_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    manifest, questions, labels, indicators = build_learning_set()
    initial_skill_path = SKILL_ROOT / "s01" / "SKILL.md"
    if resume:
        run_path = Path(resume).resolve()
        run = json.loads((run_path / "run.json").read_text(encoding="utf-8"))
        skill = (run_path / "skill.md").read_text(encoding="utf-8")
        # A resumed learner must replay the exact snapshot used by the
        # original run. The live SQLite archive can change while learning is
        # in progress and would invalidate every existing receipt.
        news = json.loads((run_path / "news-snapshot.json").read_text(encoding="utf-8"))
    else:
        news = NewsStore(PROJECT_ROOT / "newsdesk" / "data" / "news.sqlite3", seed_demo=False).all()
        run_id = "recursive-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
        run_path = artifact_root / "runs" / run_id
        run_path.mkdir(parents=True, mode=0o700)
        skill = initial_skill_path.read_text(encoding="utf-8")
        shutil.copy2(initial_skill_path, artifact_root / "initial-skill.md")
        payload = {"manifest": manifest, "questions": questions, "labels": labels}
        write_json(run_path / "dataset.json", payload)
        write_json(run_path / "news-snapshot.json", news)
        write_json(run_path / "technical-snapshot.json", indicators)
        (run_path / "skill.md").write_text(skill, encoding="utf-8")
        run = {
            "run_id": run_id, "experiment_id": "Exp06", "experiment_name": "Exp06",
            "strategy_id": "recursive-s02-learning", "strategy_name": "s02 recursive self-improvement",
            "environment_id": "env04", "protocol": ENV04_PROTOCOL, "tool_mode": ENV04_TOOL_MODE,
            "reasoning_capture": REASONING_CAPTURE, "model": model, "runner": "AIHub Chat Completions",
            "dataset_id": manifest["dataset_id"], "total": len(questions), "requested_total": len(questions),
            "started_at": now(), "phase": "learning", "status": "queued", "completed": 0, "succeeded": 0,
            "failed": 0, "skill_id": "s02", "skill_sha256": digest(skill),
            "retrieval_time_policy": RETRIEVAL_TIME_POLICY, "retrieval_timezone": "UTC",
            "dataset_snapshot_sha256": digest(payload), "news_snapshot_sha256": digest(news),
            "technical_snapshot_sha256": digest(indicators),
        }
        write_json(run_path / "run.json", run)
        write_json(artifact_root / "learning-manifest.json", manifest)
    (run_path / "skill.md").write_text(skill, encoding="utf-8")
    predictor = AIHubPricePredictor(model, timeout=300, gateway_timeout=240, max_output_tokens=16000, max_rounds=18)
    manager = EvaluationManager(run_path.parent.parent, None, None)
    gate = LocalTimeGate(news)
    cases_dir = run_path / "cases"
    version_dir = run_path / "skill-versions"
    reflection_dir = run_path / "reflections"
    version_dir.mkdir(exist_ok=True, mode=0o700)
    reflection_dir.mkdir(exist_ok=True, mode=0o700)
    existing = {}
    for result_path in cases_dir.glob("*/result.json"):
        try:
            existing[result_path.parent.name] = json.loads(result_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    cases = list(existing.values())
    start_index = int(run.get("learning_index") or 0)
    for index, question in enumerate(questions):
        if index < start_index:
            continue
        actual = labels[question["case_id"]]
        result = manager._case(run_path, question, actual, gate, predictor)
        cases = [row for row in cases if row.get("case_id") != question["case_id"]] + [result]
        trace = {}
        if result.get("trace_file"):
            trace = json.loads((run_path / result["trace_file"]).read_text(encoding="utf-8"))
        reflection_record = {"index": index, "case_id": question["case_id"], "result_status": result.get("status"),
                             "prediction": result.get("predicted"), "actual": actual,
                             "direction_correct": result.get("direction_correct"), "skill_before_sha256": digest(skill)}
        try:
            if result.get("status") == "success":
                reflection = reflect_skill(predictor, skill, question, result, trace, actual,
                                           reflection_dir / f"{index:03d}-{question['case_id']}")
                new_skill = reflection["skill_markdown"]
                reflection_record.update({"status": "updated", "reflection_summary": reflection.get("reflection_summary"),
                                          "changes": reflection.get("changes", []), "skill_after_sha256": digest(new_skill)})
                skill = new_skill
            else:
                reflection_record.update({"status": "skipped_due_to_prediction_failure", "skill_after_sha256": digest(skill)})
        except Exception as exc:
            reflection_record.update({"status": "reflection_failed", "error": str(exc)[:500], "skill_after_sha256": digest(skill)})
        (run_path / "skill.md").write_text(skill, encoding="utf-8")
        write_json(version_dir / f"{index:03d}.json", {"index": index, "case_id": question["case_id"],
                                                        "sha256": digest(skill), "skill": skill})
        with (run_path / "reflections.jsonl").open("a", encoding="utf-8") as output:
            output.write(_canonical(reflection_record) + "\n")
        run["skill_sha256"] = digest(skill)
        _write_status(run_path, run, questions, cases, index=index + 1)
        print(json.dumps({"index": index + 1, "total": len(questions), "case_id": question["case_id"],
                          "result": result.get("status"), "reflection": reflection_record.get("status"),
                          "skill_sha256": digest(skill)}, ensure_ascii=False), flush=True)
    # Failed cases remain in the audit trail and are skipped by design.
    final_skill = skill
    final_path = artifact_root / "final-s02-SKILL.md"
    final_path.write_text(final_skill, encoding="utf-8")
    target = SKILL_ROOT / "s02" / "SKILL.md"
    target.write_text(final_skill, encoding="utf-8")
    run.update(status="completed", phase="learning_complete", finished_at=now(), skill_sha256=digest(final_skill),
               completed=len(cases), succeeded=sum(row.get("status") == "success" for row in cases),
               failed=sum(row.get("status") == "failed" for row in cases))
    write_json(run_path / "run.json", run)
    write_json(run_path / "final-summary.json", {
        "run_id": run["run_id"], "total": len(questions), "completed": len(cases),
        "succeeded": run["succeeded"], "failed": run["failed"], "final_skill_sha256": digest(final_skill),
        "final_skill_path": str(final_path), "target_skill_path": str(target), "finished_at": run["finished_at"],
    })
    print(json.dumps({"status": "completed", "run_id": run["run_id"], "skill": str(final_path),
                      "skill_sha256": digest(final_skill), "succeeded": run["succeeded"], "failed": run["failed"]},
                     ensure_ascii=False), flush=True)
    return run_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-root", type=Path, default=EVALUATION_ROOT / "recursive" / "Exp06-s02")
    parser.add_argument("--resume", type=Path)
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--case-retries", type=int, default=2)
    parser.add_argument("--build-only", action="store_true")
    args = parser.parse_args()
    if args.build_only:
        manifest, questions, labels, indicators = build_learning_set()
        print(json.dumps({"questions": len(questions), "labels": len(labels), "indicators": len(indicators),
                          "families": manifest["families"], "first": questions[0]["case_id"],
                          "last": questions[-1]["case_id"]}, ensure_ascii=False))
        return
    run_learning(args.artifact_root, resume=args.resume, model=args.model, case_retries=args.case_retries)


if __name__ == "__main__":
    main()
