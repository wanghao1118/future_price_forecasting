"""Contract identity, option totals, dates and real-response cache regressions."""

import io
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_supplemental import SupplementalData, parse_farmbucks_quote, parse_ine


SOURCE = {"name": "上海国际能源交易中心", "url": "https://www.ine.cn/data/test.dat"}
SC = {"familyId": "SC", "code": "SC2612"}
WTI = {"familyId": "WTI", "code": "CLZ26"}
DAY = "2026-10-29"


def official(rows, kind="options", day="20261029"):
    return json.dumps({"report_date": day, "o_cursor" if kind == "members" else "o_curinstrument": rows})


# Actual end-of-day SC2612 contract fields from the official 2026-10-29 file.
FUTURES = {"PRODUCTID": "sc_f", "DELIVERYMONTH": "2612", "OPENINTEREST": 27626, "OPENINTERESTCHG": -2399}


def option(symbol, side, oi):
    return {"INSTRUMENTID": symbol + side + "700", "UNDERLYINGINSTRID": symbol,
            "OPTIONSTYPE": "1" if side == "C" else "2", "OPENINTEREST": oi}


class SupplementalTests(unittest.TestCase):
    def test_official_open_interest_and_previous_excludes_tas_and_other_month(self):
        rows = [FUTURES, {**FUTURES, "PRODUCTID": "sc_tas", "OPENINTEREST": 999999},
                {**FUTURES, "DELIVERYMONTH": "2701", "OPENINTEREST": 777}]
        result = parse_ine(official(rows, "futures"), "futures", "SC2612", DAY, SOURCE)
        self.assertEqual(result["value"], 27626)
        self.assertEqual(result["previous"], 30025)
        self.assertEqual(result["as_of"], DAY)

    def test_official_date_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "日期"):
            parse_ine(official([FUTURES], "futures", "20261030"), "futures", "SC2612", DAY, SOURCE)

    def test_pcr_uses_only_exact_underlying_month_and_all_strikes(self):
        rows = [option("sc2612", "C", 20), option("sc2612", "P", 30),
                {**option("sc2612", "C", 4), "INSTRUMENTID": "sc2612C710"},
                option("sc2701", "P", 999), {"UNDERLYINGINSTRID": "小计", "OPENINTEREST": 9999}]
        result = parse_ine(official(rows), "options", "SC2612", DAY, SOURCE)
        self.assertEqual((result["puts"], result["calls"]), (30, 24))
        self.assertEqual(result["basis"], "open_interest")
        self.assertIn("SC2612", result["scope"])

    def test_partial_options_cannot_be_summed_as_complete(self):
        result = parse_ine(official([option("sc2612", "C", 20), option("sc2612", "P", "")]),
                           "options", "SC2612", DAY, SOURCE)
        self.assertEqual(result["status"], "unavailable")
        self.assertIsNone(result["puts"])

    def test_no_matching_options_is_missing_not_zero(self):
        result = parse_ine(official([option("sc2701", "C", 20), option("sc2701", "P", 30)]),
                           "options", "SC2612", DAY, SOURCE)
        self.assertIsNone(result["puts"])
        self.assertIsNone(result["calls"])

    def test_members_exclude_subtotals_and_other_month(self):
        rows = [{"INSTRUMENTID": "sc2612", "RANK": 1, "CJ2": 200, "CJ3": 150},
                {"INSTRUMENTID": "sc2612", "RANK": 2, "CJ2": 100, "CJ3": 120},
                {"INSTRUMENTID": "sc2612", "RANK": 999, "CJ2": 300, "CJ3": 270},
                {"INSTRUMENTID": "sc2701", "RANK": 1, "CJ2": 900, "CJ3": 900}]
        result = parse_ine(official(rows, "members"), "members", "SC2612", DAY, SOURCE)
        self.assertEqual((result["long"], result["short"]), (300, 270))
        self.assertIn("两侧会员可能不同", result["scope"])

    def test_missing_member_contract_does_not_become_zero(self):
        result = parse_ine(official([], "members"), "members", "SC2612", DAY, SOURCE)
        self.assertIsNone(result["long"])
        self.assertIsNone(result["short"])

    def test_farm_quote_requires_exact_contract_and_keeps_oi_date_unknown(self):
        raw = json.dumps({"ok": True, "symbol": "CLZ26", "openInterest": "267,773", "lastTrade": "Oct 30, 03:06"})
        result = parse_farmbucks_quote(raw, "CLZ26", SOURCE)
        self.assertEqual(result["value"], 267773)
        self.assertIsNone(result["previous"])
        self.assertIsNone(result["as_of"])
        self.assertIn("未披露持仓统计日期", result["detail"])
        with self.assertRaisesRegex(ValueError, "合约"):
            parse_farmbucks_quote(raw, "CLF27", SOURCE)

    def test_real_response_cache_reused_across_sc_months_and_disclosure_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)

            def request(source, body=None):
                if "/pm" in source["url"]:
                    return official([], "members")
                if "/option/" in source["url"]:
                    return official([option("sc2612", "C", 20), option("sc2612", "P", 30)])
                return official([FUTURES], "futures")

            with patch.object(client, "_request", side_effect=request) as requester:
                first = client.get(SC, DAY)
                second = client.get({"familyId": "SC", "code": "SC2701"}, DAY)
            self.assertEqual(requester.call_count, 3)
            self.assertEqual(first["pcr"]["puts"], 30)
            self.assertIsNone(second["pcr"]["puts"])
            self.assertIn("10万手", first["member_positions"]["detail"])
            entries = [json.loads(path.read_text()) for path in Path(directory).glob("*.json")]
            self.assertEqual(len(entries), 3)
            self.assertTrue(all(entry["kind"] == "real_supplemental_response" and entry["raw_response"] for entry in entries))

    def test_endpoint_failure_is_independent_and_throttled(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)

            def request(source, body=None):
                if "/pm" in source["url"]:
                    raise URLError("offline")
                if "/option/" in source["url"]:
                    return official([option("sc2612", "C", 20), option("sc2612", "P", 30)])
                return official([FUTURES], "futures")

            with patch.object(client, "_request", side_effect=request) as requester:
                first = client.get(SC, DAY)
                second = client.get(SC, DAY, refresh=True)
            self.assertEqual(requester.call_count, 5)
            self.assertEqual(first["pcr"]["puts"], 30)
            self.assertEqual(second["open_interest"]["value"], 27626)
            self.assertIn("offline", first["member_positions"]["detail"])
            self.assertNotIn("门槛", first["member_positions"]["detail"])

    def test_stale_quote_retains_original_value_date_and_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory, ttl_seconds=0)
            raw = json.dumps({"ok": True, "symbol": "CLZ26", "openInterest": "267,773"})
            with patch.object(client, "_request", return_value=raw):
                first = client.get(WTI, DAY)
            with patch.object(client, "_request", side_effect=URLError("offline")):
                stale = client.get(WTI, DAY)
            self.assertEqual(stale["open_interest"]["value"], first["open_interest"]["value"])
            self.assertEqual(stale["open_interest"]["fetched_at"], first["open_interest"]["fetched_at"])
            self.assertEqual(stale["open_interest"]["status"], "stale")
            self.assertIsNone(stale["open_interest"]["as_of"])
            self.assertIn("真实缓存", stale["open_interest"]["detail"])

    def test_history_tracks_exact_dates_months_and_does_not_download_oi_again(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)

            def request(source, body=None):
                day = source["url"][-12:-4]
                if "/pm" in source["url"]:
                    return official([], "members", day)
                self.assertIn("/option/", source["url"])
                puts = 28 if day == "20261028" else 29
                return official([option("sc2612", "C", 20), option("sc2612", "P", puts),
                                 option("sc2701", "C", 2), option("sc2701", "P", 3)], day=day)

            with patch.object(client, "_request", side_effect=request) as requester:
                history = client.get_history(SC, ["2026-10-29", "2026-10-28", "2026-10-29"])
                other_month = client.get_history({"familyId": "SC", "code": "SC2701"}, ["2026-10-28", "2026-10-29"])
            self.assertEqual(requester.call_count, 4)
            self.assertEqual([item["as_of"] for item in history["snapshots"]], ["2026-10-28", "2026-10-29"])
            self.assertEqual([item["pcr"]["puts"] for item in history["snapshots"]], [28, 29])
            self.assertEqual([item["pcr"]["puts"] for item in other_month["snapshots"]], [3, 3])
            self.assertTrue(all(item["pcr"]["as_of"] == item["as_of"] for item in history["snapshots"]))
            self.assertTrue(all(item["open_interest"]["value"] is None for item in history["snapshots"]))

    def test_history_keeps_single_day_failure_as_gap(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)

            def request(source, body=None):
                day = source["url"][-12:-4]
                if "/pm" in source["url"]:
                    return official([], "members", day)
                if day == "20261028":
                    # Wrong-day response must never be plotted as October 28.
                    return official([option("sc2612", "C", 20), option("sc2612", "P", 30)])
                return official([option("sc2612", "C", 20), option("sc2612", "P", 30)], day=day)

            with patch.object(client, "_request", side_effect=request):
                result = client.get_history(SC, ["2026-10-28", "2026-10-29"])
            self.assertEqual(result["snapshots"][0]["pcr"]["status"], "unavailable")
            self.assertIsNone(result["snapshots"][0]["pcr"].get("puts"))
            self.assertEqual(result["snapshots"][1]["pcr"]["puts"], 30)
            self.assertTrue(any("1个日文件请求失败" in note for note in result["notes"]))

    def test_history_dates_are_bounded_and_validated_before_network(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            with patch.object(client, "_request") as requester:
                for invalid in [[DAY] * 1001, ["2026-10-99"], ["20261029"], ["2026-10-29?x=1"], "2026-10-29"]:
                    with self.subTest(invalid=invalid):
                        with self.assertRaises(ValueError):
                            client.get_history(SC, invalid)
                self.assertEqual(client.get_history(SC, []), {"snapshots": [], "notes": []})
            requester.assert_not_called()

    def test_undated_nymex_quote_does_not_supply_historical_oi(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            with patch.object(client, "_request") as requester:
                result = client.get_history(WTI, [DAY])
            requester.assert_not_called()
            self.assertEqual(result["snapshots"], [])
            self.assertIn("统计日期", " ".join(result["notes"]))

    def test_background_history_returns_immediately_deduplicates_months_and_finishes_missing_members(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            gate = threading.Event()

            def request(source, body=None):
                self.assertTrue(gate.wait(3))
                if "/pm" in source["url"]:
                    return official([], "members")
                return official([option("sc2612", "C", 20), option("sc2612", "P", 30),
                                 option("sc2701", "C", 2), option("sc2701", "P", 3)])

            with patch.object(client, "_request", side_effect=request) as requester:
                first = client.get_history(SC, [DAY], background=True)
                second = client.get_history({"familyId": "SC", "code": "SC2701"}, [DAY], background=True)
                self.assertTrue(first["pending"])
                self.assertTrue(second["pending"])
                self.assertEqual(len(client._history_jobs), 2)
                gate.set()
                for job in list(client._history_jobs.values()):
                    job.result(timeout=3)
                finished = client.get_history(SC, [DAY], background=True)
                other = client.get_history({"familyId": "SC", "code": "SC2701"}, [DAY], background=True)
            self.assertFalse(finished["pending"])
            self.assertEqual(finished["completed_dates"], 1)
            self.assertEqual(finished["snapshots"][0]["member_positions"]["status"], "unavailable")
            self.assertEqual(finished["snapshots"][0]["pcr"]["puts"], 30)
            self.assertEqual(other["snapshots"][0]["pcr"]["puts"], 3)
            self.assertEqual(requester.call_count, 2)
            client.close(wait=True)

    def test_background_failed_request_finishes_with_gap_without_infinite_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            with patch.object(client, "_request", side_effect=URLError("offline")) as requester:
                client.get_history(SC, [DAY], background=True)
                for job in list(client._history_jobs.values()):
                    job.result(timeout=3)
                result = client.get_history(SC, [DAY], background=True)
                again = client.get_history(SC, [DAY], background=True)
            self.assertFalse(result["pending"])
            self.assertFalse(again["pending"])
            self.assertEqual(requester.call_count, 2)
            self.assertIsNone(result["snapshots"][0]["pcr"].get("puts"))
            self.assertTrue(any("请求失败" in note for note in result["notes"]))
            client.close(wait=True)

    def test_background_views_share_a_single_pool_of_at_most_six_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            gate, six_started = threading.Event(), threading.Event()
            lock = threading.Lock()
            active = [0]
            peak = [0]

            def request(source, body=None):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                    if active[0] == 6:
                        six_started.set()
                self.assertTrue(gate.wait(3))
                day = source["url"][-12:-4]
                with lock:
                    active[0] -= 1
                return official([], "members" if "/pm" in source["url"] else "options", day)

            with patch.object(client, "_request", side_effect=request):
                client.get_history(SC, ["2026-10-{:02d}".format(day) for day in range(1, 5)], background=True)
                executor = client._history_executor
                client.get_history(SC, ["2026-10-{:02d}".format(day) for day in range(5, 9)], background=True)
                self.assertIs(client._history_executor, executor)
                self.assertTrue(six_started.wait(3))
                self.assertEqual(peak[0], 6)
                gate.set()
                for job in list(client._history_jobs.values()):
                    job.result(timeout=3)
            client.close(wait=True)

    def test_background_reuses_closed_day_cache_beyond_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory, ttl_seconds=0)

            def request(source, body=None):
                return official([], "members" if "/pm" in source["url"] else "options")

            with patch.object(client, "_request", side_effect=request):
                client.get_history(SC, [DAY])
            with patch.object(client, "_request") as requester:
                result = client.get_history(SC, [DAY], background=True)
            requester.assert_not_called()
            self.assertFalse(result["pending"])
            client.close(wait=True)

    def test_close_cancels_queued_downloads_and_rejects_new_history_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            client = SupplementalData(directory)
            gate, started = threading.Event(), threading.Event()

            def request(source, body=None):
                started.set()
                self.assertTrue(gate.wait(3))
                day = source["url"][-12:-4]
                return official([], "members" if "/pm" in source["url"] else "options", day)

            with patch.object(client, "_request", side_effect=request):
                client.get_history(SC, ["2026-10-{:02d}".format(day) for day in range(1, 9)], background=True)
                self.assertTrue(started.wait(3))
                jobs = list(client._history_jobs.values())
                client.close()
                self.assertTrue(any(job.cancelled() for job in jobs))
                with self.assertRaisesRegex(RuntimeError, "已关闭"):
                    client.get_history(SC, [DAY], background=True)
                self.assertEqual(list(client._history_jobs.values()), jobs)
                gate.set()
                client.close(wait=True)
            self.assertTrue(all(job.done() for job in jobs))


if __name__ == "__main__":
    unittest.main()
