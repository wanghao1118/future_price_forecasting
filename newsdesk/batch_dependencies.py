"""Do not begin dependent experiments until execution and audit have ended."""

import json
from pathlib import Path


def dependency_ready(root, experiment_id):
    from newsdesk.evaluation import _locked
    root = Path(root)
    path = root / "batches" / (experiment_id + ".json")
    try:
        batch = json.loads(path.read_text())
        state = json.loads(path.with_name(experiment_id + "-execution.json").read_text())
        if state.get("status") not in ("completed", "partial") or not state.get("finished_at"):
            return False
        if _locked(path.with_suffix(".watch.lock")):
            return False
        ids = set(batch["models"].values())
        if not ids or ids != {row["run_id"] for row in state.get("validation", [])}:
            return False
        for run_id in ids:
            folder = root / "runs" / run_id
            run = json.loads((folder / "run.json").read_text())
            if _locked(folder / "execution.lock") or run.get("status") not in ("completed", "partial", "failed"):
                return False
            if run.get("completed") != run.get("total"):
                return False
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False
