"""Closed-book experiment reads use their own manager and preserve price QA."""

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from newsdesk.environment_policy import allowed_tools, memory_environment, tool_mode
from newsdesk.server import create_server


class MemoryResearchAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(port=0, database=Path(self.temp.name) / "news.sqlite3", seed_demo=False)
        self.scheduler = patch.object(self.server.updates, "start")
        self.scheduler.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.scheduler.stop()
        self.temp.cleanup()

    def read(self, path):
        with urlopen(self.base + path, timeout=10) as response:
            return json.load(response), response.headers

    def test_environment_denies_all_tools_and_keeps_env00_indicators(self):
        self.assertEqual(allowed_tools({"environment_id": "env00-extend"}), ())
        self.assertEqual(tool_mode({"environment_id": "env00-extend"}), "closed_book_no_tools")
        self.assertEqual(allowed_tools({"environment_id": "env00"}), ("search_indicators",))
        self.assertEqual(memory_environment()["model_input_fields"], ["question"])
        catalog, _ = self.read("/api/research")
        self.assertIn("env00-extend", {env["id"] for env in catalog["environments"]})
        self.assertIn("memory_benchmark", catalog)
        self.assertEqual(catalog["runs"], [])
        self.assertEqual(catalog["memory_benchmark"]["runs"], [])

    def test_memory_run_and_trace_dispatch_do_not_read_price_runs(self):
        memory = self.server.memory_evaluations
        run = {"run_id": "memory-test", "environment_id": "env00-extend", "cases": []}
        trace = {"run_id": "memory-test", "case_id": "memory-2026-01-01", "search_calls": []}
        with patch.object(memory, "get_run", return_value=run) as get, patch.object(memory, "get_case_trace", return_value=trace) as get_trace, patch.object(self.server.evaluations, "get_run") as price:
            self.assertEqual(self.read("/api/evaluations/memory-test")[0], run)
            get.assert_called_once_with("memory-test")
            received, headers = self.read("/api/evaluations/memory-test/cases/memory-2026-01-01/trace?download=1")
            self.assertEqual(received, trace)
            self.assertIn("memory-memory-2026-01-01-trace.json", headers["Content-Disposition"])
            get_trace.assert_called_once_with("memory-test", "memory-2026-01-01")
            price.assert_not_called()

    def test_memory_list_is_separate_and_missing_run_is_404(self):
        rows = [{"run_id": "memory-test"}]
        with patch.object(self.server.memory_evaluations, "catalog", return_value={"runs": rows}):
            self.assertEqual(self.read("/api/evaluations?benchmark=memory-leakage")[0]["items"], rows)
            self.assertEqual(self.read("/api/evaluations")[0]["items"], [])
        with urlopen(self.base + "/api/research", timeout=10) as response:
            self.assertEqual(response.status, 200)
        for route in ("/api/evaluations/memory-missing", "/api/evaluations/memory-missing/cases/memory-2026-01-01/trace"):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.base + route, timeout=10)
            self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
