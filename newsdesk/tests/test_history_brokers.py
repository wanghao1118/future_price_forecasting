"""Offline coverage and provenance checks for date-based broker backfills."""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from newsdesk.history_brokers import fetch_selected, parse_date_list, run_source


PUBLISHER = "天风证券股份有限公司"
OTHER = "华创证券有限责任公司"


def listing(rows, last_page=1):
    return "<table>" + "".join(
        '<tr><td><a href="//stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/{}/index.phtml" title="{}">{}</a></td>'
        '<td>{}</td><td><div class="fname05"><span>{}</span></div></td></tr>'.format(identifier, title, title, day, publisher)
        for identifier, title, day, publisher in rows) + '</table><a onclick="set_page_num(\'{}\')">最末页</a>'.format(last_page)


def detail(title="原油报告", day="2026-01-05", publisher=PUBLISHER):
    return '<h1>{}</h1>机构：<a>{}</a>日期：{}<div class="blk_container">{}</div>'.format(
        title, publisher, day, "报告披露油田原油生产与炼厂开工变化。" * 12)


class FakeFetcher:
    def __init__(self, directory, pages, bodies=None):
        self.directory, self.pages, self.bodies = Path(directory), pages, bodies or {}
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        parsed = urlsplit(url)
        if "vReport_List" in url:
            query = parse_qs(parsed.query)
            value = self.pages[(query["pubdate"][0], int(query["p"][0]))]
        else:
            identifier = parsed.path.split("/rptid/")[1].split("/")[0]
            value = self.bodies.get(identifier, detail())
        if isinstance(value, Exception):
            raise value
        path = self.directory / (str(len(self.calls)) + ".json")
        path.write_text(json.dumps({"request_url": url, "raw_response": value}, ensure_ascii=False))
        return value, path, "2026-10-30T06:00:00+00:00"


class HistoryBrokerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def read_rows(self, path):
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line]

    def test_full_date_pagination_matches_author_and_preserves_real_january_date(self):
        fetcher = FakeFetcher(self.root, {
            ("2026-01-05", 1): listing([("101", "石油生产", "2026-01-05", PUBLISHER),
                                         ("102", "别的机构", "2026-01-05", OTHER)], 2),
            ("2026-01-05", 2): listing([("103", "炼厂开工", "2026-01-05", PUBLISHER)], 2)})
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=fetcher)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["days"]["2026-01-05"]["pages"], 2)
        rows = self.read_rows(result["candidates_path"])
        self.assertEqual({row["published_date"] for row in rows}, {"2026-01-05"})
        self.assertEqual({row["published_at"] for row in rows}, {""})
        self.assertEqual({row["publisher_name"] for row in rows}, {PUBLISHER})
        self.assertEqual({row["content_scope"] for row in rows}, {"public_research_summary"})
        self.assertTrue(all(Path(row["raw_path"]).exists() for row in rows))
        self.assertTrue(all("rptid/102" not in url for url in fetcher.calls))

    def test_page_cap_is_incomplete_and_resume_reuses_existing_candidate(self):
        pages = {("2026-01-05", page): listing([(str(page), "原油", "2026-01-05", PUBLISHER)], 2) for page in [1, 2]}
        first = FakeFetcher(self.root, pages)
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=first, max_pages=1)
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["stop_reason"], "max_pages")
        self.assertEqual(result["candidate_count"], 1)
        resumed = FakeFetcher(self.root, pages)
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=resumed)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["candidate_count"], 2)
        self.assertFalse(any("rptid/1/" in url for url in resumed.calls))

    def test_complete_resume_does_not_refetch_closed_days(self):
        fetcher = FakeFetcher(self.root, {("2026-01-05", 1): "没有找到相关内容.."})
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=fetcher)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["candidate_count"], 0)
        resumed = FakeFetcher(self.root, {})
        again = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=resumed)
        self.assertEqual(resumed.calls, [])
        self.assertTrue(again["coverage"]["complete"])

    def test_bounded_resume_attempts_unseen_dates_before_retrying_failed_prefix(self):
        failed = FakeFetcher(self.root, {("2026-01-01", 1): OSError("temporary source outage")})
        result = run_source("institution_14", "2026-01-01", "2026-01-02", self.root,
                            fetcher=failed, metadata_only=True, max_days=1)
        self.assertEqual(set(result["days"]), {"2026-01-01"})
        self.assertEqual(result["days"]["2026-01-01"]["status"], "incomplete")

        # A broken first date must not consume the second round's whole budget.
        unseen = FakeFetcher(self.root, {("2026-01-02", 1): "没有找到相关内容.."})
        result = run_source("institution_14", "2026-01-01", "2026-01-02", self.root,
                            fetcher=unseen, metadata_only=True, max_days=1)
        self.assertEqual(len(unseen.calls), 1)
        self.assertIn("pubdate=2026-01-02", unseen.calls[0])
        self.assertEqual(result["days"]["2026-01-02"]["status"], "complete")
        self.assertIn("temporary source outage", result["days"]["2026-01-01"]["errors"][0]["error"])
        self.assertFalse(result["coverage"]["discovery_complete"])

        recovered = FakeFetcher(self.root, {("2026-01-01", 1): "没有找到相关内容.."})
        result = run_source("institution_14", "2026-01-01", "2026-01-02", self.root,
                            fetcher=recovered, metadata_only=True, max_days=1)
        self.assertTrue(result["coverage"]["discovery_complete"])
        self.assertEqual(result["days"]["2026-01-01"]["attempt"], 2)
        history = result["days"]["2026-01-01"]["previous_attempts"]
        self.assertEqual(len(history), 1)
        self.assertIn("temporary source outage", history[0]["errors"][0]["error"])
        saved = json.loads(Path(result["report_path"]).read_text())
        self.assertEqual(saved["days"]["2026-01-01"]["previous_attempts"], history)

    def test_http_200_login_and_ignored_date_never_become_empty_success(self):
        for text in ["<html>请登录</html>", listing([("1", "旧报告", "2025-12-31", PUBLISHER)])]:
            with self.subTest(text=text[:20]):
                subroot = self.root / str(len(list(self.root.iterdir())))
                subroot.mkdir()
                fetcher = FakeFetcher(subroot, {("2026-01-05", 1): text})
                result = run_source("institution_14", "2026-01-05", "2026-01-05", subroot, fetcher=fetcher)
                self.assertFalse(result["coverage"]["complete"])
                self.assertEqual(result["candidate_count"], 0)
                self.assertTrue(result["errors"])

    def test_detail_failure_does_not_discard_other_reports_or_claim_full_coverage(self):
        fetcher = FakeFetcher(self.root, {("2026-01-05", 1): listing([
            ("1", "失败正文", "2026-01-05", PUBLISHER), ("2", "有效正文", "2026-01-05", PUBLISHER)])},
            {"1": detail(publisher=OTHER)})
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=fetcher)
        self.assertEqual(result["candidate_count"], 1)
        self.assertFalse(result["coverage"]["complete"])
        self.assertIn("机构与来源不匹配", result["errors"][0]["error"])

    def test_metadata_stage_never_fabricates_body_or_skips_final_detail_verification(self):
        fetcher = FakeFetcher(self.root, {("2026-01-05", 1): listing([("1", "待审核标题", "2026-01-05", PUBLISHER)])})
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=fetcher, metadata_only=True)
        self.assertTrue(result["coverage"]["discovery_complete"])
        self.assertFalse(result["coverage"]["complete"])
        self.assertEqual(result["candidate_count"], 0)
        self.assertIsNone(result["candidates_path"])
        rows = self.read_rows(result["discoveries_path"])
        self.assertNotIn("body", rows[0])
        self.assertTrue(rows[0]["requires_detail"])
        self.assertFalse(any("vReport_Show" in url for url in fetcher.calls))
        full = fetch_selected("institution_14", result["discoveries_path"], [rows[0]["candidate_id"]], self.root, fetcher=fetcher)
        self.assertEqual(full["candidate_count"], 1)
        self.assertEqual(full["classification_status"], "unreviewed")
        self.assertFalse(full["coverage"]["complete"])
        self.assertTrue(self.read_rows(full["candidates_path"])[0]["body"])

    def test_metadata_day_and_candidate_caps_remain_incomplete(self):
        fetcher = FakeFetcher(self.root, {("2026-01-05", 1): listing([
            ("1", "报告1", "2026-01-05", PUBLISHER), ("2", "报告2", "2026-01-05", PUBLISHER)])})
        result = run_source("institution_14", "2026-01-05", "2026-01-06", self.root, fetcher=fetcher,
                            metadata_only=True, max_candidates=1)
        self.assertFalse(result["coverage"]["discovery_complete"])
        self.assertEqual(result["discovery_count"], 1)
        self.assertEqual(result["stop_reason"], "max_candidates")

    def test_full_text_run_after_metadata_still_fetches_bodies(self):
        pages = {("2026-01-05", 1): listing([("1", "待审核标题", "2026-01-05", PUBLISHER)])}
        run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=FakeFetcher(self.root, pages), metadata_only=True)
        fetcher = FakeFetcher(self.root, pages)
        result = run_source("institution_14", "2026-01-05", "2026-01-05", self.root, fetcher=fetcher)
        self.assertTrue(result["coverage"]["complete"])
        self.assertEqual(result["candidate_count"], 1)
        self.assertTrue(any("vReport_Show" in url for url in fetcher.calls))

    def test_priority_months_interleave_calendar_dates_before_october(self):
        priorities = tuple("2026-{:02}".format(month) for month in range(1, 10))
        expected = [month + "-01" for month in priorities] + ["2026-01-02", "2026-02-02"]
        fetcher = FakeFetcher(self.root, {(day, 1): "没有找到相关内容.." for day in expected})
        result = run_source("institution_14", "2026-01-01", "2026-10-02", self.root,
                            fetcher=fetcher, metadata_only=True, max_days=11, priority_months=priorities)
        observed = [parse_qs(urlsplit(url).query)["pubdate"][0] for url in fetcher.calls]
        self.assertEqual(observed, expected)
        self.assertEqual(result["priority_months"], list(priorities))
        self.assertFalse(result["coverage"]["discovery_complete"])

    def test_old_checkpoint_can_resume_with_month_priorities_without_restart(self):
        first = FakeFetcher(self.root, {("2026-01-01", 1): "没有找到相关内容..",
                                        ("2026-01-02", 1): OSError("temporary outage")})
        run_source("institution_14", "2026-01-01", "2026-03-02", self.root,
                   fetcher=first, metadata_only=True, max_days=2)
        expected = ["2026-02-01", "2026-03-01", "2026-02-02", "2026-03-02"]
        resumed = FakeFetcher(self.root, {(day, 1): "没有找到相关内容.." for day in expected})
        result = run_source("institution_14", "2026-01-01", "2026-03-02", self.root,
                            fetcher=resumed, metadata_only=True, max_days=4,
                            priority_months=("2026-02", "2026-03"))
        self.assertEqual([parse_qs(urlsplit(url).query)["pubdate"][0] for url in resumed.calls], expected)
        self.assertEqual(result["days"]["2026-01-01"]["status"], "complete")
        self.assertIn("temporary outage", result["days"]["2026-01-02"]["errors"][0]["error"])

    def test_priority_month_dates_retry_after_every_unseen_target_date(self):
        failed = FakeFetcher(self.root, {("2026-01-31", 1): OSError("temporary outage")})
        run_source("institution_14", "2026-01-31", "2026-02-01", self.root,
                   fetcher=failed, metadata_only=True, max_days=1)
        resumed = FakeFetcher(self.root, {("2026-02-01", 1): "没有找到相关内容..",
                                         ("2026-01-31", 1): "没有找到相关内容.."})
        result = run_source("institution_14", "2026-01-31", "2026-02-01", self.root,
                            fetcher=resumed, metadata_only=True,
                            priority_months=("2026-01", "2026-02"))
        self.assertEqual([parse_qs(urlsplit(url).query)["pubdate"][0] for url in resumed.calls],
                         ["2026-02-01", "2026-01-31"])
        self.assertEqual(result["days"]["2026-01-31"]["attempt"], 2)
        self.assertTrue(result["coverage"]["discovery_complete"])

    def test_invalid_priority_months_are_rejected_before_fetching(self):
        for priorities in ["2026-01", ["2026-1"], ["2026-13"]]:
            with self.subTest(priorities=priorities), self.assertRaises(ValueError):
                run_source("institution_14", "2026-01-01", "2026-02-01", self.root,
                           priority_months=priorities, fetcher=FakeFetcher(self.root, {}))


if __name__ == "__main__":
    unittest.main()
