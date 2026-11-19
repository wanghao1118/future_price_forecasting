import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from newsdesk.history_media import aa_more_rows, caixin_rows, run_source, ths_rows

TZ = ZoneInfo("Asia/Shanghai")


class FixtureFetcher:
    def __init__(self, root, responder):
        self.root = Path(root)
        self.responder = responder
        self.calls = []

    def get(self, source_id, url):
        self.calls.append(url)
        value = self.responder(url)
        if isinstance(value, Exception):
            raise value
        raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        path = self.root / ("raw-" + str(len(self.calls)) + ".json")
        path.write_text(json.dumps({"request_url": url, "raw_response": raw}), encoding="utf-8")
        return raw, path, "2026-10-30T00:00:00+00:00"


def caixin_item(identifier, stamp, body="公开能源摘要，供应及运输变化"):
    return {"nid": identifier, "link": "https://www.caixin.com/" + stamp[:10] + "/" + str(identifier) + ".html",
            "time": stamp, "desc": "公开标题" + str(identifier), "summ": body,
            "relationNews": [{"nid": 999, "desc": "不属于栏目结果的关联推荐"}]}


def flash(identifier, stamp, body="原油运输受到影响"):
    return {"id": identifier, "ctime": int(datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp()),
            "title": "原始新闻标题", "content": body}


def ths_listing(day, identifiers, next_page=None):
    blocks = []
    for identifier in identifiers:
        blocks.append('<li><span class="arc-title"><a href="http://futures.10jqka.com.cn/' + day + '/c' + str(identifier) + '.shtml">原油新闻</a><span>' + day[4:6] + '月' + day[6:] + '日 12:30</span></span><a class="arc-cont news-link" href="#">栏目公开摘要：油轮运输中断</a></li>')
    return '<title>股指期货要闻_同花顺财经</title><div class="view_otherday"></div><ul>' + ''.join(blocks) + '</ul>' + ('<a href="' + next_page + '" class="next">下一页</a>' if next_page else '')


