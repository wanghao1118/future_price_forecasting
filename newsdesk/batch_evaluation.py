"""Resumable, bounded AIHub benchmark batches sharing immutable input snapshots."""

import argparse
from collections import Counter, deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from newsdesk.benchmark import BenchmarkStore
from newsdesk.evaluation import (AIHUB_MODELS, AIHubPricePredictor, EvaluationManager,
    DEFAULT_ROOT, EVALUATION_PROTOCOL, LEGACY_ACTIVE_PROTOCOL, RETRIEVAL_TIME_POLICY,
    digest, read_json, write_json, now, validate_run_retrieval_policy)
from time_gate_agent.local import LocalTimeGate
from newsdesk.environment_policy import ENV00_PROTOCOL, ENV02_PROTOCOL, ENV03_PROTOCOL, ENV04_PROTOCOL, NEWS_SEARCH_ENVIRONMENTS
from newsdesk.batch_dependencies import dependency_ready


class FrozenNews:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows


class PreparedManager(EvaluationManager):
    def _launch(self, run_id, workers, retry_failed):
        # The batch scheduler owns the execution locks and the shared API cap.
        pass


def verify_baseline(root, run_id, require_completed=True):
    path = root / "runs" / run_id
    run = read_json(path / "run.json")
    dataset = read_json(path / "dataset.json")
    if ((require_completed and run.get("status") != "completed") or run.get("mode") != "backtest"
            or run.get("one_day") or run.get("family") or not dataset.get("questions")
            or run.get("total") != len(dataset["questions"])):
        raise ValueError("基准运行必须覆盖冻结题库的完整回测")
    for filename, key in (("dataset.json", "dataset_snapshot_sha256"),
                          ("news-snapshot.json", "news_snapshot_sha256"),
                          ("technical-snapshot.json", "technical_snapshot_sha256")):
        if digest(read_json(path / filename)) != run[key]:
            raise ValueError("基准快照校验失败：" + filename)
    validate_run_retrieval_policy(run, dataset)
    return run


def verify_batch_reference(root, batch):
    if batch.get("code_sha256"):
        import hashlib
        project = Path(__file__).resolve().parent.parent
        for relative, expected in batch["code_sha256"].items():
            source = project / relative
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                raise ValueError("请使用该实验冻结的代码目录续跑：" + relative)
    reference_id = batch.get("snapshot_run_id") or batch["baseline_run_id"]
    reference = verify_baseline(root, reference_id, require_completed=not bool(batch.get("snapshot_run_id")))
    if batch.get("experiment_id") and reference.get("experiment_id") != batch["experiment_id"]:
        raise ValueError("实验快照归属不一致")
    return reference


