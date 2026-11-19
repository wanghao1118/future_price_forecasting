"""Offline fixtures only; no production database or Agent is used."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.ingest import (REVIEW_EFFORT, REVIEW_MODEL, REVIEW_VERSION, archive_candidates,
                             prepare_candidate, process, write_json)
from newsdesk.repair_aastocks import commit, prepare, _read_jsonl
from newsdesk.server import NewsStore
from newsdesk.tests.test_ingest import candidate, decision


class RepairAastocksTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "live-fixture.sqlite3"
        self.package = self.root / "package"
        self.package.mkdir()
        self.workdir = self.root / "staged-review"

    def tearDown(self):
        self.temp.cleanup()

    def test_legal_unicode_text_does_not_split_repair_audit_records(self):
        rows = [{'body':'原油\u2028供应\u2029下降\u0085，保留原始文本'},{'body':'第二条真实记录'}]
        path = self.root/'unicode.jsonl'
        path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
        self.assertEqual(_read_jsonl(path),rows)

    def jsonl(self, name, rows):
        (self.package / name).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))

    def fixture(self, negative=False):
        raw = self.root / "raw.json"
        raw.write_text("<script>ConvertToLocalTime({dt:'2026/10/28 09:00'});</script>")
        base = candidate()
        clean_body = "WTI原油收于每桶60美元。" if negative else base["body"]
        old_body = clean_body + "\n相关内容：" + base["body"]
        old = prepare_candidate(dict(base, source_id="aastocks", candidate_id="aastocks:fixture",
                                     external_id="fixture", source_name="AA fixture", raw_path=str(raw), publication_raw_path=str(raw), body=old_body,
                                     content_scope="public_article"))
        clean = prepare_candidate(dict(old, body=clean_body))
        original_review = {"version": REVIEW_VERSION, "model": REVIEW_MODEL, "reasoning_effort": REVIEW_EFFORT,
                           "content_hash": old["content_hash"], "reviewer": "Codex CLI · " + REVIEW_MODEL,
                           "reviewed_at": "2026-10-30T07:40:25+00:00", "decision": decision(old)}
        archive_candidates([old], [original_review], self.database, "original-fixture")
        record = NewsStore(self.database, seed_demo=False).all()[0]
        cache = self.root / "original-review.json"
        write_json(cache, original_review)
        self.jsonl("original-candidates.jsonl", [old])
        self.jsonl("review-safe-candidates.jsonl", [clean])
        self.jsonl("database-before.jsonl", [record])
        self.jsonl("affected-audit.jsonl", [{"candidate_id": old["candidate_id"], "news_id": record["id"],
            "safe_for_root_review": True, "cached_auto_review_path": str(cache),
            "old_content_hash": old["content_hash"], "new_content_hash": clean["content_hash"],
            "old_body": old["body"], "clean_body": clean["body"]}])
        return old, clean, record

    def review_stage(self, manifest, negative=False):
        class FixtureReviewer:
            def review(self, rows):
                values = [decision(row) for row in rows]
                if negative:
                    for value in values:
                        value.update(relevant=False, primary_category="", categories=[], category_reasons=[],
                                     evidence=[], summary="", attributes=None,
                                     reason="清理推荐后仅有报价，缺少原油新闻独立驱动")
                return values

        with patch("newsdesk.ingest.CodexReviewer", return_value=FixtureReviewer()):
            return process(Path(manifest["stage_input"]), self.root / "review-cache-fixture",
                           Path(manifest["stage_database"]), workers=1)

    def raw_live_row(self, identifier):
        with sqlite3.connect(self.database) as connection:
            return connection.execute("SELECT payload,updated_at FROM news WHERE id=?", (identifier,)).fetchone()

    def test_positive_repair_updates_existing_id_only_after_complete_review(self):
        old, clean, record = self.fixture()
        original = self.raw_live_row(record["id"])
        manifest = prepare(self.package, self.database, self.workdir)
        self.assertEqual(manifest["prepared_count"], 1)
        self.assertEqual(self.raw_live_row(record["id"]), original)
        staged = NewsStore(manifest["stage_database"], seed_demo=False).get(record["id"])
        self.assertEqual(staged["body"], clean["body"])
        self.assertEqual(staged["ingestion"]["content_hash"], clean["content_hash"])
        self.assertNotEqual(old["content_hash"], clean["content_hash"])
        with self.assertRaisesRegex(ValueError, "尚未完整成功"):
            commit(self.workdir)
        self.assertEqual(self.raw_live_row(record["id"]), original)
        self.review_stage(manifest)
        report = commit(self.workdir)
        self.assertEqual((report["updated"], report["excluded"], report["created"]), (1, 0, 0))
        records = NewsStore(self.database, seed_demo=False).all()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["id"], record["id"])
        self.assertEqual(records[0]["body"], clean["body"])
        self.assertTrue((self.workdir / "commit-plan.jsonl").exists())
        self.assertEqual(json.loads((self.workdir / "commit-audit.jsonl").read_text())["action"], "updated")
        first_audit = (self.workdir / "commit-audit.jsonl").read_text()
        self.assertEqual(commit(self.workdir), report)
        self.assertEqual((self.workdir / "commit-audit.jsonl").read_text(), first_audit)

    def test_negative_review_hides_clean_hash_record_instead_of_retaining_old_positive(self):
        old, clean, record = self.fixture(negative=True)
        manifest = prepare(self.package, self.database, self.workdir)
        result = self.review_stage(manifest, negative=True)
        self.assertEqual(result["excluded_previous"], 1)
        report = commit(self.workdir)
        self.assertEqual((report["updated"], report["excluded"], report["created"]), (0, 1, 0))
        after = NewsStore(self.database, seed_demo=False).get(record["id"])
        self.assertFalse(after["agent_review"]["relevance"])
        self.assertEqual(after["body"], clean["body"])
        self.assertEqual(after["ingestion"]["content_hash"], clean["content_hash"])
        self.assertNotEqual(after["ingestion"]["content_hash"], old["content_hash"])

    def test_human_change_after_prepare_is_skipped_with_exact_current_payload_preserved(self):
        _, _, record = self.fixture()
        manifest = prepare(self.package, self.database, self.workdir)
        self.review_stage(manifest)
        NewsStore(self.database, seed_demo=False).save({"summary": "用户后来添加的研究观点"}, record["id"])
        modified = self.raw_live_row(record["id"])
        report = commit(self.workdir)
        self.assertEqual((report["updated"], report["excluded"], report["skipped"]), (0, 0, 1))
        self.assertEqual(self.raw_live_row(record["id"]), modified)

    def test_timestamp_only_concurrent_change_is_also_protected(self):
        _, _, record = self.fixture()
        manifest = prepare(self.package, self.database, self.workdir)
        self.review_stage(manifest)
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE news SET updated_at=? WHERE id=?", ("later-table-timestamp", record["id"]))
        modified = self.raw_live_row(record["id"])
        report = commit(self.workdir)
        self.assertEqual(report["skipped"], 1)
        self.assertEqual(self.raw_live_row(record["id"]), modified)

    def test_snapshot_changes_before_prepare_are_not_staged(self):
        _, _, record = self.fixture()
        NewsStore(self.database, seed_demo=False).save({"verification": "已确认"}, record["id"])
        modified = self.raw_live_row(record["id"])
        manifest = prepare(self.package, self.database, self.workdir)
        self.assertEqual((manifest["prepared_count"], manifest["skipped_count"]), (0, 1))
        self.assertEqual(NewsStore(manifest["stage_database"], seed_demo=False).all(), [])
        self.assertEqual(self.raw_live_row(record["id"]), modified)

    def test_stage_new_id_aborts_before_any_live_write(self):
        _, _, record = self.fixture()
        original = self.raw_live_row(record["id"])
        manifest = prepare(self.package, self.database, self.workdir)
        self.review_stage(manifest)
        NewsStore(manifest["stage_database"], seed_demo=False).save({"title": "Unapproved extra stage record", "category": "supply", "published_at":"2026-10-28T01:00:00+00:00"})
        with self.assertRaisesRegex(ValueError, "新增或缺失 ID"):
            commit(self.workdir)
        self.assertEqual(self.raw_live_row(record["id"]), original)
        self.assertEqual(len(NewsStore(self.database, seed_demo=False).all()), 1)

    def test_tampered_stage_input_is_rejected_even_with_successful_pipeline(self):
        _, _, record = self.fixture()
        original = self.raw_live_row(record["id"])
        manifest = prepare(self.package, self.database, self.workdir)
        self.review_stage(manifest)
        path = Path(manifest["stage_input"])
        path.write_text(path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "prepare 后被改变"):
            commit(self.workdir)
        self.assertEqual(self.raw_live_row(record["id"]), original)

    def test_empty_stage_needs_no_model_and_creates_no_record(self):
        NewsStore(self.database, seed_demo=False)
        for name in ("original-candidates.jsonl", "review-safe-candidates.jsonl", "affected-audit.jsonl", "database-before.jsonl"):
            self.jsonl(name, [])
        manifest = prepare(self.package, self.database, self.workdir)
        self.assertEqual(manifest["prepared_count"], 0)
        with patch("newsdesk.ingest.CodexReviewer") as reviewer:
            process(Path(manifest["stage_input"]), self.root / "empty-review", Path(manifest["stage_database"]), workers=1)
            reviewer.assert_not_called()
        report = commit(self.workdir)
        self.assertEqual((report["updated"], report["excluded"], report["skipped"], report["created"]), (0, 0, 0, 0))
        self.assertEqual(NewsStore(self.database, seed_demo=False).all(), [])


if __name__ == "__main__":
    unittest.main()
