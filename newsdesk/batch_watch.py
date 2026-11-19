"""Finish an already-launched comparison batch, retry failures and audit results."""

import argparse
from datetime import datetime
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import time

from newsdesk.batch_audit import audit_run
from newsdesk.batch_dependencies import dependency_ready
from newsdesk.evaluation import DEFAULT_ROOT, _locked, now, read_json, write_json


def watch(path, recovery_limit=2, root=DEFAULT_ROOT):
    lock = path.with_suffix(".watch.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    state_path = path.with_name(path.stem + "-execution.json")
    state = read_json(state_path) if state_path.exists() else {"recoveries": {}}
    state.update(owner_pid=os.getpid(), status="running", started_at=state.get("started_at", now()))
    children = {}
    batch = read_json(path)
    dependency = batch.get("depends_on_experiment")
    while dependency and not dependency_ready(root, dependency):
        state.update(status="waiting_dependency", depends_on_experiment=dependency, updated_at=now())
        write_json(state_path, state)
        time.sleep(30)
    state.update(status="running")
    while True:
        batch = read_json(path)
        pending, groups = False, []
        for group in batch["groups"]:
            group_path = path.parent / group["path"]
            config = read_json(group_path)
            models = {m: rid for m, rid in config["models"].items()
                      if not group.get("models") or m in group["models"]}
            records = [read_json(root / "runs" / rid / "run.json") for rid in models.values()]
            busy = any(_locked(root / "runs" / rid / "execution.lock") for rid in models.values())
            # A maintenance worker verifies large frozen snapshots before it
            # takes the first run lock. Give that known new process time to
            # acquire locks instead of launching a competing recovery.
            maintenance = batch.get("maintenance_restart_file")
            if not busy and maintenance:
                restart = read_json(path.parent / maintenance)
                started = restart.get("groups", {}).get(group["path"], {})
                if started.get("new_pid") and started.get("resumed_at"):
                    age = time.time() - datetime.fromisoformat(started["resumed_at"]).timestamp()
                    if 0 <= age < 60:
                        try:
                            os.kill(started["new_pid"], 0)
                            busy = True
                        except ProcessLookupError:
                            pass
            complete = all(r["succeeded"] == r["total"] and r["status"] == "completed" for r in records)
            child = children.get(group["path"])
            if child and child.poll() is None:
                busy = True
            recovered = state["recoveries"].get(group["path"], 0)
            row = {"group": group["path"], "active": busy, "complete": complete,
                   "succeeded": sum(r["succeeded"] for r in records), "total": sum(r["total"] for r in records),
                   "failed": sum(r["failed"] for r in records), "recovery_passes": recovered}
            if busy:
                pending = True
            elif not complete and recovered < recovery_limit:
                command = [sys.executable, "-u", "-m", "newsdesk.batch_evaluation", "--batch", str(group_path),
                           "--root", str(root),
                           "--retries", "0", "--api-workers", str(group["api_workers"]),
                           "--case-workers", str(group["case_workers"]), "--per-model", str(group["per_model"])]
                if group.get("models"):
                    command += ["--models"] + group["models"]
                log = path.parent / (group_path.stem + "-recovery-{}.log".format(recovered + 1))
                with log.open("a") as output:
                    os.chmod(log, 0o600)
                    children[group["path"]] = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
                state["recoveries"][group["path"]] = recovered + 1
                row.update(active=True, recovery_passes=recovered + 1)
                pending = True
            groups.append(row)
        state.update(groups=groups, updated_at=now())
        write_json(state_path, state)
        if not pending:
            break
        time.sleep(30)
    baseline = read_json(root / "runs" / (batch.get("snapshot_run_id") or batch["baseline_run_id"]) / "run.json")
    try:
        verified = [audit_run(root, rid, baseline) for rid in batch["models"].values()]
        state.update(status="completed" if all(r["complete"] for r in verified) else "partial", validation=verified)
    except Exception as error:
        state.update(status="audit_failed", audit_error=str(error))
        raise
    finally:
        state["finished_at"] = now()
        write_json(state_path, state)
        batch.update(phase=state["status"], finished_at=state["finished_at"], execution_file=state_path.name)
        write_json(path, batch)
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--recovery-limit", type=int, default=2)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()
    watch(args.batch.resolve(), args.recovery_limit, args.root.resolve())


if __name__ == "__main__":
    main()
