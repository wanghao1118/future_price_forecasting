"""Run with python3 -m time_gate_agent."""

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

from . import AGENT_NAME, TimeGateAgent
from .guard import mcp_main, serve_registry
from .model import CodexJSONClient


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "newsdesk" / "data" / "time-gate"


def main(argv=None):
    parser = argparse.ArgumentParser(description="time gate agent — verify news dates and exclude target results")
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("search", "open", "verify", "serve"):
        item = sub.add_parser(action)
        item.add_argument("--task", type=Path, required=True, help="public prediction question JSON")
        item.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="host-only audit directory")
        item.add_argument("--model", default="gpt-6.1-sol")
        item.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
        item.add_argument("--timeout", type=int, default=600)
        if action == "search":
            item.add_argument("--query", required=True)
        elif action == "open":
            item.add_argument("--url", action="append", required=True, help="repeat for multiple articles")
        elif action == "serve":
            item.add_argument("--port", type=int, default=17891)
            item.add_argument("--token-file", type=Path, required=True, help="new private MCP token file")
    mcp = sub.add_parser("mcp", help="stdio MCP bridge to a running local agent")
    mcp.add_argument("--endpoint", default="http://127.0.0.1:17891")
    mcp.add_argument("--token-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "mcp":
            mcp_main(args.endpoint, args.token_file)
            return 0
        task = json.loads(args.task.read_text(encoding="utf-8"))
        client = CodexJSONClient(model=args.model, effort=args.effort, timeout=args.timeout)
        run = args.output_dir.resolve() / uuid.uuid4().hex
        agent = TimeGateAgent(task, run, client, client, client)
        if args.action == "search":
            response = agent.search(args.query)
        elif args.action == "open":
            sources = {}
            for url in args.url:
                for source in agent.open(url)["sources"]:
                    sources[source["url"]] = source
            response = {"status": "ok" if sources else "unavailable", "sources": list(sources.values())}
        elif args.action == "verify":
            agent.verify_boundary()
            response = None
        else:
            token_path = args.token_file.resolve()
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token = secrets.token_urlsafe(32)
            with os.fdopen(os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
                handle.write(token + "\n")
            # No token or private path is printed to the prediction client.
            print("time gate agent listening on 127.0.0.1:{}".format(args.port), file=sys.stderr, flush=True)
            serve_registry({token: agent}, port=args.port)
            return 0
        print(json.dumps({"agent": AGENT_NAME, "policy": agent.policy, "response": response}, ensure_ascii=False, indent=2))
        print("Private audit: {}".format(run), file=sys.stderr)
        return 0
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        # Do not echo user/source/provider text or credentials into public output.
        print("time gate agent failed ({}); inspect the private audit or task fields.".format(type(exc).__name__), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
