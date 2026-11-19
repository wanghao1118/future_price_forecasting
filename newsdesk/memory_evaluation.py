"""Independent, frozen and resumable closed-book historical-memory experiments.

Predictions receive only one question string. Reference answers and citations are
kept in a separate snapshot and are used exclusively by the Codex evaluator.
"""

import argparse
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import threading
import uuid

from newsdesk.evaluation import AIHUB_MODELS
from newsdesk.memory_leakage import load_dataset, digest


DEFAULT_ROOT = Path(__file__).resolve().parent / "data" / "memory-evaluations"
PROTOCOL = "env00-extend-memory-closed-book-v1"
ENVIRONMENT_ID = "env00-extend"
TOOL_MODE = "closed_book_no_tools"
JUDGE_PROTOCOL = "codex-semantic-cloze-judge-v1"
SCORING_POLICY = "binary-correct-over-all-questions-v1"
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,99}\Z")
_FAILED = {"prediction_failed", "judge_failed", "tool_violation"}
_PROFILE_KEYS = {"api_protocol", "provider", "reasoning_effort", "client_timeout_seconds", "gateway_timeout_seconds"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    """Atomic replacement, private permissions, unique temporary files per writer."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


@contextmanager
def file_lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("记忆评估已由另一进程运行或准备")
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def safe_error(error):
    message = str(error)[:4000]
    key = os.environ.get("AIHUB_API_KEY")
    if key:
        message = message.replace(key, "[redacted]")
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", message)
    return "{}: {}".format(type(error).__name__, message)


def metric(cases):
    total = len(cases)
    graded = sum(x.get("status") == "graded" and type(x.get("correct")) is bool for x in cases)
    correct = sum(x.get("status") == "graded" and x.get("correct") is True for x in cases)
    failed = sum(x.get("status") in _FAILED for x in cases)
    resolved = graded + failed
    return {"correct": correct, "wrong": resolved - correct, "incorrect": resolved - correct,
            "graded": graded, "resolved": resolved, "completed": resolved, "total": total,
            "accuracy": correct / total if total else None,
            "completion": resolved / total if total else 0.0,
            "complete": total > 0 and resolved == total, "failed": failed,
            "pending": total - resolved, "scoring_policy": SCORING_POLICY}


def closed_book_audit(result):
    audit = result.get("tool_audit")
    if not isinstance(audit, dict) or audit.get("passed") is not True:
        return False
    if audit.get("tools_enabled") is not False or audit.get("web_search_enabled") is not False:
        return False
    if audit.get("network_retrieval_enabled") is not False or audit.get("tool_calls") != []:
        return False
    return all(audit.get(key, 0) == 0 for key in
               ("tool_call_count", "search_call_count", "indicator_call_count"))


class MemoryEvaluationManager:
    def __init__(self, root=DEFAULT_ROOT, dataset_loader=None, predictor_factory=None, evaluator_factory=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.dataset_loader = dataset_loader or load_dataset
        self.predictor_factory = predictor_factory
        self.evaluator_factory = evaluator_factory
        self._lock = threading.RLock()

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
            raise ValueError("记忆评估编号无效")
        return value

    def _batch_dir(self, experiment_id):
        return self.root / "experiments" / self._identifier(experiment_id)

    def _run_dir(self, run_id):
        self._identifier(run_id)
        if not run_id.startswith("memory-"):
            raise ValueError("不是记忆评估运行编号")
        path = self.root / "runs" / run_id
        if not (path / "run.json").is_file():
            raise KeyError("记忆评估运行不存在")
        return path

    def _profiles(self, models):
        previous = {}
        price_root = self.root.parent / "evaluations"
        for experiment in ("Exp01", "Exp00"):
            batch_path = price_root / "batches" / (experiment + ".json")
            if not batch_path.is_file():
                continue
            batch = read_json(batch_path)
            for model, run_id in batch.get("models", {}).items():
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(run_id)):
                    continue
                path = price_root / "runs" / run_id / "run.json"
                if path.is_file():
                    config = read_json(path).get("generation_config", {})
                    previous[model] = {key: value for key, value in config.items() if key in _PROFILE_KEYS}
        profiles = {}
        for model in models:
            profiles[model] = {"max_output_tokens": 4000 if model.startswith("gpt-") else 8000,
                               "max_rounds": 1, "client_timeout_seconds": 300,
                               "gateway_timeout_seconds": 240,
                               **previous.get(model, {})}
            # Known connected-model settings; no authentication values are saved.
            if model == "gpt-6-luna":
                profiles[model].update(api_protocol="chat", provider="azure", reasoning_effort="none")
            elif model == "gpt-6-sol":
                profiles[model].setdefault("provider", "azure")
            elif model == "claude-haiku-5-5":
                profiles[model].setdefault("provider", "anthropic")
        return profiles

    def prepare(self, experiment_id="Exp00-extend", models=None):
        models = list(AIHUB_MODELS) if models is None else list(models)
        if not models or len(set(models)) != len(models) or set(models) - set(AIHUB_MODELS):
            raise ValueError("记忆评估模型清单无效")
        directory = self._batch_dir(experiment_id)
        with file_lock(self.root / "locks" / (experiment_id + ".lock")):
            if (directory / "batch.json").is_file():
                existing = read_json(directory / "batch.json")
                if set(existing["models"]) != set(models):
                    raise ValueError("实验已冻结，请保留原模型清单")
                self._verify(existing)
                return existing
            dataset = self.dataset_loader()
            if dataset.get("total_questions") != 100 or len(dataset.get("questions", [])) != 100:
                raise ValueError("记忆评估必须冻结完整100题")
            if Counter(x["month"] for x in dataset["questions"]) != Counter({"2026-{:02d}".format(m): 10 for m in range(1, 11)}):
                raise ValueError("记忆评估需要1—10月各10题")
            if len({x["case_id"] for x in dataset["questions"]}) != 100:
                raise ValueError("记忆题号重复")
            for question in dataset["questions"]:
                self._identifier(question["case_id"])
            public = {"dataset_id": dataset["dataset_id"], "questions": [
                {"case_id": x["case_id"], "question": x["question"]} for x in dataset["questions"]]}
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(directory / "reference-dataset.json", dataset)
            write_json(directory / "questions.json", public)
            profiles = self._profiles(models)
            write_json(directory / "profiles.json", profiles)
            project = Path(__file__).resolve().parent.parent
            hashes = {}
            for package in ("newsdesk", "time_gate_agent"):
                for path in sorted((project / package).glob("*.py")):
                    relative = path.relative_to(project)
                    target = directory / "source" / relative
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(path, target)
                    hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
            batch = {"batch_id": experiment_id, "experiment_id": experiment_id,
                     "experiment_name": experiment_id, "benchmark": "memory-leakage",
                     "environment_id": ENVIRONMENT_ID, "protocol": PROTOCOL,
                     "tool_mode": TOOL_MODE, "evaluator_protocol": JUDGE_PROTOCOL,
                     "scoring_policy": SCORING_POLICY,
                     "dataset_id": dataset["dataset_id"], "dataset_version": dataset["version"],
                     "created_at": now(), "status": "prepared", "models": {},
                     "total_per_model": 100, "expected_total": 100 * len(models),
                     "dataset_snapshot_sha256": digest(dataset), "questions_sha256": digest(public),
                     "profiles_sha256": digest(profiles), "generation_profiles": profiles,
                     "code_sha256": hashes, "code_frozen": True,
                     "retry_policy": "Successful predictions and accepted judgments are immutable; failed judgments reuse the original prediction."}
            for model in models:
                run_id = "memory-{}-{}".format(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:10])
                record = {key: batch[key] for key in ("experiment_id", "experiment_name", "benchmark", "environment_id", "protocol", "tool_mode", "evaluator_protocol", "dataset_id", "dataset_version", "created_at", "dataset_snapshot_sha256", "questions_sha256", "profiles_sha256")}
                record.update(run_id=run_id, model=model, model_id=model, status="prepared", total=100,
                              generation_config=profiles[model], code_frozen=True)
                write_json(self.root / "runs" / run_id / "run.json", record)
                batch["models"][model] = run_id
            write_json(directory / "batch.json", batch)
            return batch

    def revise_profiles(self, experiment_id, overrides, reason):
        """Archive and revise authorized model settings after a phase exits.

        The execution lock prevents any live runner from observing mixed
        profiles. Frozen code, prompts, responses and judgments are untouched.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("路由修订必须记录非空原因")
        if not isinstance(overrides, dict) or not overrides:
            raise ValueError("路由修订参数不能为空")
        for model, fields in overrides.items():
            if model == "hy4-preview":
                if fields != {"reasoning_effort": "none"}:
                    raise ValueError("HY4修订仅允许reasoning_effort=none，不改变预算或路由")
            elif model in {"gpt-6-astra", "claude-fable-5-1"}:
                if not isinstance(fields, dict) or not fields or set(fields) - {"api_protocol", "provider"}:
                    raise ValueError("Astra和Fable修订仅允许api_protocol和provider")
            else:
                raise ValueError("本次仅授权Astra、Fable路由及HY4指定推理模式修订")
            if "api_protocol" in fields and fields["api_protocol"] not in {"chat", "responses"}:
                raise ValueError("无效API协议")
            if "provider" in fields and (not isinstance(fields["provider"], str) or
                    not re.fullmatch(r"[a-z0-9_-]{1,64}", fields["provider"])):
                raise ValueError("无效提供方")
        directory = self._batch_dir(experiment_id)
        with file_lock(self.root / "locks" / (experiment_id + ".lock")):
            batch = read_json(directory / "batch.json")
            self._verify(batch)
            if set(overrides) - set(batch["models"]):
                raise ValueError("修订型号不属于本实验")
            profiles = read_json(directory / "profiles.json")
            revised = json.loads(json.dumps(profiles))
            for model, fields in overrides.items():
                revised[model].update(fields)
            if revised == profiles:
                return batch
            run_records = {model: read_json(self._run_dir(run_id) / "run.json")
                           for model, run_id in batch["models"].items()}
            revision_id = "routing-{}-{}".format(datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"), uuid.uuid4().hex[:8])
            archive = {"revision_id": revision_id, "created_at": now(), "reason": reason.strip(),
                       "overrides": overrides, "previous_profiles_sha256": batch["profiles_sha256"],
                       "new_profiles_sha256": digest(revised), "previous_profiles": profiles,
                       "previous_batch": batch, "previous_run_metadata": run_records}
            archive_path = directory / "profile-revisions" / (revision_id + ".json")
            write_json(archive_path, archive)
            entry = {"revision_id": revision_id, "created_at": archive["created_at"],
                     "reason": archive["reason"], "overrides": overrides,
                     "previous_profiles_sha256": archive["previous_profiles_sha256"],
                     "new_profiles_sha256": archive["new_profiles_sha256"],
                     "archive": str(archive_path.relative_to(self.root)), "archive_sha256": digest(archive)}
            updated_batch = {**batch, "profiles_sha256": digest(revised), "generation_profiles": revised,
                             "profile_revisions": [*batch.get("profile_revisions", []), entry], "updated_at": now()}
            try:
                for model, record in run_records.items():
                    updated = {**record, "profiles_sha256": digest(revised)}
                    if model in overrides:
                        updated["generation_config"] = revised[model]
                        updated["config_history"] = [*record.get("config_history", []),
                            {**entry, "previous_generation_config": record["generation_config"],
                             "generation_config": revised[model]}]
                    write_json(self._run_dir(record["run_id"]) / "run.json", updated)
                write_json(directory / "profiles.json", revised)
                # Publish the new fingerprint only after all dependent files.
                write_json(directory / "batch.json", updated_batch)
            except Exception:
                for record in run_records.values():
                    write_json(self._run_dir(record["run_id"]) / "run.json", record)
                write_json(directory / "profiles.json", profiles)
                write_json(directory / "batch.json", batch)
                raise
            return updated_batch

    def _verify(self, batch):
        directory = self._batch_dir(batch["experiment_id"])
        for filename, key in (("reference-dataset.json", "dataset_snapshot_sha256"),
                              ("questions.json", "questions_sha256"), ("profiles.json", "profiles_sha256")):
            if digest(read_json(directory / filename)) != batch[key]:
                raise ValueError("冻结记忆评估快照校验失败：" + filename)
        for relative, expected in batch.get("code_sha256", {}).items():
            path = directory / "source" / relative
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError("冻结记忆评估源码校验失败：" + relative)
        if batch.get("protocol") != PROTOCOL or batch.get("evaluator_protocol") != JUDGE_PROTOCOL:
            raise ValueError("记忆评估协议不兼容")

    def _cases(self, run):
        directory = self._batch_dir(run["experiment_id"])
        dataset = read_json(directory / "reference-dataset.json")
        result = []
        for question in dataset["questions"]:
            path = self.root / "runs" / run["run_id"] / "cases" / question["case_id"]
            state = read_json(path / "state.json") if (path / "state.json").is_file() else {}
            item = {"case_id": question["case_id"], "question": question["question"], "month": question["month"],
                    "answer": question["answer"], "reference_answer": question["answer"],
                    "answer_aliases": question.get("answer_aliases", []), "source": question.get("source", {}),
                    "status": "pending", "model_answer": None, "correct": None, "reason": "", "error": "",
                    "attempts": {"prediction": 0, "judge": 0}, **state}
            if (path / "prediction.json").is_file():
                prediction = read_json(path / "prediction.json")
                item["model_answer"] = prediction["answer"]
                if item["status"] in {"pending", "predicting", "prediction_failed"}:
                    item["status"] = "awaiting_judge"
            if (path / "verdict.json").is_file():
                verdict = read_json(path / "verdict.json")
                item.update(status="graded", correct=verdict["correct"], reason=verdict["reason"], error="", evaluator=verdict)
            item["outcome"] = ("correct" if item["correct"] is True else "incorrect") if item["status"] == "graded" or item["status"] in _FAILED else None
            result.append(item)
        return result

    def get_run(self, run_id):
        run = read_json(self._run_dir(run_id) / "run.json")
        cases = self._cases(run)
        overall = metric(cases)
        months = [{"month": month, **metric([x for x in cases if x["month"] == month])}
                  for month in sorted({x["month"] for x in cases})]
        status = "completed" if overall["complete"] else run["status"]
        if status == "completed" and not overall["complete"]:
            status = "partial"
        return {**run, **overall, "status": status, "completed": overall["resolved"],
                "succeeded": overall["correct"], "provisional": not overall["complete"],
                "metrics": {"overall": overall, "months": months}, "cases": cases}

    def catalog(self):
        runs = []
        for path in (self.root / "runs").glob("memory-*/run.json"):
            try:
                run = self.get_run(path.parent.name)
                runs.append({key: value for key, value in run.items() if key != "cases"})
            except (OSError, ValueError, KeyError):
                continue
        runs.sort(key=lambda x: (x["created_at"], x["model"]), reverse=True)
        by_id = {x["run_id"]: x for x in runs}
        experiments = []
        for path in (self.root / "experiments").glob("*/batch.json"):
            try:
                batch = read_json(path)
                selected = [by_id[rid] for rid in batch["models"].values() if rid in by_id]
                completed = sum(x["resolved"] for x in selected)
                graded = sum(x["graded"] for x in selected)
                correct = sum(x["correct"] for x in selected)
                failed = sum(x["failed"] for x in selected)
                complete = completed == batch["expected_total"]
                experiments.append({**batch, "status": "completed" if complete else batch["status"],
                                    "completed": completed, "resolved": completed, "graded": graded, "correct": correct,
                                    "wrong": completed - correct, "incorrect": completed - correct,
                                    "succeeded": correct, "failed": failed,
                                    "pending": batch["expected_total"] - completed,
                                    "total": batch["expected_total"], "complete": complete,
                                    "provisional": not complete,
                                    "accuracy": correct / batch["expected_total"],
                                    "completion": completed / batch["expected_total"], "scoring_policy": SCORING_POLICY})
            except (OSError, ValueError, KeyError):
                continue
        experiments.sort(key=lambda x: x["created_at"], reverse=True)
        return {"experiments": experiments, "runs": runs,
                "models": [{"id": model, "name": model} for model in AIHUB_MODELS]}

    def get_case_trace(self, run_id, case_id):
        run = self.get_run(run_id)
        self._identifier(case_id)
        case = next((x for x in run["cases"] if x["case_id"] == case_id), None)
        if case is None:
            raise KeyError("记忆评估题目不存在")
        path = self._run_dir(run_id) / "cases" / case_id
        trace = {"run_id": run_id, **case, "protocol": PROTOCOL,
                 "evaluator_protocol": JUDGE_PROTOCOL, "artifacts": []}
        for filename, key in (("prediction.json", "prediction"), ("verdict.json", "evaluator")):
            if (path / filename).is_file():
                trace[key] = read_json(path / filename)
        if (path / "attempts.json").is_file():
            trace["attempt_history"] = read_json(path / "attempts.json")
        # Include complete public round trips; referenced judge directories are
        # validated as descendants before any file is read.
        directories = [("prediction", p) for p in sorted((path / "attempts").glob("prediction-*")) if p.is_dir()]
        for reference in (path / "judge-artifacts.json",):
            if reference.is_file():
                for relative in read_json(reference):
                    target = (self.root / relative).resolve()
                    if self.root in target.parents and target.is_dir():
                        directories.append(("evaluator", target))
        for stage, directory in directories:
            for file in sorted(directory.rglob("*")):
                if file.is_file() and file.suffix in {".json", ".jsonl", ".txt", ".log", ".md"}:
                    trace["artifacts"].append({"stage": stage, "name": str(file.relative_to(self.root)),
                                               "content": file.read_text(encoding="utf-8", errors="replace")})
        return trace

    def _save_state(self, run_id, case):
        keys = ("status", "correct", "reason", "error", "attempts", "updated_at")
        case["updated_at"] = now()
        write_json(self.root / "runs" / run_id / "cases" / case["case_id"] / "state.json",
                   {key: case[key] for key in keys if key in case})

    def _recover_committed_helpers(self, run, questions):
        """Recover helper receipts committed just before a coordinator crash.

        Only complete, hash-verified and capability-audited helper output can
        become an immutable result. Read-only catalog requests never recover.
        """
        for question in questions:
            path = self.root / "runs" / run["run_id"] / "cases" / question["case_id"]
            prediction_path = path / "prediction.json"
            if not prediction_path.exists():
                for work in sorted((path / "attempts").glob("prediction-*"), reverse=True):
                    try:
                        receipt = read_json(work / "gate-receipt.json")
                        prompt = read_json(work / "prompt.json")
                        raw = (work / "raw-output.txt").read_text(encoding="utf-8")
                        config = read_json(work / "generation-config.json")
                        if (not closed_book_audit({"tool_audit": receipt}) or not raw.strip()
                                or prompt.get("question") != question["question"]
                                or receipt.get("prompt_sha256") != digest(prompt)
                                or receipt.get("response_sha256") != hashlib.sha256(raw.encode("utf-8")).hexdigest()):
                            continue
                        write_json(prediction_path, {"answer": raw.strip(), "raw_output": raw,
                            "prompt_sha256": receipt["prompt_sha256"], "response_sha256": receipt["response_sha256"],
                            "tool_audit": receipt, "generation_config": config,
                            "recovered_from": str(work.relative_to(self.root))})
                        break
                    except (OSError, ValueError, KeyError):
                        continue
            if not prediction_path.exists() or (path / "verdict.json").exists() or not (path / "judge-artifacts.json").is_file():
                continue
            prediction = read_json(prediction_path)
            expected = {key: question[key] for key in ("case_id", "question", "answer", "answer_aliases")}
            expected["model_answer"] = prediction["answer"]
            for relative in reversed(read_json(path / "judge-artifacts.json")):
                work = (self.root / relative).resolve()
                if self.root not in work.parents:
                    continue
                try:
                    receipt = read_json(work / "gate-receipt.json")
                    material = read_json(work / "evaluator-request.json")
                    verdicts = read_json(work / "verdicts.json")
                    if (receipt.get("passed") is not True or receipt.get("independent") is not True
                            or receipt.get("evaluator") != "codex_cli"
                            or receipt.get("prompt_sha256") != digest(material)
                            or receipt.get("verdicts_sha256") != digest(verdicts)
                            or expected not in material.get("items", [])):
                        continue
                    verdict = next((v for v in verdicts if v.get("case_id") == question["case_id"]), None)
                    if verdict is None or type(verdict.get("correct")) is not bool or not isinstance(verdict.get("reason"), str) or not verdict["reason"].strip():
                        continue
                    write_json(path / "verdict.json", {**verdict, "judge_protocol": JUDGE_PROTOCOL,
                               "judged_at": now(), "work": str(work.relative_to(self.root)), "recovered": True})
                    break
                except (OSError, ValueError, KeyError):
                    continue

    def _record_attempt(self, run_id, case, stage, work, error=None):
        path = self.root / "runs" / run_id / "cases" / case["case_id"] / "attempts.json"
        history = read_json(path) if path.is_file() else []
        history.append({"stage": stage, "number": case["attempts"][stage], "at": now(),
                        "work": str(work.relative_to(self.root)), "error": error or "", "succeeded": error is None})
        write_json(path, history)

    def _predict(self, model, question, profile, work):
        factory = self.predictor_factory
        if factory is None:
            from newsdesk.memory_model import ClosedBookPredictor
            factory = ClosedBookPredictor
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        result = factory(model, generation_config=profile).predict(question, work)
        if not isinstance(result, dict) or not isinstance(result.get("answer"), str):
            raise ValueError("闭卷模型响应缺少文本答案")
        return result

    def _judge(self, rows, work):
        factory = self.evaluator_factory
        if factory is None:
            from newsdesk.memory_model import CodexMemoryEvaluator
            factory = CodexMemoryEvaluator
        work.mkdir(parents=True, exist_ok=True, mode=0o700)
        results = factory().evaluate(rows, work)
        if not isinstance(results, list) or len(results) != len(rows):
            raise ValueError("评估Agent未返回完整批次")
        expected = {row["case_id"] for row in rows}
        if {row.get("case_id") for row in results} != expected:
            raise ValueError("评估Agent题号不匹配")
        for row in results:
            if type(row.get("correct")) is not bool or not isinstance(row.get("reason"), str) or not row["reason"].strip():
                raise ValueError("评估Agent判分格式无效")
            if row.get("tool_audit", {}).get("passed") is False:
                raise ValueError("评估Agent工具审计失败")
        return results

    def run(self, experiment_id="Exp00-extend", workers=12, per_model=2, judge_workers=3,
            retry_failed=False, max_attempts=2, judge_batch_size=20):
        for value, high in ((workers, 64), (per_model, 16), (judge_workers, 16), (max_attempts, 10), (judge_batch_size, 20)):
            if type(value) is not int or not 1 <= value <= high:
                raise ValueError("记忆评估并发或重试参数无效")
        directory = self._batch_dir(experiment_id)
        batch = read_json(directory / "batch.json")
        self._verify(batch)
        with file_lock(self.root / "locks" / (experiment_id + ".lock")):
            locks = []
            try:
                for run_id in batch["models"].values():
                    lock = file_lock(self.root / "runs" / run_id / ".run.lock")
                    lock.__enter__(); locks.append(lock)
                return self._execute(batch, workers, per_model, judge_workers, retry_failed, max_attempts, judge_batch_size)
            finally:
                for lock in reversed(locks):
                    lock.__exit__(None, None, None)

    def _execute(self, batch, workers, per_model, judge_workers, retry_failed, max_attempts, judge_batch_size):
        directory = self._batch_dir(batch["experiment_id"])
        batch.update(status="running", started_at=batch.get("started_at") or now(), updated_at=now())
        write_json(directory / "batch.json", batch)
        cases, records = {}, {}
        model_order = deque(batch["models"])
        predicted_this_run, judged_this_run = set(), set()
        for model, run_id in batch["models"].items():
            records[model] = read_json(self._run_dir(run_id) / "run.json")
            records[model].update(status="running", started_at=records[model].get("started_at") or now())
            write_json(self._run_dir(run_id) / "run.json", records[model])
            self._recover_committed_helpers(records[model], read_json(directory / "reference-dataset.json")["questions"])
            cases[model] = self._cases(records[model])
            # Stale in-progress states follow the existence of immutable outputs.
            for case in cases[model]:
                if case["status"] == "predicting":case["status"] = "pending"
                if case["status"] == "judging":case["status"] = "awaiting_judge"
        active_models = Counter()
        prediction_futures, judge_futures = {}, {}
        with ThreadPoolExecutor(max_workers=workers) as predictors, ThreadPoolExecutor(max_workers=judge_workers) as judges:
            while True:
                # Rotate after each submission; no model monopolizes the queue.
                checked = 0
                while len(prediction_futures) < workers and checked < len(model_order):
                    model = model_order[0];model_order.rotate(-1);checked += 1
                    if active_models[model] >= per_model:continue
                    candidate = next((x for x in cases[model] if (model, x["case_id"]) not in predicted_this_run
                        and x["status"] in ({"pending", "prediction_failed", "tool_violation"} if retry_failed else {"pending"})
                        and x["attempts"].get("prediction", 0) < max_attempts), None)
                    if candidate is None:continue
                    checked = 0
                    run_id = batch["models"][model]
                    candidate["attempts"]["prediction"] = candidate["attempts"].get("prediction", 0) + 1
                    candidate.update(status="predicting", error="", correct=None)
                    self._save_state(run_id, candidate)
                    work = self.root / "runs" / run_id / "cases" / candidate["case_id"] / "attempts" / ("prediction-{:03d}".format(candidate["attempts"]["prediction"]))
                    future = predictors.submit(self._predict, model, candidate["question"], records[model]["generation_config"], work)
                    prediction_futures[future] = (model, candidate, work)
                    active_models[model] += 1;predicted_this_run.add((model,candidate["case_id"]))
                # Judges operate concurrently as soon as enough answers exist.
                for model in list(model_order):
                    if len(judge_futures) >= judge_workers:break
                    eligible = [x for x in cases[model] if (model, x["case_id"]) not in judged_this_run
                                and x["status"] in ({"awaiting_judge", "judge_failed"} if retry_failed else {"awaiting_judge"})
                                and x["attempts"].get("judge", 0) < max_attempts]
                    more_predictions = any(x["status"] in {"pending", "predicting"} for x in cases[model])
                    if not eligible or len(eligible) < judge_batch_size and more_predictions and prediction_futures:continue
                    selected = eligible[:judge_batch_size]
                    run_id = batch["models"][model]
                    work = self.root / "runs" / run_id / "judges" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8])
                    rows = []
                    for case in selected:
                        case["attempts"]["judge"] = case["attempts"].get("judge", 0) + 1
                        case.update(status="judging", error="")
                        self._save_state(run_id, case)
                        references_path = self.root / "runs" / run_id / "cases" / case["case_id"] / "judge-artifacts.json"
                        references = read_json(references_path) if references_path.exists() else []
                        references.append(str(work.relative_to(self.root)));write_json(references_path, references)
                        rows.append({key: case[key] for key in ("case_id", "question", "answer", "answer_aliases", "model_answer")})
                        judged_this_run.add((model,case["case_id"]))
                    judge_futures[judges.submit(self._judge, rows, work)] = (model, selected, work)
                all_futures = set(prediction_futures) | set(judge_futures)
                if not all_futures:break
                completed, _ = wait(all_futures, timeout=1, return_when=FIRST_COMPLETED)
                for future in completed:
                    if future in prediction_futures:
                        model, case, work = prediction_futures.pop(future);active_models[model] -= 1
                        run_id = batch["models"][model]
                        error = None
                        try:
                            prediction = future.result()
                            if not closed_book_audit(prediction):
                                write_json(work / "rejected-prediction.json", prediction)
                                case.update(status="tool_violation", error="闭卷预测工具审计未通过", correct=None)
                                error = case["error"]
                            else:
                                target = self.root / "runs" / run_id / "cases" / case["case_id"] / "prediction.json"
                                if target.exists():raise RuntimeError("拒绝覆盖已保存的成功预测")
                                write_json(target, prediction)
                                case.update(status="awaiting_judge", model_answer=prediction["answer"], error="")
                        except Exception as failure:
                            error = safe_error(failure);case.update(status="prediction_failed", error=error, correct=None)
                        self._record_attempt(run_id,case,"prediction",work,error)
                        self._save_state(run_id,case)
                    else:
                        model, selected, work = judge_futures.pop(future);run_id=batch["models"][model]
                        try:
                            verdicts = {x["case_id"]:x for x in future.result()}
                            for case in selected:
                                verdict = verdicts[case["case_id"]]
                                target = self.root / "runs" / run_id / "cases" / case["case_id"] / "verdict.json"
                                if target.exists():raise RuntimeError("拒绝覆盖已保存的成功判分")
                                write_json(target,{**verdict,"judge_protocol":JUDGE_PROTOCOL,"judged_at":now(),"work":str(work.relative_to(self.root))})
                                case.update(status="graded",correct=verdict["correct"],reason=verdict["reason"],error="")
                                self._record_attempt(run_id,case,"judge",work)
                                self._save_state(run_id,case)
                        except Exception as failure:
                            error=safe_error(failure)
                            for case in selected:
                                if case["status"]=="graded":continue
                                case.update(status="judge_failed",error=error,correct=None)
                                self._record_attempt(run_id,case,"judge",work,error)
                                self._save_state(run_id,case)
        for model, run_id in batch["models"].items():
            summary=self.get_run(run_id)
            records[model].update(status="completed" if summary["complete"] else "partial",finished_at=now())
            write_json(self._run_dir(run_id)/"run.json",records[model])
        final=self.catalog()
        selected=next(x for x in final["experiments"] if x["experiment_id"]==batch["experiment_id"])
        batch.update(status="completed" if selected["complete"] else "partial",finished_at=now(),updated_at=now())
        write_json(directory/"batch.json",batch)
        return next(x for x in self.catalog()["experiments"] if x["experiment_id"]==batch["experiment_id"])


