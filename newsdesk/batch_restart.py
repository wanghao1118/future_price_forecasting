"""Drain selected batch groups, then resume with updated runtime settings."""

import argparse
import fcntl
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from newsdesk.evaluation import DEFAULT_ROOT, _locked, now, read_json, write_json


def verified_process(pid, module, path):
    result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True)
    return result.returncode == 0 and ("-m " + module) in result.stdout and Path(path).name in result.stdout


def launch(command, log):
    with log.open("a") as output:
        os.chmod(log, 0o600)
        return subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)


def restart(path, selected):
    control_lock = path.with_suffix(".restart.lock").open("a")
    fcntl.flock(control_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    batch = read_json(path)
    groups = [g for g in batch["groups"] if g["path"] in selected]
    if len(groups) != len(set(selected)):
        raise ValueError("Unknown batch group")
    state_path = path.with_name(path.stem + "-restart.json")
    state = {"owner_pid": os.getpid(), "status": "draining", "started_at": now(), "groups": {}}
    watcher = read_json(path.with_name(path.stem + "-execution.json"))
    if not verified_process(watcher["owner_pid"], "newsdesk.batch_watch", path):
        raise RuntimeError("Existing supervisor identity could not be verified")
    for group in groups:
        group_path = path.parent / group["path"]
        current = read_json(group_path)
        if not verified_process(current["owner_pid"], "newsdesk.batch_evaluation", group_path):
            raise RuntimeError("Existing worker identity could not be verified: " + group["path"])
        state["groups"][group["path"]] = {"old_pid": current["owner_pid"], "status": "draining"}
    write_json(state_path, state)
    batch["maintenance_restart_file"] = state_path.name
    write_json(path, batch)
    subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    os.kill(watcher["owner_pid"], signal.SIGTERM)
    # Wait for the supervisor lock to be released before starting our drain.
    deadline = time.monotonic() + 15
    while _locked(path.with_suffix(".watch.lock")):
        if time.monotonic() > deadline:
            raise RuntimeError("Supervisor did not stop")
        time.sleep(0.2)
    try:
        for item in state["groups"].values():
            # SIGINT exits the producer loop; ThreadPoolExecutor waits for
            # every in-flight case before execution locks are released.
            os.kill(item["old_pid"], signal.SIGINT)
        pending = list(groups)
        while pending:
            for group in list(pending):
                group_path = path.parent / group["path"]
                config = read_json(group_path)
                if any(_locked(DEFAULT_ROOT / "runs" / rid / "execution.lock") for rid in config["models"].values()):
                    continue
                command = [sys.executable, "-u", "-m", "newsdesk.batch_evaluation", "--batch", str(group_path),
                           "--retries", "2", "--api-workers", str(group["api_workers"]),
                           "--case-workers", str(group["case_workers"]), "--per-model", str(group["per_model"])]
                if group.get("models"):
                    command += ["--models"] + group["models"]
                child = launch(command, group_path.with_name(group_path.stem + "-runtime-restart.log"))
                state["groups"][group["path"]].update(status="resumed", new_pid=child.pid, resumed_at=now())
                pending.remove(group)
            state["updated_at"] = now()
            write_json(state_path, state)
            if pending:
                time.sleep(5)
        state.update(status="completed", finished_at=now())
    except Exception as error:
        state.update(status="failed", error=str(error), finished_at=now())
        raise
    finally:
        child = launch([sys.executable, "-u", "-m", "newsdesk.batch_watch", str(path)],
                       path.with_name(path.stem + "-watch.log"))
        subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(child.pid)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        state["supervisor_pid"] = child.pid
        write_json(state_path, state)
        fcntl.flock(control_lock, fcntl.LOCK_UN)
        control_lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch", type=Path)
    parser.add_argument("--groups", nargs="+", required=True)
    args = parser.parse_args()
    restart(args.batch.resolve(), args.groups)


if __name__ == "__main__":
    main()
