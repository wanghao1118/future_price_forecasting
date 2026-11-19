"""No-network regressions for publication boundaries and article quarantine."""

import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from time_gate_agent import boundary, guard
from time_gate_agent.agent import TimeGateAgent


def public_task():
    return {
        "case_id": "migration-fixture",
        "question": "Will the released indicator exceed the threshold?",
        "options": [{"label": "A", "text": "Above"}, {"label": "B", "text": "Not above"}],
        "answer_type": "choice",
        "cutoff": "2026-05-01T00:00:00+00:00",
        "target_date": "2026-05-13T16:00:00+00:00",
    }


def article_page(url="https://example.org/news/background", day="2026-05-10", **changes):
    values = {
        "url": url,
        "title": "Economic expectations",
        "text": "Published {}. Analysts expect an increase; no target result has been released.".format(day),
        "metadata_dates": [{"kind": "published", "value": day}],
        "raw_sha256": "a" * 64,
    }
    values.update(changes)
    return guard.FetchedPage(**values)


def article_review(day="2026-05-10", **changes):
    value = {
        "verdict": "allow",
        "direct_answer": False,
        "retrospective": False,
        "original_date_verified": True,
        "historical_version_verified": True,
        "full_article_reviewed": True,
        "article_body_complete": True,
        "answer_publication_boundary_verified": True,
        "source_before_first_answer_publication": True,
        "publication_date": day,
        "date_evidence_quote": day,
        "approved_excerpt": "Analysts expect an increase; no target result has been released.",
        "reason": "Pre-event expectations with a verified original publication date.",
    }
    value.update(changes)
    return value


class BoundaryFixture:
    """Each callback represents an independent review, with fixture-only traces."""

    model = "fixture-no-model-call"

    def __init__(self, days=None, unresolved=None):
        self.days = days or {arm: "2026-05-12" for arm in boundary.ARMS}
        self.unresolved = unresolved
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, material, schema, work):
        arm = material["review_arm"]
        with self.lock:
            self.calls.append(copy.deepcopy(material))
        self.assert_schema(schema)
        work = Path(work)
        work.mkdir(parents=True, exist_ok=True)
        trace = work / "tool-trace.json"
        trace.write_text(json.dumps({"fixture": True, "network_used": False}), encoding="utf-8")
        trace.chmod(0o600)
        task = material["public_task"]
        day = self.days[arm]
        if arm == self.unresolved:
            return {
                "status": "unresolved", "case_id": task["case_id"],
                "public_task_sha256": boundary.digest(task),
                "answer_first_publication_date": None,
                "actual_publication_verified": False,
                "earlier_result_search_completed": True,
                "review_reason": "unresolved_publication_day", "evidence": [],
            }
        return {
            "status": "verified", "case_id": task["case_id"],
            "public_task_sha256": boundary.digest(task),
            "answer_first_publication_date": day,
            "actual_publication_verified": True,
            "earlier_result_search_completed": True,
            "review_reason": "verified_actual_publication_day",
            "evidence": [{
                "source_kind": "official_original_release",
                "url": "https://example.org/releases/{}/{}".format(arm, day),
                "determines_target_result": True,
                "publication_basis": "original_visible_publication_date",
                "date_evidence_quote": "Published {}".format(day),
                "answer_publication_date": day,
            }],
        }

    @staticmethod
    def assert_schema(schema):
        if schema != boundary.REVIEW_SCHEMA:
            raise AssertionError("Boundary reviewer must receive the strict publication schema")


def release_fetcher(url):
    if "/releases/" not in url:
        return article_page(url=url)
    day = url.rsplit("/", 1)[-1]
    return article_page(url=url, day=day,
                        text="Published {}. PRIVATE_TARGET_RESULT is present only in quarantined release evidence.".format(day))


