"""JSONL records split only at physical LF, preserving Unicode text separators."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk import domestic_brokers_a, history_broker_channels, history_brokers
from newsdesk import history_oil_research, public_company_sources, xingye_source


class JsonlUnicodeReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.rows = [
            {"candidate_id": "unicode:" + str(index),
             "title": "标题前" + separator + "标题后",
             "body": "正文前" + separator + "正文后"}
            for index, separator in enumerate(("\u2028", "\u2029", "\u0085"))
        ]
        self.path = self.root / "input.jsonl"
        self.path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n"
                                     for row in self.rows), encoding="utf-8")

    def read_output(self, report):
        return [json.loads(line) for line in
                Path(report["candidates_path"]).read_text(encoding="utf-8").split("\n")
                if line.strip()]

    def test_history_candidate_loaders_preserve_all_unicode_separators(self):
        expected = {row["candidate_id"]: row for row in self.rows}
        loaders = (history_brokers._load_candidates, history_broker_channels._load,
                   history_oil_research._read_candidates)
        for loader in loaders:
            with self.subTest(loader=loader.__module__):
                self.assertEqual(loader(self.path), expected)
                self.assertEqual(loader(self.root / "missing.jsonl"), {})

    def test_broker_a_fallback_roundtrip_preserves_all_records(self):
        with patch.object(domestic_brokers_a, "crawl_sina_brokers", return_value={
                "candidates_path": str(self.path), "sources": []}):
            report = domestic_brokers_a.crawl_domestic_brokers_a(
                self.root / "broker-a", ["institution_14"])
        self.assertEqual(self.read_output(report), self.rows)
        self.assertEqual(report["candidate_count"], len(self.rows))

    def test_xingye_subreports_roundtrip_preserves_all_records(self):
        empty = self.root / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        first = {"candidates_path": str(self.path), "sources": [{"errors": []}]}
        second = {"candidates_path": str(empty), "sources": [{"errors": []}]}
        with patch.object(xingye_source, "crawl_xingye_f10", return_value=first), \
                patch.object(xingye_source, "crawl_xingye_source", return_value=second):
            report = xingye_source.crawl_xingye_sources(self.root / "xingye")
        self.assertEqual(self.read_output(report), self.rows)
        self.assertEqual(report["candidate_count"], len(self.rows))

    def test_public_company_roundtrip_preserves_all_records(self):
        upstream = {"candidates_path": str(self.path), "status": "complete",
                    "candidate_count": len(self.rows), "gap_count": 0}
        with patch.object(public_company_sources, "run_source", return_value=upstream):
            report = public_company_sources.crawl_public_company_sources(
                self.root / "company", ["zijin_mining"])
        self.assertEqual(self.read_output(report), self.rows)
        self.assertEqual(report["candidate_count"], len(self.rows))


if __name__ == "__main__":
    unittest.main()
