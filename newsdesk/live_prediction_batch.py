"""Deadline-bound live predictions with the per-model settings of Exp01."""

import argparse
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
import fcntl
import hashlib
import json
import os
from pathlib import Path
import threading

from newsdesk.batch_evaluation import FrozenNews, PreparedManager
from newsdesk.benchmark import BenchmarkStore
from newsdesk.evaluation import (
    AIHUB_MODELS, AIHubPricePredictor, DEFAULT_ROOT, EvaluationManager,
    EVALUATION_PROTOCOL, _indicator_receipts, _search_receipts, _validate_live_open,
    PUBLIC_CASE_FIELDS, digest, extract_forecast, now, prepare_retrieval_cases, project_evaluator_question, project_live_submission,
    read_json, validate_run_retrieval_policy, write_json,
)
from time_gate_agent.local import LocalTimeGate


SNAPSHOTS = (("dataset.json", "dataset_snapshot_sha256"),
             ("news-snapshot.json", "news_snapshot_sha256"),
             ("technical-snapshot.json", "technical_snapshot_sha256"))

# Claude endpoints are currently unavailable for the live gateway. Keep them
# in the frozen backtest experiment, while live batches use every other Exp01
# base model.
LIVE_PREDICTION_MODELS = tuple(model for model in AIHUB_MODELS if not model.startswith("claude-"))


def _surviving_live_profile(root, model):
    """Recover an earlier frozen live profile after its run directory vanished."""
    candidates = sorted((Path(root) / "live-batches").glob("live-Exp01-*.json*"))
    profiles = []
    for path in candidates:
        try:
            payload = read_json(path)
        except (OSError, ValueError, TypeError):
            continue
        profile = payload.get("profiles", {}).get(model) if isinstance(payload.get("profiles"), dict) else None
        if isinstance(profile, dict):
            profiles.append(profile)
    unique = {digest(profile): profile for profile in profiles}
    if len(unique) > 1:
        raise ValueError("同一 Exp01 模型的历史实盘配置不一致：" + model)
    return next(iter(unique.values()), None)


def profiles_from_reference(root, reference_path):
    reference = read_json(reference_path)
    if (reference.get("experiment_id") != "Exp01" or reference.get("environment_id") != "env01"
            or reference.get("protocol") != EVALUATION_PROTOCOL):
        raise ValueError("实盘参考必须是 Exp01 的 env01 v5 设置")
    models = reference.get("models", {})
    if not models or set(models) - set(AIHUB_MODELS):
        raise ValueError("Exp01 模型清单无效")
    models = {model: models[model] for model in LIVE_PREDICTION_MODELS if model in models}
    if not models:
        raise ValueError("Exp01 没有可用于实盘的非 Claude 模型")
    profiles = {}
    for model, identifier in models.items():
        run_path = Path(root) / "runs" / identifier / "run.json"
        try:
            run = read_json(run_path)
        except FileNotFoundError:
            # The immutable Exp01 manifest can outlive its per-run working
            # directories. Its batch-level generation settings are the
            # surviving reference for live inference. Keep model identity and
            # provider defaults from AIHubPricePredictor, while preserving the
            # exact per-run override whenever that run is still available.
            recovered = _surviving_live_profile(root, model)
            profiles[model] = recovered or dict(reference.get("generation", {}))
            continue
        if run.get("model") != model or run.get("experiment_id") != "Exp01":
            raise ValueError("Exp01 模型设置归属不一致")
        profiles[model] = {**reference.get("generation", {}), **run.get("generation_config", {})}
        if "temperature" in profiles[model]:
            # The established predictor has no temperature override. Refuse to
            # claim exact reuse if a future reference introduces one.
            raise ValueError("当前适配器不支持参考实验的 temperature 设置")
    return reference, profiles