class TimeGateAgentTest(unittest.TestCase):
    def make_agent(self, folder, boundary_reviewer=None, discover=None, reviewer=None, fetcher=None, task=None):
        return TimeGateAgent(task or public_task(), folder,
                             boundary_reviewer or BoundaryFixture(),
                             discover or mock.Mock(return_value={"candidate_urls": []}),
                             reviewer or mock.Mock(), fetcher=fetcher or release_fetcher)

    def test_three_verified_reviews_choose_the_earliest_exclusive_day(self):
        days = {"strategy": "2026-05-12", "original_search": "2026-05-11", "llm_base": "2026-05-12"}
        callback = BoundaryFixture(days)
        task = public_task()
        original = copy.deepcopy(task)
        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, boundary_reviewer=callback, task=task)
            result = agent.verify_boundary()
            self.assertTrue(result["news_allowed"])
            self.assertEqual(result["excluded_from_date"], "2026-05-11")
            self.assertEqual(result["last_allowed_calendar_date"], "2026-05-10")
            self.assertEqual({call["review_arm"] for call in callback.calls}, set(boundary.ARMS))
            self.assertEqual(len(callback.calls), 3)
            self.assertEqual(agent.verify_boundary(), result)
            self.assertEqual(len(callback.calls), 3, "A bound instance must not repeat successful date reviews")
            self.assertEqual(task, original)
            self.assertNotIn("PRIVATE_TARGET_RESULT", json.dumps(agent.policy))
            self.assertNotIn("example.org", json.dumps(agent.policy))
            self.assertEqual(Path(folder).stat().st_mode & 0o777, 0o700)
            files = list(Path(folder).rglob("*.json"))
            self.assertTrue(files)
            self.assertTrue(all(path.stat().st_mode & 0o777 == 0o600 for path in files))

    def test_any_unresolved_review_blocks_all_news_without_settlement_fallback(self):
        for failed in boundary.ARMS:
            with self.subTest(arm=failed), tempfile.TemporaryDirectory() as folder:
                discovery = mock.Mock()
                review = mock.Mock()
                fetch = mock.Mock(side_effect=release_fetcher)
                agent = self.make_agent(folder, BoundaryFixture(unresolved=failed), discovery, review, fetch)
                result = agent.verify_boundary()
                self.assertFalse(result["news_allowed"])
                self.assertIsNone(result["last_allowed_calendar_date"])
                fetch.reset_mock()
                self.assertEqual(agent.search("public economic news"), guard.PUBLIC_UNAVAILABLE)
                self.assertEqual(agent.open("https://example.org/news/result"), guard.PUBLIC_UNAVAILABLE)
                discovery.assert_not_called()
                review.assert_not_called()
                fetch.assert_not_called()
                self.assertNotIn("settlement_day_fallback", json.dumps(result))
                self.assertFalse(result["counts_as_model_failure"])

    def test_source_date_mismatch_cannot_be_overridden_by_model_verified_claim(self):
        def conflicting_release(url):
            return article_page(url=url, day="2026-05-13",
                                text="Published 2026-05-12. Metadata exposes a conflicting original date.")

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, fetcher=conflicting_release)
            self.assertFalse(agent.verify_boundary()["news_allowed"])
            self.assertEqual(agent.search("news"), guard.PUBLIC_UNAVAILABLE)

    def test_fabricated_publication_quote_blocks_news(self):
        def missing_date(url):
            return article_page(url=url, text="The original release date is unknown.", metadata_dates=[])

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, fetcher=missing_date)
            self.assertFalse(agent.verify_boundary()["news_allowed"])

    def test_review_bound_to_a_different_task_cannot_open_news(self):
        fixture = BoundaryFixture()

        def wrong_task(material, schema, work):
            review = fixture(material, schema, work)
            review["public_task_sha256"] = "0" * 64
            return review

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, boundary_reviewer=wrong_task)
            self.assertFalse(agent.verify_boundary()["news_allowed"])

    def test_boundary_callback_failure_is_fail_closed(self):
        def failure(*args):
            raise RuntimeError("PRIVATE_BOUNDARY_ERROR")

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, boundary_reviewer=failure)
            result = agent.verify_boundary()
            self.assertFalse(result["news_allowed"])
            self.assertNotIn("PRIVATE_BOUNDARY_ERROR", json.dumps(agent.policy))
            self.assertEqual(agent.open("https://example.org/news/result"), guard.PUBLIC_UNAVAILABLE)

    def test_private_task_fields_are_rejected_before_callbacks(self):
        for field in ("labels", "answer", "prediction", "_news_allowed"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as folder:
                callback = mock.Mock()
                with self.assertRaises(ValueError):
                    self.make_agent(folder, boundary_reviewer=callback,
                                    task=dict(public_task(), **{field: "PRIVATE_VALUE"}))
                callback.assert_not_called()

    def test_pre_event_article_passes_and_search_cannot_relax_the_boundary(self):
        seen = []

        def discovery(material, schema, work):
            seen.append(copy.deepcopy(material))
            return {"candidate_urls": ["https://example.org/news/background"]}

        def review(material, schema, work):
            return {"reviews": [dict(article_review(), candidate_id=row["candidate_id"])
                                for row in material["articles"]]}

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, discover=discovery, reviewer=review)
            result = agent.search("economic news before:2030-01-01")
            self.assertEqual(result["status"], "ok")
            self.assertEqual(len(result["sources"]), 1)
            self.assertEqual(result["sources"][0]["published_date"], "2026-05-10")
            self.assertEqual(result["sources"][0]["content"], article_review()["approved_excerpt"])
            self.assertTrue(seen[0]["query"].endswith("before:2026-05-12"))
            self.assertNotIn("2030", seen[0]["query"])
            self.assertNotIn("PRIVATE_TARGET_RESULT", json.dumps(result))

    def test_reviewer_failure_is_opaque_to_prediction_context(self):
        def failure(*args):
            raise RuntimeError("PRIVATE_PROVIDER_VALUE and PRIVATE_TARGET_RESULT")

        with tempfile.TemporaryDirectory() as folder:
            agent = self.make_agent(folder, reviewer=failure)
            result = agent.open("https://example.org/news/background")
            self.assertEqual(result, guard.PUBLIC_UNAVAILABLE)
            self.assertNotIn("PRIVATE_", json.dumps(result))
            receipts = [p.read_text(encoding="utf-8") for p in Path(folder).rglob("receipt.json")]
            self.assertTrue(any("PRIVATE_PROVIDER_VALUE" in value for value in receipts))