def main():
    parser=argparse.ArgumentParser(description="独立闭卷记忆评估；不会读取新闻或运行期货预测")
    parser.add_argument("action",choices=["prepare","run","status"])
    parser.add_argument("--root",type=Path,default=DEFAULT_ROOT)
    parser.add_argument("--experiment",default="Exp00-extend")
    parser.add_argument("--models",nargs="+",choices=AIHUB_MODELS)
    parser.add_argument("--workers",type=int,default=12)
    parser.add_argument("--per-model",type=int,default=2)
    parser.add_argument("--judge-workers",type=int,default=3)
    parser.add_argument("--judge-batch-size",type=int,default=20)
    parser.add_argument("--max-attempts",type=int,default=2)
    parser.add_argument("--retry-failed",action="store_true")
    args=parser.parse_args();manager=MemoryEvaluationManager(args.root)
    if args.action=="prepare":result=manager.prepare(args.experiment,args.models)
    elif args.action=="status":result=manager.catalog()
    else:
        directory=manager._batch_dir(args.experiment)
        if not (directory/"batch.json").exists():manager.prepare(args.experiment,args.models)
        batch=read_json(directory/"batch.json");manager._verify(batch)
        frozen=(directory/"source"/"newsdesk"/"memory_evaluation.py").resolve()
        if Path(__file__).resolve()!=frozen:
            # Resume executes the originally frozen implementations and profiles.
            command=[sys.executable,"-m","newsdesk.memory_evaluation","run","--root",str(manager.root),
                     "--experiment",args.experiment,"--workers",str(args.workers),"--per-model",str(args.per_model),
                     "--judge-workers",str(args.judge_workers),"--judge-batch-size",str(args.judge_batch_size),
                     "--max-attempts",str(args.max_attempts)]
            if args.retry_failed:command.append("--retry-failed")
            os.chdir(directory/"source");os.execv(sys.executable,command)
        result=manager.run(args.experiment,args.workers,args.per_model,args.judge_workers,args.retry_failed,args.max_attempts,args.judge_batch_size)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
