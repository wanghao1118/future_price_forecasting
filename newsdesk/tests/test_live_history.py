"""Cumulative live views use timely frozen publications and bound truth only."""

import copy
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.benchmark import BenchmarkStore, VERSION, _digest, _write


def question(case_id, input_day="2026-11-02", target_day="2026-11-03", family="SC", *,
             deadline="2026-11-02T13:00:00+00:00", state="ready", price=70):
    return {"case_id": case_id, "family": family, "contract_id": family.lower() + "-main",
            "contract_code": "SC0" if family == "SC" else "CL.1:NYM", "mode": "live",
            "exchange": "INE" if family == "SC" else "NYMEX", "exchange_timezone": "UTC",
            "input_date": input_day, "target_date": target_day, "current_price": price,
            "current_bar": {"open": price, "close": price, "high": price + 1, "low": price - 1},
            "question": "Frozen question " + case_id, "live_status": state, "forecast_status": state,
            "forecast_deadline": deadline, "unit": "元 / 桶" if family == "SC" else "美元 / 桶",
            "time_windows": {"timezone": "UTC", "status": "ready"}}


def freeze(store, created_at, questions, *, marker="first", mode="live"):
    questions, labels, sources = copy.deepcopy(questions), {}, {"marker": marker}
    fingerprints = {"marker": marker}
    identity = {"version": VERSION, "mode": mode, "days_per_contract": 30,
                "questions": questions, "labels": labels, "sources": fingerprints}
    digest = _digest(identity)
    manifest = {"dataset_id": "qa-" + digest[:20], "version": VERSION, "mode": mode,
                "created_at": created_at, "days_per_contract": 1, "build_days": 30,
                "sha256": digest, "questions_sha256": _digest(questions), "labels_sha256": _digest(labels),
                "source_snapshots_sha256": _digest(sources), "source_fingerprints": fingerprints,
                "families": [], "total_questions": len(questions)}
    manifest["manifest_sha256"] = _digest(manifest)
    path = store.root / "datasets" / manifest["dataset_id"]
    for name, value in (("manifest", manifest), ("questions", questions), ("labels", labels), ("source-snapshots", sources)):
        _write(path / (name + ".json"), value)
    return manifest


def settle(store, manifest, case_id, *, price=72, return_pct=2):
    actual = {"direction": "up", "return_pct": return_pct, "high": price + 1, "low": price - 1,
              "close": price, "target_date": "2026-11-03"}
    record = {"dataset_id": manifest["dataset_id"], "questions_sha256": manifest["questions_sha256"],
              "updated_at": "2026-11-03T08:00:00+00:00", "items": {case_id: {"actual": actual}}, "errors": {}}
    record["settlement_sha256"] = _digest(record)
    _write(store.root / "datasets" / manifest["dataset_id"] / "settlement.json", record)
    return actual


class LiveHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = BenchmarkStore(self.temp.name)
        self.now = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)
        self.clock = patch("newsdesk.benchmark._current_time", side_effect=lambda zone: self.now)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def pointer(self, manifest):
        _write(self.store._pointer("live"), {"dataset_id": manifest["dataset_id"]})

    def test_deduplicates_logical_days_and_excludes_late_or_never_ready_previews(self):
        first = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        second = freeze(self.store, "2026-11-02T09:00:00+00:00", [question("revised", price=71)], marker="second")
        freeze(self.store, "2026-11-02T14:00:00+00:00", [question("expired")], marker="late-copy")
        # Multiple frozen versions of an ineligible day still count as one exclusion.
        for index in range(2):
            freeze(self.store, "2026-10-30T14:00:00+00:00", [question(
                "late-" + str(index), "2026-10-30", "2026-11-02", deadline="2026-10-30T13:00:00+00:00")], marker=str(index))
        freeze(self.store, "2026-10-29T10:00:00+00:00", [question(
            "preview", "2026-10-16", "2026-10-19", deadline="2026-10-18T22:00:00+00:00", state="waiting_new_completed_bar")])
        latest = freeze(self.store, "2026-11-03T08:00:00+00:00", [question(
            "today", "2026-11-03", "2026-11-04", deadline="2026-11-03T13:00:00+00:00")])
        self.pointer(latest)
        result = self.store.live_history()
        old, current = result["questions"]
        self.assertEqual(old["case_id"], "first")
        self.assertEqual(old["dataset_id"], first["dataset_id"])
        self.assertEqual([item["case_id"] for item in old["versions"]], ["first", "revised"])
        self.assertEqual(old["versions"][1]["dataset_id"], second["dataset_id"])
        self.assertEqual(old["live_status"], "forecast_closed")
        self.assertFalse(old["is_current"])
        self.assertTrue(current["is_current"])
        self.assertEqual(current["live_status"], "ready")
        summary = result["summary"]
        self.assertEqual((summary["total_questions"], summary["ready_questions"], summary["excluded_questions"]), (2, 1, 2))
        self.assertEqual(summary["errors"], {})
        self.assertEqual(summary["input_start"], "2026-11-02")
        self.assertEqual(summary["target_end"], "2026-11-04")

    def test_current_membership_uses_day_group_and_time_alone_changes_revision(self):
        first = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        current = freeze(self.store, "2026-11-02T09:00:00+00:00", [question("current")], marker="second")
        self.pointer(current)
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        result = self.store.live_history()
        item = result["questions"][0]
        self.assertEqual(item["dataset_id"], first["dataset_id"])
        self.assertTrue(item["is_current"])
        self.assertEqual(item["live_status"], "ready")
        self.now = datetime(2026, 11, 2, 13, tzinfo=timezone.utc)
        closed = self.store.live_history()
        self.assertEqual(closed["questions"][0]["live_status"], "forecast_closed")
        self.assertNotEqual(result["summary"]["history_revision"], closed["summary"]["history_revision"])

    def test_equal_deadline_is_closed_and_unzoned_publication_is_reported(self):
        freeze(self.store, "2026-11-02T13:00:00+00:00", [question("at-deadline")])
        invalid = freeze(self.store, "2026-11-03T08:00:00", [question(
            "no-zone", "2026-11-03", "2026-11-04", deadline="2026-11-03T13:00:00+00:00")])
        result = self.store.live_history_summary()
        self.assertEqual(result["total_questions"], 0)
        self.assertEqual(result["excluded_questions"], 1)
        self.assertIn("timezone", result["errors"][invalid["dataset_id"]]["message"])

    def test_current_stale_preview_does_not_reactivate_earlier_ready_publication(self):
        first = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        current = freeze(self.store, "2026-11-02T09:00:00+00:00", [question(
            "stale", state="waiting_new_completed_bar")], marker="stale-source")
        self.pointer(current)
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        self.assertEqual(self.store.summary("live")["ready_questions"], 0)
        result = self.store.live_history()
        item = result["questions"][0]
        self.assertEqual(item["dataset_id"], first["dataset_id"])
        self.assertTrue(item["is_current"], "Current membership still follows the logical trading-day group")
        self.assertEqual([value["case_id"] for value in item["versions"]], ["first"])
        self.assertEqual(item["live_status"], "waiting_new_completed_bar")
        self.assertEqual(result["summary"]["ready_questions"], 0)
        self.assertEqual(result["summary"]["families"][-1]["ready"], 0)
        self.now = datetime(2026, 11, 2, 13, tzinfo=timezone.utc)
        self.assertEqual(self.store.live_history_questions()[0]["live_status"], "forecast_closed")

    def test_current_ready_case_must_belong_to_a_valid_publication_version(self):
        freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        current = freeze(self.store, "2026-11-02T13:00:00+00:00", [question("too-late")], marker="late-publication")
        self.pointer(current)
        self.now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        item = self.store.live_history_questions()[0]
        self.assertTrue(item["is_current"])
        self.assertEqual(item["live_status"], "waiting_new_completed_bar")
        self.assertEqual(self.store.live_history_summary()["ready_questions"], 0)

    def test_truth_remains_bound_to_canonical_baseline_and_settlement_invalidates_cache(self):
        first = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first", price=70)])
        revised = freeze(self.store, "2026-11-02T09:00:00+00:00", [question("revised", price=71)], marker="second")
        self.pointer(revised)
        settle(self.store, revised, "revised", return_pct=99)
        with patch.object(self.store, "load_dataset", wraps=self.store.load_dataset) as load:
            result = self.store.live_history()
            self.assertIsNone(result["questions"][0]["actual"])
            calls = load.call_count
            self.assertEqual(self.store.live_history(), result)
            self.assertEqual(load.call_count, calls, "Unchanged files must reuse prior full integrity checks")
            actual = settle(self.store, first, "first", return_pct=2)
            updated = self.store.live_history()
            self.assertEqual(load.call_count, calls + 1)
        self.assertEqual(updated["questions"][0]["actual"], actual)
        self.assertEqual(updated["questions"][0]["live_status"], "settled")
        self.assertEqual(updated["summary"]["settled_questions"], 1)
        self.assertNotEqual(updated["summary"]["history_revision"], result["summary"]["history_revision"])

    def test_reads_are_non_mutating_and_leave_current_dataset_semantics_unchanged(self):
        first = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        current = freeze(self.store, "2026-11-03T08:00:00+00:00", [question(
            "today", "2026-11-03", "2026-11-04", deadline="2026-11-03T13:00:00+00:00")])
        self.pointer(current)
        before = {str(path.relative_to(self.store.root)): path.read_bytes() for path in self.store.root.rglob("*") if path.is_file()}
        with patch.object(self.store, "_snapshot", side_effect=AssertionError("History must not refresh markets")), patch.object(self.store, "settle_live", side_effect=AssertionError("History must not settle")):
            self.assertEqual(self.store.live_history_summary()["total_questions"], 2)
            self.assertEqual(len(self.store.live_history_questions("SC")), 2)
            self.assertEqual(self.store.live_history_questions("WTI"), [])
            self.assertIn("live_history", self.store.catalog())
        self.assertEqual(self.store.summary("live")["dataset_id"], current["dataset_id"])
        self.assertEqual([item["case_id"] for item in self.store.questions(mode="live")], ["today"])
        self.assertEqual(self.store.load_dataset(first["dataset_id"], mode="live")["questions"][0]["case_id"], "first")
        after = {str(path.relative_to(self.store.root)): path.read_bytes() for path in self.store.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        with self.assertRaises(ValueError):
            self.store.live_history_questions("unknown")

    def test_cached_source_and_settlement_tampering_are_reported_explicitly(self):
        manifest = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("first")])
        self.pointer(manifest)
        self.assertEqual(self.store.live_history_summary()["total_questions"], 1)
        path = self.store.root / "datasets" / manifest["dataset_id"]
        original = (path / "source-snapshots.json").read_bytes()
        (path / "source-snapshots.json").write_text('{"marker":"tampered"}')
        invalid = self.store.live_history_summary()
        self.assertEqual(invalid["status"], "partial")
        self.assertEqual(invalid["total_questions"], 0)
        self.assertIn(manifest["dataset_id"], invalid["errors"])
        self.assertIn("integrity", invalid["errors"][manifest["dataset_id"]]["message"])
        (path / "source-snapshots.json").write_bytes(original)
        settle(self.store, manifest, "first")
        self.assertEqual(self.store.live_history_summary()["settled_questions"], 1)
        (path / "settlement.json").write_text('{"items":{}}')
        self.assertIn(manifest["dataset_id"], self.store.live_history_summary()["errors"])

    def test_explicit_refresh_snapshot_settles_only_selected_published_cases(self):
        manifest = freeze(self.store, "2026-11-02T08:00:00+00:00", [question("sc"), question("wti", family="WTI")])
        snapshot = {"source_stale": False, "source": {"provider": "fixture"}, "fetched_at": "2026-11-03T08:00:00+00:00",
                    "raw_response_sha256": "a" * 64, "normalized_sha256": "b" * 64,
                    "ordered": [("2026-11-03", {"trading_date": "2026-11-03", "close": 72, "high": 73, "low": 71})]}
        with patch.object(self.store, "_snapshot", side_effect=AssertionError("Must reuse captured update snapshot")):
            pending = self.store.settle_live(manifest["dataset_id"], refresh=False, snapshots={}, case_ids={"sc"})
            self.assertEqual(pending["errors"], {})
            self.assertEqual(pending["settled"], 0)
            settled = self.store.settle_live(manifest["dataset_id"], refresh=False, snapshots={"SC": snapshot}, case_ids={"sc"})
        self.assertEqual(set(settled["items"]), {"sc"})
        self.assertEqual(settled["pending"], 1)
        with self.assertRaises(ValueError):
            self.store.settle_live(manifest["dataset_id"], snapshots={}, case_ids={"unrelated"})


if __name__ == "__main__":
    unittest.main()