class ArticleQuarantineTest(unittest.TestCase):
    def test_target_result_or_retrospective_is_rejected_even_before_boundary(self):
        for field in ("direct_answer", "retrospective"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                guard.approved_source(article_page(), article_review(**{field: True}), "2026-05-12")

    def test_boundary_day_is_excluded_and_unknown_dates_are_not_filled(self):
        with self.assertRaises(ValueError):
            guard.approved_source(article_page(day="2026-05-12"),
                                  article_review(day="2026-05-12"), "2026-05-12")
        with self.assertRaises(ValueError):
            guard.approved_source(article_page(), article_review(publication_date=""), "2026-05-12")

    def test_rewritten_excerpt_and_unsupported_date_quote_are_rejected(self):
        for changes in ({"approved_excerpt": "Analysts predict a higher number."},
                        {"date_evidence_quote": "Published May 10, 2026"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                guard.approved_source(article_page(), article_review(**changes), "2026-05-12")

    def test_later_revision_cannot_be_approved_without_host_verified_archive(self):
        page = article_page(metadata_dates=[{"kind": "published", "value": "2026-05-10"},
                                            {"kind": "modified", "value": "2026-05-14"}])
        with self.assertRaisesRegex(ValueError, "Later modification"):
            guard.approved_source(page, article_review(), "2026-05-12")

    def test_missing_or_duplicate_reviews_deny_entire_batch(self):
        for mode in ("missing", "duplicate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                def reviewer(material, schema, work):
                    row = dict(article_review(), candidate_id=material["articles"][0]["candidate_id"])
                    return {"reviews": [] if mode == "missing" else [row, row]}

                checker = guard.Guard(dict(public_task(), _news_allowed=True), "2026-05-12", folder,
                                      lambda *_: {"candidate_urls": ["https://example.org/news/background"]},
                                      reviewer, article_page)
                self.assertEqual(checker.search("news"), guard.PUBLIC_UNAVAILABLE)
                self.assertEqual(checker.approved, {})

    def test_all_denied_response_does_not_expose_title_or_denial_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            def reviewer(material, schema, work):
                return {"reviews": [dict(article_review(verdict="deny", direct_answer=True,
                                                         reason="PRIVATE_TARGET_RESULT"),
                                          candidate_id=row["candidate_id"])
                                    for row in material["articles"]]}

            checker = guard.Guard(dict(public_task(), _news_allowed=True), "2026-05-12", folder,
                                  lambda *_: {"candidate_urls": ["https://example.org/PRIVATE_RESULT"]},
                                  reviewer, lambda url: article_page(url=url, title="PRIVATE_RESULT_TITLE"))
            public = checker.search("economic news")
            self.assertEqual(public, guard.PUBLIC_UNAVAILABLE)
            self.assertNotIn("PRIVATE", guard.canonical(public))
            self.assertEqual(checker.approved, {})


class ReceiptAndMCPTest(unittest.TestCase):
    def test_changed_source_receipt_invalidates_verified_day(self):
        task = public_task()
        with tempfile.TemporaryDirectory() as folder:
            refs = {}
            for label in ("source", "request", "response", "tool_trace"):
                path = Path(folder, label + ".json")
                path.write_text("{}", encoding="utf-8")
                refs[label] = {"path": str(path.resolve()), "sha256": boundary.file_sha256(path)}
            review = BoundaryFixture()({"review_arm": "strategy", "public_task": task},
                                       boundary.REVIEW_SCHEMA, Path(folder, "callback"))
            review["evidence"][0].update(original_publication_date_verified=True, source_receipt=refs["source"])
            review["review_receipt"] = {
                "labels_read": False, "prior_predictions_or_scores_read": False,
                "reviewer_model": "fixture-no-model-call", "reviewer_protocol": boundary.PROTOCOL,
                **{label: refs[label] for label in ("request", "response", "tool_trace")},
            }
            reviews = {arm: copy.deepcopy(review) for arm in boundary.ARMS}
            self.assertTrue(boundary.derive_boundary(task, reviews)["news_allowed"])
            Path(refs["source"]["path"]).write_text("changed", encoding="utf-8")
            self.assertFalse(boundary.derive_boundary(task, reviews)["news_allowed"])

    def test_mcp_initialize_exposes_the_requested_agent_name(self):
        request = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        with tempfile.TemporaryDirectory() as folder:
            token = Path(folder, "token")
            token.write_text("fixture-capability", encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(guard.sys, "stdin", io.StringIO(json.dumps(request) + "\n")), \
                    mock.patch.object(guard.sys, "stdout", output):
                guard.mcp_main("http://127.0.0.1:17891", token)
            self.assertEqual(json.loads(output.getvalue())["result"]["serverInfo"]["name"], "time gate agent")


if __name__ == "__main__":
    unittest.main()