def prepare_experiment(root, experiment_id, models, dataset_id=None, news_store=None,
                       environment_id="env01", reference_batch=None):
    """Create all model runs from one new snapshot, without reusing old predictions."""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", experiment_id):
        raise ValueError("实验名称无效")
    if environment_id not in ("env00", "env01", "env02", "env03", "env04"):
        raise ValueError("未知环境")
    if reference_batch and environment_id != "env00":
        raise ValueError("技术指标对照实验需要 env00")
    if not models or len(set(models)) != len(models) or set(models) - set(AIHUB_MODELS):
        raise ValueError("实验模型清单无效")
    root = Path(root).resolve()
    path = root / "batches" / (experiment_id + ".json")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if path.exists():
            raise ValueError("实验已存在，请续跑现有批次，不要重复创建：" + experiment_id)
        parent_batch = read_json(Path(reference_batch)) if reference_batch else None
        parent_reference = None
        if parent_batch:
            parent_reference = verify_baseline(root, parent_batch["snapshot_run_id"], require_completed=False)
            if set(models) != set(parent_batch["models"]):
                raise ValueError("对照实验必须包含前置实验的全部模型")
            source = root / "runs" / parent_reference["run_id"]
            dataset = read_json(source / "dataset.json")
            class FrozenBenchmark:
                def load_dataset(self, *args, **kwargs):
                    return json.loads(json.dumps(dataset))
            store = FrozenBenchmark()
        else:
            store = BenchmarkStore(root.parent / "benchmark")
            dataset = store.load_dataset(dataset_id, mode="backtest")
        if environment_id in ("env00", "env02"):
            frozen_news = FrozenNews([])
        else:
            if news_store is None:
                from newsdesk.server import NewsStore
                news_store = NewsStore(root.parent / "news.sqlite3", seed_demo=False)
            frozen_news = FrozenNews(news_store.all())
        total = len(dataset["questions"])
        counts = dict(Counter(q["family"] for q in dataset["questions"]))
        manifest = dataset["manifest"]
        expected = ({f["id"]: f["count"] for f in manifest.get("families", [])}
                    if manifest.get("profile") == "v2" else {f: 30 for f in ("WTI", "Brent", "SC")})
        if (set(counts) != {"WTI", "Brent", "SC"} or counts != expected
                or total != manifest.get("expected_questions", total)
                or len({q["case_id"] for q in dataset["questions"]}) != total
                or any(q["case_id"] not in dataset["labels"] for q in dataset["questions"])):
            raise ValueError("实验需要三个主连的完整冻结回测集，题数必须与题库清单一致")
        manager = PreparedManager(root, store, frozen_news)
        generation = {"max_output_tokens":16000, "max_rounds":18,
                      "gateway_timeout_seconds":240, "client_timeout_seconds":300}
        previous = {}
        for run in reversed(manager.runs()):
            if run.get("generation_config") and run.get("succeeded", 0) > 0:
                previous[run["model"]] = run["generation_config"]
        if parent_batch:
            previous = {model: read_json(root / "runs" / rid / "run.json")["generation_config"]
                        for model, rid in parent_batch["models"].items()}
        # Fail on missing credentials before creating any queued model runs.
        for model in models:
            AIHubPricePredictor(model)
        batch = {"batch_id":experiment_id, "experiment_id":experiment_id, "experiment_name":experiment_id,
                 "dataset_id":dataset["manifest"]["dataset_id"], "environment_id":environment_id,
                 "created_at":now(), "phase":"preparing", "models":{}, "generation":generation,
                 "protocol":{"env00":ENV00_PROTOCOL, "env02":ENV02_PROTOCOL,
                             "env03":ENV03_PROTOCOL, "env04":ENV04_PROTOCOL}.get(environment_id, EVALUATION_PROTOCOL),
                 "total_per_model":total, "expected_total":len(models)*total,
                 "benchmark_profile":manifest.get("profile", "v1"), "family_question_counts":counts,
                 "retry_policy":"Only failed or missing predictions; successful predictions are immutable."}
        batch_skill = {"env03": "s01", "env04": "s02"}.get(environment_id)
        if batch_skill:
            skill_file = Path(__file__).resolve().parent.parent / "search-strategy" / batch_skill / "SKILL.md"
            if not skill_file.is_file():
                raise ValueError("实验技能文件不存在：" + str(skill_file))
            batch.update(skill_id=batch_skill, skill_sha256=hashlib.sha256(skill_file.read_bytes()).hexdigest())
        if environment_id != "env02":
            batch.update(retrieval_time_policy=RETRIEVAL_TIME_POLICY,
                         retrieval_buffer_minutes=30, retrieval_timezone="UTC")
        if parent_batch:
            batch.update(depends_on_experiment=parent_batch["experiment_id"],
                         reference_experiment=parent_batch["experiment_id"])
        write_json(path, batch)
        reference = None
        for model in models:
            row = manager.start(strategy_id="aihub-"+model, dataset_id=batch["dataset_id"], workers=2,
                                mode="backtest", one_day=False, experiment_id=experiment_id, environment_id=environment_id)
            if parent_reference:
                target = root / "runs" / row["run_id"]
                for filename, key in (("dataset.json", "dataset_snapshot_sha256"),
                                      ("technical-snapshot.json", "technical_snapshot_sha256")):
                    shutil.copy2(source / filename, target / filename)
                    row[key] = parent_reference[key]
                row.update(depends_on_experiment=parent_batch["experiment_id"], status="waiting_dependency")
                validate_run_retrieval_policy(row, read_json(target / "dataset.json"))
            if reference is None:
                reference = row
                batch["snapshot_run_id"] = row["run_id"]
            for key in ("dataset_snapshot_sha256", "news_snapshot_sha256", "technical_snapshot_sha256"):
                if row[key] != reference[key]:
                    raise ValueError("实验冻结输入不一致：" + key)
                batch[key] = row[key]
            row.update(comparison_batch_id=experiment_id, generation_config={**generation, **previous.get(model, {})},
                       batch_phase="prepared")
            write_json(root / "runs" / row["run_id"] / "run.json", row)
            batch["models"][model] = row["run_id"]
            write_json(path, batch)
            print(json.dumps({"experiment":experiment_id, "prepared":model, "run_id":row["run_id"], "total":total}), flush=True)
        # Retain the exact Python environment implementation for long-running retries.
        source_root = path.parent / experiment_id / "source"
        project = Path(__file__).resolve().parent.parent
        code_hashes = {}
        for package in ("newsdesk", "time_gate_agent"):
            for original in (project / package).glob("*.py"):
                relative = original.relative_to(project)
                destination = source_root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(original, destination)
                code_hashes[str(relative)] = hashlib.sha256(original.read_bytes()).hexdigest()
        skill_id = {"env03": "s01", "env04": "s02"}.get(environment_id)
        if skill_id:
            original = project / "search-strategy" / skill_id / "SKILL.md"
            if not original.is_file():
                raise ValueError("实验技能文件不存在：" + str(original))
            relative = original.relative_to(project)
            destination = source_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original, destination)
            code_hashes[str(relative)] = hashlib.sha256(original.read_bytes()).hexdigest()
        batch.update(phase="waiting_dependency" if parent_batch else "prepared", source_root=str(source_root), code_sha256=code_hashes,
                     groups=[{"path":path.name, "api_workers":12, "case_workers":24, "per_model":2}])
        write_json(path, batch)
        return path


