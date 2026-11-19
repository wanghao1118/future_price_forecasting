"""Continuous daily modes, immutable labels and independently settled live truth."""

import copy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from newsdesk.benchmark import BenchmarkStore, FAMILIES, OUTPUTS, VERSION, _deadline, _digest, _write, display_time_windows


def bars(count=40):
    result, day = [], date(2026, 8, 10)
    while len(result) < count:
        if day.weekday() < 5:
            value = 70 + len(result) / 10
            result.append({"trading_date": day.isoformat(), "open": value - 0.2,
                           "high": value + 1, "low": value - 1, "close": value, "volume": 50})
        day += timedelta(days=1)
    return result


def row(day, close=72, complete=False):
    return {"trading_date": day, "open": close - .2, "high": close + 1, "low": close - 1,
            "close": close, "volume": 50, "is_complete": complete}


class FakeContinuous:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else bars()
        self.failures, self.calls = set(), []
        self.stale = False
        self.continuous = True

    def get(self, family, refresh=False):
        self.calls.append((family, refresh))
        if family in self.failures:
            raise RuntimeError("unavailable source")
        contract = {"id": family.lower() + "-main", "familyId": family,
                    "code": {"WTI": "CL=F", "Brent": "BZ=F", "SC": "SC0"}[family],
                    "currency": "CNY" if family == "SC" else "USD",
                    "unit": "元 / 桶" if family == "SC" else "美元 / 桶", "isDemo": False,
                    "series_type": "dominant_continuous" if self.continuous else "specific_month"}
        return {"contract": contract, "period": "1d", "bars": copy.deepcopy(self.rows),
                "source": {"name": "Captured continuous fixture", "provider": "fixture",
                           "timezone": "Asia/Shanghai" if family == "SC" else "America/New_York",
                           "continuous_method": "vendor_roll_unadjusted"},
                "fetched_at": "2026-10-29T09:30:00+00:00", "data_as_of": self.rows[-1]["trading_date"],
                "raw_response_sha256": "a" * 64, "cache": {"stale": self.stale}, "notes": []}


