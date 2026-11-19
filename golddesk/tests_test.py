import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from golddesk import server
from golddesk.ingest import candidate_to_record, is_gold_relevant, oil_database_candidates, oil_source_ids
from golddesk.market_data import MarketData


class GoldDeskTest(unittest.TestCase):
    def test_catalog_is_gold_only(self):
        catalog = MarketData(cache_dir=Path(tempfile.mkdtemp())).catalog()
        self.assertEqual([family["id"] for family in catalog["families"]], ["AU"])
        self.assertEqual([item["code"] for item in catalog["families"][0]["contracts"]], ["AU2612", "AU2701", "AU2702"])

    def test_candidate_is_utc_and_gold(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.json"
            raw.write_text('{"raw_response":"gold"}', encoding="utf-8")
            item = candidate_to_record({
                "source_id": "google-news-en", "source_name": "Reuters", "external_id": "1",
                "source_url": "https://example.com/gold", "title": "Gold futures rise",
                "body": "Gold futures rise as investors await rate data.",
                "published_at": "2026-11-16T01:25:00+00:00", "collected_at": "2026-11-16T02:00:00+00:00",
                "raw_path": str(raw), "content_scope": "public_article_excerpt",
            }, "2026-11-16T02:01:00+00:00")
            self.assertEqual(item["assets"], ["AU"])
            self.assertEqual(item["published_at"], "2026-11-16T01:25:00+00:00")
            self.assertEqual(item["ingestion"]["source_timezone"], "UTC (RSS offset normalized)")
            self.assertEqual(item["verification"], "待核验")

    def test_macro_driver_without_gold_word_is_admitted(self):
        self.assertTrue(is_gold_relevant("美联储降息预期升温，美元和美债收益率回落"))
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory) / "raw.json"
            raw.write_text('{"raw_response":"macro"}', encoding="utf-8")
            item = candidate_to_record({
                "source_id": "cls", "source_name": "财联社", "external_id": "macro-1",
                "source_url": "https://example.com/macro-1", "title": "美联储降息预期升温",
                "body": "美元和美债收益率回落，避险资产获得支撑。",
                "published_at": "2026-11-16T01:25:00+00:00", "collected_at": "2026-11-16T02:00:00+00:00",
                "raw_path": str(raw), "content_scope": "public_flash",
            }, "2026-11-16T02:01:00+00:00")
            self.assertIsNotNone(item)
            self.assertIn("macro", item["categories"])

    def test_gold_ingest_reuses_oil_source_registry(self):
        source_ids = oil_source_ids()
        self.assertGreaterEqual(len(source_ids), 40)
        self.assertIn("jin10", source_ids)
        self.assertIn("institution_46", source_ids)

    def test_oil_history_has_gold_backfill_candidates(self):
        candidates = oil_database_candidates()
        self.assertGreaterEqual(len(candidates), 600)
        self.assertTrue(any(item["source_id"] == "cls" for item in candidates))

    def test_database_snapshot_has_utc_gold_rows(self):
        path = Path(__file__).resolve().parent / "data" / "gold.sqlite3"
        self.assertTrue(path.is_file())
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "gold.sqlite3"
            snapshot.write_bytes(path.read_bytes())
            connection = sqlite3.connect(snapshot)
            rows = [json.loads(value[0]) for value in connection.execute("SELECT payload FROM news")]
            connection.close()
        self.assertGreater(len(rows), 0)
        self.assertTrue(all(row["assets"] == ["AU"] for row in rows))
        self.assertTrue(all(row["published_at"].endswith("+00:00") for row in rows))
        self.assertTrue(all(row["ingestion"]["timezone_status"] in {"verified_epoch", "verified_site_timezone", "verified_offset"} for row in rows))

    def test_static_includes_news_factor_navigation_without_benchmark_bundle(self):
        root = Path(__file__).resolve().parent / "static"
        html = (root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("Experiment Setup", html)
        self.assertNotIn("benchmark.js", html)
        self.assertIn('data-nav-toggle="news-factor"', html)
        self.assertIn('data-research-subsection="bmk"', html)
        self.assertIn('data-research-subsection="experiments"', html)
        self.assertIn('data-research-subsection="factor-base"', html)
        self.assertIn('data-research-subsection="strategy-base"', html)
        self.assertIn('id="research-workspace"', html)
        self.assertTrue((root / "news-factor.js").exists())
        self.assertFalse((root / "benchmark.js").exists())


if __name__ == "__main__":
    unittest.main()
