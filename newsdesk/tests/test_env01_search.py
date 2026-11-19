import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from newsdesk.env01_search import (LocalNewsSearch, MAX_RESPONSE_BYTES, canonical,
                                  digest, load_context, serve)
from time_gate_agent.local import LocalTimeGate


ROOT = Path(__file__).resolve().parents[2]


def question(family="WTI", **changes):
    value = {"case_id": "wti-fixture", "family": family, "input_date": "2026-10-20",
             "target_date": "2026-10-21", "current_price": 70, "question": "预测次日价格。",
             "exchange_timezone": "Asia/Shanghai" if family == "SC" else "America/New_York"}
    value.update(changes)
    return value


def article(identifier="safe", **changes):
    value = {"id": identifier, "title": "原油库存变化 OPEC", "body": "原油供应减少，库存下降。",
             "source_name": "新闻档案", "source_url": "https://example.invalid/news/" + identifier,
             "assets": ["WTI", "Brent", "SC"], "is_demo": False,
             "ingestion": {"published_date": "2026-10-20"},
             "published_at": "2026-10-20T12:00:00+08:00", "collected_at": "2026-10-28T00:00:00Z",
             "agent_review": {"relevance": True}}
    value.update(changes)
    return value


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


class LocalNewsSearchTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.audit_dir = Path(self.directory.name).resolve() / "private-search"

    def searcher(self, articles=None, case=None, **kwargs):
        return LocalNewsSearch(case or question(), articles if articles is not None else [article()],
                               self.audit_dir, **kwargs)

    def verify_receipt(self, search):
        summary = read_json(self.audit_dir / "summary.json")
        self.assertEqual(summary, search.summary())
        entry = summary["receipts"][-1]
        receipt = read_json(entry["receipt_path"])
        self.assertEqual(digest(receipt), entry["receipt_sha256"])
        for artifact in receipt["files"].values():
            path = Path(artifact["path"])
            self.assertTrue(path.is_relative_to(self.audit_dir))
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(digest(read_json(path)), artifact["sha256"])
        self.assertEqual(stat.S_IMODE(Path(entry["receipt_path"]).parent.stat().st_mode), 0o700)
        self.assertEqual(receipt["response_sha256"], receipt["files"]["response"]["sha256"])
        return receipt

    def test_utc_window_reaches_search_gate_and_receipts_without_future_news(self):
        from newsdesk.benchmark import display_time_windows
        from time_gate_agent.local import UTC_PROTOCOL
        case = question("SC")
        windows = display_time_windows(case)
        windows.pop("display_only")
        case["time_windows"] = windows
        search = self.searcher(case=case, articles=[article(), article("late", published_at="2026-10-20T07:01:00Z")])
        result = search.search("原油")
        self.assertEqual([row["id"] for row in result["articles"]], ["safe"])
        receipt = self.verify_receipt(search)
        gate = read_json(receipt["files"]["time_gate"]["path"])
        self.assertEqual(gate["protocol"], UTC_PROTOCOL)
        self.assertEqual(gate["cutoff_utc"], "2026-10-20T07:00:00Z")
        self.assertTrue(gate["time_windows_validated"])
        with self.assertRaisesRegex(ValueError, "invalid_public_time_windows"):
            LocalNewsSearch(dict(case, time_windows={}), [], Path(self.directory.name) / "invalid")

    def test_chinese_english_multi_term_or_ranking_and_stable_snapshot(self):
        rows = [article("body", title="能源供给", body="原油需求下降。"),
                article("title", title="原油库存", body="库存下降。"),
                article("english", title="OPEC meeting", body="Producer statement"),
                article("source", title="政策", body="会议情况", source_name="Reuters"),
                article("irrelevant", title="铜供应", body="铜矿变化。")]
        search = self.searcher(rows)
        rows[1]["body"] = "MUTATED"
        response = search.search("原油, oPeC，Reuters", limit=8)
        self.assertEqual(response["status"], "ok")
        self.assertEqual(response["articles"][0]["id"], "title")
        self.assertEqual({item["id"] for item in response["articles"]}, {"title", "body", "english", "source"})
        self.assertNotIn("MUTATED", canonical(response))
        self.assertEqual(search.summary()["call_count"], 1)
        receipt = self.verify_receipt(search)
        self.assertEqual(read_json(receipt["files"]["response"]["path"]), response)

    def test_whole_cjk_terms_and_weak_bigram_fallback(self):
        rows = [article("whole", title="原油库存减少", body="消息"),
                article("gram", title="库存", body="消息"),
                article("none", title="铜", body="消息")]
        response = self.searcher(rows).search("原油库存")
        self.assertEqual([row["id"] for row in response["articles"]], ["whole", "gram"])

    def test_every_candidate_is_gated_every_time_even_when_future_matches_exceed_cap(self):
        rows = [article("future-{}".format(index), title="原油库存 原油供应 OPEC",
                        published_at="2026-10-21T12:00:00Z", ingestion={"published_date": "2026-10-21"})
                for index in range(45)] + [article()]
        original = LocalTimeGate.select
        observed = []
        def gated(gate, case):
            observed.append([row["id"] for row in gate.articles])
            return original(gate, case)
        search = self.searcher(rows)
        with mock.patch.object(LocalTimeGate, "select", gated):
            first = search.search("原油")
            second = search.search("原油")
        self.assertEqual([item["id"] for item in first["articles"]], ["safe"])
        self.assertEqual(first, second)
        self.assertEqual(len(observed), len(rows) * 2)
        self.assertEqual(search.summary()["unique_returned_article_count"], 1)
        self.assertEqual(search.summary()["returned_article_count"], 2)

    def test_future_unknown_excluded_revision_and_other_family_remain_private(self):
        rows = [article(), article("SECRET-FUTURE", title="SECRET FUTURE 原油",
                                  published_at="2026-10-21T12:00:00Z", ingestion={"published_date": "2026-10-21"}),
                article("SECRET-UNKNOWN", title="SECRET UNKNOWN 原油", published_at="", ingestion={"source_id": "fixture"}),
                article("SECRET-EXCLUDED", title="SECRET EXCLUDED 原油", agent_review={"relevance": False}),
                article("SECRET-MALFORMED", title="SECRET MALFORMED 原油", ingestion="not an object"),
                article("SECRET-REVISION", title="SECRET REVISION 原油", source_modified_at="2026-10-22T00:00:00Z"),
                article("SECRET-OTHER", title="SECRET OTHER 原油", assets=["SC"])]
        search = self.searcher(rows)
        result = search.search("原油")
        visible = canonical(result)
        self.assertNotIn("SECRET", visible)
        self.assertEqual(set(result), {"status", "query", "articles", "returned_count"})
        receipt = self.verify_receipt(search)
        self.assertIn("SECRET", canonical(read_json(receipt["files"]["raw_recall"]["path"])))
        gate = read_json(receipt["files"]["time_gate"]["path"])
        reasons = [check["audit"]["decisions"][0]["reason"] for check in gate["checks"]]
        self.assertIn("unknown_original_publication_date", reasons)
        self.assertIn("excluded_by_archive_reviewer", reasons)
        self.assertIn("source_revision_after_input_day", reasons)

    def test_strict_publication_metadata_and_contract_timezone(self):
        # Source calendar 20th but SC local21st must be denied; WTI local20th
        # may accept a source calendar21st publication.
        sc_rows = [article("late", published_at="2026-10-20T23:59:00-04:00"),
                   article("naive", published_at="2026-10-20T12:00:00"),
                   article("conflict", ingestion={"published_date": "2026-10-19"})]
        search = self.searcher(sc_rows, question("SC"))
        self.assertEqual(search.search("原油")["status"], "no_results")
        separate = Path(self.directory.name) / "wti"
        early = article("early", published_at="2026-10-21T00:30:00+08:00", ingestion={"published_date": "2026-10-21"})
        wti = LocalNewsSearch(question(), [early], separate)
        result = wti.search("原油")
        self.assertEqual(result["articles"][0]["eligibility_date"], "2026-10-20")

    def test_empty_recall_is_valid_search_gated_and_audited(self):
        search = self.searcher()
        original = LocalTimeGate.select
        observed = []
        def gated(gate, case):
            observed.append(gate.articles)
            return original(gate, case)
        with mock.patch.object(LocalTimeGate, "select", gated):
            result = search.search("火星陨石")
        self.assertEqual(result, {"status": "no_results", "query": "火星陨石", "articles": [], "returned_count": 0})
        self.assertEqual(observed, [[]])
        self.assertEqual(search.summary()["successful_call_count"], 1)
        self.verify_receipt(search)

    def test_invalid_limits_empty_query_and_quota_never_count_as_search(self):
        search = self.searcher(max_calls=1)
        for keywords, limit in (("", 5), (" ", 5), ("，,", 5), ("原油", 0), ("原油", 9),
                                ("原油", True), ("原油", 1.5), ([], 1)):
            self.assertEqual(search.search(keywords, limit)["status"], "unavailable")
        self.assertEqual(search.summary()["call_count"], 0)
        self.assertEqual(search.search("原油")["status"], "ok")
        self.assertEqual(search.search("原油")["status"], "unavailable")
        self.assertEqual(search.summary()["call_count"], 1)
        self.assertEqual(search.summary()["error_call_count"], 9)
        self.verify_receipt(search)
        # A restarted server retains the host audit and the same quota.
        restarted = self.searcher(max_calls=1)
        self.assertEqual(restarted.search("原油")["status"], "unavailable")
        self.assertEqual(restarted.summary()["call_count"], 1)

    def test_response_has_exact_original_prefix_and_byte_budget(self):
        rows = [article(str(index), title="原油" + "长" * 2000, body="原油库存消息" * 2000) for index in range(8)]
        search = self.searcher(rows)
        result = search.search("原油", limit=8)
        self.assertLessEqual(len(canonical(result).encode("utf-8")), MAX_RESPONSE_BYTES)
        self.assertLessEqual(sum(len(item["body"]) for item in result["articles"]), 10000)
        self.assertGreater(result["returned_count"], 0)
        for item in result["articles"]:
            original = next(row["body"] for row in rows if row["id"] == item["id"])
            self.assertLessEqual(len(item["body"]), 4000)
            self.assertEqual(item["body"], original[:len(item["body"])])
            self.assertEqual(item["stored_body_characters"], len(original))
            self.assertEqual(item["provided_content_scope"], "excerpt")
        self.verify_receipt(search)

    def test_gate_failure_returns_opaque_error_and_private_audit(self):
        search = self.searcher()
        with mock.patch.object(LocalTimeGate, "select", side_effect=RuntimeError("SECRET path and title")):
            result = search.search("原油")
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("SECRET", canonical(result))
        self.assertEqual(search.summary()["call_count"], 0)
        self.assertEqual(search.summary()["error_call_count"], 1)
        self.verify_receipt(search)

    def test_context_is_bound_to_only_public_question_and_exact_snapshot(self):
        context_path = Path(self.directory.name) / "context.json"
        snapshot = Path(self.directory.name) / "news.json"
        rows = [article()]
        snapshot.write_text(canonical(rows), encoding="utf-8")
        context = {"case": question(), "news_snapshot_path": str(snapshot), "news_snapshot_sha256": digest(rows)}
        context_path.write_text(canonical(context), encoding="utf-8")
        self.assertEqual(load_context(context_path), (question(), rows))
        for changed in (dict(context, labels={"actual": 1}), dict(context, news_snapshot_sha256="0" * 64),
                        dict(context, case=dict(question(), actual={"high": 100})),
                        dict(context, news_snapshot_path="relative.json"),
                        dict(context, case=dict(question(), news_cutoff_exclusive="2026-10-22"))):
            context_path.write_text(canonical(changed), encoding="utf-8")
            with self.subTest(context=changed), self.assertRaises((ValueError, OSError)):
                load_context(context_path)
        self.searcher()
        with self.assertRaises(ValueError):
            self.searcher(case=question(case_id="other-case"))

    def test_mcp_refuses_caller_context_path_cutoff_and_invalid_limit(self):
        search = self.searcher()
        messages = [{"jsonrpc": "2.0", "id": index, "method": "tools/call", "params":
                     {"name": "search_news", "arguments": arguments}} for index, arguments in enumerate((
                         {"keywords": "原油", "case": question()}, {"keywords": "原油", "path": "/private"},
                         {"keywords": "原油", "cutoff": "2030-01-01"}, {"keywords": "原油", "limit": 9}))]
        output = io.StringIO()
        serve(search, io.StringIO("\n".join(canonical(message) for message in messages)), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertTrue(all(reply["result"]["isError"] for reply in replies))
        self.assertTrue(all("structuredContent" not in reply["result"] for reply in replies))
        self.assertEqual(search.summary()["call_count"], 0)
        self.assertEqual(search.summary()["error_call_count"], 4)

    def test_real_stdio_subprocess_handshake_search_and_exact_receipt(self):
        snapshot = Path(self.directory.name) / "news.json"
        snapshot.write_text(canonical([article()]), encoding="utf-8")
        context = Path(self.directory.name) / "context.json"
        context.write_text(canonical({"case": question(), "news_snapshot_path": str(snapshot),
                                      "news_snapshot_sha256": digest([article()])}), encoding="utf-8")
        messages = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "search_news", "arguments": {"keywords": "OPEC"}}},
        ]
        completed = subprocess.run([sys.executable, "-m", "newsdesk.env01_search", "--context", str(context),
                                    "--audit-dir", str(self.audit_dir)],
                                   cwd=ROOT, input="\n".join(canonical(message) for message in messages) + "\n",
                                   text=True, capture_output=True, timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        replies = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([reply["id"] for reply in replies], [1, 2, 3, 4])
        self.assertEqual(replies[0]["result"]["serverInfo"]["name"], "env01 news search")
        tool = replies[2]["result"]["tools"][0]
        self.assertFalse(tool["inputSchema"]["additionalProperties"])
        self.assertTrue(tool["annotations"]["readOnlyHint"])
        self.assertFalse(tool["annotations"]["openWorldHint"])
        content = replies[-1]["result"]["content"]
        self.assertEqual(len(content), 1)
        response = json.loads(content[0]["text"])
        summary = read_json(self.audit_dir / "summary.json")
        receipt = read_json(summary["receipts"][0]["receipt_path"])
        self.assertEqual(digest(response), receipt["response_sha256"])
        self.assertEqual(content[0]["text"], Path(receipt["files"]["response"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(summary["call_count"], 1)


if __name__ == "__main__":
    unittest.main()
