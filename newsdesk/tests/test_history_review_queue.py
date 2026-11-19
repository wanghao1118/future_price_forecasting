"""Offline review-queue fairness and transactional cursor regression tests."""

import copy
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock, patch

from newsdesk.history_backfill import Backfill, Stopped, review_channel


OIL = "oil_topics"
COMMODITY = "commodity_archive"
OTHER = "other_public_sources"
CYCLE = [OIL, OIL, OIL, COMMODITY, COMMODITY, OTHER]


def candidate(source, identifier, month="2026-01", *, topic=None, industry=None,
              commodity=False):
    row = {"candidate_id": source + ":" + identifier, "source_id": source,
           "source_name": "Fixture " + source, "external_id": identifier,
           "source_url": "https://queue.example.test/" + source + "/" + identifier,
           "title": "Public report " + identifier, "body": "Original report " + identifier,
           "published_date": month + "-05", "collected_at": "2026-10-30T01:00:00+00:00"}
    if topic is not None:
        row["discovery_topic_id"] = topic
    if industry is not None:
        row.update(discovery_industry_code=industry,
                   discovery_research_channel="sina_public_oil_industries")
    if commodity:
        row["source_listing_url"] = "https://goodsfu.10jqka.com.cn/qhkx_list/20260105/"
    return row


class HistoryReviewQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runner_number = 0
        # The path is deliberately absent. An accidental archive access must
        # fail instead of opening any real or temporary SQLite database.
        self.store = patch("newsdesk.history_backfill.NewsStore",
                           side_effect=AssertionError("queue tests must not open a database"))
        self.store.start()
        self.addCleanup(self.store.stop)

    @staticmethod
    def completed(path, database, cache, stop):
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n")
                if line.strip()]
        return {"status": "complete", "reviewed_count": len(rows), "created": len(rows),
                "written": len(rows)}

    def runner(self, rows_by_source, *, months=("2026-01",), processor=None):
        self.runner_number += 1
        output = self.root / ("runner-" + str(self.runner_number))
        specs = {source: {"name": "Fixture " + source, "group": "media"}
                 for source in rows_by_source}
        runner = Backfill("2026-01-01", "2026-10-30", output,
            output / "never-open.sqlite3", specs,
            adapter=Mock(side_effect=AssertionError("queue tests never collect")),
            processor=processor or self.completed, priority_months=list(months),
            poll_seconds=.001, retry_delay=0, service_backoff_base=.001,
            service_backoff_max=.002)
        for source, rows in rows_by_source.items():
            path = output / (source + "-candidates.jsonl")
            path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
                            encoding="utf-8")
            runner.state["sources"][source]["report"] = {"candidates_path": str(path)}
        runner.state["review_channel_positions"] = {month: 0 for month in months}
        runner.state["review_stream_positions"] = {}
        runner._save()
        return runner

    def balanced(self, count=20):
        return {
            "cls": [candidate("cls", "oil-" + str(index), topic="1583")
                    for index in range(count)],
            "ths": [candidate("ths", "commodity-" + str(index), commodity=True)
                    for index in range(count)],
            "other": [candidate("other", "other-" + str(index)) for index in range(count)],
        }

    def review_one(self, runner, limit):
        """Run one real review attempt; terminate before the next queue read.

        A failed multi-row review intentionally asks for a smaller partition.
        Returning an empty second read lets that split be observed without an
        unbounded retry loop or accidentally committing a later partition.
        """
        original = runner._pending
        reads = []

        def once(*, limit):
            if reads:
                return [], []
            result = original(limit=limit)
            reads.append(result)
            return result

        runner.state["review_chunk_limit"] = limit
        runner.collected.set()
        with patch.object(runner, "_pending", side_effect=once), patch("builtins.print"):
            runner._review()
        self.assertEqual(len(reads), 1)
        return reads[0]

    def test_appended_macro_topic_gets_a_turn_before_old_topic_backlog(self):
        rows = [candidate("cls", "old-" + str(index), topic="1583") for index in range(100)]
        rows += [candidate("cls", "macro-" + str(index), topic="3140") for index in range(2)]
        runner = self.runner({"cls": rows})
        selected, keys = runner._pending(limit=3)
        self.assertEqual([row["external_id"] for row in selected], ["old-0", "macro-0", "old-1"])
        self.assertEqual(len(set(keys)), 3)
        self.assertEqual(runner.state["review_queue_channel_counts"], {OIL: 102})

    def test_broker_industries_are_separate_streams_within_one_source(self):
        source = "institution_12"
        rows = [candidate(source, "production-" + str(index), industry="sw2_750100")
                for index in range(100)]
        rows += [candidate(source, "service", industry="sw2_750200"),
                 candidate(source, "refining", industry="sw2_750300")]
        selected, _ = self.runner({source: rows})._pending(limit=3)
        self.assertEqual([row["discovery_industry_code"] for row in selected],
                         ["sw2_750100", "sw2_750200", "sw2_750300"])

    def test_single_item_successes_rotate_cls_topic_streams(self):
        rows = [candidate("cls", "old-" + str(index), topic="1583") for index in range(100)]
        rows += [candidate("cls", "macro-" + str(index), topic="3140") for index in range(2)]
        runner = self.runner({"cls": rows})
        topics = []
        for _ in range(5):
            selected, _ = self.review_one(runner, 1)
            topics.extend(row["discovery_topic_id"] for row in selected)
        self.assertEqual(topics, ["1583", "3140", "1583", "3140", "1583"])

    def test_same_industry_rotates_across_broker_sources(self):
        rows = {
            "institution_12": [candidate("institution_12", str(index), industry="sw2_750100")
                               for index in range(50)],
            "institution_13": [candidate("institution_13", "first", industry="sw2_750100")],
        }
        selected, _ = self.runner(rows)._pending(limit=2)
        self.assertEqual([row["source_id"] for row in selected], ["institution_12", "institution_13"])

    def test_other_sources_rotate_first_items_before_second_items(self):
        rows = {"a": [candidate("a", str(index)) for index in range(50)],
                "b": [candidate("b", "first")], "c": [candidate("c", "first")]}
        selected, _ = self.runner(rows)._pending(limit=4)
        self.assertEqual([row["source_id"] for row in selected], ["a", "b", "c", "a"])

    def test_small_channel_heaps_retain_each_channels_next_turn(self):
        for cursor, expected in ((3, COMMODITY), (5, OTHER)):
            with self.subTest(cursor=cursor):
                rows = self.balanced(100)
                rows["other"] = rows["other"][:1]
                runner = self.runner(rows)
                runner.state["review_channel_positions"]["2026-01"] = cursor
                selected, _ = runner._pending(limit=1)
                self.assertEqual([review_channel(row) for row in selected], [expected])
                self.assertEqual(runner.state["review_queue_counts_by_month"]["2026-01"], 201)
                self.assertEqual(runner.state["review_queue_channel_counts"],
                                 {OIL: 100, COMMODITY: 100, OTHER: 1})

    def test_weighted_cycle_survives_successful_chunks_of_one_two_or_four(self):
        for limit in (1, 2, 4):
            with self.subTest(limit=limit):
                runner = self.runner(self.balanced())
                channels = []
                for _ in range(12 // limit):
                    rows, _ = self.review_one(runner, limit)
                    channels.extend(review_channel(row) for row in rows)
                self.assertEqual(channels, CYCLE * 2)
                self.assertEqual(Counter(channels), {OIL: 6, COMMODITY: 4, OTHER: 2})
                self.assertEqual(runner.state["review_channel_positions"]["2026-01"], 0)

    def test_empty_channels_are_skipped_without_blocking_work(self):
        runner = self.runner({"ths": [candidate("ths", str(index), commodity=True)
                                       for index in range(5)]})
        selected, _ = runner._pending(limit=4)
        self.assertEqual([row["external_id"] for row in selected], ["0", "1", "2", "3"])
        self.assertEqual({review_channel(row) for row in selected}, {COMMODITY})

    def test_unverified_topic_is_other_channel(self):
        runner = self.runner({"cls": [candidate("cls", "unverified", topic="not-a-public-topic"),
                                       candidate("cls", "verified", topic="3140")]})
        selected, _ = runner._pending(limit=2)
        self.assertEqual([row["external_id"] for row in selected], ["verified", "unverified"])
        self.assertEqual([review_channel(row) for row in selected], [OIL, OTHER])

    def test_pending_is_deterministic_and_does_not_commit_cursor(self):
        runner = self.runner(self.balanced())
        runner.state["review_channel_positions"]["2026-01"] = 3
        before = copy.deepcopy(runner.state["review_channel_positions"])
        before_streams = copy.deepcopy(runner.state["review_stream_positions"])
        first = runner._pending(limit=2)
        second = runner._pending(limit=2)
        self.assertEqual(first, second)
        self.assertEqual(runner.state["review_channel_positions"], before)
        self.assertEqual(runner.state["review_stream_positions"], before_streams)
        self.assertEqual([review_channel(row) for row in first[0]], [COMMODITY, COMMODITY])
        manifest = json.loads(runner.path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["review_channel_positions"], before)
        self.assertEqual(manifest["review_stream_positions"], before_streams)

    def test_success_commits_cursor_and_resume_continues_next_cycle_slot(self):
        runner = self.runner(self.balanced())
        rows, first_keys = self.review_one(runner, 2)
        self.assertEqual([review_channel(row) for row in rows], [OIL, OIL])
        self.assertEqual(runner.state["review_channel_positions"], {"2026-01": 2})
        saved = json.loads(runner.path.read_text(encoding="utf-8"))
        self.assertEqual(saved["review_channel_positions"], {"2026-01": 2})
        self.assertTrue(saved["review_stream_positions"])
        resumed = Backfill(runner.start, runner.end, runner.output, runner.database, runner.specs,
            adapter=Mock(side_effect=AssertionError("resume must not collect")),
            processor=self.completed, poll_seconds=.001, retry_delay=0)
        selected, keys = resumed._pending(limit=4)
        self.assertEqual([review_channel(row) for row in selected], [OIL, COMMODITY, COMMODITY, OTHER])
        self.assertFalse(set(first_keys) & set(keys))
        self.assertEqual(resumed.state["review_channel_positions"], {"2026-01": 2})
        self.assertEqual(resumed.state["review_stream_positions"], saved["review_stream_positions"])

    def test_resume_continues_after_last_successful_cls_topic_stream(self):
        rows = [candidate("cls", "old-" + str(index), topic="1583") for index in range(100)]
        rows += [candidate("cls", "macro", topic="3140")]
        runner = self.runner({"cls": rows})
        first, _ = self.review_one(runner, 1)
        self.assertEqual(first[0]["discovery_topic_id"], "1583")
        resumed = Backfill(runner.start, runner.end, runner.output, runner.database, runner.specs,
            adapter=Mock(side_effect=AssertionError("resume must not collect")),
            processor=self.completed, poll_seconds=.001, retry_delay=0)
        selected, _ = resumed._pending(limit=1)
        self.assertEqual(selected[0]["discovery_topic_id"], "3140")

    def assert_failed_review_preserves_cursor(self, error, *, limit=1, service=False):
        processor = Mock(side_effect=error)
        runner = self.runner(self.balanced(), processor=processor)
        before = copy.deepcopy(runner.state["review_channel_positions"])
        before_streams = copy.deepcopy(runner.state["review_stream_positions"])
        if service:
            with patch.object(runner.stop, "wait", return_value=True):
                self.review_one(runner, limit)
        else:
            self.review_one(runner, limit)
        self.assertEqual(runner.state["review_channel_positions"], before)
        self.assertEqual(runner.state["review_stream_positions"], before_streams)
        manifest = json.loads(runner.path.read_text(encoding="utf-8"))
        self.assertEqual(manifest["review_channel_positions"], before)
        self.assertEqual(manifest["review_stream_positions"], before_streams)
        self.assertEqual(runner.state["completed_chunks"], [])
        self.assertNotIn("reviewed", runner.ledger.values())
        return runner, processor

    def test_failed_singleton_does_not_commit_cursor(self):
        runner, processor = self.assert_failed_review_preserves_cursor(
            RuntimeError("fixture semantic validation failed"))
        self.assertEqual(processor.call_count, 3)
        self.assertEqual(list(runner.ledger.values()), ["review_failed"])

    def test_failed_chunk_split_does_not_commit_cursor(self):
        runner, processor = self.assert_failed_review_preserves_cursor(
            RuntimeError("fixture semantic validation failed"), limit=4)
        self.assertEqual(processor.call_count, 3)
        self.assertEqual(runner.state["review_chunk_limit"], 2)
        self.assertEqual(runner.ledger, {})

    def test_service_failure_does_not_commit_cursor_or_review_ledger(self):
        runner, processor = self.assert_failed_review_preserves_cursor(
            RuntimeError("service unavailable"), service=True)
        self.assertEqual(processor.call_count, 1)
        self.assertEqual(runner.state["review_service_status"], "waiting")
        self.assertEqual(runner.ledger, {})

    def test_service_retry_rebuilds_identical_rows_keys_and_does_not_commit_proposals(self):
        runner = self.runner(self.balanced(), processor=Mock(side_effect=RuntimeError("service unavailable")))
        runner.state["review_channel_positions"]["2026-01"] = 3
        before = runner._pending(limit=2)
        positions = copy.deepcopy(runner.state["review_channel_positions"])
        streams = copy.deepcopy(runner.state["review_stream_positions"])
        with patch.object(runner.stop, "wait", return_value=True):
            attempted = self.review_one(runner, 2)
        self.assertEqual(attempted, before)
        self.assertEqual(runner._pending(limit=2), before)
        self.assertEqual(runner.state["review_channel_positions"], positions)
        self.assertEqual(runner.state["review_stream_positions"], streams)

    def test_stopped_review_does_not_commit_cursor_or_review_ledger(self):
        runner, processor = self.assert_failed_review_preserves_cursor(Stopped())
        self.assertEqual(processor.call_count, 1)
        self.assertEqual(runner.ledger, {})

    def test_no_priority_keeps_file_and_source_order(self):
        rows = {"a": [candidate("a", "october", "2026-10"),
                      candidate("a", "january", topic="1583"),
                      candidate("a", "february", "2026-02")],
                "cls": [candidate("cls", "topic", topic="3140")]}
        runner = self.runner(rows, months=())
        selected, _ = runner._pending(limit=3)
        self.assertEqual([row["external_id"] for row in selected], ["october", "january", "february"])
        self.assertEqual(runner.state["review_channel_positions"], {})

    def test_month_selection_balances_archive_plus_selected_candidates(self):
        runner = self.runner({"a": [candidate("a", "jan-" + str(index)) for index in range(4)] +
            [candidate("a", "feb-" + str(index), "2026-02") for index in range(4)]},
            months=("2026-01", "2026-02"))
        original = runner._update_archive_distribution
        fixture_archive = [{"id": "existing-jan", "published_at": "2026-01-05T09:00:00+08:00"}]
        runner._update_archive_distribution = lambda records: original(fixture_archive)
        selected, _ = runner._pending(limit=3)
        self.assertEqual([row["published_date"][:7] for row in selected],
                         ["2026-02", "2026-01", "2026-02"])
        self.assertEqual(runner.state["review_chunk_months"], {"2026-01": 1, "2026-02": 2})

    def test_october_waits_for_collection_then_releases_after_priority_rows(self):
        runner = self.runner({"cls": [candidate("cls", "october", "2026-10", topic="1583")]})
        self.assertEqual(runner._pending(limit=2), ([], []))
        runner.collected.set()
        selected, _ = runner._pending(limit=2)
        self.assertEqual([row["external_id"] for row in selected], ["october"])
        self.assertEqual(runner.state["review_channel_positions"], {"2026-01": 0})


if __name__ == "__main__":
    unittest.main()
