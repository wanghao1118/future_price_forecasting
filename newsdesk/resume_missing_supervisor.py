"""Run missing cases for a frozen batch one model process at a time per slot."""

import argparse
import json
from pathlib import Path
import os
import subprocess
import sys
import time


def missing_count(run_path):
    dataset = json.loads((run_path / "dataset.json").read_text(encoding="utf-8"))
    return sum(not (run_path / "cases" / q["case_id"] / "result.json").exists()
               for q in dataset["questions"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--experiment", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--slots", type=int, default=4)
    args = parser.parse_args()
    root = args.root.resolve()
    batch_path = root / "batches" / (args.experiment + ".json")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    logs = batch_path.with_name(args.experiment + "-missing-supervisor.log")
    log = logs.open("a", encoding="utf-8")
    jobs = []
    for model, run_id in batch["models"].items():
        run_path = root / "runs" / run_id
        if missing_count(run_path):
            jobs.append((model, run_id))
    log.write(json.dumps({"pid": os.getpid(), "models": [m for m, _ in jobs], "started_at": time.time()}, ensure_ascii=False) + "\n")
    log.flush()
    active = {}
    pending = list(jobs)
    while pending or active:
        while pending and len(active) < args.slots:
            model, run_id = pending.pop(0)
            child_log = batch_path.with_name(args.experiment + "-" + model + ".log").open("a", encoding="utf-8")
            cmd = [sys.executable, "-u", str(Path(__file__).resolve().with_name("resume_one_model.py")),
                   "--batch", str(batch_path), "--run-id", run_id, "--root", str(root), "--workers", "2"]
            child = subprocess.Popen(cmd, cwd=str(root.parent.parent), stdout=child_log, stderr=subprocess.STDOUT,
                                     start_new_session=True)
            active[child.pid] = (model, child, child_log)
            log.write(json.dumps({"started": model, "pid": child.pid}, ensure_ascii=False) + "\n")
            log.flush()
        for pid, (model, child, child_log) in list(active.items()):
            if child.poll() is not None:
                child_log.close()
                active.pop(pid, None)
                log.write(json.dumps({"finished": model, "pid": pid, "exit_code": child.returncode}, ensure_ascii=False) + "\n")
                log.flush()
        time.sleep(30)
    log.write(json.dumps({"finished_at": time.time()}, ensure_ascii=False) + "\n")
    log.close()


if __name__ == "__main__":
    main()
