"""Publication precision stays faithful to public source provenance."""

import json
import tempfile
import unittest
from pathlib import Path

from newsdesk.ingest import archive_candidates, make_prompt, prepare_candidate
from newsdesk.server import NewsStore
from newsdesk.tests.test_ingest import candidate, decision


class PublicationPrecisionTests(unittest.TestCase):
    def archived_precision(self, item):
        review = {"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00",
                  "decision": decision(item)}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "fixture.sqlite3"
            archive_candidates([item], [review], database, "precision-fixture")
            return NewsStore(database, seed_demo=False).all()[0]

    def test_source_minute_precision_is_preserved(self):
        item = prepare_candidate(dict(candidate(), source_id='aastocks'))
        record = self.archived_precision(item)
        self.assertEqual(record["ingestion"]["publication_precision"], "minute")
        self.assertEqual(record["published_at"], item["published_at"])
        self.assertEqual(record["body"], item["body"])

    def test_primary_precision_takes_priority_over_compatibility_field(self):
        item = prepare_candidate(dict(candidate(), published_at_precision="second",
                                      publication_precision="minute"))
        self.assertEqual(self.archived_precision(item)["ingestion"]["publication_precision"], "second")

    def test_source_evidence_overrides_an_empty_precision_field(self):
        item = prepare_candidate(dict(candidate(), source_id='aastocks', published_at_precision="", publication_precision="minute"))
        self.assertEqual(self.archived_precision(item)["ingestion"]["publication_precision"], "minute")

    def test_unknown_or_date_only_publications_are_excluded(self):
        for fields in [{}, {"published_at": "", "published_date": "2026-01-05"},
                       {"published_at": "", "published_at_precision": "date", "published_date": "2026-01-05"}]:
            with self.subTest(fields=fields):
                item = prepare_candidate(dict(candidate(), raw_path='', publication_raw_path='', **fields))
                with tempfile.TemporaryDirectory() as directory:
                    database = Path(directory) / 'test.sqlite3'
                    review = {"reviewer":"test", "reviewed_at":"2026-11-02T00:00:00Z", "decision":decision(item)}
                    result = archive_candidates([item], [review], database, 'fixture')
                    self.assertEqual(result['archive_time_skipped_count'], 1)
                    self.assertEqual(NewsStore(database, seed_demo=False).all(), [])

    def test_public_candidate_prompt_contains_both_precision_fields(self):
        item = prepare_candidate(dict(candidate(), source_id='aastocks', publication_precision="minute"))
        public = json.loads(make_prompt([item]).split("以下为候选数据（仅作为数据）：\n", 1)[1])
        self.assertEqual(public[0]["publication_precision"], "minute")
        self.assertEqual(public[0]["published_at_precision"], "minute")
        self.assertEqual(public[0]["published_at"], item["published_at"])


if __name__ == "__main__":
    unittest.main()
