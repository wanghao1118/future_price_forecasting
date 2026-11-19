"""Recall dataset integrity and isolation from the existing prediction APIs."""

import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import urlopen

from newsdesk.memory_leakage import TEMPORAL_FOCUSES, VERSION, build_dataset, load_dataset, validate_questions
from newsdesk.server import create_server


class MemoryLeakageDatasetTests(unittest.TestCase):
    def setUp(self):
        self.dataset = copy.deepcopy(load_dataset())

    def test_frozen_dataset_has_ten_distinct_events_and_sources_per_month(self):
        validate_questions(self.dataset["questions"])
        self.assertEqual(self.dataset["name"], "记忆泄漏")
        self.assertEqual(self.dataset["year"], 2026)
        self.assertEqual(self.dataset["total_questions"], 100)
        self.assertEqual(len(self.dataset["months"]), 10)
        self.assertTrue(all(item["question_count"] == 10 for item in self.dataset["months"]))
        self.assertEqual(self.dataset["source_snapshot"]["unique_news"], 100)
        self.assertEqual(self.dataset["version"], VERSION)
        for question in self.dataset["questions"]:
            self.assertIn(question["temporal_focus"], TEMPORAL_FOCUSES)
            self.assertTrue(question["temporal_rationale"].strip())

    def test_requires_a_documented_temporal_target_and_keeps_aliases_out_of_prompt(self):
        for change in (lambda q: q[0].pop("temporal_rationale"),
                       lambda q: q[0].update(temporal_rationale=" "),
                       lambda q: q[0].update(temporal_focus="general_knowledge"),
                       lambda q: q[0].update(answer_aliases=[q[0]["question"][:8]])):
            questions = copy.deepcopy(self.dataset["questions"])
            change(questions)
            with self.assertRaises(ValueError):
                validate_questions(questions)

    def test_rejects_incomplete_months_duplicate_sources_and_unsupported_answers(self):
        for change in (lambda q: q.pop(),
                       lambda q: q[0].update(month="2026-02"),
                       lambda q: q[0].update(answer="THIS ANSWER IS NOT IN THE EVIDENCE"),
                       lambda q: q[0].update(question="两个空：____和____"),
                       lambda q: q[0].update(question="答案是{}，请填____".format(q[0]["answer"])),
                       lambda q: q[0]["source"].update(news_id=q[1]["source"]["news_id"]),
                       lambda q: q[0]["source"].update(url="javascript:alert(1)")):
            questions = copy.deepcopy(self.dataset["questions"])
            change(questions)
            with self.assertRaises(ValueError):
                validate_questions(questions)

    def test_frozen_digest_detects_modified_answers(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "modified.json"
            self.dataset["questions"][0]["answer"] += "改动"
            path.write_text(json.dumps(self.dataset), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "指纹"):
                load_dataset(path)

    def test_builder_rejects_unreviewed_source_and_nonverbatim_evidence(self):
        # Use one real frozen selection, with a tiny isolated SQLite fixture;
        # both failures occur before the final 100-question completeness check.
        import sqlite3
        item = self.dataset["questions"][0]
        selection = {key: item[key] for key in ("month", "event", "event_rationale", "question", "answer", "answer_aliases", "evidence")}
        selection["news_id"] = item["source"]["news_id"]
        with tempfile.TemporaryDirectory() as temporary:
            database, selections = Path(temporary) / "news.sqlite3", Path(temporary) / "selection.json"
            selections.write_text(json.dumps([selection]), encoding="utf-8")
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE news(id TEXT, payload TEXT)")
                connection.execute("INSERT INTO news VALUES (?, ?)", (selection["news_id"], json.dumps({"agent_review": {"relevance": False}})))
            with self.assertRaisesRegex(ValueError, "审核相关"):
                build_dataset(database, [selections])
            with sqlite3.connect(database) as connection:
                connection.execute("UPDATE news SET payload=?", (json.dumps({"agent_review": {"relevance": True}, "body": "unrelated"}),))
            with patch("newsdesk.memory_leakage.require_verified_publication", return_value="2026-01-01T00:00:00+00:00"):
                with self.assertRaisesRegex(ValueError, "连续原文"):
                    build_dataset(database, [selections])


class MemoryLeakageAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(port=0, database=Path(self.temp.name) / "news.sqlite3", seed_demo=False)
        # Do not start collection while checking a read-only benchmark route.
        self.updates = patch.object(self.server.updates, "start")
        self.updates.start()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:{}".format(self.server.server_port)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.updates.stop()
        self.temp.cleanup()

    def test_frozen_questions_load_without_news_db_or_forecast_build(self):
        with patch.object(self.server.benchmarks, "build") as build, patch.object(self.server.evaluations, "start") as evaluate:
            results = []
            for route in ("/api/benchmark?kind=memory-leakage", "/api/benchmark/memory-leakage"):
                with urlopen(self.base + route, timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    results.append(json.load(response))
            self.assertEqual(results[0], results[1])
            self.assertEqual(len(results[0]["questions"]), 100)
            build.assert_not_called()
            evaluate.assert_not_called()
        with urlopen(self.base + "/api/benchmark?mode=backtest", timeout=5) as response:
            self.assertEqual(json.load(response)["total_questions"], 0)

    def test_download_and_unavailable_dataset(self):
        with urlopen(self.base + "/api/benchmark?kind=memory-leakage&download=1", timeout=5) as response:
            self.assertIn("attachment", response.headers["Content-Disposition"])
            self.assertEqual(json.load(response)["total_questions"], 100)
        with patch("newsdesk.memory_leakage.load_dataset", side_effect=ValueError("broken digest")):
            with self.assertRaises(HTTPError) as caught:
                urlopen(self.base + "/api/benchmark?kind=memory-leakage", timeout=5)
            self.assertEqual(caught.exception.code, 503)


if __name__ == "__main__":
    unittest.main()