def prepare(root, baseline_id, models):
    baseline = verify_baseline(root, baseline_id)
    if baseline.get("protocol") != EVALUATION_PROTOCOL:
        raise ValueError("新批次必须使用新UTC30分钟检索协议的已完成基准；旧基准仅供历史比较，不自动重跑")
    source = root / "runs" / baseline_id
    manager = PreparedManager(root, BenchmarkStore(root.parent / "benchmark"),
                              FrozenNews(read_json(source / "news-snapshot.json")))
    batch_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    batch = {"batch_id": batch_id, "baseline_run_id": baseline_id,
             "dataset_id": baseline["dataset_id"], "environment_id": "env01",
             "created_at": now(), "phase": "prepared", "models": {},
             "protocol": EVALUATION_PROTOCOL, "retrieval_time_policy": RETRIEVAL_TIME_POLICY,
             "retrieval_buffer_minutes": 30, "retrieval_timezone": "UTC",
             "generation": {"max_output_tokens": 16000, "max_rounds": 18,
                            "gateway_timeout_seconds": 240, "client_timeout_seconds": 300},
             "retry_policy": "Retry failed cases only; never replace a scored successful forecast."}
    path = root / "batches" / (batch_id + ".json")
    for model in models:
        row = manager.start(strategy_id="aihub-" + model, dataset_id=baseline["dataset_id"],
                            workers=2, mode="backtest", one_day=False)
        for key in ("dataset_snapshot_sha256", "news_snapshot_sha256", "technical_snapshot_sha256"):
            if row[key] != baseline[key]:
                raise ValueError("比较运行的冻结输入与基准不一致：" + key)
        row.update(comparison_batch_id=batch_id, comparison_baseline_run_id=baseline_id,
                   generation_config=batch["generation"], batch_phase="prepared")
        write_json(root / "runs" / row["run_id"] / "run.json", row)
        batch["models"][model] = row["run_id"]
        write_json(path, batch)
        print(json.dumps({"prepared": model, "run_id": row["run_id"], "total": 90}), flush=True)
    return path


