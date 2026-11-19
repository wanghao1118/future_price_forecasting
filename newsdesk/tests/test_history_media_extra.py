import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from newsdesk.history_media_extra import DEFAULT_TOPIC_IDS, TOPICS, run_source, topic_rows, topic_url
from newsdesk.tests.test_history_media import FixtureFetcher, TZ


def item(identifier, stamp, *, kind=-1, title="原油库存和运输消息", brief="公开摘要"):
    return {"article_id": identifier, "article_type": kind, "article_title": title,
            "article_brief": brief, "article_time": int(datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp())}


def detail(identifier, *, free=True, brief="公开文章摘要", content="公开完整正文", stamp="2026-01-02T12:00:00"):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {
        "articleDetail": {"id": identifier, "title": "原油分析", "isFree": free, "brief": brief,
                          "content": content, "ctime": int(datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp())}}}}) + '</script>'


class ClsTopicHistoryTests(unittest.TestCase):
    def test_public_website_digest_vector_sends_no_user_authentication(self):
        url = topic_url("1583", 1769443200)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(set(query), {"Subject_Id", "last_article_time", "os", "sv", "app", "sign"})
        # Captured from the successful anonymous Jan-27 cursor probe.
        self.assertEqual(query["sign"], ["5ae3b708699265fff3025dc5ef9de088"])
        self.assertEqual(query["app"], ["CailianpressWeb"])
        with self.assertRaises(ValueError):
            topic_url("unknown", 1)

    def test_caps_resume_pending_rows_then_continue_oldest_cursor(self):
        with tempfile.TemporaryDirectory() as temp:
            def respond(url):
                cursor = int(parse_qs(urlsplit(url).query)["last_article_time"][0])
                if cursor > int(datetime(2026, 1, 2, tzinfo=TZ).timestamp()):
                    return {"errno": 0, "data": [item(1, "2026-01-02T12:00:00"), item(2, "2026-01-02T00:00:00")]}
                return {"errno": 0, "data": [item(3, "2026-01-01T01:00:00"), item(4, "2025-12-31T20:00:00")]}
            fetcher = FixtureFetcher(temp, respond)
            first = run_source("cls", "2026-01-01", "2026-01-03", temp, max_pages=1, max_articles=1,
                               topic_ids=["1583"], fetcher=fetcher)
            self.assertEqual((first["status"], first["terminal"], first["candidate_count"]), ("incomplete", False, 1))
            second = run_source("cls", "2026-01-01", "2026-01-03", temp, max_pages=1,
                                topic_ids=["1583"], fetcher=fetcher)
            self.assertEqual((second["status"], second["terminal"], second["candidate_count"]), ("complete", True, 3))
            self.assertEqual(second["processed_article_count"], 4)
            self.assertEqual(len(fetcher.calls), 2)
            self.assertEqual(parse_qs(urlsplit(fetcher.calls[1]).query)["last_article_time"], [str(int(datetime(2026, 1, 2, tzinfo=TZ).timestamp()))])
            third = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], fetcher=fetcher)
            self.assertEqual(third["new_candidates"], 0)
            self.assertEqual(len(fetcher.calls), 2)

    def test_five_topics_deduplicate_but_are_never_described_as_full_site(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: {"errno": 0, "data": [
                item(1, "2026-01-02T12:00:00"), item(2, "2025-12-31T20:00:00")]})
            report = run_source("cls", "2026-01-01", "2026-01-03", temp, fetcher=fetcher)
            self.assertEqual((report["status"], report["candidate_count"], report["completed_topics"]), ("complete", 1, 5))
            self.assertEqual(len(fetcher.calls), len(DEFAULT_TOPIC_IDS))
            self.assertEqual(tuple(report['topic_ids']),DEFAULT_TOPIC_IDS)
            self.assertGreater(len(TOPICS),len(DEFAULT_TOPIC_IDS))
            self.assertIn("不代表财联社全站", report["coverage"]["scope"])
            row = json.loads(Path(report["candidates_path"]).read_text())
            self.assertEqual(row["candidate_id"], "cls:1")
            self.assertEqual(row["body"], "原油库存和运输消息")

    def test_free_full_body_and_subscriber_public_excerpt_are_separated(self):
        with tempfile.TemporaryDirectory() as temp:
            def respond(url):
                if "/detail/1" in url:
                    return detail(1, content="油轮停航，港口装载受到影响。")
                if "/detail/2" in url:
                    return detail(2, free=False, brief="订阅文章公开摘要", content="不允许采入的订阅正文")
                return {"errno": 0, "data": [item(1, "2026-01-02T12:00:00", kind=0),
                                                item(2, "2026-01-02T10:00:00", kind=0), item(3, "2025-12-31T20:00:00")]}
            report = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], fetcher=FixtureFetcher(temp, respond))
            rows = [json.loads(line) for line in Path(report["candidates_path"]).read_text().splitlines()]
            self.assertEqual(rows[0]["content_scope"], "public_article")
            self.assertIn("港口装载", rows[0]["body"])
            self.assertEqual(rows[1]["content_scope"], "public_article_excerpt")
            self.assertEqual(rows[1]["body"], "订阅文章公开摘要")
            self.assertNotIn("不允许采入", json.dumps(rows, ensure_ascii=False))

    def test_missing_long_article_body_is_not_invented_from_headline(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: {"errno": 0, "data": [
                item(1, "2026-01-02T12:00:00", kind=0, brief=""), item(2, "2025-12-31T20:00:00")]})
            report = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"],
                                fetch_articles=False, fetcher=fetcher)
            self.assertEqual((report["candidate_count"], report["invalid_count"], report["status"]), (0, 1, "incomplete"))
            self.assertEqual(json.loads((Path(temp) / "invalid-candidates.jsonl").read_text())["candidate"]["body"], "")

    def test_detail_failure_retains_actual_excerpt_with_coverage_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: OSError("unavailable") if "/detail/" in url else {
                "errno": 0, "data": [item(1, "2026-01-02T12:00:00", kind=0), item(2, "2025-12-31T20:00:00")]})
            report = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], fetcher=fetcher)
            self.assertEqual((report["status"], report["candidate_count"], report["terminal"]), ("incomplete", 1, True))
            self.assertEqual(json.loads(Path(report["candidates_path"]).read_text())["body"], "公开摘要")
            self.assertEqual(len(report["errors"]), 1)

    def test_explicit_permission_limit_is_not_retried_and_other_topics_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            def respond(url):
                if "/1583/" in url:
                    return {"errno": 20101, "msg": "请登录"}
                return {"errno": 0, "data": [item(1, "2026-01-02T12:00:00"), item(2, "2025-12-31T20:00:00")]}
            fetcher = FixtureFetcher(temp, respond)
            report = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583", "1101"], fetcher=fetcher)
            self.assertEqual((report["status"], report["candidate_count"], report["completed_topics"]), ("incomplete", 1, 2))
            self.assertEqual(len(fetcher.calls), 2)
            self.assertFalse(report["retry_pending"])
            self.assertEqual(report["progress"][0]["status"], "blocked")

    def test_three_bounded_retries_then_next_topic_progress(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: OSError("network unavailable"))
            for remaining in [2, 1, 0]:
                report = run_source("cls", "2026-01-01", "2026-01-03", temp, max_pages=1,
                                    topic_ids=["1583"], fetcher=fetcher)
                self.assertEqual(report["retry_attempts_remaining"], remaining)
                self.assertEqual(report["retry_pending"], remaining > 0)
            self.assertEqual((report["status"], report["terminal"], report["completed_topics"]), ("blocked", True, 1))

    def test_empty_repeated_or_same_second_page_cannot_claim_completion(self):
        for mode in ["empty", "repeat", "same_second"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                calls = [0]
                def respond(url):
                    calls[0] += 1
                    if mode == "empty":
                        return {"errno": 0, "data": []}
                    rows = [item(1, "2026-01-02T12:00:00")]
                    if mode == "same_second" and calls[0] > 1:
                        rows[0]["article_id"] = 2
                    return {"errno": 0, "data": rows}
                report = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], fetcher=FixtureFetcher(temp, respond))
                self.assertTrue(report["terminal"])
                self.assertNotEqual(report["status"], "complete")
                self.assertFalse(report["coverage"]["complete"])
                self.assertLessEqual(calls[0], 2)

    def test_interrupted_append_and_scope_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: {"errno": 0, "data": [
                item(1, "2026-01-02T12:00:00"), item(2, "2026-01-01T12:00:00"), item(3, "2025-12-31T12:00:00")]})
            first = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], max_articles=1, fetcher=fetcher)
            path = Path(first["candidates_path"])
            path.write_text(path.read_text() + '{"interrupted":')
            second = run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1583"], fetcher=fetcher)
            self.assertEqual((second["status"], second["candidate_count"]), ("complete", 2))
            with self.assertRaisesRegex(ValueError, "断点"):
                run_source("cls", "2026-01-01", "2026-01-03", temp, topic_ids=["1101"], fetcher=fetcher)

    def test_default_topic_checkpoint_remains_the_original_five_when_allowlist_grows(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp,lambda url:{'errno':0,'data':[
                item(1,'2026-01-02T12:00:00'),item(2,'2025-12-31T12:00:00')]})
            first = run_source('cls','2026-01-01','2026-01-03',temp,max_pages=1,fetcher=fetcher)
            before = json.loads((Path(temp)/'checkpoint.json').read_text())
            self.assertEqual(tuple(before['topic_ids']),DEFAULT_TOPIC_IDS)
            resumed = run_source('cls','2026-01-01','2026-01-03',temp,fetcher=fetcher)
            self.assertEqual(resumed['completed_topics'],5)
            self.assertEqual(len(fetcher.calls),5)
            with self.assertRaises(ValueError):
                run_source('cls','2026-01-01','2026-01-03',temp,topic_ids=['3140'],fetcher=fetcher)

    def test_verified_macro_topic_can_be_requested_explicitly_but_not_as_a_string(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp,lambda url:{'errno':0,'data':[
                item(1,'2026-01-02T12:00:00',title='美联储政策利率决定'),item(2,'2025-12-31T12:00:00')]})
            report = run_source('cls','2026-01-01','2026-01-03',temp,topic_ids=['3140'],fetcher=fetcher)
            row = json.loads(Path(report['candidates_path']).read_text())
            self.assertEqual(row['discovery_topic_id'],'3140')
            self.assertEqual(row['discovery_topic'],'美联储动态')
            self.assertEqual(report['coverage']['topic_ids'],['3140'])
            with self.assertRaises(ValueError):
                run_source('cls','2026-01-01','2026-01-03',temp,topic_ids='3140',fetcher=fetcher)


if __name__ == "__main__":
    unittest.main()
