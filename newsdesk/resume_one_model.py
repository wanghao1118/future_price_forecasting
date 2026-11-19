"""Resume only missing cases for one frozen benchmark model run."""

import argparse
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    batch = json.loads(args.batch.read_text(encoding="utf-8"))
    sys.path.insert(0, batch["source_root"])
    from newsdesk.batch_evaluation import FrozenNews, verify_batch_reference
    from newsdesk.benchmark import BenchmarkStore
    from newsdesk.evaluation import AIHubPricePredictor, EvaluationManager, read_json

    verify_batch_reference(args.root, batch)
    run_path = args.root / "runs" / args.run_id
    run = read_json(run_path / "run.json")
    config = run["generation_config"]
    predictor = AIHubPricePredictor(
        run["model"], timeout=config["client_timeout_seconds"],
        max_output_tokens=config["max_output_tokens"], max_rounds=config["max_rounds"],
        gateway_timeout=config["gateway_timeout_seconds"],
        api_protocol=config.get("api_protocol"), reasoning_effort=config.get("reasoning_effort"),
        provider=config.get("provider"),
    )
    manager = EvaluationManager(args.root, BenchmarkStore(args.root.parent / "benchmark"), FrozenNews([]), predictor=predictor)
    manager.resume(args.run_id, workers=args.workers, retry_failed=False)
    while True:
        run = read_json(run_path / "run.json")
        print(json.dumps({k: run.get(k) for k in ("run_id", "model", "status", "completed", "succeeded", "failed", "total", "error")}, ensure_ascii=False), flush=True)
        if run["status"] not in ("running", "queued"):
            return 0
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