def execute(root, batch_path, pilot=False, retries=2, case_workers=24, api_workers=12, per_model=2, models=None):
    batch = read_json(batch_path)
    if batch.get("depends_on_experiment") and not dependency_ready(root, batch["depends_on_experiment"]):
        raise ValueError("前置实验尚未执行和审计结束")
    if models:
        selected_models = {m: rid for m, rid in batch["models"].items() if m in models}
    else:
        selected_models = batch["models"]
    baseline = verify_batch_reference(root, batch)
    gate = (None if baseline.get("environment_id") == "env02" else
            LocalTimeGate(read_json(root / "runs" / baseline["run_id"] / "news-snapshot.json")))
    manager = EvaluationManager(root, BenchmarkStore(root.parent / "benchmark"), FrozenNews([]))
    slots = threading.BoundedSemaphore(api_workers)
    datasets, predictors, locks, model_limits = {}, {}, {}, {}
    try:
        for model, rid in selected_models.items():
            path = root / "runs" / rid
            lock = (path / "execution.lock").open("a")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lock.close()
                raise RuntimeError("运行已由其他进程执行：" + rid)
            locks[model] = lock
            run = read_json(path / "run.json")
            if batch.get("experiment_id") and run.get("experiment_id") != batch["experiment_id"]:
                raise ValueError("批次不能混合不同实验：" + rid)
            if run.get("environment_id", "env01") != baseline.get("environment_id", "env01"):
                raise ValueError("批次不能混合环境")
            if run.get("protocol", LEGACY_ACTIVE_PROTOCOL) != baseline.get("protocol", LEGACY_ACTIVE_PROTOCOL):
                raise ValueError("批次不能混合不同评估协议：" + rid)
            for filename, key in (("dataset.json", "dataset_snapshot_sha256"),
                                  ("news-snapshot.json", "news_snapshot_sha256"),
                                  ("technical-snapshot.json", "technical_snapshot_sha256")):
                value = read_json(path / filename)
                if digest(value) != baseline[key] or run[key] != baseline[key]:
                    raise ValueError("批量评估快照校验失败：" + rid)
                if filename == "dataset.json":
                    datasets[model] = value
                    validate_run_retrieval_policy(run, value)
            profile = run.get("generation_config", batch["generation"])
            model_limits[model] = profile.get("max_parallel_cases", per_model)
            predictors[model] = AIHubPricePredictor(model, timeout=profile["client_timeout_seconds"],
                max_output_tokens=profile["max_output_tokens"], max_rounds=profile["max_rounds"],
                gateway_timeout=profile["gateway_timeout_seconds"], request_slots=slots,
                api_protocol=profile.get("api_protocol"), reasoning_effort=profile.get("reasoning_effort"),
                provider=profile.get("provider"))
            if run.get("status") != "completed" or run.get("succeeded") != len(datasets[model]["questions"]):
                run.update(status="running", owner_pid=os.getpid(), workers=model_limits[model], resumed_at=now(),
                           finished_at=None, batch_phase="pilot" if pilot else "full")
                run.pop("error", None)
                write_json(path / "run.json", run)
        batch.update(phase="pilot" if pilot else "full", owner_pid=os.getpid(),
                     limits={"case_workers": case_workers, "api_workers": api_workers, "per_model": per_model})
        write_json(batch_path, batch)
        for attempt in range(1 if pilot else retries + 1):
            queues = {}
            for model, data in datasets.items():
                questions = data["questions"]
                if pilot:
                    questions = [max((q for q in questions if q["family"] == "WTI"), key=lambda q: q["input_date"])]
                pending = []
                for q in questions:
                    result = root / "runs" / batch["models"][model] / "cases" / q["case_id"] / "result.json"
                    if not result.exists() or read_json(result).get("status") != "success":
                        pending.append(q)
                queues[model] = deque(pending)
            inflight, counts = {}, {m: 0 for m in queues}
            rotation = deque(queues)
            with ThreadPoolExecutor(max_workers=case_workers) as pool:
                while any(queues.values()) or inflight:
                    for _ in range(len(rotation) * per_model):
                        if len(inflight) >= case_workers:
                            break
                        model = rotation[0]
                        rotation.rotate(-1)
                        if not queues[model] or counts[model] >= model_limits[model]:
                            continue
                        q = queues[model].popleft()
                        run_path = root / "runs" / batch["models"][model]
                        future = pool.submit(manager._case, run_path, q, datasets[model]["labels"][q["case_id"]],
                                             gate, predictors[model])
                        inflight[future] = model
                        counts[model] += 1
                    done, _ = wait(inflight, timeout=30, return_when=FIRST_COMPLETED)
                    for future in done:
                        model = inflight.pop(future)
                        counts[model] -= 1
                        result = future.result()
                        run_path = root / "runs" / batch["models"][model]
                        manager._update(run_path, datasets[model]["questions"])
                        # A slow peer must not keep a finished model labelled
                        # running until the entire group has drained.
                        if not pilot and read_json(run_path / "run.json")["succeeded"] == len(datasets[model]["questions"]):
                            manager._update(run_path, datasets[model]["questions"], finish=True)
                        print(json.dumps({"model": model, "case_id": result["case_id"], "status": result["status"],
                                          "error": result.get("error"), "pass": attempt + 1}, ensure_ascii=False), flush=True)
            if pilot:
                break
            if all(read_json(root / "runs" / rid / "run.json")["succeeded"] == len(datasets[model]["questions"]) for model, rid in selected_models.items()):
                break
        for model, rid in selected_models.items():
            path = root / "runs" / rid
            manager._update(path, datasets[model]["questions"], finish=not pilot)
            row = read_json(path / "run.json")
            row["batch_phase"] = "pilot_complete" if pilot else "finished"
            if pilot:
                row["status"] = "queued"
            write_json(path / "run.json", row)
        batch.update(phase="pilot_complete" if pilot else "finished", updated_at=now())
        write_json(batch_path, batch)
    finally:
        for lock in locks.values():
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-run")
    parser.add_argument("--experiment")
    parser.add_argument("--dataset")
    parser.add_argument("--environment", default="env01", choices=("env01", "env00", "env02", "env03", "env04"))
    parser.add_argument("--reference-batch", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--batch")
    parser.add_argument("--pilot", action="store_true")
    parser.add_argument("--models", nargs="+")
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--case-workers", type=int, default=24)
    parser.add_argument("--api-workers", type=int, default=12)
    parser.add_argument("--per-model", type=int, default=3)
    args = parser.parse_args()
    models = args.models or (list(AIHUB_MODELS) if args.experiment else [m for m in AIHUB_MODELS if m != "deepseek-v4-pro"])
    if set(models) - set(AIHUB_MODELS):
        raise ValueError("模型尚未通过已有连通性测试")
    root = args.root.resolve()
    path = Path(args.batch) if args.batch else (prepare_experiment(root, args.experiment, models, args.dataset,
        environment_id=args.environment, reference_batch=args.reference_batch)
        if args.experiment else prepare(root, args.baseline_run, models))
    print(json.dumps({"batch_path": str(path)}), flush=True)
    if args.prepare_only:
        return
    execute(root, path, pilot=args.pilot, retries=args.retries,
            case_workers=args.case_workers, api_workers=args.api_workers, per_model=args.per_model, models=args.models)


if __name__ == "__main__":
    main()