class HistoryMediaTests(unittest.TestCase):
    def test_aastocks_integer_and_string_end_markers_are_not_parser_errors(self):
        for payload in [1,'1','']:
            with self.subTest(payload=payload):
                self.assertEqual(aa_more_rows(json.dumps(payload),'/tmp/raw','now'),([],None))

    def test_aastocks_datafeed_error_is_never_an_empty_successful_page(self):
        for payload in [2,'2',True,None]:
            with self.subTest(payload=payload),self.assertRaises(ValueError):
                aa_more_rows(json.dumps(payload),'/tmp/raw','now')

    def test_caixin_resumes_pending_rows_then_crosses_year_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                offset = int(parse_qs(urlsplit(url).query)["start"][0])
                return {"datas": [caixin_item(1, "2026-01-04 10:00:00"), caixin_item(2, "2026-01-02 11:00:00")]
                        if offset == 0 else [caixin_item(3, "2025-12-31 11:00:00")]}
            fetcher = FixtureFetcher(temp, response)
            first = run_source("caixin", "2026-01-01", "2026-01-05", temp, max_pages=1, max_articles=1, fetcher=fetcher)
            self.assertEqual((first["status"], first["terminal"], first["candidate_count"]), ("incomplete", False, 1))
            second = run_source("caixin", "2026-01-01", "2026-01-05", temp, max_pages=1, fetcher=fetcher)
            self.assertEqual((second["status"], second["candidate_count"]), ("complete", 2))
            self.assertEqual(len(fetcher.calls), 2)
            rows = [json.loads(line) for line in Path(second["candidates_path"]).read_text().splitlines()]
            self.assertEqual({row["external_id"] for row in rows}, {"1", "2"})
            self.assertTrue(all(row["content_scope"] == "public_article_lead" for row in rows))
            third = run_source("caixin", "2026-01-01", "2026-01-05", temp, fetcher=fetcher)
            self.assertEqual(third["new_candidates"], 0)
            self.assertEqual(len(fetcher.calls), 2)

    def test_caixin_excludes_nested_related_stories_and_missing_public_text(self):
        with tempfile.TemporaryDirectory() as temp:
            data = {"datas": [caixin_item(1, "2026-01-02 10:00:00", body=""), caixin_item(2, "2025-12-30 10:00:00")]}
            _, rows = caixin_rows(json.dumps(data), Path(temp) / "raw.json", "2026-10-30T00:00:00Z")
            self.assertEqual(len(rows), 2)
            fetcher = FixtureFetcher(temp, lambda url: data)
            report = run_source("caixin", "2026-01-01", "2026-01-05", temp, fetcher=fetcher)
            self.assertEqual(report["candidate_count"], 0)
            self.assertEqual(report["invalid_count"], 1)
            audit = json.loads((Path(temp) / "invalid-candidates.jsonl").read_text())
            self.assertEqual(audit["candidate"]["body"], "")

    def test_interrupted_candidate_append_can_resume_without_duplicate_or_loss(self):
        for broken_tail in [True, False]:
            with self.subTest(broken_tail=broken_tail), tempfile.TemporaryDirectory() as temp:
                def response(url):
                    offset = int(parse_qs(urlsplit(url).query)["start"][0])
                    return {"datas": [caixin_item(1, "2026-01-04 10:00:00"), caixin_item(2, "2026-01-02 11:00:00")]
                            if offset == 0 else [caixin_item(3, "2025-12-31 11:00:00")]}
                fetcher = FixtureFetcher(temp, response)
                report = run_source("caixin", "2026-01-01", "2026-01-05", temp, max_pages=1, max_articles=1, fetcher=fetcher)
                path = Path(report["candidates_path"])
                path.write_text(path.read_text() + '{"unfinished":' if broken_tail else path.read_text().rstrip("\n"))
                report = run_source("caixin", "2026-01-01", "2026-01-05", temp, fetcher=fetcher)
                rows = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertEqual({row["external_id"] for row in rows}, {"1", "2"})
                self.assertEqual((report["status"], report["candidate_count"]), ("complete", 2))

    def test_ths_daily_listing_pagination_resume_is_not_a_year_completion(self):
        with tempfile.TemporaryDirectory() as temp:
            following = "http://futures.10jqka.com.cn/gzqhyw_list/20260101/index_2.shtml"
            def response(url):
                if "20260102" in url:
                    return ths_listing("20260102", [])
                if "index_2.shtml" in url:
                    return ths_listing("20260101", [3])
                return ths_listing("20260101", [1, 2], following)
            fetcher = FixtureFetcher(temp, response)
            first = run_source("ths", "2026-01-01", "2026-01-02", temp, max_pages=1, max_articles=1, fetch_articles=False, fetcher=fetcher)
            self.assertEqual(first["status"], "incomplete")
            second = run_source("ths", "2026-01-01", "2026-01-02", temp, max_pages=1, fetch_articles=False, fetcher=fetcher)
            self.assertEqual((second["status"], second["candidate_count"]), ("incomplete", 3))
            third = run_source("ths", "2026-01-01", "2026-01-02", temp, max_pages=1, fetch_articles=False, fetcher=fetcher)
            self.assertEqual((third["status"], third["candidate_count"]), ("complete", 3))
            self.assertEqual(len(fetcher.calls), 3)
            self.assertIn("/20260101/", fetcher.calls[0])
            self.assertIn("index_2.shtml", fetcher.calls[1])

    def test_ths_priority_months_interleave_days_before_october(self):
        with tempfile.TemporaryDirectory() as temp:
            priorities = tuple("2026-{:02}".format(month) for month in range(1, 10))
            def response(url):
                day = urlsplit(url).path.split("/")[2]
                return ths_listing(day, [])
            fetcher = FixtureFetcher(temp, response)
            report = run_source("ths", "2026-01-01", "2026-10-02", temp, max_pages=11,
                                fetch_articles=False, fetcher=fetcher, priority_months=priorities)
            observed = [urlsplit(url).path.split("/")[2] for url in fetcher.calls]
            self.assertEqual(observed, [month.replace("-", "") + "01" for month in priorities]
                             + ["20260102", "20260202"])
            self.assertEqual(report["completed_days"], 11)
            self.assertFalse(report["terminal"])
            self.assertEqual(report["frontier_date"], "2026-03-02")

    def test_ths_adopts_priorities_without_losing_old_pending_articles_or_next_page(self):
        with tempfile.TemporaryDirectory() as temp:
            following = "http://futures.10jqka.com.cn/gzqhyw_list/20260131/index_2.shtml"
            def response(url):
                if "20260201" in url:
                    return ths_listing("20260201", [4])
                if "index_2.shtml" in url:
                    return ths_listing("20260131", [3])
                return ths_listing("20260131", [1, 2], following)
            fetcher = FixtureFetcher(temp, response)
            first = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                               max_articles=1, fetch_articles=False, fetcher=fetcher)
            self.assertEqual(first["candidate_count"], 1)
            second = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                                fetch_articles=False, fetcher=fetcher, priority_months=("2026-02",))
            self.assertIn("20260201", fetcher.calls[1])
            self.assertEqual(second["candidate_count"], 3)
            self.assertFalse(second["terminal"])
            third = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                               fetch_articles=False, fetcher=fetcher, priority_months=("2026-02",))
            self.assertEqual(fetcher.calls[2], following.replace("http://", "https://"))
            self.assertEqual((third["status"], third["candidate_count"]), ("complete", 4))
            self.assertEqual(third["completed_days"], 2)

    def test_ths_month_priorities_keep_bounded_failure_retries_after_unseen_month(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                return OSError("temporary date outage") if "20260131" in url else ths_listing("20260201", [4])
            fetcher = FixtureFetcher(temp, response)
            run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                       fetch_articles=False, fetcher=fetcher)
            second = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                                fetch_articles=False, fetcher=fetcher, priority_months=("2026-01", "2026-02"))
            self.assertIn("20260201", fetcher.calls[1])
            self.assertEqual(second["retry_attempts_remaining"], 2)
            self.assertIn("temporary date outage", second["errors"][0]["error"])
            third = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                               fetch_articles=False, fetcher=fetcher, priority_months=("2026-01", "2026-02"))
            self.assertEqual(third["retry_attempts_remaining"], 1)
            final = run_source("ths", "2026-01-31", "2026-02-01", temp, max_pages=1,
                               fetch_articles=False, fetcher=fetcher, priority_months=("2026-01", "2026-02"))
            self.assertEqual((final["status"], final["terminal"], final["candidate_count"]), ("incomplete", True, 1))
            self.assertEqual(final["retry_attempts_remaining"], 0)
            self.assertEqual(final["coverage"]["gap_dates"], ["2026-01-31"])
            self.assertEqual(len(fetcher.calls), 4)

    def test_ths_invalid_priority_months_are_rejected_before_fetching(self):
        with tempfile.TemporaryDirectory() as temp:
            for priorities in ["2026-01", ["2026-1"], ["2026-13"]]:
                with self.subTest(priorities=priorities), self.assertRaises(ValueError):
                    run_source("ths", "2026-01-01", "2026-02-01", temp,
                               priority_months=priorities, fetcher=FixtureFetcher(temp, lambda url: ""))

    def test_ths_priority_failure_remains_attached_to_its_original_date(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                return OSError("temporary January outage") if "20260101" in url else ths_listing("20260201", [])
            fetcher = FixtureFetcher(temp, response)
            run_source("ths", "2026-01-01", "2026-02-01", temp, max_pages=1,
                       fetch_articles=False, fetcher=fetcher, priority_months=("2026-01", "2026-02"))
            report = run_source("ths", "2026-01-01", "2026-02-01", temp, max_pages=1,
                                fetch_articles=False, fetcher=fetcher, priority_months=("2026-01", "2026-02"))
            state = json.loads((Path(temp) / "checkpoint.json").read_text())
            self.assertEqual(set(state["date_errors"]), {"2026-01-01"})
            self.assertEqual(report["retry_attempts_remaining"], 2)
            self.assertIn("20260201", fetcher.calls[1])

    def test_ths_clearing_month_priorities_still_processes_all_remaining_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                return ths_listing(urlsplit(url).path.split("/")[2], [])
            fetcher = FixtureFetcher(temp, response)
            run_source("ths", "2026-01-31", "2026-02-02", temp, max_pages=1,
                       fetch_articles=False, fetcher=fetcher, priority_months=("2026-02",))
            self.assertIn("20260201", fetcher.calls[0])
            report = run_source("ths", "2026-01-31", "2026-02-02", temp, max_pages=2,
                                fetch_articles=False, fetcher=fetcher)
            self.assertEqual([urlsplit(url).path.split("/")[2] for url in fetcher.calls],
                             ["20260201", "20260131", "20260202"])
            self.assertEqual(report["status"], "complete")
            self.assertEqual(report["completed_days"], 3)

    def test_ths_public_body_is_fetched_without_using_title_as_body(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                if "/c1.shtml" in url:
                    return '<h1>原油新闻</h1><span>2026-01-01 12:30:05</span><div class="news-content-parsed"><p>油轮交付延迟，港口暂停运作。</p></div>'
                return ths_listing("20260101", [1])
            report = run_source("ths", "2026-01-01", "2026-01-01", temp, fetcher=FixtureFetcher(temp, response))
            row = json.loads(Path(report["candidates_path"]).read_text())
            self.assertEqual(row["content_scope"], "public_article")
            self.assertIn("港口暂停", row["body"])
            self.assertEqual(row["published_at"], "2026-01-01T12:30:05+08:00")

    def test_ths_placeholder_excerpt_is_gap_with_original_raw_and_does_not_skip_next_page(self):
        with tempfile.TemporaryDirectory() as temp:
            following = 'https://futures.10jqka.com.cn/gzqhyw_list/20260101/index_2.html'
            def response(url):
                if url == following:
                    return ths_listing('20260101', [2])
                return ths_listing('20260101', [1], following).replace('栏目公开摘要：油轮运输中断', '...')
            fetcher = FixtureFetcher(temp, response)
            report = run_source('ths', '2026-01-01', '2026-01-01', temp,
                                fetch_articles=False, fetcher=fetcher)
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['invalid_count'], 1)
            self.assertEqual(report['status'], 'incomplete')
            self.assertTrue(report['terminal'])
            self.assertEqual(report['coverage']['content_gap_dates'], ['2026-01-01'])
            self.assertEqual(report['coverage']['gap_dates'], ['2026-01-01'])
            saved = json.loads(Path(report['candidates_path']).read_text())
            self.assertEqual(saved['external_id'], '2')
            invalid = json.loads((Path(temp) / 'invalid-candidates.jsonl').read_text())['candidate']
            self.assertEqual(invalid['body'], '...')
            self.assertTrue(invalid['source_url'].endswith('/c1.shtml'))
            self.assertEqual(invalid['source_listing_url'], fetcher.calls[0])
            self.assertEqual(invalid['raw_path'], invalid['discovery_raw_path'])
            self.assertIn('...', Path(invalid['raw_path']).read_text())
            again = run_source('ths', '2026-01-01', '2026-01-01', temp,
                               fetch_articles=False, fetcher=fetcher)
            self.assertEqual(len(fetcher.calls), 2)
            self.assertEqual(again['coverage']['content_gap_dates'], ['2026-01-01'])

    def test_ths_placeholder_detail_falls_back_only_to_real_listing_excerpt(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                if '/c1.shtml' in url:
                    return '<div class="news-content-parsed"><p>…！？</p></div>'
                return ths_listing('20260101', [1])
            report = run_source('ths', '2026-01-01', '2026-01-01', temp,
                                fetcher=FixtureFetcher(temp, response))
            saved = json.loads(Path(report['candidates_path']).read_text())
            self.assertEqual(saved['body'], '栏目公开摘要：油轮运输中断')
            self.assertEqual(saved['content_scope'], 'public_article_excerpt')
            self.assertTrue(report['errors'][0]['public_excerpt_retained'])

    def test_unavailable_ths_date_does_not_prevent_later_dates(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                return OSError("public date unavailable") if "20260101" in url else ths_listing("20260102", [2])
            fetcher = FixtureFetcher(temp, response)
            for _ in range(3):
                report = run_source("ths", "2026-01-01", "2026-01-02", temp, fetch_articles=False, fetcher=fetcher)
            self.assertEqual((report["status"], report["terminal"], report["candidate_count"]), ("incomplete", True, 1))
            self.assertEqual(len(report["errors"]), 1)

    def test_ths_january_holiday_404s_advance_to_valid_january_fifth(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                return ths_listing("20260105", [5]) if "20260105" in url else HTTPError(url, 404, "Date archive unavailable", None, None)
            fetcher = FixtureFetcher(temp, response)
            first = run_source("ths", "2026-01-01", "2026-01-05", temp, max_pages=2, fetch_articles=False, fetcher=fetcher)
            self.assertEqual((first["candidate_count"], first["completed_days"], first["attempted_days"]), (0, 2, 2))
            self.assertEqual(first["frontier_date"], "2026-01-03")
            self.assertFalse(first["retry_pending"])
            second = run_source("ths", "2026-01-01", "2026-01-05", temp, max_pages=2, fetch_articles=False, fetcher=fetcher)
            self.assertEqual(second["completed_days"], 4)
            self.assertEqual(second["frontier_date"], "2026-01-05")
            final = run_source("ths", "2026-01-01", "2026-01-05", temp, max_pages=2, fetch_articles=False, fetcher=fetcher)
            self.assertEqual((final["candidate_count"], final["completed_days"], final["attempted_days"]), (1, 5, 5))
            self.assertEqual((final["status"], final["terminal"]), ("incomplete", True))
            self.assertEqual(final["coverage"]["gap_dates"], ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"])
            self.assertEqual(final["processed_article_count"], 1)
            self.assertEqual(len(fetcher.calls), 5)

    def test_retry_pending_only_covers_the_remaining_bounded_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: OSError("temporary network error"))
            for remaining in [2, 1, 0]:
                report = run_source("cls", "2026-01-01", "2026-01-05", temp, fetcher=fetcher)
                self.assertEqual(report["retry_attempts_remaining"], remaining)
                self.assertEqual(report["retry_pending"], remaining > 0)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(report["terminal"])

    def test_flash_empty_or_repeating_public_history_is_blocked_not_complete(self):
        for mode in ["empty", "repeat"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                counter = [0]
                def response(url):
                    counter[0] += 1
                    return {"errno": 0, "noise": counter[0], "data": {"roll_data": [] if mode == "empty" else [flash(1, "2026-01-02T12:00:00")]}}
                report = run_source("cls", "2026-01-01", "2026-01-05", temp, fetcher=FixtureFetcher(temp, response))
                self.assertEqual(report["status"], "blocked")
                self.assertFalse(report["coverage"]["complete"])
                self.assertTrue(report["terminal"])

    def test_flash_date_boundary_valid_rows_and_invalid_body_are_isolated(self):
        with tempfile.TemporaryDirectory() as temp:
            count = [0]
            def response(url):
                count[0] += 1
                rows = [flash(1, "2026-01-03T12:00:00", ""), flash(2, "2026-01-02T12:00:00")] if count[0] == 1 else [flash(3, "2025-12-31T10:00:00")]
                return {"errno": 0, "data": {"roll_data": rows}}
            report = run_source("cls", "2026-01-01", "2026-01-05", temp, fetcher=FixtureFetcher(temp, response))
            self.assertEqual((report["status"], report["candidate_count"], report["invalid_count"]), ("complete", 1, 1))

    def test_jiemian_uses_time_cursor_without_adding_a_second_page_offset(self):
        with tempfile.TemporaryDirectory() as temp:
            counter = [0]
            def response(url):
                self.assertEqual(parse_qs(urlsplit(url).query)["page"], ["1"])
                counter[0] += 1
                stamp = "2026-01-02T12:00:00" if counter[0] == 1 else "2025-12-31T10:00:00"
                row = {"id": counter[0], "title": "原油消息", "summary": "油轮运输交付延迟", "publishtime": int(datetime.fromisoformat(stamp).replace(tzinfo=TZ).timestamp())}
                return {"code": "0", "result": {"list": [row]}}
            report = run_source("jiemian", "2026-01-01", "2026-01-05", temp, fetcher=FixtureFetcher(temp, response))
            self.assertEqual((report["status"], report["candidate_count"]), ("complete", 1))

    def test_jiemian_empty_result_array_is_a_limit_not_a_parser_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            report = run_source("jiemian", "2026-01-01", "2026-01-05", temp, fetcher=FixtureFetcher(temp, lambda url: {"code": "0", "result": []}))
            self.assertEqual(report["status"], "blocked")
            self.assertEqual(report["errors"], [])

    def test_changed_date_range_cannot_reuse_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            fetcher = FixtureFetcher(temp, lambda url: {"datas": []})
            run_source("caixin", "2026-01-01", "2026-01-05", temp, fetcher=fetcher)
            with self.assertRaisesRegex(ValueError, "断点"):
                run_source("caixin", "2026-02-01", "2026-02-05", temp, fetcher=fetcher)

    def test_aastocks_search_finds_january_but_is_not_claimed_as_whole_site_history(self):
        with tempfile.TemporaryDirectory() as temp:
            def response(url):
                if "/aafn-con/" in url:
                    stamp = "2026/01/07 08:22" if "NOW.1/" in url else "2026/03/11 09:14"
                    return '<meta property="og:title" content="原油供应变化"><div id="spanContent"><p>原油运输和库存均有变化。</p></div><script>ConvertToLocalTime({dt:"' + stamp + '"})</script>'
                return '<div id="aafn-search-c1"><a href="/sc/stocks/news/aafn-con/NOW.1/top-news/AAFN">原油供应</a><a href="/sc/stocks/news/aafn-con/NOW.2/top-news/AAFN">油市库存</a></div><div id="checker_content_end"></div><script>var param = {"cat":"top-news","newstime":"opaque","newsid":"NOW.1","period":"1","key":"oil","newsrev":"5"};</script>'
            fetcher = FixtureFetcher(temp, response)
            first = run_source("aastocks", "2026-01-01", "2026-10-30", temp, max_articles=1, fetcher=fetcher, discovery_terms=["原油"])
            self.assertFalse(first["terminal"])
            report = run_source("aastocks", "2026-01-01", "2026-10-30", temp, fetcher=fetcher, discovery_terms=["原油"])
            self.assertEqual(report["status"], "incomplete")
            self.assertIn("300", report["coverage"]["reason"])
            self.assertEqual(report["candidate_count"], 2)
            self.assertTrue(report["terminal"])
            self.assertFalse(report["coverage"]["complete"])
            rows = [json.loads(line) for line in Path(report["candidates_path"]).read_text().splitlines()]
            self.assertEqual(rows[0]["published_at"], "2026-01-07T08:22:00+08:00")


if __name__ == "__main__":
    unittest.main()
