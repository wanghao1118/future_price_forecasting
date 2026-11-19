"""Original-channel history remains distinct from the Sina supplement."""
import io
import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from newsdesk.history_broker_channels import (
    ChannelFetchError, Fetcher, fetch_selected_original, parse_eastmoney_page,
    parse_founder_page, run_original_channel,
)


def em_row(identifier="1", kind="宏观研究", day="2026-01-05"):
    return {"id": identifier, "title": "公开研究摘要", "orgCode": "80000031", "orgSName": "东吴证券",
            "orgName": "东吴证券股份有限公司", "publishDate": day + " 00:00:00.000", "columnType": kind,
            "encodeUrl": "encoded" + identifier, "infoCode": "AP" + identifier}


def em_page(page, rows, total=1, hits=None):
    return json.dumps({"pageNo": page, "TotalPage": total, "hits": len(rows) if hits is None else hits, "data": rows})


def founder_page(kind, page=1, total=2):
    return ('<a href="/infoResearch{}/{}{}.jhtml" title="2026公开研究">文章</a>'
            '<div>当前第{}页&nbsp; 共{}页</div>').format(kind, page, "".join(str(ord(char)) for char in kind), page, total)


def next_html(properties):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": properties}}) + '</script>'


def glh_profile(rows=None):
    return {"userId": 624647, "userBrief": "兴业证券研究所策略团队", "myHomePageItemVOS": rows or []}


def glh_row(identifier, day):
    stamp = int(datetime.fromisoformat(day + "T06:00:00+00:00").timestamp())
    return {"type": 3, "userId": 624647, "id": identifier, "title": "兴证策略公开研究", "createTimestamp": stamp}


class FakeFetcher:
    def __init__(self, root, responder):
        self.root, self.responder, self.calls = root, responder, []
        self.prefix = uuid.uuid4().hex[:8]

    def get(self, url, post=None, encoding="utf-8"):
        self.calls.append((url, post))
        value = self.responder(url, post)
        if isinstance(value, Exception):
            raise value
        path = self.root / ("raw-fixture-" + self.prefix + "-" + str(len(self.calls)) + ".json")
        path.write_text(json.dumps({"request_url": url, "raw_response": value}, ensure_ascii=False))
        return value, path, "2026-10-30T06:00:00+00:00"


class OriginalChannelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_all_five_eastmoney_report_types_use_publisher_declared_public_routes(self):
        kinds = ["个股研报", "行业研报", "策略报告", "宏观研究", "券商晨会"]
        rows, pages, hits = parse_eastmoney_page(em_page(1, [em_row(str(i), kind) for i, kind in enumerate(kinds)]),
                                               "institution_11", "2026-01-01", "2026-10-30", 1, "raw.json")
        self.assertEqual((pages, hits), (1, 5))
        expected = ["/report/info/", "zw_industry.jshtml", "zw_strategy.jshtml", "zw_macresearch.jshtml", "zw_brokerreport.jshtml"]
        for row, route in zip(rows, expected):
            self.assertIn(route, row["source_url"])
            self.assertEqual(row["publisher_name"], "东吴证券股份有限公司")
            self.assertNotIn("body", row)

    def test_eastmoney_rejects_ignored_publisher_date_or_missing_real_page(self):
        wrong_org = em_row()
        wrong_org["orgName"] = "其他证券股份有限公司"
        for text in [em_page(1, [wrong_org]), em_page(1, [em_row(day="2025-12-31")]), em_page(1, [], 2, hits=50)]:
            with self.subTest(text=text[:80]), self.assertRaises(ValueError):
                parse_eastmoney_page(text, "institution_11", "2026-01-01", "2026-10-30", 1, "raw.json")

    def test_eastmoney_budget_resumes_next_page_and_metadata_is_not_full_collection(self):
        def respond(url, post):
            query = parse_qs(urlsplit(url).query)
            page = int(query["pageNo"][0])
            self.assertEqual(query["beginTime"], ["2026-01-01"])
            return em_page(page, [em_row(str(page))], 2, hits=2)
        first_fetcher = FakeFetcher(self.root, respond)
        result = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                      max_pages=1, fetcher=first_fetcher)
        self.assertTrue(result["original_channel_attempted"])
        self.assertFalse(result["coverage"]["terminal"])
        self.assertEqual(result["discovery_count"], 1)
        second_fetcher = FakeFetcher(self.root, respond)
        again = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                     max_pages=1, fetcher=second_fetcher)
        self.assertIn("pageNo=2", second_fetcher.calls[0][0])
        self.assertEqual(again["discovery_count"], 2)
        self.assertTrue(again["coverage"]["terminal"])
        self.assertTrue(again["coverage"]["discovery_complete"])
        self.assertFalse(again["original_channel_complete"])

    def body_responder(self, url, post):
        if "reportapi.eastmoney.com" in url:
            page = int(parse_qs(urlsplit(url).query)["pageNo"][0])
            rows = [em_row("1"), em_row("2")] if page == 1 else [em_row("3")]
            return em_page(page, rows, 2, hits=3)
        identifier = parse_qs(urlsplit(url).query)["encodeUrl"][0][-1]
        content = "" if identifier == "1" else "公开原油研究内容。" * 30
        return '<h1 id="zw-title">公开报告</h1><div class="c-infos">东吴证券</div><div id="ctx-content">' + content + '</div>'

    def test_bad_article_is_quarantined_while_same_page_and_later_pages_continue(self):
        fetcher = FakeFetcher(self.root, self.body_responder)
        result = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                      metadata_only=False, fetcher=fetcher)
        self.assertEqual(result["candidate_count"], 2)
        self.assertEqual(result["discovery_count"], 3)
        self.assertTrue(any("pageNo=2" in url for url, _ in fetcher.calls))
        self.assertTrue(result["coverage"]["discovery_complete"])
        self.assertTrue(result["coverage"]["terminal"])
        self.assertFalse(result["original_channel_complete"])
        self.assertEqual(result["coverage"]["article_failures"], 1)
        article = result["article_errors"][0]
        self.assertEqual(article["candidate_id"], "institution_11:eastmoney:1")
        self.assertEqual(article["phase"], "article")
        self.assertIn("encodeUrl=encoded1", article["url"])
        self.assertEqual(article["http_status"], 200)
        self.assertTrue(Path(article["raw_path"]).exists())
        self.assertIn('id="ctx-content"></div>', json.loads(Path(article["raw_path"]).read_text())["raw_response"])
        self.assertEqual(result["channels"][0]["errors"], [])
        saved = [json.loads(line) for line in Path(result["article_errors_path"]).read_text().splitlines()]
        self.assertEqual(saved, result["article_errors"])
        candidates = [json.loads(line) for line in Path(result["candidates_path"]).read_text().splitlines()]
        self.assertEqual({row["candidate_id"] for row in candidates}, {"institution_11:eastmoney:2", "institution_11:eastmoney:3"})

    def test_legacy_article_terminal_resumes_index_and_skips_identified_failed_body(self):
        fetcher = FakeFetcher(self.root, self.body_responder)
        first = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                     metadata_only=False, max_pages=1, fetcher=fetcher)
        article = first["article_errors"][0]
        old = json.loads(Path(first["state_path"]).read_text())
        channel = old["channels"]["eastmoney_all_reports"]
        channel.update(terminal=True, complete=False, pages=0, next_page=1, stop_reason="public_request_failed", article_errors=[],
                       errors=[{"url": fetcher.calls[0][0], "error": "ValueError: 没有可读取的公开研究正文或摘要", "raw_path": article["raw_path"]}])
        Path(first["state_path"]).write_text(json.dumps(old, ensure_ascii=False))
        resumed_fetcher = FakeFetcher(self.root, self.body_responder)
        result = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                      metadata_only=False, fetcher=resumed_fetcher)
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(result["coverage"]["discovery_complete"])
        self.assertFalse(result["original_channel_complete"])
        self.assertFalse(any("encodeUrl=encoded1" in url for url, _ in resumed_fetcher.calls))
        self.assertTrue(any("pageNo=2" in url for url, _ in resumed_fetcher.calls))
        channel = result["channels"][0]
        self.assertTrue(channel["migration_history"])
        self.assertEqual(channel["errors"], [])
        self.assertEqual(len(channel["article_errors"]), 1)
        self.assertTrue(channel["article_errors"][0]["migrated_from_legacy_terminal"])
        self.assertEqual(channel["article_errors"][0]["candidate_id"], article["candidate_id"])

    def test_legacy_parse_failure_without_detail_id_is_not_guessed_or_permanently_terminal(self):
        first = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                     metadata_only=False, max_pages=1, fetcher=FakeFetcher(self.root, self.body_responder))
        old = json.loads(Path(first["state_path"]).read_text())
        channel = old["channels"]["eastmoney_all_reports"]
        channel.update(terminal=True, complete=False, pages=0, next_page=1, stop_reason="public_request_failed", article_errors=[],
                       errors=[{"url": "https://reportapi.eastmoney.com/report/dg?pageNo=1",
                                "error": "ValueError: 没有可读取的公开研究正文或摘要"}])
        Path(first["state_path"]).write_text(json.dumps(old, ensure_ascii=False))
        fetcher = FakeFetcher(self.root, self.body_responder)
        result = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root,
                                      metadata_only=False, fetcher=fetcher)
        self.assertTrue(result["coverage"]["discovery_complete"])
        self.assertEqual(result["candidate_count"], 2)
        channel = result["channels"][0]
        self.assertEqual(len(channel["legacy_article_errors"]), 1)
        self.assertNotIn("candidate_id", channel["legacy_article_errors"][0])
        self.assertEqual(len(channel["article_errors"]), 1)
        self.assertEqual(channel["article_errors"][0]["candidate_id"], "institution_11:eastmoney:1")
        self.assertEqual(sum("encodeUrl=encoded1" in url for url, _ in fetcher.calls), 1)

    def test_http_567_is_attempted_blocked_evidence_not_complete_zero(self):
        evidence = self.root / "blocked-response.json"
        evidence.write_text('{"http_status":567}')
        fetcher = FakeFetcher(self.root, lambda url, post: ChannelFetchError("HTTP 567", url=url, status=567, raw_path=str(evidence)))
        result = run_original_channel("institution_11", "2026-01-01", "2026-10-30", self.root, fetcher=fetcher)
        self.assertTrue(result["original_channel_attempted"])
        self.assertTrue(result["coverage"]["terminal"])
        self.assertFalse(result["original_channel_complete"])
        self.assertEqual(result["errors"][0]["http_status"], 567)
        self.assertEqual(result["errors"][0]["raw_path"], str(evidence))

    def test_citic_keeps_older_public_2026_core_views_but_not_login_gated_history(self):
        payload = {"code": 0, "data": {"101": {"loginList": 1, "loginDetail": 1,
            "rptList": [{"id": 123, "psTitle": "2026中期公开观点", "psCoreView": "原油需求与宏观政策变化。" * 30, "rptDate": "2026-06-15"},
                        {"id": 124, "psTitle": "2025旧文", "psCoreView": "历史材料。" * 30, "rptDate": "2025-12-15"}]}}}
        fetcher = FakeFetcher(self.root, lambda url, post: json.dumps(payload))
        result = run_original_channel("institution_23", "2026-01-01", "2026-10-30", self.root, fetcher=fetcher)
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertEqual(fetcher.calls[0][1], {})
        self.assertFalse(result["original_channel_complete"])
        self.assertTrue(result["coverage"]["terminal"])
        self.assertIn("loginList=1", result["errors"][0]["error"])
        row = json.loads(Path(result["candidates_path"]).read_text().splitlines()[0])
        self.assertEqual(row["published_date"], "2026-06-15")
        self.assertTrue(row["detail_requires_login"])

    def test_founder_all_seven_types_rotate_and_checkpoint_independent_pages(self):
        def respond(url, post):
            match = __import__('re').search(r"infoResearch([^/]+)/index(?:_(\d+))?\.jhtml", url)
            return founder_page(match.group(1), int(match.group(2) or 1), 2)
        fetcher = FakeFetcher(self.root, respond)
        result = run_original_channel("institution_49", "2026-01-01", "2026-10-30", self.root, max_pages=3, fetcher=fetcher)
        self.assertEqual(sum(channel["attempted"] for channel in result["channels"]), 3)
        next_fetcher = FakeFetcher(self.root, respond)
        result = run_original_channel("institution_49", "2026-01-01", "2026-10-30", self.root, max_pages=4, fetcher=next_fetcher)
        self.assertEqual(sum(channel["attempted"] for channel in result["channels"]), 7)
        self.assertTrue(all(channel["next_page"] == 2 for channel in result["channels"]))
        self.assertFalse(result["coverage"]["terminal"])
        self.assertFalse(result["original_channel_complete"])
        self.assertEqual(len({row["channel"] for row in (json.loads(line) for line in Path(result["discoveries_path"]).read_text().splitlines())}), 7)

    def test_xingye_cursor_reaches_january_and_ths_stock_window_stays_limited(self):
        first = [glh_row(1, "2026-06-01"), glh_row(2, "2026-01-05")]
        second = [glh_row(3, "2026-01-02"), glh_row(4, "2025-12-31")]
        def respond(url, post):
            if "/column/" in url:
                return next_html({"articles": glh_profile()})
            if "/api/community/" in url:
                query = parse_qs(urlsplit(url).query)
                rows = second if query.get("timestamp") else first
                return json.dumps({"statusCode": 200, "result": glh_profile(rows)})
            if "news.html" in url:
                return '<div id="report_list_contents">[]</div>'
            return '<html>宁德时代六个月盈利预测窗口</html>'
        fetcher = FakeFetcher(self.root, respond)
        first_run = run_original_channel("institution_18", "2026-01-01", "2026-10-30", self.root, max_pages=1, fetcher=fetcher)
        self.assertEqual(first_run["discovery_count"], 2)
        again = run_original_channel("institution_18", "2026-01-01", "2026-10-30", self.root, max_pages=3, fetcher=fetcher)
        self.assertEqual(again["discovery_count"], 3)
        glh = next(channel for channel in again["channels"] if channel["channel"] == "xingye_gelonghui")
        self.assertTrue(glh["complete"])
        self.assertTrue(again["coverage"]["terminal"])
        self.assertFalse(again["original_channel_complete"])
        self.assertTrue(any("宁德时代" in error["error"] for error in again["errors"]))

    def test_xingye_paywalled_article_does_not_block_other_bodies_or_history_cursor(self):
        first = [glh_row(1, "2026-06-01"), glh_row(2, "2026-01-05")]
        second = [glh_row(3, "2026-01-02"), glh_row(4, "2025-12-31")]
        by_id = {row["id"]: row for row in first + second}
        def respond(url, post):
            if "/column/" in url:
                return next_html({"articles": glh_profile()})
            if "/api/community/" in url:
                rows = second if parse_qs(urlsplit(url).query).get("timestamp") else first
                return json.dumps({"statusCode": 200, "result": glh_profile(rows)})
            if "/p/" in url:
                identifier = int(urlsplit(url).path.rsplit("/", 1)[1])
                article = {"id": identifier, "title": "真实公开研究", "content": "原油供需变化。" * 30,
                           "createTimestamp": by_id[identifier]["createTimestamp"], "purchaseInfo": {"paid": True} if identifier == 1 else None,
                           "column": {"id": 624647, "columnAuthorStatus": "authenticated", "brief": "兴业证券研究所策略团队"}}
                return next_html({"articleInfo": article})
            if "news.html" in url:
                return '<div id="report_list_contents">[]</div>'
            return "<html>宁德时代个股当前窗口</html>"
        fetcher = FakeFetcher(self.root, respond)
        result = run_original_channel("institution_18", "2026-01-01", "2026-10-30", self.root,
                                      metadata_only=False, fetcher=fetcher)
        self.assertEqual(result["candidate_count"], 2)
        glh = next(channel for channel in result["channels"] if channel["channel"] == "xingye_gelonghui")
        self.assertTrue(glh["complete"])
        self.assertEqual(glh["pages"], 2)
        self.assertEqual(glh["errors"], [])
        self.assertEqual(len(glh["article_errors"]), 1)
        self.assertIn("付费", glh["article_errors"][0]["error"])
        self.assertFalse(result["original_channel_complete"])

    def test_bad_embedded_single_record_does_not_discard_other_public_citic_views(self):
        payload = {"code": 0, "data": {"101": {"loginList": 1, "rptList": [
            {"id": 1, "psTitle": "单篇日期有误", "psCoreView": "公开内容。" * 30, "rptDate": "2026-02-31"},
            {"id": 2, "psTitle": "正常公开观点", "psCoreView": "公开原油宏观研究。" * 30, "rptDate": "2026-01-05"}]}}}
        result = run_original_channel("institution_23", "2026-01-01", "2026-10-30", self.root,
                                      fetcher=FakeFetcher(self.root, lambda u,p: json.dumps(payload)))
        self.assertEqual(result["candidate_count"], 1)
        self.assertEqual(result["article_errors"][0]["candidate_id"], "institution_23:citic:1")
        self.assertTrue(Path(result["article_errors"][0]["raw_path"]).exists())
        self.assertEqual(result["channels"][0]["stop_reason"], "history_requires_login")

    def test_xingye_rejects_unverified_or_different_column_owner(self):
        profile = glh_profile()
        profile["userId"] = 999
        fetcher = FakeFetcher(self.root, lambda u,p: next_html({"articles": profile}))
        result = run_original_channel("institution_18", "2026-01-01", "2026-10-30", self.root,
                                      max_pages=1, fetcher=fetcher)
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["discovery_count"], 0)
        self.assertEqual(len(fetcher.calls), 1)
        self.assertTrue(any("核验" in error["error"] for error in result["errors"]))

    def test_selected_eastmoney_public_body_author_is_checked_again(self):
        rows, _, _ = parse_eastmoney_page(em_page(1, [em_row()]), "institution_11", "2026-01-01", "2026-10-30", 1, "listing.json")
        path = self.root / "discoveries.jsonl"
        path.write_text(json.dumps(rows[0], ensure_ascii=False) + "\n")
        html = '<h1 id="zw-title">公开报告</h1><div class="c-infos">其他证券</div><div id="ctx-content">' + "公开研究内容。" * 40 + "</div>"
        result = fetch_selected_original("institution_11", path, [rows[0]["candidate_id"]], self.root,
            start_date="2026-01-01", end_date="2026-10-30", fetcher=FakeFetcher(self.root, lambda u,p: html))
        self.assertEqual(result["candidate_count"], 0)
        self.assertIn("作者机构", result["errors"][0]["error"])

    def test_selected_original_checks_body_author_and_excludes_real_out_of_range_date(self):
        discovery = {"candidate_id": "institution_49:founder:1", "source_id": "institution_49", "source_name": "方正证券",
                     "external_id": "founder:1", "channel": "founder_Macro", "source_url": "https://gwlink.foundersc.com/infoResearchMacro/1.jhtml",
                     "title": "无列表日期的公开文章", "published_at": "", "published_date": "", "publisher_name": "方正证券股份有限公司"}
        path = self.root / "discoveries.jsonl"
        path.write_text(json.dumps(discovery, ensure_ascii=False) + "\n")
        html = '<div>方正证券研究所 时间：2025-12-01</div><div class="k-infor_d-2">旧文</div><div class="k-infor_d-3">' + '公开正文。' * 40 + '</div>'
        result = fetch_selected_original("institution_49", path, [discovery["candidate_id"]], self.root,
            start_date="2026-01-01", end_date="2026-10-30", fetcher=FakeFetcher(self.root, lambda u,p: html))
        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["outside_range"], [discovery["candidate_id"]])
        self.assertEqual(result["errors"], [])

    def test_real_fetcher_preserves_blocked_http_raw_response(self):
        url = "https://reportapi.eastmoney.com/report/dg?orgCode=80000031"
        error = HTTPError(url, 567, "blocked", {}, io.BytesIO(b"ordinary request blocked"))
        fetcher = Fetcher(self.root, delay=0)
        with patch("newsdesk.history_broker_channels.urlopen", side_effect=error):
            with self.assertRaises(ChannelFetchError) as caught:
                fetcher.get(url)
        record = json.loads(Path(caught.exception.raw_path).read_text())
        self.assertEqual(record["http_status"], 567)
        self.assertEqual(record["raw_response"], "ordinary request blocked")
        self.assertEqual(record["request_url"], url)


if __name__ == "__main__":
    unittest.main()