def prepare_live_batch(root=DEFAULT_ROOT, reference_path=None, benchmark_store=None, news_store=None, *,
                       dataset_id=None, case_ids=None):
    if case_ids is not None and (not isinstance(case_ids, (list, tuple, set, frozenset)) or not case_ids
                                 or any(not isinstance(value, str) or not value for value in case_ids)
                                 or len(set(case_ids)) != len(case_ids)):
        raise ValueError("case_ids 必须是非空且不重复的题目编号集合")
    root = Path(root).resolve()
    reference_path = Path(reference_path) if reference_path else root / "batches" / "Exp01.json"
    reference, profiles = profiles_from_reference(root, reference_path)
    store = benchmark_store or BenchmarkStore(root.parent / "benchmark")
    dataset = store.load_dataset(dataset_id, mode="live") if dataset_id is not None else store.load_dataset(mode="live")
    if dataset["manifest"].get("mode") != "live":
        raise ValueError("仅支持使用实盘数据集准备预测批次")
    if dataset_id is not None and dataset["manifest"].get("dataset_id") != dataset_id:
        raise ValueError("指定实盘数据集与实际加载的版本不一致")
    selected_ids = set(case_ids) if case_ids is not None else None
    if selected_ids is not None and selected_ids - {question["case_id"] for question in dataset["questions"]}:
        raise ValueError("指定的实盘题目不存在于该数据集")
    selected_questions = [question for question in dataset["questions"]
                          if selected_ids is None or question["case_id"] in selected_ids]
    ready = []
    for question in prepare_retrieval_cases(selected_questions, dataset["manifest"]):
        try:
            _validate_live_open(question)
            if question["case_id"] in dataset["labels"]:
                continue
            ready.append(question)
        except ValueError:
            continue
    if not ready:
        raise ValueError("当前没有尚未截止、未结算的实盘题目")
    keys = sorted((q["family"], q["contract_id"], q["input_date"], q["target_date"]) for q in ready)
    ready_ids = {question["case_id"] for question in ready}
    selected_dataset = copy.deepcopy(dataset)
    selected_dataset["questions"] = [question for question in selected_dataset["questions"] if question["case_id"] in ready_ids]
    selected_dataset["labels"] = {key: value for key, value in selected_dataset["labels"].items() if key in ready_ids}

    class FrozenBenchmark:
        def __init__(self, snapshot):
            self.snapshot = snapshot

        def load_dataset(self, *args, **kwargs):
            return copy.deepcopy(self.snapshot)

    # PreparedManager.start reloads its benchmark. Supply the filtered frozen
    # snapshot so another ready family cannot enter a selected-question batch.
    store = FrozenBenchmark(selected_dataset)
    identifier = "live-Exp01-" + digest(keys)[:20]
    path = root / "live-batches" / (identifier + ".json")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with path.with_suffix(".prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = None
        if path.is_file():
            existing = read_json(path)
            if existing.get("reference_profiles_sha256") != digest(profiles):
                raise ValueError("同日实盘已有冻结设置，禁止静默替换")
            if existing.get("phase") != "preparing":
                if set(existing.get("models", {})) != set(profiles):
                    raise ValueError("实盘批次模型准备不完整")
                return path
        # Fail before creating queued runs if the private configuration is absent.
        for model in profiles:
            AIHubPricePredictor(model)
        if existing and existing.get("snapshot_run_id"):
            anchor_path = root / "runs" / existing["snapshot_run_id"]
            values = {}
            for filename, key in SNAPSHOTS:
                values[filename] = read_json(anchor_path / filename)
                if digest(values[filename]) != existing[key]:
                    raise ValueError("待恢复实盘批次的冻结输入校验失败")
            frozen = values["dataset.json"]
            frozen_keys = sorted((q["family"], q["contract_id"], q["input_date"], q["target_date"])
                                 for q in frozen["questions"])
            if frozen_keys != keys:
                raise ValueError("待恢复实盘批次的题目范围与选定题目不一致")
            frozen_ids = {question["case_id"] for question in frozen["questions"]}
            frozen["labels"] = {key: value for key, value in frozen["labels"].items() if key in frozen_ids}
            if digest(frozen) != existing["dataset_snapshot_sha256"]:
                raise ValueError("待恢复实盘批次含未选定题目的标签")
            store = FrozenBenchmark(frozen)
            frozen_news = FrozenNews(values["news-snapshot.json"])
        else:
            if news_store is None:
                from newsdesk.server import NewsStore
                news_store = NewsStore(root.parent / "news.sqlite3", seed_demo=False)
            frozen_news = FrozenNews(news_store.all())
        manager = PreparedManager(root, store, frozen_news)
        batch = existing or {"batch_id": identifier, "experiment_id": "Exp01", "experiment_name": "Exp01",
                 "mode": "live", "environment_id": "env01", "protocol": EVALUATION_PROTOCOL,
                 "created_at": now(), "phase": "preparing", "dataset_id": dataset["manifest"]["dataset_id"],
                 "reference_batch": str(reference_path.resolve()), "reference_profiles_sha256": digest(profiles),
                 "generation": reference.get("generation", {}), "profiles": profiles, "models": {},
                 "questions": [{"case_id": q["case_id"], "family": q["family"], "contract_id": q["contract_id"], "input_date": q["input_date"],
                                "target_date": q["target_date"], "forecast_deadline": project_live_submission(q)["forecast_deadline"]}
                               for q in ready],
                 "total_per_model": len(ready), "expected_total": len(profiles) * len(ready),
                 "retry_policy": "Only failed or missing outputs before deadline; never repeat a locked prediction."}
        write_json(path, batch)
        anchor = (read_json(root / "runs" / batch["snapshot_run_id"] / "run.json")
                  if batch.get("snapshot_run_id") else None)
        for model, profile in profiles.items():
            if model in batch["models"]:
                continue
            row = manager.start(strategy_id="aihub-" + model, dataset_id=batch["dataset_id"], workers=1,
                                mode="live", experiment_id="Exp01", environment_id="env01")
            if anchor is None:
                anchor = row
                batch["snapshot_run_id"] = row["run_id"]
            for _, key in SNAPSHOTS:
                if row[key] != anchor[key]:
                    raise ValueError("实盘基模冻结输入不一致：" + key)
                batch[key] = row[key]
            row.update(comparison_batch_id=identifier, generation_config=profile,
                       batch_phase="prepared", reference_experiment="Exp01",
                       reference_run_id=reference["models"][model])
            write_json(root / "runs" / row["run_id"] / "run.json", row)
            batch["models"][model] = row["run_id"]
            write_json(path, batch)
        batch.update(phase="prepared", updated_at=now())
        write_json(path, batch)
        return path


def audit_live_run(root, run_id, batch):
    path = Path(root) / "runs" / run_id
    run, dataset = read_json(path / "run.json"), read_json(path / "dataset.json")
    for filename, key in SNAPSHOTS:
        if digest(read_json(path / filename)) != run[key] or run[key] != batch[key]:
            raise ValueError("实盘冻结快照校验失败：" + filename)
    validate_run_retrieval_policy(run, dataset)
    checks = []
    for question in dataset["questions"]:
        result_path = path / "cases" / question["case_id"] / "result.json"
        if not result_path.exists():
            checks.append({"case_id": question["case_id"], "valid": False, "error": "missing_prediction"})
            continue
        result = read_json(result_path)
        if result.get("status") not in ("predicted_pending", "success"):
            checks.append({"case_id": question["case_id"], "valid": False, "error": result.get("error", result.get("status"))})
            continue
        attempt = path / "cases" / question["case_id"] / result["attempt"]
        locked = read_json(attempt / "prediction-lock.json")
        if (digest(locked) != result["prediction_lock_sha256"] or locked["predicted"] != result["predicted"]
                or locked["raw_output_sha256"] != digest(result["raw_output"])
                or extract_forecast(result["raw_output"], question["current_price"]) != locked["predicted"]):
            raise ValueError("实盘预测锁定凭证校验失败")
        submitted = project_live_submission(question)
        if run.get("protocol") == EVALUATION_PROTOCOL:
            submitted = project_evaluator_question(submitted)
        for instant in (locked["submitted_at"], locked["prediction_locked_at"]):
            _validate_live_open(submitted, instant)
        material = read_json(attempt / "input.json")
        public_case = {key: submitted[key] for key in PUBLIC_CASE_FIELDS if key in submitted}
        if (digest(material) != result["input_sha256"] or material["question"] != public_case
                or locked["input_sha256"] != result["input_sha256"]):
            raise ValueError("实盘模型输入与冻结题目不一致")
        context = read_json(attempt / "model" / "search-context.json")
        technical = next(item["rows"] for item in read_json(path / "technical-snapshot.json")
                         if item["case_id"] == question["case_id"])
        if (context["case"] != public_case or context["news_snapshot_sha256"] != run["news_snapshot_sha256"]
                or Path(context["news_snapshot_path"]).resolve() != (path / "news-snapshot.json").resolve()
                or context["indicator_snapshot_sha256"] != digest(technical)
                or read_json(context["indicator_snapshot_path"]) != technical):
            raise ValueError("实盘检索凭证与冻结快照绑定失败")
        trace = read_json(attempt / "model" / "conversation.json")
        if trace.get("validation_errors") or trace.get("status") != "completed":
            raise ValueError("实盘检索轨迹审计未通过")
        # Independent receipt replay repeats time-gate and cutoff verification.
        news = _search_receipts(attempt / "model")
        technical = _indicator_receipts(attempt / "model")
        if not news or not technical:
            raise ValueError("实盘预测缺少 env01 新闻或技术检索凭证")
        checks.append({"case_id": question["case_id"], "valid": True,
                       "submitted_at": locked["submitted_at"], "prediction_locked_at": locked["prediction_locked_at"],
                       "forecast_deadline": submitted["forecast_deadline"],
                       "news_calls": len(news), "indicator_calls": len(technical),
                       "prediction_lock_sha256": result["prediction_lock_sha256"]})
    audit = {"run_id": run_id, "mode": "live", "checked_at": now(),
             "valid": bool(checks) and all(item["valid"] for item in checks), "cases": checks}
    write_json(path / "live-run-validation.json", audit)
    return audit


def preserve_interrupted_lock(path, question, old):
    """An on-disk lock wins even if the process died before result.json.

    Reconstructing a successful result requires the trace, public output and
    timing receipts together. Leave an explicit review record instead of
    resubmitting a model while any prior lock may already bind its answer.
    """
    case_root = path / "cases" / question["case_id"]
    locks = sorted(case_root.glob("*/prediction-lock.json"))
    if not locks:
        return False
    evidence = []
    for lock in locks:
        raw = lock.read_bytes()
        item = {"path": str(lock.relative_to(path)), "file_sha256": hashlib.sha256(raw).hexdigest()}
        try:
            value = json.loads(raw)
            item.update(case_binding_valid=value.get("case_id") == question["case_id"],
                        prediction_lock_sha256=digest(value), submitted_at=value.get("submitted_at"),
                        prediction_locked_at=value.get("prediction_locked_at"))
        except (ValueError, TypeError, AttributeError):
            item.update(case_binding_valid=False, error="malformed_prediction_lock")
        evidence.append(item)
    review = {"case_id": question["case_id"], "checked_at": now(),
              "status": "prediction_lock_requires_audit", "locks": evidence}
    write_json(case_root / "interrupted-lock-review.json", review)
    record = {key: question[key] for key in ("case_id", "family", "input_date", "target_date", "current_price") if key in question}
    record.update(old)
    record.update(status="failed", recovery_status="prediction_lock_requires_audit",
                  error="存在中断前写入的预测锁定凭证；保留原锁，等待独立审核，禁止重新调用模型",
                  error_type="InterruptedPredictionLock", finished_at=now(), orphan_prediction_locks=evidence)
    write_json(case_root / "result.json", record)
    return True


def _live_attempt_budget(path, question, max_attempts, *, claim=False):
    """Account for old attempts and durable dispatch claims across restarts.

    The caller holds the run execution lock. One _case dispatch creates at
    most one attempt directory. A claim written before that dispatch covers
    exactly one newly observed directory, including a directory left by a
    process that died before writing result.json. Unmatched claims still use
    their slot; unknown or deleted old attempts never restore the budget.
    """
    path = Path(path)
    case_root = path / "cases" / question["case_id"]
    budget_path = case_root / "dispatch-budget.json"
    attempts = {item.name for item in case_root.glob("attempt-*") if item.is_dir()}
    result_path = case_root / "result.json"
    if result_path.exists():
        result = read_json(result_path)
        if result.get("attempt"):
            attempts.add(result["attempt"])
        elif result.get("status") in ("failed", "running") and not attempts:
            # Older integrations may retain just a terminal result, without
            # the attempt directory. It still proves at least one dispatch.
            attempts.add("legacy-result")
    if budget_path.exists():
        budget = read_json(budget_path)
        unsigned = {key: value for key, value in budget.items() if key != "state_sha256"}
        if (budget.get("version") != 1 or budget.get("run_id") != path.name
                or budget.get("case_id") != question["case_id"]
                or budget.get("state_sha256") != digest(unsigned)
                or type(budget.get("max_attempts")) is not int or budget["max_attempts"] < 1
                or not isinstance(budget.get("legacy_attempts"), list)
                or not isinstance(budget.get("dispatches"), list)):
            raise ValueError("实盘持久派发预算校验失败")
    else:
        budget = {"version": 1, "run_id": path.name, "case_id": question["case_id"],
                  "max_attempts": max_attempts, "created_at": now(),
                  "legacy_attempts": sorted(attempts), "dispatches": []}
    legacy = set(budget["legacy_attempts"])
    covered = legacy | {row["attempt"] for row in budget["dispatches"] if row.get("attempt")}
    for dispatch in budget["dispatches"]:
        if dispatch.get("attempt"):
            continue
        candidates = attempts - covered - set(dispatch["attempts_before"])
        if candidates:
            # Each reservation can cover one dispatch, never every later
            # directory. Extra old-runner attempts consume separate slots.
            dispatch["attempt"] = sorted(candidates)[0]
            covered.add(dispatch["attempt"])
    legacy.update(attempts - covered)
    budget["legacy_attempts"] = sorted(legacy)
    used = len(legacy) + len(budget["dispatches"])
    limit = min(max_attempts, budget["max_attempts"])
    claimed = False
    if claim and used < limit:
        budget["dispatches"].append({"sequence": len(budget["dispatches"]) + 1,
                                      "claimed_at": now(), "attempts_before": sorted(attempts)})
        used += 1
        claimed = True
    budget.update(used_attempts=used, remaining_attempts=max(0, limit - used), updated_at=now())
    budget.pop("state_sha256", None)
    budget["state_sha256"] = digest(budget)
    write_json(budget_path, budget)
    return budget, claimed


def _dispatch_live_case(manager, path, question, gate, predictor, max_attempts):
    result_file = path / "cases" / question["case_id"] / "result.json"
    old = read_json(result_file) if result_file.exists() else {}
    if (old.get("status") in ("success", "predicted_pending") or old.get("prediction_lock_sha256")
            or preserve_interrupted_lock(path, question, old)):
        return None
    if old.get("status") == "failed" and "HTTP 403" in str(old.get("error", "")):
        return None
    try:
        _validate_live_open(project_live_submission(question))
    except ValueError:
        return None
    _, claimed = _live_attempt_budget(path, question, max_attempts, claim=True)
    if not claimed:
        return None
    try:
        return manager._case(path, question, None, gate, predictor)
    finally:
        _live_attempt_budget(path, question, max_attempts)


def execute_live_batch(root, batch_path, api_workers=4, retries=1, *, request_slots=None):
    root, batch_path = Path(root).resolve(), Path(batch_path).resolve()
    batch_lock = batch_path.with_suffix(".execution.lock").open("a")
    fcntl.flock(batch_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    batch = read_json(batch_path)
    if (batch.get("mode") != "live" or batch.get("experiment_id") != "Exp01"
            or batch.get("environment_id") != "env01" or batch.get("protocol") != EVALUATION_PROTOCOL
            or digest(batch.get("profiles", {})) != batch.get("reference_profiles_sha256")
            or set(batch.get("models", {})) != set(batch.get("profiles", {}))):
        batch_lock.close()
        raise ValueError("实盘批次归属或冻结模型设置校验失败")
    slots = request_slots if request_slots is not None else threading.BoundedSemaphore(api_workers)
    manager = EvaluationManager(root, BenchmarkStore(root.parent / "benchmark"), FrozenNews([]))
    locks, datasets, predictors = {}, {}, {}
    try:
        for model, run_id in batch["models"].items():
            path = root / "runs" / run_id
            lock = (path / "execution.lock").open("a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locks[model] = lock
            run = read_json(path / "run.json")
            if (run.get("mode") != "live" or run.get("experiment_id") != "Exp01"
                    or run.get("model") != model or run.get("comparison_batch_id") != batch["batch_id"]
                    or run.get("generation_config") != batch["profiles"][model]):
                raise ValueError("实盘运行归属或冻结模型设置校验失败")
            for filename, key in SNAPSHOTS:
                value = read_json(path / filename)
                if digest(value) != run[key] or run[key] != batch[key]:
                    raise ValueError("实盘模型冻结快照不一致")
                if filename == "dataset.json":
                    datasets[model] = value
                    validate_run_retrieval_policy(run, value)
            profile = run["generation_config"]
            predictors[model] = AIHubPricePredictor(model, timeout=profile["client_timeout_seconds"],
                max_output_tokens=profile["max_output_tokens"], max_rounds=profile["max_rounds"],
                gateway_timeout=profile["gateway_timeout_seconds"], request_slots=slots,
                api_protocol=profile.get("api_protocol"), reasoning_effort=profile.get("reasoning_effort"),
                provider=profile.get("provider"))
        gate = LocalTimeGate(read_json(root / "runs" / batch["snapshot_run_id"] / "news-snapshot.json"))
        batch.update(phase="running", owner_pid=os.getpid(), updated_at=now(), api_workers=api_workers)
        write_json(batch_path, batch)
        for attempt in range(retries + 1):
            tasks = []
            for model, dataset in datasets.items():
                path = root / "runs" / batch["models"][model]
                for question in dataset["questions"]:
                    result_file = path / "cases" / question["case_id"] / "result.json"
                    old = read_json(result_file) if result_file.exists() else {}
                    if old.get("status") in ("success", "predicted_pending") or old.get("prediction_lock_sha256"):
                        continue
                    if preserve_interrupted_lock(path, question, old):
                        continue
                    if old.get("status") == "failed" and "HTTP 403" in str(old.get("error", "")):
                        continue
                    try:
                        _validate_live_open(project_live_submission(question))
                    except ValueError:
                        continue
                    budget, _ = _live_attempt_budget(path, question, retries + 1)
                    if not budget["remaining_attempts"]:
                        continue
                    run = read_json(path / "run.json")
                    run.update(status="running", owner_pid=os.getpid(), batch_phase="live", resumed_at=now())
                    write_json(path / "run.json", run)
                    tasks.append((model, path, question))
            if not tasks:
                break
            # Model conversations can wait or back off independently. Only
            # actual upstream requests share the bounded API concurrency.
            with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
                futures = {pool.submit(_dispatch_live_case, manager, path, question, gate,
                                       predictors[model], retries + 1): (model, path)
                           for model, path, question in tasks}
                for future in as_completed(futures):
                    model, path = futures[future]
                    result = future.result()
                    if result is None:
                        continue
                    manager._update(path, datasets[model]["questions"], finish=True)
                    print(json.dumps({"at": now(), "model": model, "case_id": result["case_id"],
                                      "status": result["status"], "error": result.get("error"),
                                      "pass": attempt + 1}, ensure_ascii=False), flush=True)
                    batch.update(updated_at=now(), locked=sum(read_json(root / "runs" / rid / "run.json").get("pending", 0)
                                                            for rid in batch["models"].values()))
                    write_json(batch_path, batch)
        validation = []
        for model, run_id in batch["models"].items():
            path = root / "runs" / run_id
            manager._update(path, datasets[model]["questions"], finish=True)
            try:
                validation.append(audit_live_run(root, run_id, batch))
            except (OSError, ValueError, KeyError, TypeError, AssertionError) as error:
                invalid = {"run_id": run_id, "valid": False, "error": str(error)[:500], "checked_at": now()}
                write_json(path / "live-run-validation.json", invalid)
                validation.append(invalid)
        batch.update(phase="awaiting_settlement" if all(item["valid"] for item in validation) else "partial",
                     finished_at=now(), validation=validation)
        write_json(batch_path, batch)
    finally:
        for lock in locks.values():
            lock.close()
        batch_lock.close()
    return batch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--batch", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--api-workers", type=int, default=4)
    parser.add_argument("--retries", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.api_workers <= 6 or not 0 <= args.retries <= 3:
        raise ValueError("实盘并发或重试次数无效")
    path = args.batch or prepare_live_batch(args.root, args.reference)
    print(json.dumps({"batch_path": str(path)}, ensure_ascii=False), flush=True)
    if not args.prepare_only:
        execute_live_batch(args.root, path, args.api_workers, args.retries)


if __name__ == "__main__":
    main()
