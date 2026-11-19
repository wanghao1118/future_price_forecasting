"""Server-owned, restartable Exp01 predictions for each new live question.

Discovery never calls a model. A separate, globally locked worker freezes and
executes one batch per economic question, reusing older combined batches too.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from newsdesk.benchmark import BenchmarkStore
from newsdesk.evaluation import (DEFAULT_ROOT, EVALUATION_PROTOCOL, _validate_live_open,
                                digest, now, read_json, write_json)
from newsdesk.live_prediction_batch import prepare_live_batch, execute_live_batch


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATE_NAME = "automatic-live.json"
RECOVERY_LIMIT = 3
RECOVERY_DELAY = 60
TERMINAL_PHASES = {"awaiting_settlement", "partial"}


def question_key(question):
    return tuple(question[field] for field in ("family", "contract_id", "input_date", "target_date"))


def _job_id(key):
    return "Exp01-" + digest(key)[:20]


def _state(root):
    path = Path(root) / "live-batches" / STATE_NAME
    return read_json(path) if path.exists() else {"enabled": True, "jobs": {}}


def dispatcher_running(root):
    path = Path(root) / "live-batches" / "automatic-dispatch.lock"
    if not path.exists():
        return False
    with path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def _quarantine_missing_batch(path, error):
    """Keep a stale batch for audit while allowing its question to be rebuilt.

    A missing frozen run is a permanent integrity failure. Leaving its batch
    in the discovery directory makes every subsequent publication scan fail
    before it can create a fresh non-Claude batch for the same question.
    """
    path = Path(path)
    if not path.is_file():
        return None
    quarantine = path.parent / "quarantine"
    quarantine.mkdir(parents=True, mode=0o700, exist_ok=True)
    target = quarantine / (path.name + ".corrupt-{}".format(int(time.time())))
    os.replace(path, target)
    return {"type": "CorruptLiveBatch", "batch": path.name,
            "quarantined_path": str(target), "message": str(error)[:500]}
def prediction_plan(root, benchmarks):
    """Use verified publications and all batch claims, independent of versions.

    Existing terminal batches count as an attempt even when a provider failed.
    Prepared/running batches are resumed from their original frozen inputs.
    """
    root = Path(root)
    claims, errors = {}, []
    for path in sorted((root / "live-batches").glob("live-Exp01-*.json")):
        try:
            batch = read_json(path)
            if not isinstance(batch.get("models"), dict) or not batch.get("questions"):
                continue  # Independent validation reports are not batches.
            if (batch.get("mode") != "live" or batch.get("experiment_id") != "Exp01"
                    or batch.get("environment_id") != "env01" or batch.get("protocol") != EVALUATION_PROTOCOL):
                raise ValueError("实盘自动预测发现不匹配的批次归属：" + path.name)
            questions = batch["questions"]
            if batch.get("snapshot_run_id"):
                frozen = read_json(root / "runs" / batch["snapshot_run_id"] / "dataset.json")
                if digest(frozen) != batch.get("dataset_snapshot_sha256"):
                    raise ValueError("实盘自动预测冻结输入校验失败：" + path.name)
                questions = frozen["questions"]
            elif any("contract_id" not in question for question in questions):
                # Compatibility with a preparation interrupted before the first
                # snapshot in the original combined-batch implementation.
                source = benchmarks.load_dataset(batch["dataset_id"], mode="live")
                identifiers = {question["case_id"] for question in questions}
                questions = [question for question in source["questions"] if question["case_id"] in identifiers]
                if len(questions) != len(identifiers):
                    raise ValueError("待恢复实盘批次的题目来源缺失")
            for question in questions:
                key = question_key(question)
                claims.setdefault(key, []).append((path, batch))
        except (OSError, ValueError, KeyError, TypeError) as error:
            quarantine = _quarantine_missing_batch(path, error)
            errors.append(quarantine or {"type": "CorruptLiveBatch", "batch": path.name,
                                         "message": str(error)[:500]})
    history = benchmarks.live_history()
    errors.extend(history["summary"].get("errors", {}).values())
    jobs, scheduled = [], set()
    for published in history["questions"]:
        if published.get("actual") is not None:
            continue
        # Read the original ready publication. Display status may refer to a
        # newer current dataset; it cannot invalidate a still-open frozen one.
        dataset = benchmarks.load_dataset(published["dataset_id"], mode="live")
        question = next(q for q in dataset["questions"] if q["case_id"] == published["case_id"])
        try:
            _validate_live_open(question)
        except ValueError:
            continue
        if question["case_id"] in dataset["labels"]:
            continue
        key = question_key(question)
        matches = claims.get(key, [])
        if len(matches) > 1:
            errors.append({"type": "DuplicateLiveBatch", "message": "同题存在多个实盘批次，停止重复派发", "question_key": key})
            continue
        batch_path = matches[0][0] if matches else None
        if matches and matches[0][1].get("phase") in TERMINAL_PHASES:
            continue
        if batch_path in scheduled and batch_path is not None:
            continue
        jobs.append({"job_id": _job_id(key), "question_key": list(key),
                     "dataset_id": published["dataset_id"], "case_id": question["case_id"],
                     "batch_path": str(batch_path) if batch_path else None,
                     "forecast_deadline": published["forecast_deadline"]})
        scheduled.add(batch_path)
    jobs.sort(key=lambda job: job["forecast_deadline"])
    return jobs, errors


def runnable_jobs(jobs, state, current=None):
    current = time.time() if current is None else current
    result = []
    for job in jobs:
        previous = state.get("jobs", {}).get(job["job_id"], {})
        if previous.get("dispatch_attempts", 0) >= RECOVERY_LIMIT:
            continue
        if previous.get("retry_after", 0) > current:
            continue
        result.append(job)
    return result


def _prepare_job(root, benchmarks, news_store, job):
    path = (Path(job["batch_path"]) if job["batch_path"] else
            prepare_live_batch(root, benchmark_store=benchmarks, news_store=news_store,
                               dataset_id=job["dataset_id"], case_ids=[job["case_id"]]))
    batch = read_json(path)
    if batch.get("phase") == "preparing":
        # Restore an older combined batch's full selection, never a substitute
        # single-question batch if one of its original members has expired.
        source = benchmarks.load_dataset(batch["dataset_id"], mode="live")
        members = {question["case_id"] for question in batch["questions"]}
        selected = [question for question in source["questions"] if question["case_id"] in members]
        if len(selected) != len(members):
            raise ValueError("中断批次原题目范围不完整，禁止创建替代批次")
        for question in selected:
            _validate_live_open(question)
            if question["case_id"] in source["labels"]:
                raise ValueError("中断批次已有结算题目，禁止重新准备实盘")
        restored = prepare_live_batch(root, benchmark_store=benchmarks, news_store=news_store,
                                      dataset_id=batch["dataset_id"], case_ids=sorted(members))
        if restored.resolve() != path.resolve():
            raise ValueError("中断实盘批次身份不一致，禁止派发替代批次")
    return path


def dispatch_once(root=DEFAULT_ROOT, benchmarks=None, news_store=None):
    """Drain open questions, discovering new publications while others run."""
    root = Path(root).resolve()
    folder = root / "live-batches"
    folder.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (folder / "automatic-dispatch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "already_running"}
        benchmarks = benchmarks or BenchmarkStore(root.parent / "benchmark")
        state = _state(root)
        state.update(enabled=True, owner_pid=os.getpid(), status="running", started_at=now(), errors=[])
        slots, attempted, futures = threading.BoundedSemaphore(4), set(), {}
        # Separate market sessions may overlap. The coordinator keeps scanning
        # while they execute; all model sessions share the same four API slots.
        with ThreadPoolExecutor(max_workers=3) as pool:
            while True:
                jobs, errors = prediction_plan(root, benchmarks)
                state["errors"] = errors
                for job in runnable_jobs(jobs, state):
                    if job["job_id"] in attempted:
                        continue
                    attempted.add(job["job_id"])
                    previous = state["jobs"].get(job["job_id"], {})
                    record = {**previous, **job, "status": "running", "started_at": now(),
                              "dispatch_attempts": previous.get("dispatch_attempts", 0) + 1}
                    state["jobs"][job["job_id"]] = record
                    write_json(folder / STATE_NAME, state)
                    try:
                        path = _prepare_job(root, benchmarks, news_store, job)
                        record["batch_path"] = str(path)
                        write_json(folder / STATE_NAME, state)
                        future = pool.submit(execute_live_batch, root, path, api_workers=4, retries=1,
                                             request_slots=slots)
                        futures[future] = record
                    except Exception as error:
                        record.update(status="failed", finished_at=now(), error=str(error)[:1000],
                                      retry_after=time.time() + RECOVERY_DELAY)
                    write_json(folder / STATE_NAME, state)
                if not futures:
                    break
                done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                for future in done:
                    record = futures.pop(future)
                    try:
                        result = future.result()
                        record.update(status=result["phase"], finished_at=now(), error=None, retry_after=0)
                    except BlockingIOError:
                        record.update(status="waiting_worker", dispatch_attempts=record["dispatch_attempts"] - 1,
                                      retry_after=time.time() + RECOVERY_DELAY)
                    except Exception as error:
                        record.update(status="failed", finished_at=now(), error=str(error)[:1000],
                                      retry_after=time.time() + RECOVERY_DELAY)
                    write_json(folder / STATE_NAME, state)
        failed = any(item.get("status") in {"failed", "partial"} for item in state["jobs"].values())
        state.update(status="partial" if failed or errors else "idle", finished_at=now(), owner_pid=None)
        write_json(folder / STATE_NAME, state)
        return state


class AutomaticLivePredictions:
    """Lightweight server monitor; slow inference runs in a detached process."""

    def __init__(self, root, benchmarks, database):
        self.root, self.benchmarks, self.database = Path(root).resolve(), benchmarks, Path(database).resolve()
        self._stop, self._wake = threading.Event(), threading.Event()
        self._thread, self._process = None, None
        self._error = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-prediction-monitor", daemon=True)
        self._thread.start()

    def notify(self):
        self._wake.set()
        return {"status": "scheduled", "experiment_id": "Exp01"}

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        # In-flight predictions retain their own execution lock across a web
        # server restart. They still enforce both submission and lock deadlines.

    def snapshot(self):
        try:
            state = _state(self.root)
        except (OSError, ValueError):
            state = {"status": "failed", "jobs": {}}
        return {**state, "enabled": True, "experiment_id": "Exp01", "error": self._error,
                "worker_running": ((self._process is not None and self._process.poll() is None)
                                   or dispatcher_running(self.root))}

    def check(self):
        if self._process is not None and self._process.poll() is None:
            return
        if dispatcher_running(self.root):
            return
        # A new checkout/test server without the reference experiment cannot
        # dispatch inference. A later installed reference is picked up normally.
        if not (self.root / "batches" / "Exp01.json").is_file():
            return
        jobs, _ = prediction_plan(self.root, self.benchmarks)
        if not runnable_jobs(jobs, _state(self.root)):
            return
        folder = self.root / "live-batches"
        folder.mkdir(parents=True, mode=0o700, exist_ok=True)
        with (folder / "automatic-live.log").open("a") as log:
            self._process = subprocess.Popen(
                [sys.executable, "-u", "-m", "newsdesk.live_prediction_auto", "--root", str(self.root),
                 "--database", str(self.database)], cwd=PROJECT_ROOT,
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)

    def _loop(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self.check()
                self._error = None
            except Exception as error:
                self._error = str(error)[:1000]
            self._wake.wait(timeout=15)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    from newsdesk.private_config import load_private_aihub_config
    from newsdesk.server import NewsStore
    load_private_aihub_config()
    database = args.database or args.root.parent / "news.sqlite3"
    dispatch_once(args.root, news_store=NewsStore(database, seed_demo=False))


if __name__ == "__main__":
    main()
