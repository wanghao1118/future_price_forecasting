import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.history_priority_topics import run_monthly_topics


class MonthlyTopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []

    def fake(self, source_id, first, last, output, *, max_pages, max_articles, fetcher,topic_ids=None):
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        report_path = output / "report.json"
        old = json.loads(report_path.read_text()) if report_path.exists() else {}
        old_count = old.get("candidate_count", 0)
        count = max_articles if max_articles is not None else 2
        rows = [{"candidate_id": "cls:" + first + ":" + str(index), "source_id": "cls",
                 "source_url": "https://www.cls.cn/detail/" + str(index), "title": "真实来源快讯标题",
                 "body": "来源返回的公开正文", "published_date": first} for index in range(old_count + count)]
        candidates = output / "candidates.jsonl"
        candidates.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
        report = {"source_id": source_id, "status": "incomplete", "terminal": False,
                  "pages": old.get("pages", 0) + 1, "pages_this_call": 1,
                  "candidate_count": len(rows), "new_candidates": count,
                  "processed_article_count": old.get("processed_article_count", 0) + count,
                  "candidates_path": str(candidates), "errors": [],
                  "coverage": {"requested_start": first, "requested_end": last, "complete": False}}
        if topic_ids is not None:
            report['topic_ids'] = list(topic_ids)
            report['coverage']['topic_ids'] = list(topic_ids)
        report_path.write_text(json.dumps(report))
        self.calls.append((first, last, max_pages, max_articles))
        return report

    def test_budget_is_shared_and_months_rotate_after_restart(self):
        priority = ["2026-%02d" % month for month in range(1, 10)]
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=self.fake):
            first = run_monthly_topics("cls", "2026-01-01", "2026-10-30", self.root, priority)
            second = run_monthly_topics("cls", "2026-01-01", "2026-10-30", self.root, priority)
        self.assertEqual(first["months_visited_this_call"], priority[:5])
        self.assertEqual(second["months_visited_this_call"], priority[5:] + priority[:1])
        self.assertEqual(first["processed_articles_this_call"], 40)
        self.assertEqual(second["processed_articles_this_call"], 40)
        self.assertTrue(all(call[2] <= 2 for call in self.calls))
        self.assertFalse(first["terminal"])
        self.assertFalse(first["coverage"]["complete"])
        self.assertEqual(first["monthly_candidate_counts"]["2026-10"], 0)
        self.assertEqual(second["candidate_count"], 80)

    def test_partial_month_ranges_are_clipped(self):
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=self.fake):
            report = run_monthly_topics("cls", "2026-01-15", "2026-02-05", self.root,
                                        ["2026-01", "2026-02"], max_pages=4, max_articles=6)
        self.assertEqual([call[:2] for call in self.calls], [("2026-01-15", "2026-01-31"), ("2026-02-01", "2026-02-05")])
        self.assertEqual(report["candidate_count"], 6)

    def test_no_full_range_completion_until_nonpriority_month_finishes(self):
        def complete(*args, **kwargs):
            report = self.fake(*args, **kwargs)
            report.update(status="complete", terminal=True)
            report["coverage"]["complete"] = True
            (Path(args[3]) / "report.json").write_text(json.dumps(report))
            return report
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=complete):
            first = run_monthly_topics("cls", "2026-01-01", "2026-02-28", self.root, ["2026-01"])
            self.assertFalse(first["terminal"])
            self.assertFalse(first["coverage"]["complete"])
            second = run_monthly_topics("cls", "2026-01-01", "2026-02-28", self.root, ["2026-01"])
            self.assertTrue(second["terminal"])
            self.assertTrue(second["coverage"]["complete"])
        self.assertEqual(second["months_visited_this_call"], ["2026-02"])

    def test_blocked_month_retains_gap_and_other_month_advances(self):
        def blocked_first(*args, **kwargs):
            report = self.fake(*args, **kwargs)
            if args[1].startswith("2026-01"):
                report.update(status="blocked", terminal=True, errors=["官网明确限制匿名访问"])
                (Path(args[3]) / "report.json").write_text(json.dumps(report))
            return report
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=blocked_first):
            report = run_monthly_topics("cls", "2026-01-01", "2026-02-28", self.root,
                                        ["2026-01", "2026-02"], max_pages=4)
        self.assertEqual(report["months_visited_this_call"], ["2026-01", "2026-02"])
        self.assertFalse(report["terminal"])
        self.assertFalse(report["coverage"]["complete"])
        self.assertTrue(report["errors"])

    def test_one_page_budget_only_one_month_and_real_body_retained(self):
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=self.fake):
            report = run_monthly_topics("cls", "2026-01-01", "2026-09-30", self.root,
                                        ["2026-09", "2026-01"], max_pages=1, max_articles=1)
        self.assertEqual(report["months_visited_this_call"], ["2026-09"])
        row = json.loads(Path(report["candidates_path"]).read_text())
        self.assertEqual(row["body"], "来源返回的公开正文")
        self.assertEqual(row["published_date"], "2026-09-01")

    def test_recovers_month_report_after_parent_checkpoint_loss(self):
        month_dir = self.root / "months/2026-01"
        old = self.fake("cls", "2026-01-01", "2026-01-31", month_dir, max_pages=1, max_articles=2, fetcher=None)
        old.update(status="complete", terminal=True)
        old["coverage"]["complete"] = True
        (month_dir / "report.json").write_text(json.dumps(old))
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=self.fake):
            report = run_monthly_topics("cls", "2026-01-01", "2026-02-28", self.root, ["2026-01", "2026-02"])
        self.assertEqual(report["months_visited_this_call"], ["2026-02"])
        self.assertEqual(report["monthly_candidate_counts"]["2026-01"], 2)

    def test_scope_mismatch_and_invalid_priority_are_rejected(self):
        with patch("newsdesk.history_priority_topics.topics.run_source", side_effect=self.fake):
            run_monthly_topics("cls", "2026-01-01", "2026-01-31", self.root)
        with self.assertRaises(ValueError):
            run_monthly_topics("cls", "2026-01-01", "2026-02-28", self.root)
        with self.assertRaises(ValueError):
            run_monthly_topics("cls", "2026-01-01", "2026-01-31", self.root / "another", ["2026-02"])
        with self.assertRaises(ValueError):
            run_monthly_topics("not-cls", "2026-01-01", "2026-01-31", self.root)

    def test_explicit_topic_scope_survives_restart_and_rejects_changed_ids(self):
        with patch('newsdesk.history_priority_topics.topics.run_source',side_effect=self.fake) as called:
            one = run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,
                ['2026-01','2026-02'],topic_ids=['3140'],max_pages=2,max_articles=2)
            two = run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,
                ['2026-01','2026-02'],topic_ids=['3140'],max_pages=2,max_articles=2)
        self.assertEqual(one['months_visited_this_call'],['2026-01'])
        self.assertEqual(two['months_visited_this_call'],['2026-02'])
        self.assertEqual(called.call_args.kwargs['topic_ids'],['3140'])
        self.assertEqual(json.loads((self.root/'monthly-checkpoint.json').read_text())['topic_ids'],['3140'])
        before = (self.root/'monthly-checkpoint.json').read_bytes()
        with self.assertRaises(ValueError):
            run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,topic_ids=['7381'])
        self.assertEqual((self.root/'monthly-checkpoint.json').read_bytes(),before)

    def test_old_default_wrapper_checkpoint_migrates_only_to_original_five(self):
        from newsdesk.history_media_extra import DEFAULT_TOPIC_IDS
        with patch('newsdesk.history_priority_topics.topics.run_source',side_effect=self.fake):
            run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,max_pages=2,max_articles=1)
            path = self.root/'monthly-checkpoint.json'
            old = json.loads(path.read_text());old.pop('topic_ids')
            path.write_text(json.dumps(old))
            with self.assertRaisesRegex(ValueError,'原五个'):
                run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,topic_ids=['3140'])
            self.assertNotIn('topic_ids',json.loads(path.read_text()))
            resumed = run_monthly_topics('cls','2026-01-01','2026-02-28',self.root,max_pages=2,max_articles=1)
        self.assertEqual(tuple(resumed['topic_ids']),DEFAULT_TOPIC_IDS)
        self.assertEqual(json.loads(path.read_text())['topic_ids'],list(DEFAULT_TOPIC_IDS))

    def test_legacy_wrapper_cannot_adopt_a_foreign_child_checkpoint(self):
        with patch('newsdesk.history_priority_topics.topics.run_source',side_effect=self.fake):
            run_monthly_topics('cls','2026-01-01','2026-01-31',self.root,max_pages=1,max_articles=1)
        path = self.root/'monthly-checkpoint.json';old=json.loads(path.read_text());old.pop('topic_ids')
        path.write_text(json.dumps(old))
        child=self.root/'months/2026-01/checkpoint.json'
        child.write_text(json.dumps({'topic_ids':['3140']}))
        with self.assertRaisesRegex(ValueError,'子专题范围'):
            run_monthly_topics('cls','2026-01-01','2026-01-31',self.root)


if __name__ == "__main__":
    unittest.main()
