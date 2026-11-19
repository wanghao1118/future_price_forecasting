"""Wait for recursive s02 learning, then prepare and execute Exp06 v2."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import time


PROJECT = Path(__file__).resolve().parent.parent
EVAL_ROOT = PROJECT / "newsdesk" / "data" / "evaluations"
ARTIFACT = EVAL_ROOT / "recursive" / "Exp06-s02"
DATASET = "qa-6508a948e223740f22dd"
MODELS = [
    "deepseek-v4-pro", "deepseek-v4.1-flash", "doubao-seed-2-0-lite-260428",
    "glm-5.3", "glm-5.3-flash", "gpt-6-astra", "gpt-6-luna", "gpt-6-sol",
    "gpt-6.1-sol", "gpt-6.1-terra", "hy3", "hy4-preview", "kimi-k3",
    "kimi-k3.1-pro", "qwen3.7-plus",
]


def learning_state():
    runs = sorted((ARTIFACT / "runs").glob("*/run.json"))
    if not runs:
        return None
    return json.loads(runs[-1].read_text(encoding="utf-8"))


def main():
    log = (ARTIFACT / "final-exp06.log").open("a", encoding="utf-8")
    log.write("watcher_started\n")
    log.flush()
    while True:
        state = learning_state()
        if state and state.get("status") == "completed" and state.get("phase") == "learning_complete":
            break
        if state and state.get("status") in ("failed", "partial"):
            log.write(json.dumps({"learning_status": state.get("status"), "error": state.get("error")}, ensure_ascii=False) + "\n")
            log.flush()
            return 2
        time.sleep(60)
    log.write(json.dumps({"learning_complete": state.get("run_id"), "skill_sha256": state.get("skill_sha256")}, ensure_ascii=False) + "\n")
    log.flush()
    prepare = [sys.executable, "-u", "-m", "newsdesk.batch_evaluation", "--experiment", "Exp06",
               "--dataset", DATASET, "--environment", "env04", "--prepare-only", "--root", str(EVAL_ROOT)]
    prepare += ["--models", *MODELS]
    result = subprocess.run(prepare, cwd=PROJECT, text=True, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        return result.returncode
    batch_path = EVAL_ROOT / "batches" / "Exp06.json"
    execute = [sys.executable, "-u", "-m", "newsdesk.batch_evaluation", "--batch", str(batch_path),
               "--root", str(EVAL_ROOT), "--models", *MODELS, "--retries", "2", "--api-workers", "6",
               "--case-workers", "24", "--per-model", "2"]
    result = subprocess.run(execute, cwd=PROJECT, text=True, stdout=log, stderr=subprocess.STDOUT)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
