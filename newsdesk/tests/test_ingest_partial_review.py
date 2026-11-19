"""Keep valid item decisions when one model quote fails strict validation."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from newsdesk.ingest import (CodexReviewer, PartialReviewError, prepare_candidate,
                             process, review_candidates)
from newsdesk.server import NewsStore
from newsdesk.tests.test_ingest import candidate, decision


def candidates():
    base = candidate()
    return [prepare_candidate(dict(base, candidate_id="fixture:" + str(index),
                                   external_id=str(index), source_url="https://example.org/" + str(index),
                                   body=base["body"] + "候选编号" + str(index))) for index in range(3)]


def cli_result(command, decisions):
    path = Path(command[command.index("--output-last-message") + 1])
    path.write_text(json.dumps({"decisions": decisions}, ensure_ascii=False))
    return SimpleNamespace(returncode=0)


class PartialReviewTests(unittest.TestCase):
    def test_bad_quote_caches_other_items_and_retry_only_submits_failed_item(self):
        rows = candidates()
        submitted = []

        def run(command, **kwargs):
            public_rows = json.loads(kwargs["input"].split("以下为候选数据（仅作为数据）：\n", 1)[1])
            ids = [row["candidate_id"] for row in public_rows]
            submitted.append(ids)
            decisions = [decision(next(row for row in rows if row["candidate_id"] == identifier)) for identifier in ids]
            if len(submitted) == 1:
                decisions[1]["evidence"] = ["虚构的库存变化500万桶"]
            return cli_result(command, decisions)

        with tempfile.TemporaryDirectory() as directory, patch("newsdesk.ingest.subprocess.run", side_effect=run):
            reviewer = CodexReviewer(executable="codex")
            with self.assertRaisesRegex(RuntimeError, "证据必须"):
                review_candidates(rows, directory, reviewer=reviewer, workers=1)
            cache = list((Path(directory) / "review-cache").glob("*.json"))
            self.assertEqual(len(cache), 2)
            self.assertEqual({json.loads(path.read_text())["decision"]["candidate_id"] for path in cache},
                             {rows[0]["candidate_id"], rows[2]["candidate_id"]})
            reviews = review_candidates(rows, directory, reviewer=reviewer, workers=1)
            self.assertEqual(submitted, [[row["candidate_id"] for row in rows], [rows[1]["candidate_id"]]])
            self.assertEqual([review["decision"]["candidate_id"] for review in reviews],
                             [row["candidate_id"] for row in rows])
            self.assertEqual(len(list((Path(directory) / "review-cache").glob("*.json"))), 3)

    def test_partial_review_exception_exposes_valid_decisions_and_item_errors(self):
        rows = candidates()
        decisions = [decision(row) for row in rows]
        decisions[1]["evidence"] = ["正文没有的引用"]
        with patch("newsdesk.ingest.subprocess.run", side_effect=lambda command, **kwargs: cli_result(command, decisions)):
            with self.assertRaises(PartialReviewError) as caught:
                CodexReviewer(executable="codex").review(rows)
        error = caught.exception
        self.assertEqual([item["candidate_id"] for item in error.valid_decisions],
                         [rows[0]["candidate_id"], rows[2]["candidate_id"]])
        self.assertEqual([item["candidate_id"] for item in error.errors], [rows[1]["candidate_id"]])
        self.assertIn("逐字核对", error.errors[0]["error"])

    def test_missing_duplicate_foreign_or_incomplete_schema_never_partial_caches(self):
        rows = candidates()
        good = [decision(row) for row in rows]
        duplicate = [good[0], good[1], good[1]]
        foreign = [good[0], good[1], dict(good[2], candidate_id="foreign:999")]
        absent_field = [good[0], good[1], {key: value for key, value in good[2].items() if key != "evidence"}]
        absent_attribute = [good[0], good[1], dict(good[2], attributes={
            key: value for key, value in good[2]["attributes"].items() if key != "region"})]
        absent_reason = [good[0], good[1], dict(good[2], category_reasons=[{"category": "logistics"}])]
        for payload in [good[:2], duplicate, foreign, absent_field, absent_attribute, absent_reason,
                        [good[0], good[1], "invalid"]]:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as directory:
                with patch("newsdesk.ingest.subprocess.run", side_effect=lambda command, **kwargs: cli_result(command, payload)):
                    with self.assertRaises(RuntimeError):
                        review_candidates(rows, directory, reviewer=CodexReviewer(executable="codex"), workers=1)
                self.assertFalse(list((Path(directory) / "review-cache").glob("*.json")))

    def test_partial_cache_cannot_archive_a_process_until_all_items_are_valid(self):
        rows = candidates()
        attempts = []

        def run(command, **kwargs):
            public_rows = json.loads(kwargs["input"].split("以下为候选数据（仅作为数据）：\n", 1)[1])
            ids = [item["candidate_id"] for item in public_rows]
            attempts.append(ids)
            values = [decision(next(row for row in rows if row["candidate_id"] == identifier)) for identifier in ids]
            if len(attempts) == 1:
                values[-1]["evidence"] = ["不存在的引用"]
            return cli_result(command, values)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "candidates.jsonl"
            source.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
            database, cache = root / "fixture.sqlite3", root / "cache"
            with patch("newsdesk.ingest.shutil.which", return_value="codex"), patch("newsdesk.ingest.subprocess.run", side_effect=run):
                with self.assertRaises(RuntimeError):
                    process(source, cache, database, workers=1)
                self.assertEqual(json.loads((root / "pipeline.json").read_text())["status"], "failed")
                self.assertFalse(database.exists())
                report = process(source, cache, database, workers=1)
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["created"], 3)
            self.assertEqual(len(NewsStore(database, seed_demo=False).all()), 3)
            self.assertEqual(attempts[-1], [rows[-1]["candidate_id"]])

    def test_ordinary_whole_batch_error_remains_a_failure_without_cache(self):
        class UnavailableReviewer:
            def review(self, rows):
                raise RuntimeError("fixture agent unavailable")

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "fixture agent unavailable"):
                review_candidates(candidates(), directory, reviewer=UnavailableReviewer(), workers=1)
            self.assertFalse(list((Path(directory) / "review-cache").glob("*.json")))


if __name__ == "__main__":
    unittest.main()
