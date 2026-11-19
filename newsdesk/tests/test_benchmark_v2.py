"""Fixed-range backtest selection, isolated publication and frozen identity."""

import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from newsdesk.benchmark import BACKTEST_V2_DATE_RANGE, BACKTEST_V2_SUPPORTED_DATE_RANGES, BenchmarkStore, _digest
from newsdesk.trading_sessions import ICE_SPECIAL_2026


class FixedRangeContinuous:
    def __init__(self):
        self.calls, self.failed = [], set()
        self.rows = {}
        for family in ("WTI", "Brent", "SC"):
            rows, day = [], date(2026, 7, 30)
            while day <= date(2026, 10, 9):
                if day.weekday() < 5 and not (family == "WTI" and day == date(2026, 9, 7)) and not (family == "SC" and (day == date(2026, 9, 25)
                                                               or date(2026, 10, 1) <= day <= date(2026, 10, 7))):
                    value = 70 + len(rows)
                    rows.append({"trading_date": day.isoformat(), "open": value, "high": value + 2,
                                 "low": value - 1, "close": value + 1, "volume": 1000, "is_complete": True})
                day += timedelta(days=1)
            self.rows[family] = rows

    def get(self, family, refresh=False):
        self.calls.append((family, refresh))
        if family in self.failed:
            raise RuntimeError("source unavailable")
        exchange, code, zone = {
            "WTI": ("NYMEX", "CL.1:NYM", "America/New_York"),
            "Brent": ("ICE", "IB.1:IEU", "Europe/London"),
            "SC": ("INE", "SC0", "Asia/Shanghai"),
        }[family]
        rows = copy.deepcopy(self.rows[family])
        return {"contract": {"id": family.lower() + "-main", "familyId": family, "code": code,
                              "exchange": exchange, "currency": "CNY" if family == "SC" else "USD",
                              "unit": "元 / 桶" if family == "SC" else "美元 / 桶", "series_type": "dominant_continuous"},
                "period": "1d", "bars": rows, "source": {"name": "frozen continuous fixture", "timezone": zone},
                "fetched_at": "2026-11-04T09:00:00Z", "data_as_of": rows[-1]["trading_date"],
                "cache": {"stale": False}, "notes": [], "raw_response_sha256": "b" * 64}


class FixedRangeBacktestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.market = FixedRangeContinuous()
        self.store = BenchmarkStore(Path(self.temp.name) / "benchmark", continuous_market=self.market)
        self.clock = patch("newsdesk.benchmark._current_time", side_effect=lambda zone:
                           datetime(2026, 11, 4, 12, tzinfo=ZoneInfo(zone)))
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def test_profile_loads_empty_independently_before_its_first_build(self):
        self.assertEqual(self.store.summary(profile="v2")["status"], "empty")
        self.assertEqual(self.store.summary(profile="v2")["date_range"], BACKTEST_V2_DATE_RANGE)
        self.assertEqual(self.store.questions(profile="v2"), [])
        self.assertIn("backtest_v2", self.store.catalog())
        self.assertEqual(self.store.catalog()["backtest_v2"]["total_questions"], 0)

    def test_all_eligible_pairs_have_both_dates_in_august_september_and_no_fixed_quota(self):
        summary = self.store.build(profile="v2")
        dataset = self.store.load_dataset(profile="v2")
        self.assertEqual(summary["mode"], "backtest")
        self.assertEqual(summary["profile"], "v2")
        self.assertEqual(summary["date_range"], BACKTEST_V2_DATE_RANGE)
        self.assertEqual([family["count"] for family in summary["families"]], [41, 42, 41])
        self.assertEqual(summary["total_questions"], 124)
        self.assertEqual(summary["expected_questions"], 124)
        self.assertEqual(summary["coverage_status"], "complete", "Coverage follows the actual range rather than a fixed question quota")
        for case in dataset["questions"]:
            self.assertLessEqual("2026-08-01", case["input_date"])
            self.assertLessEqual(case["input_date"], "2026-09-30")
            self.assertLessEqual("2026-08-01", case["target_date"])
            self.assertLessEqual(case["target_date"], "2026-09-30")
            self.assertEqual(case["time_windows"]["status"], "ready")
            self.assertEqual(case["time_windows"]["timezone"], "UTC")
            self.assertIn(case["case_id"], dataset["labels"])
        self.assertEqual(summary["input_start"], "2026-08-03")
        self.assertEqual(summary["target_end"], "2026-09-30")
        self.assertFalse(any(case["input_date"] == "2026-07-31" or case["target_date"] == "2026-10-01"
                             for case in dataset["questions"]))
        self.assertTrue(any(case["input_date"] == "2026-08-31" and case["target_date"] == "2026-09-01"
                            for case in self.store.questions("WTI", profile="v2")),
                        "The month boundary inside the selected range is not an exclusion")

    def test_v2_uses_identical_v1_questions_utc_windows_ohlc_and_truth_for_shared_pairs(self):
        first = self.store.build(days=366)
        v1 = self.store.load_dataset(first["dataset_id"])
        self.store.build(profile="v2")
        v2 = self.store.load_dataset(profile="v2")
        questions = {case["case_id"]: case for case in v1["questions"]}
        for case in v2["questions"]:
            self.assertEqual(case, questions[case["case_id"]])
            self.assertEqual(v2["labels"][case["case_id"]], v1["labels"][case["case_id"]])

    def test_v1_live_pointers_frozen_files_and_existing_results_survive_v2_build(self):
        v1 = self.store.build(days=2)
        self.store.build(mode="live")
        before = {str(path.relative_to(self.store.root)): path.read_bytes()
                  for path in self.store.root.rglob("*") if path.is_file()}
        score = Path(self.temp.name) / "evaluations" / "run-old" / "result.json"
        score.parent.mkdir(parents=True)
        score.write_text('{"direction_accuracy":0.5}')
        result_bytes = score.read_bytes()
        v2 = self.store.build(profile="v2")
        self.assertNotEqual(v1["dataset_id"], v2["dataset_id"])
        self.assertEqual(self.store.summary()["dataset_id"], v1["dataset_id"])
        self.assertEqual(self.store.summary(profile="v1")["dataset_id"], v1["dataset_id"])
        self.assertEqual(self.store.summary(profile="v2")["dataset_id"], v2["dataset_id"])
        self.assertTrue((self.store.root / "current-backtest-v2.json").is_file())
        for relative, content in before.items():
            self.assertEqual((self.store.root / relative).read_bytes(), content)
        self.assertEqual(score.read_bytes(), result_bytes)

    def test_days_argument_neither_truncates_nor_changes_v2_identity(self):
        first = self.store.build(days=1, profile="v2")
        directory = self.store.root / "datasets" / first["dataset_id"]
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        for days in (30, 366):
            repeated = self.store.build(days=days, profile="v2")
            self.assertEqual(repeated["dataset_id"], first["dataset_id"])
            self.assertEqual(repeated["total_questions"], 124)
            self.assertIsNone(repeated["build_days"])
            self.assertIsNone(repeated["days_per_contract"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})

    def test_verified_special_windows_and_sc_holiday_have_distinct_calendar_metadata(self):
        self.store.build(profile="v2")
        selections = {family["id"]: family["contracts"][0]["selection"]
                      for family in self.store.summary(profile="v2")["families"]}
        self.assertEqual(selections["WTI"]["candidate_pairs"], 41)
        self.assertEqual(selections["WTI"]["excluded_counts"]["unconfirmed_time_windows"], 0)
        self.assertEqual(selections["Brent"]["excluded_counts"]["unconfirmed_time_windows"], 0)
        self.assertEqual(selections["WTI"]["no_separate_trade_dates"], ["2026-09-07"])
        self.assertEqual(selections["WTI"]["confirmed_holiday_dates"], [])
        self.assertEqual(selections["WTI"]["unverified_missing_weekdays"], [])
        self.assertEqual(selections["WTI"]["trading_date_assignments"][0]["assigned_trading_date"], "2026-09-08")
        brent = {(case["input_date"], case["target_date"])
                 for case in self.store.questions("Brent", profile="v2")}
        self.assertTrue({("2026-08-06", "2026-08-07"), ("2026-08-07", "2026-08-10"),
                         ("2026-08-28", "2026-08-31"), ("2026-08-31", "2026-09-01")}.issubset(brent))
        self.assertEqual(selections["SC"]["confirmed_holiday_dates"], ["2026-09-25"])
        self.assertEqual(selections["SC"]["unverified_missing_weekdays"], [])
        self.assertTrue(any(case["input_date"] == "2026-09-24" and case["target_date"] == "2026-09-28"
                            for case in self.store.questions("SC", profile="v2")))

    def test_unverified_gap_is_reported_as_partial_even_when_actual_expected_count_matches(self):
        self.market.rows["WTI"] = [row for row in self.market.rows["WTI"] if row["trading_date"] != "2026-09-17"]
        summary = self.store.build(profile="v2")
        selection = summary["families"][0]["contracts"][0]["selection"]
        self.assertEqual(selection["unverified_missing_weekdays"], ["2026-09-17"])
        self.assertEqual(selection["excluded_counts"]["data_gap_unverified"], 1)
        self.assertEqual(summary["total_questions"], summary["expected_questions"])
        self.assertEqual(summary["coverage_status"], "partial", "Actual pair counts must not hide unknown missing sessions")
        self.assertEqual(summary["families"][0]["contracts"][0]["status"], "partial")
        self.assertFalse(any(case["input_date"] == "2026-09-16" and case["target_date"] == "2026-09-18"
                             for case in self.store.questions("WTI", profile="v2")))

    def test_invalid_row_remains_a_barrier_instead_of_being_removed_before_pairing(self):
        broken = next(row for row in self.market.rows["SC"] if row["trading_date"] == "2026-09-16")
        broken["close"] = None
        summary = self.store.build(profile="v2")
        selection = summary["families"][2]["contracts"][0]["selection"]
        self.assertEqual(selection["excluded_counts"]["invalid_ohlc"], 2)
        self.assertEqual(selection["coverage_status"], "partial")
        self.assertFalse(any(case["input_date"] <= "2026-09-16" <= case["target_date"]
                             for case in self.store.questions("SC", profile="v2")))

    def test_history_keeps_earlier_indicator_context_but_no_post_september_bars(self):
        summary = self.store.build(profile="v2")
        for source in self.store.load_dataset(summary["dataset_id"])["manifest"]["source_fingerprints"].values():
            self.assertTrue(any(row["trading_date"] < "2026-08-01" for row in source["history_bars"]))
            self.assertTrue(all(row["trading_date"] <= "2026-09-30" for row in source["history_bars"]))
            self.assertTrue(all("2026-08-01" <= row["trading_date"] <= "2026-09-30" for row in source["selected_bars"]))

    def test_missing_boundary_and_provider_failure_report_partial_coverage_without_padding(self):
        self.market.rows["WTI"] = [row for row in self.market.rows["WTI"] if row["trading_date"] >= "2026-09-09"]
        self.market.failed.add("Brent")
        summary = self.store.build(profile="v2")
        self.assertEqual(summary["coverage_status"], "partial")
        self.assertFalse(summary["families"][0]["contracts"][0]["selection"]["source_spans_range"])
        self.assertEqual(summary["families"][1]["contracts"][0]["status"], "failed")
        self.assertEqual(self.store.questions("Brent", profile="v2"), [])
        self.assertIn("Brent", summary["errors"])

    def test_explicit_dataset_loading_and_one_day_slicing_work_without_changing_default_profile(self):
        summary = self.store.build(profile="v2")
        explicit = self.store.load_dataset(summary["dataset_id"])
        self.assertEqual(explicit["manifest"]["profile"], "v2")
        self.assertEqual(self.store.questions(), [])
        selected = self.store.one_day(summary["dataset_id"])
        self.assertEqual(len(selected["questions"]), 3)
        self.assertEqual(selected["manifest"]["profile"], "v2")
        self.assertEqual({case["input_date"] for case in selected["questions"]}, {"2026-09-29"})

    def test_profile_and_date_bounds_are_bound_to_frozen_integrity(self):
        summary = self.store.build(profile="v2")
        path = self.store.root / "datasets" / summary["dataset_id"] / "manifest.json"
        saved = json.loads(path.read_text())
        for change in (lambda record: record["date_range"].update(start="2026-08-31"),
                       lambda record: record.update(date_range=copy.deepcopy(BACKTEST_V2_SUPPORTED_DATE_RANGES[0])),
                       lambda record: record.update(profile="v1"),
                       lambda record: record.update(selection_policy="target_date_only")):
            modified = copy.deepcopy(saved)
            change(modified)
            modified["manifest_sha256"] = _digest({key: value for key, value in modified.items() if key != "manifest_sha256"})
            path.write_text(json.dumps(modified))
            with self.assertRaisesRegex(ValueError, "integrity"):
                self.store.load_dataset(profile="v2")
        path.write_text(json.dumps(saved))
        self.assertEqual(self.store.load_dataset(profile="v2")["manifest"]["sha256"], summary["sha256"])

    def test_old_september_only_v2_remains_readable_after_current_range_expands(self):
        old_range = copy.deepcopy(BACKTEST_V2_SUPPORTED_DATE_RANGES[0])
        self.assertEqual(old_range["start"], "2026-09-01")
        # Reproduce the previously published interpretation without revising
        # that immutable 57-question cohort under today's calendar evidence.
        with patch("newsdesk.benchmark.BACKTEST_V2_DATE_RANGE", old_range), \
             patch("newsdesk.trading_sessions.CME_COMBINED_TRADE_DATES", {}), \
             patch("newsdesk.trading_sessions.ICE_VERIFIED_EARLY_CLOSES", {}):
            old = self.store.build(profile="v2")
        old_directory = self.store.root / "datasets" / old["dataset_id"]
        before = {path.name: path.read_bytes() for path in old_directory.iterdir()}
        historical_results = Path(self.temp.name) / "evaluations" / "old-v2-result.json"
        historical_results.parent.mkdir()
        historical_results.write_text(json.dumps({"dataset_id": old["dataset_id"], "accuracy": 0.5}))
        result_bytes = historical_results.read_bytes()

        expanded = self.store.build(profile="v2")
        self.assertNotEqual(expanded["dataset_id"], old["dataset_id"])
        self.assertEqual(expanded["date_range"]["start"], "2026-08-01")
        self.assertEqual(expanded["total_questions"], 124)
        preserved = self.store.load_dataset(old["dataset_id"])
        self.assertEqual(preserved["manifest"]["date_range"], old_range)
        self.assertEqual(preserved["manifest"]["total_questions"], 57)
        self.assertTrue(all("2026-09-01" <= case["input_date"] <= case["target_date"] <= "2026-09-30"
                            for case in preserved["questions"]))
        self.assertEqual(before, {path.name: path.read_bytes() for path in old_directory.iterdir()})
        self.assertEqual(historical_results.read_bytes(), result_bytes)
        self.assertEqual(self.store.summary(profile="v2")["dataset_id"], expanded["dataset_id"])
        self.assertEqual(len(self.store.one_day(old["dataset_id"])["questions"]), 3)

    def test_pre_correction_116_question_publication_remains_frozen_and_readable(self):
        old_special_dates = ICE_SPECIAL_2026 | {"2026-08-07", "2026-08-31"}
        with patch("newsdesk.trading_sessions.ICE_SPECIAL_2026", old_special_dates), \
             patch("newsdesk.trading_sessions.CME_COMBINED_TRADE_DATES", {}), \
             patch("newsdesk.trading_sessions.ICE_VERIFIED_EARLY_CLOSES", {}):
            old = self.store.build(profile="v2")
        self.assertEqual(old["total_questions"], 116)
        directory = self.store.root / "datasets" / old["dataset_id"]
        before = {path.name: path.read_bytes() for path in directory.iterdir()}
        current = self.store.build(profile="v2")
        self.assertEqual(current["total_questions"], 124)
        self.assertNotEqual(current["dataset_id"], old["dataset_id"])
        preserved = self.store.load_dataset(old["dataset_id"])
        self.assertEqual(len(preserved["questions"]), 116)
        self.assertEqual(preserved["manifest"]["sha256"], old["sha256"])
        self.assertEqual(before, {path.name: path.read_bytes() for path in directory.iterdir()})
        old_questions = {case["case_id"]: case for case in preserved["questions"]}
        new = self.store.load_dataset(current["dataset_id"])
        new_questions = {case["case_id"]: case for case in new["questions"]}
        self.assertEqual(len(set(new_questions) - set(old_questions)), 8)
        for case_id, case in old_questions.items():
            self.assertEqual(new_questions[case_id], case, "Every previously published question remains byte-content equivalent")
            self.assertEqual(new["labels"][case_id], preserved["labels"][case_id])

    def test_labor_day_pairs_use_verified_windows_and_cutoffs_without_fabricated_bars(self):
        summary = self.store.build(profile="v2")
        self.assertEqual(summary["total_questions"], 124)
        self.assertEqual(summary["coverage_status"], "complete")
        self.assertEqual(sum(family["contracts"][0]["selection"]["candidate_pairs"] for family in summary["families"]), 124)
        for family in summary["families"]:
            selection = family["contracts"][0]["selection"]
            self.assertEqual(sum(selection["excluded_counts"].values()), 0)
            self.assertEqual(selection["unverified_missing_weekdays"], [])
        pairs = {(case["family"], case["input_date"], case["target_date"]): case
                 for case in self.store.questions(profile="v2")}
        wti_first = pairs[("WTI", "2026-09-04", "2026-09-08")]
        wti_next = pairs[("WTI", "2026-09-08", "2026-09-09")]
        self.assertEqual(wti_first["time_windows"]["forecast"]["start_utc"], "2026-09-06T22:00:00Z")
        self.assertEqual(wti_first["time_windows"]["forecast"]["end_utc"], "2026-09-08T21:00:00Z")
        self.assertEqual(len(wti_first["time_windows"]["forecast"]["segments"]), 2)
        self.assertEqual(wti_first["time_windows"]["information_cutoff_utc"], "2026-09-04T21:00:00Z")
        self.assertEqual(wti_next["time_windows"]["information_cutoff_utc"], "2026-09-08T21:00:00Z")
        for case in (wti_first, wti_next, pairs[("Brent", "2026-09-04", "2026-09-07")], pairs[("Brent", "2026-09-07", "2026-09-08")]):
            self.assertEqual(case["time_windows"]["status"], "ready")
            self.assertLess(case["time_windows"]["information_cutoff_utc"], case["time_windows"]["forecast"]["start_utc"])
            self.assertIn("已核实的节假日特殊安排", case["question"])
        self.assertEqual(pairs[("Brent", "2026-09-04", "2026-09-07")]["time_windows"]["forecast"]["end_utc"], "2026-09-07T17:30:00Z")
        self.assertFalse(any(case["family"] == "WTI" and "2026-09-07" in (case["input_date"], case["target_date"])
                             for case in pairs.values()))

    def test_bad_or_live_profile_is_rejected_before_fetching(self):
        for kwargs in ({"profile": "v3"}, {"profile": ""}, {"profile": True},
                       {"profile": "v2", "mode": "live"}, {"profile": "v1", "mode": "live"}):
            for method in (self.store.build, self.store.summary, self.store.questions, self.store.load_dataset):
                with self.subTest(method=method.__name__, kwargs=kwargs):
                    with self.assertRaises(ValueError):
                        method(**kwargs)
        self.assertEqual(self.market.calls, [])


if __name__ == "__main__":
    unittest.main()
