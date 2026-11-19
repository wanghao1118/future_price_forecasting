"""Settings and publication state for explicitly named experiments only."""

from collections import Counter, defaultdict
import json
from pathlib import Path
import re


def _read(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def experiment_catalog(root, runs):
    root = Path(root)
    groups = defaultdict(list)
    for run in runs:
        experiment_id = run.get("experiment_id")
        if (run.get("mode") != "backtest" or not isinstance(experiment_id, str)
                or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", experiment_id)):
            continue
        groups[experiment_id].append(run)
    cards = []
    for experiment_id, records in groups.items():
        records.sort(key=lambda run: run.get("started_at", ""), reverse=True)
        anchor = records[0]
        batch = _read(root / "batches" / (experiment_id + ".json"))
        state = _read(root / "batches" / (experiment_id + "-execution.json"))
        dataset = _read(root / "runs" / anchor["run_id"] / "dataset.json")
        manifest = dataset.get("manifest", {})
        latest = {}
        for run in records:
            latest.setdefault(run.get("model"), run)
        rows = list(latest.values())
        expected_runs = set(batch.get("models", {}).values())
        audited = {row["run_id"] for row in state.get("validation", []) if row.get("complete") and row.get("verified") == row.get("total")}
        ready = bool(experiment_id and expected_runs and state.get("status") == "completed"
                     and expected_runs <= audited and expected_runs == {r["run_id"] for r in rows}
                     and all(r.get("status") == "completed" and r.get("succeeded") == r.get("total") for r in rows))
        total = sum(r.get("total", 0) for r in rows)
        succeeded = sum(r.get("succeeded", 0) for r in rows)
        failed = sum(r.get("failed", 0) for r in rows)
        question_count = len(dataset.get("questions", [])) or batch.get("total_per_model", 90)
        full = all(not r.get("one_day") and not r.get("family") and r.get("total") == question_count for r in rows)
        protocol = anchor.get("protocol", "legacy")
        status = "completed" if ready else state.get("status") or ("waiting_dependency" if batch.get("phase") == "waiting_dependency" else None) or ("running" if any(r.get("status") in ("running", "queued") for r in rows)
                  else "completed" if succeeded == total else "partial")
        model_profiles = {r["model"]: {k:v for k,v in r.get("generation_config", {}).items()
                         if k in ("max_output_tokens", "max_rounds", "client_timeout_seconds", "gateway_timeout_seconds",
                                  "max_parallel_cases", "api_protocol", "reasoning_effort", "provider")} for r in rows}
        cards.append({"id":experiment_id,
            "experiment_id":experiment_id, "name":experiment_id,
            "environment_id":anchor.get("environment_id", "env01"), "protocol":protocol,
            "retrieval_time_policy":anchor.get("retrieval_time_policy"),
            "retrieval_buffer_minutes":anchor.get("retrieval_buffer_minutes"),
            "retrieval_buffer_hours":anchor.get("retrieval_buffer_hours"),
            "created_at":batch.get("created_at") or min(r.get("started_at", "") for r in rows),
            "status":status, "ready_for_leaderboard":ready, "full_backtest":full,
            "dataset_id":anchor.get("dataset_id"), "benchmark_version":manifest.get("version"),
            "target_start":manifest.get("target_start"), "target_end":manifest.get("target_end"),
            "questions_per_model":anchor.get("total"), "families":manifest.get("families", []),
            "benchmark_profile":manifest.get("profile", "v1"),
            "family_question_counts":dict(Counter(q["family"] for q in dataset.get("questions", []))),
            "models":sorted(latest), "model_profiles":model_profiles, "run_ids":[r["run_id"] for r in rows],
            "total":batch.get("expected_total", total), "succeeded":succeeded, "failed":failed,
            "audit_status":state.get("status", "not_started"),
            "dataset_snapshot_sha256":anchor.get("dataset_snapshot_sha256"),
            "news_snapshot_sha256":anchor.get("news_snapshot_sha256"),
            "technical_snapshot_sha256":anchor.get("technical_snapshot_sha256"),
            "code_frozen":bool(batch.get("code_sha256")), "network_search":False,
            "depends_on_experiment":batch.get("depends_on_experiment"),
            "news_policy":"disabled" if anchor.get("environment_id") in ("env00", "env02") else "local_crawled_only"})
    return sorted(cards, key=lambda card: ({"Exp01":0, "Exp00":1}.get(card["id"], 2), card["created_at"]))