class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.market = FakeContinuous()
        self.store = BenchmarkStore(self.temp.name, continuous_market=self.market)
        self.clock_day, self.clock_hour = "2026-10-29", 17
        self.clock = patch("newsdesk.benchmark._current_time", side_effect=self.current_time)
        self.clock.start()

    def current_time(self, zone):
        return datetime.fromisoformat(self.clock_day + "T{:02d}:30:00".format(self.clock_hour)).replace(tzinfo=ZoneInfo(zone))

    def tearDown(self):
        self.clock.stop()
        self.temp.cleanup()

    def live_rows(self):
        self.market.rows = [row("2026-10-28", 70), row("2026-10-29", 72, True)]

    def test_three_continuous_series_make_90_four_target_questions_without_model_labels(self):
        summary = self.store.build()
        self.assertEqual(summary["total_questions"], 90)
        self.assertEqual(summary["mode"], "backtest")
        self.assertEqual(summary["coverage_status"], "complete")
        self.assertEqual([family["count"] for family in summary["families"]], [30, 30, 30])
        self.assertEqual(summary["outputs"], OUTPUTS)
        self.assertEqual(len(self.market.calls), 3)
        dataset = self.store.load_dataset()
        self.assertEqual(len(dataset["labels"]), 90)
        self.assertNotIn("source_fingerprints", summary)
        for question in dataset["questions"]:
            self.assertEqual(question["series_type"], "dominant_continuous")
            self.assertNotIn("actual", question)
            self.assertNotIn("labels", question)
            self.assertNotIn("answer", question)
            self.assertEqual(question["news_cutoff_exclusive"], question["target_date"])
            self.assertEqual(question["current_bar"]["close"], question["current_price"])
            label = dataset["labels"][question["case_id"]]
            self.assertEqual(label["direction"], "up")
            self.assertAlmostEqual(label["return_pct"], (label["close"] / question["current_price"] - 1) * 100)
            self.assertGreaterEqual(label["high"], label["close"])
            self.assertLessEqual(label["low"], label["close"])
        label_file = self.store.root / "datasets" / summary["dataset_id"] / "labels.json"
        self.assertEqual(label_file.stat().st_mode & 0o077, 0)
        self.assertEqual(len(self.store.questions("WTI")), 30)
        self.assertIn("actual_direction", self.store.questions("WTI")[0])

    def test_one_day_selects_latest_common_session_without_mutating_frozen_dataset(self):
        summary = self.store.build()
        before = {path.name: path.read_bytes() for path in
                  (self.store.root / "datasets" / summary["dataset_id"]).iterdir()}
        selected = self.store.one_day(summary["dataset_id"])
        self.assertEqual(selected["manifest"]["one_day_input_date"], "2026-09-29")
        self.assertEqual([item["family"] for item in selected["questions"]], list(FAMILIES))
        self.assertEqual(len(selected["labels"]), 3)
        self.assertTrue(all(item["case_id"] in selected["labels"] for item in selected["questions"]))
        after = {path.name: path.read_bytes() for path in
                 (self.store.root / "datasets" / summary["dataset_id"]).iterdir()}
        self.assertEqual(before, after)

    def test_one_day_rejects_missing_family_or_missing_truth(self):
        self.store.build()
        with self.assertRaisesRegex(ValueError, "没有覆盖全部主连"):
            self.store.one_day(input_date="2026-08-10")

    def test_explicit_monthly_series_cannot_silently_substitute_for_continuous(self):
        self.market.continuous = False
        summary = self.store.build()
        self.assertEqual(summary["total_questions"], 0)
        self.assertEqual(summary["coverage_status"], "partial")
        self.assertEqual(set(summary["errors"]), set(FAMILIES))

    def test_brent_manifest_uses_actual_ice_exchange_and_london_timezone(self):
        original = self.market.get
        def ice_source(family, refresh=False):
            result = original(family, refresh=refresh)
            if family == "Brent":
                result["contract"].update(code="IB.1:IEU", exchange="ICE")
                result["source"]["timezone"] = "Europe/London"
            return result
        with patch.object(self.market, "get", side_effect=ice_source):
            summary = self.store.build()
        brent = next(family for family in summary["families"] if family["id"] == "Brent")
        self.assertEqual(brent["exchange"], "ICE")
        self.assertEqual(brent["contracts"][0]["code"], "IB.1:IEU")
        self.assertTrue(all(item["exchange_timezone"] == "Europe/London" for item in self.store.questions("Brent")))

    def test_identical_build_reuses_immutable_version_but_a_real_price_change_versions_it(self):
        first = self.store.build()
        path = self.store.root / "datasets" / first["dataset_id"]
        before = {p.name: p.read_bytes() for p in path.iterdir()}
        second = self.store.build()
        self.assertEqual(first["dataset_id"], second["dataset_id"])
        self.assertEqual(before, {p.name: p.read_bytes() for p in path.iterdir()})
        self.market.rows[-1]["close"] += .1
        self.assertNotEqual(first["dataset_id"], self.store.build()["dataset_id"])

    def test_next_observed_trading_day_skips_weekend_and_preserves_historical_values(self):
        self.store.build(days=39)
        item = next(q for q in self.store.load_dataset()["questions"] if q["input_date"] == "2026-08-14")
        self.assertEqual(item["target_date"], "2026-08-17")
        self.assertIn("主连", item["question"])
        self.assertIn("行情表现", item["question"])
        self.assertNotIn("返回这四项具体结果", item["question"])
        self.assertEqual(item["evaluator_id"], "eval-v1")
        self.assertIn("direction", item["evaluator_injection"])

    def test_ft_missing_weekday_interval_is_not_a_next_day_backtest_pair(self):
        self.market.rows = [row("2026-10-15", 70), row("2026-10-16", 71), row("2026-10-29", 74, True)]
        summary = self.store.build()
        questions = self.store.load_dataset()["questions"]
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q["input_date"] == "2026-10-15" and q["target_date"] == "2026-10-16" for q in questions))
        self.assertEqual(summary["families"][0]["contracts"][0]["data_gap_unverified_count"], 1)
        self.assertIn("data_gap_unverified", summary["data_gap_policy"])

    def test_unfinished_today_future_and_invalid_bar_do_not_create_next_day_truth(self):
        self.market.rows = [row("2026-10-23", 70), row("2026-10-26", 71), row("2026-10-27", 72),
                            row("2026-10-28", 73), row("2026-10-29", 74), row("2026-10-30", 75, True)]
        self.market.rows[2]["close"] = None
        summary = self.store.build()
        questions = self.store.load_dataset()["questions"]
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q["input_date"] == "2026-10-23" and q["target_date"] == "2026-10-26" for q in questions))
        self.assertEqual(summary["families"][0]["contracts"][0]["ignored_unfinished_bars"], 2)

    def test_partial_source_failure_retains_other_true_series_without_fabrication(self):
        self.market.failures.add("SC")
        summary = self.store.build()
        self.assertEqual(summary["total_questions"], 60)
        self.assertEqual(summary["families"][-1]["contracts"][0]["status"], "failed")
        self.assertEqual(self.store.questions("SC"), [])

    def test_backtest_and_live_pointers_are_separate_and_live_has_no_truth(self):
        backtest = self.store.build()
        self.live_rows()
        self.clock_hour = 16
        live = self.store.build(mode="live")
        self.assertNotEqual(backtest["dataset_id"], live["dataset_id"])
        self.assertEqual(self.store.summary()["dataset_id"], backtest["dataset_id"])
        self.assertEqual(self.store.summary("live")["dataset_id"], live["dataset_id"])
        dataset = self.store.load_dataset(mode="live")
        self.assertEqual(len(dataset["questions"]), 3)
        self.assertEqual(dataset["labels"], {})
        self.assertEqual(live["pending_questions"], 3)
        self.assertEqual(live["settled_questions"], 0)
        for question in self.store.questions(mode="live"):
            self.assertIsNone(question["actual"])
            self.assertEqual(question["settlement_status"], "pending")
            self.assertEqual(question["live_status"], "ready")
            self.assertTrue(question["target_date_is_expected"])
            deadline = datetime.fromisoformat(question["forecast_deadline"])
            local_deadline = deadline.astimezone(ZoneInfo(question["exchange_timezone"]))
            self.assertEqual(local_deadline.hour, 20 if question["family"] == "SC" else 17)
            self.assertEqual(local_deadline.minute, 30)
            self.assertEqual(deadline.date().isoformat(), "2026-10-29")

    def test_old_daily_input_or_passed_night_session_cannot_claim_today_live(self):
        self.store.build(mode="live")
        self.assertTrue(all(q["live_status"] == "waiting_new_completed_bar" for q in self.store.questions(mode="live")))
        self.live_rows()
        self.store.build(mode="live")
        self.clock_hour = 22
        self.assertTrue(all(q["live_status"] == "waiting_new_completed_bar" for q in self.store.questions(mode="live")))
        self.assertEqual(self.store.summary("live")["ready_questions"], 0)

    def test_ice_brent_deadline_leaves_time_after_completed_london_close(self):
        zone = ZoneInfo("Europe/London")
        close_available = datetime(2026, 10, 29, 23, 30, tzinfo=zone)
        # ICE's US/UK DST mismatch week opens 00:00 UTC, per its Oct 2026 PDF.
        next_open = datetime(2026, 10, 30, 0, 0, tzinfo=zone)
        deadline = datetime.fromisoformat(_deadline("Brent", "2026-10-29", "2026-10-30", "Europe/London"))
        self.assertEqual(deadline, close_available)
        self.assertEqual(deadline, next_open - timedelta(minutes=30))
        self.assertLess(deadline - timedelta(microseconds=1), next_open)

    def test_ice_brent_monday_session_deadline_is_sunday_london_evening(self):
        zone = ZoneInfo("Europe/London")
        friday_close_available = datetime(2026, 10, 30, 23, 30, tzinfo=zone)
        next_open = datetime(2026, 11, 1, 23, 0, tzinfo=zone)
        deadline = datetime.fromisoformat(_deadline("Brent", "2026-10-30", "2026-11-02", "Europe/London"))
        self.assertGreater(deadline, friday_close_available)
        self.assertEqual(deadline, next_open - timedelta(minutes=30))
        self.assertLess(deadline - timedelta(microseconds=1), next_open)

    def test_pending_live_settles_only_after_completed_next_bar_without_rewriting_frozen_inputs(self):
        self.live_rows()
        live = self.store.build(mode="live")
        path = self.store.root / "datasets" / live["dataset_id"]
        frozen = {name: (path / name).read_bytes() for name in ("questions.json", "labels.json", "manifest.json")}
        self.assertEqual(self.store.settle_live(refresh=False)["settled"], 0)
        self.market.rows.append(row("2026-10-30", 74))
        self.clock_day = "2026-10-30"
        self.assertEqual(self.store.settle_live(refresh=False)["settled"], 0)
        self.market.rows[-1]["is_complete"] = True
        settled = self.store.settle_live(refresh=False)
        self.assertEqual((settled["settled"], settled["pending"]), (3, 0))
        self.assertEqual(frozen, {name: (path / name).read_bytes() for name in frozen})
        dataset = self.store.load_dataset(mode="live")
        self.assertEqual(len(dataset["labels"]), 3)
        self.assertTrue(all(label["close"] == 74 and label["high"] == 75 for label in dataset["labels"].values()))
        self.assertTrue(all(label["target_date"] == "2026-10-30" for label in dataset["labels"].values()))
        self.market.rows[-1]["close"] = 74.1
        self.store.settle_live(refresh=False)
        self.assertTrue(all(label["close"] == 74 for label in self.store.load_dataset(mode="live")["labels"].values()))

    def test_unverified_holiday_gap_does_not_create_live_truth_or_relax_news_cutoff(self):
        self.live_rows()
        self.store.build(mode="live")
        self.market.rows.append(row("2026-11-02", 71, True))
        self.clock_day = "2026-11-02"
        result = self.store.settle_live(refresh=False)
        item = self.store.questions(mode="live")[0]
        self.assertEqual(item["target_date"], "2026-10-30")
        self.assertEqual(item["news_cutoff_exclusive"], "2026-10-30")
        self.assertIsNone(item["actual"])
        self.assertEqual((result["settled"], result["pending"]), (0, 3))
        self.assertTrue(all("data_gap_unverified" in error["message"] for error in result["errors"].values()))

    def test_ft_missing_weekday_interval_cannot_settle_a_live_forecast(self):
        self.clock_day = "2026-10-16"
        self.market.rows = [row("2026-10-15", 70), row("2026-10-16", 71, True)]
        self.store.build(mode="live")
        self.market.rows.append(row("2026-10-29", 74, True))
        self.clock_day = "2026-10-29"
        result = self.store.settle_live(refresh=False)
        self.assertEqual((result["settled"], result["pending"]), (0, 3))
        self.assertEqual(self.store.load_dataset(mode="live")["labels"], {})
        self.assertTrue(all("data_gap_unverified" in error["message"] for error in result["errors"].values()))

    def test_stale_sources_cannot_create_new_live_settlement(self):
        self.live_rows()
        self.store.build(mode="live")
        self.market.rows.append(row("2026-10-30", 74, True))
        self.clock_day = "2026-10-30"
        self.market.stale = True
        result = self.store.settle_live(refresh=False)
        self.assertEqual(result["settled"], 0)
        self.assertEqual(len(result["errors"]), 3)

    def test_frozen_labels_or_delayed_settlement_tampering_breaks_integrity(self):
        first = self.store.build()
        path = self.store.root / "datasets" / first["dataset_id"] / "labels.json"
        data = json.loads(path.read_text())
        data[next(iter(data))]["high"] += .1
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.load_dataset(first["dataset_id"])
        self.live_rows()
        live = self.store.build(mode="live")
        self.store.settle_live(refresh=False)
        path = self.store.root / "datasets" / live["dataset_id"] / "settlement.json"
        data = json.loads(path.read_text())
        data["items"]["invented"] = {"actual": {"direction": "up"}}
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "integrity"):
            self.store.load_dataset(mode="live")

    def test_flat_direction_and_zero_baseline_are_explicit(self):
        self.market.rows = [row("2026-10-26", 70), row("2026-10-27", 70), row("2026-10-28", 0)]
        for item in self.market.rows:
            item["low"] = min(item["low"], item["close"])
        self.store.build()
        self.assertEqual(self.store.questions()[0]["actual_direction"], "flat")
        self.assertEqual(self.store.questions()[0]["actual_return_pct"], 0)
        self.market.rows = [row("2026-10-27", 0), row("2026-10-28", 1)]
        self.assertEqual(self.store.build()["total_questions"], 0)

    def test_legacy_dataset_remains_readable_but_is_not_the_new_default(self):
        # Construct a minimal authentic v1 identity, independent of local data.
        questions, labels, sources, fingerprints = [{"case_id": "old"}], {"old": 70}, {}, {}
        identity = {"version": "futures-next-trading-day-close-qa-v1", "days_per_contract": 30,
                    "questions": questions, "labels": labels, "sources": fingerprints}
        content = _digest(identity)
        manifest = {"dataset_id": "qa-" + content[:20], "version": identity["version"], "days_per_contract": 30,
                    "sha256": content, "questions_sha256": _digest(questions), "labels_sha256": _digest(labels),
                    "source_snapshots_sha256": _digest(sources), "source_fingerprints": fingerprints}
        manifest["manifest_sha256"] = _digest(manifest)
        path = self.store.root / "datasets" / manifest["dataset_id"]
        for name, value in (("manifest", manifest), ("questions", questions), ("labels", labels), ("source-snapshots", sources)):
            _write(path / (name + ".json"), value)
        _write(self.store.root / "current.json", {"dataset_id": manifest["dataset_id"]})
        self.assertEqual(self.store.load_dataset(manifest["dataset_id"])["labels"], labels)
        self.assertEqual(self.store.summary()["total_questions"], 0)

    def test_empty_catalog_and_invalid_inputs(self):
        self.assertEqual(set(self.store.catalog()), {"backtest", "backtest_v2", "live", "live_history"})
        self.assertEqual(self.store.questions(mode="live"), [])
        for days in (True, 0, -1, 367, "30"):
            with self.assertRaises(ValueError):
                self.store.build(days=days)
        with self.assertRaises(ValueError):
            self.store.build(mode="bad")
        with self.assertRaises(ValueError):
            self.store.questions("bad")
        with self.assertRaises(ValueError):
            self.store.load_dataset("../../secrets")

    def test_new_questions_bind_known_and_forecast_utc_windows_without_target_values(self):
        self.live_rows()
        summary = self.store.build(days=1)
        self.assertEqual(summary["version"], VERSION)
        self.assertIn("v3-utc", VERSION)
        for case in self.store.load_dataset()["questions"]:
            windows = case["time_windows"]
            self.assertEqual(windows["status"], "ready")
            self.assertEqual(windows["timezone"], "UTC")
            self.assertEqual(windows["basis"], "exchange_schedule_reference")
            self.assertFalse(windows["supplier_aggregation_verified"])
            self.assertEqual(windows["known"]["trading_date"], case["input_date"])
            self.assertEqual(windows["forecast"]["trading_date"], case["target_date"])
            self.assertEqual(windows["information_cutoff_utc"], windows["known"]["end_utc"])
            self.assertLess(windows["known"]["end_utc"], windows["forecast"]["start_utc"])
            self.assertNotIn("high", windows["forecast"])
            self.assertNotIn("close", windows["forecast"])
            self.assertIn("UTC+0", case["question"])
            self.assertIn("常规交易时段参考", case["question"])
            self.assertIn("聚合边界未经核实", case["question"])
            for window in (windows["known"], windows["forecast"]):
                self.assertTrue(window["start_utc"].endswith("Z"))
                self.assertTrue(window["end_utc"].endswith("Z"))
                self.assertIn(window["start_utc"][:16].replace("T", " "), case["question"])
                self.assertIn(window["end_utc"][:16].replace("T", " "), case["question"])

    def test_ice_brent_utc_windows_are_distinct_from_nymex_bz_and_include_dst_week(self):
        self.market.rows = [row("2026-10-29", 70), row("2026-10-30", 71, True)]
        self.clock_day = "2026-10-30"
        original = self.market.get

        def ice_source(family, refresh=False):
            result = original(family, refresh)
            if family == "Brent":
                result["contract"].update(code="IB.1:IEU", exchange="ICE")
                result["source"]["timezone"] = "Europe/London"
            return result

        with patch.object(self.market, "get", side_effect=ice_source):
            self.store.build(days=1)
        case = self.store.questions("Brent")[0]
        self.assertEqual(case["exchange"], "ICE")
        self.assertEqual(case["time_windows"]["forecast"]["start_utc"], "2026-10-30T00:00:00Z")
        self.assertEqual(case["time_windows"]["forecast"]["end_utc"], "2026-10-30T22:00:00Z")
        self.assertIn("ice.com/", case["time_windows"]["forecast"]["source_url"])
        wti = self.store.questions("WTI")[0]
        self.assertEqual(wti["time_windows"]["forecast"]["start_utc"], "2026-10-29T22:00:00Z")

    def test_sc_live_target_skips_confirmed_national_holiday_and_has_no_invented_night(self):
        self.clock_day, self.clock_hour = "2026-09-30", 16
        self.market.rows = [row("2026-09-29", 70), row("2026-09-30", 71, True)]
        self.store.build(mode="live")
        case = self.store.questions("SC", mode="live")[0]
        self.assertEqual(case["target_date"], "2026-10-08")
        self.assertEqual(case["time_windows"]["forecast"]["start_utc"], "2026-10-08T01:00:00Z")
        self.assertEqual(case["time_windows"]["forecast"]["end_utc"], "2026-10-08T07:00:00Z")
        self.assertEqual(case["forecast_deadline"], "2026-10-08T00:30:00+00:00")
        self.assertEqual(len(case["time_windows"]["forecast"]["segments"]), 3)
        self.assertTrue(all(item["label"] == "日盘" for item in case["time_windows"]["forecast"]["segments"]))

    def test_sc_question_lists_all_night_and_daily_segments_in_utc(self):
        self.live_rows()
        self.store.build(days=1)
        case = self.store.questions("SC")[0]
        known, forecast = case["time_windows"]["known"], case["time_windows"]["forecast"]
        self.assertEqual(len(known["segments"]), 4)
        for session in (known, forecast):
            for segment in session["segments"]:
                for key in ("start_utc", "end_utc"):
                    self.assertIn(segment[key][:16].replace("T", " "), case["question"])

    def test_special_time_pairs_are_skipped_instead_of_exposing_missing_times(self):
        self.market.rows = [row("2026-09-03", 70), row("2026-09-04", 71),
                            row("2026-09-07", 72), row("2026-09-08", 73)]
        self.store.build(days=30)
        cases = self.store.questions("WTI")
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["input_date"], "2026-09-03")
        self.assertTrue(all(case["time_windows"]["status"] == "ready" for case in self.store.questions()))

    def test_live_unknown_special_schedule_does_not_publish_ambiguous_utc_question(self):
        self.clock_day, self.clock_hour = "2026-11-25", 17
        self.market.rows = [row("2026-11-24", 70), row("2026-11-25", 71, True)]
        self.store.build(mode="live")
        self.assertEqual(self.store.questions("WTI", mode="live"), [])
        info = self.store.summary(mode="live")["families"][0]["contracts"][0]
        self.assertEqual(info["unconfirmed_time_windows_skipped"], 1)
        self.assertIn("特殊交易时段", info["error"])

    def test_verified_sc_holiday_gap_can_build_and_settle_new_version_only(self):
        self.market.rows = [row("2026-09-29", 70), row("2026-09-30", 71, True)]
        self.clock_day, self.clock_hour = "2026-09-30", 16
        live = self.store.build(mode="live")
        self.market.rows.append(row("2026-10-08", 72, True))
        self.clock_day = "2026-10-08"
        result = self.store.settle_live(refresh=False)
        self.assertEqual(result["settled"], 1)
        settled = self.store.questions("SC", mode="live")[0]
        self.assertEqual(settled["actual_target_date"], "2026-10-08")
        self.assertEqual(settled["time_windows"]["forecast"]["trading_date"], "2026-10-08")
        self.store.build(days=1)
        case = self.store.questions("SC")[0]
        self.assertEqual((case["input_date"], case["target_date"]), ("2026-09-30", "2026-10-08"))

    def test_v2_display_decoration_preserves_frozen_questions_hash_files_and_labels(self):
        question = {"case_id": "backtest__brent-main__2026-10-29__2026-10-30", "family": "Brent",
                    "contract_code": "IB.1:IEU", "input_date": "2026-10-29", "target_date": "2026-10-30",
                    "question": "原始日期问句不得修改", "mode": "backtest"}
        questions = [question]
        labels = {question["case_id"]: {"direction": "up", "return_pct": 1, "high": 73, "low": 70,
                                            "close": 72, "target_date": "2026-10-30"}}
        sources = {}
        fingerprints = {"Brent": {"contract": {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}}}
        identity = {"version": "futures-continuous-direction-range-v2", "mode": "backtest", "days_per_contract": 30,
                    "questions": questions, "labels": labels, "sources": fingerprints}
        content = _digest(identity)
        manifest = {"dataset_id": "qa-" + content[:20], "version": identity["version"], "mode": "backtest",
                    "days_per_contract": 30, "sha256": content, "questions_sha256": _digest(questions),
                    "labels_sha256": _digest(labels), "source_snapshots_sha256": _digest(sources), "source_fingerprints": fingerprints}
        manifest["manifest_sha256"] = _digest(manifest)
        path = self.store.root / "datasets" / manifest["dataset_id"]
        for name, value in (("manifest", manifest), ("questions", questions), ("labels", labels), ("source-snapshots", sources)):
            _write(path / (name + ".json"), value)
        _write(self.store._pointer("backtest"), {"dataset_id": manifest["dataset_id"]})
        before = {file.name: file.read_bytes() for file in path.iterdir()}
        displayed = self.store.questions()[0]
        self.assertNotIn("time_windows", displayed)
        self.assertEqual(displayed["question"], question["question"])
        self.assertTrue(displayed["display_time_windows"]["display_only"])
        self.assertEqual(displayed["display_time_windows"]["forecast"]["exchange"], "ICE")
        self.assertEqual(self.store.load_dataset()["questions"], questions)
        self.assertEqual(self.store.load_dataset()["manifest"]["sha256"], content)
        self.assertEqual(before, {file.name: file.read_bytes() for file in path.iterdir()})

    def test_display_helper_is_read_only_and_does_not_guess_unknown_brent_exchange(self):
        question = {"case_id": "old", "family": "Brent", "contract_code": "unknown",
                    "input_date": "2026-10-29", "target_date": "2026-10-30"}
        before = copy.deepcopy(question)
        display = display_time_windows(question)
        self.assertEqual(question, before)
        self.assertEqual(display["status"], "unconfirmed")
        self.assertIsNone(display["forecast"]["start_utc"])
        self.assertIsNone(display_time_windows({"family": "SC"}))


if __name__ == "__main__":
    unittest.main()
