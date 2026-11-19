"""Offline regressions using small excerpts of actual 2026-10-28 responses."""

import copy
import io
import json
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_data import (  # noqa: E402
    CONTRACTS, MAX_RESPONSE_BYTES, MarketData, MarketDataError, aggregate_bars,
    parse_farmbucks, parse_sina,
)


# Exact final rows captured from Farmbucks CLZ26 window=6M / window=5D.
# Public page: https://farmbucks.com/futures/energy/wti-crude
DAILY = {"chartData": {
    "labels": ["2026-10-23", "2026-10-26", "2026-10-27"],
    "data": [103.27, 101.76, 99.9], "volume": [309751.0, 246789.0, 255150.0],
    "open": [99.1, 102.61, 101.29], "high": [103.92, 103.13, 102.82],
    "low": [98.84, 100.33, 99.44], "interval": "1d", "asOf": "2026-10-27",
}}
HOURLY = {"chartData": {
    "labels": ["2026-10-24T00:00:00-04:00", "2026-10-28T03:00:00-04:00", "2026-10-28T04:00:00-04:00", "2026-10-28T05:00:00-04:00"],
    "data": [None, 101.47, 101.73, 101.74], "volume": [None, 8320, 9620, 1496],
    "open": [None, 101.7, 101.47, 101.73], "high": [None, 102.24, 101.73, 101.89],
    "low": [None, 101.46, 101.13, 101.59], "interval": "1h", "asOf": "2026-10-28T05:00:00-04:00",
}}
# Exact SC2701 final 60-minute bar captured from Sina's getFewMinLine.
SINA_HOURLY = '/*<script>not executed</script>*/\nvar _SC2701=([{"d":"2026-10-28 15:00:00","o":"724.400","h":"725.900","l":"723.800","c":"725.100","v":"280","p":"17371"}]);'


class Response(io.BytesIO):
    def __init__(self, payload, declared_length=None):
        encoded = payload.encode("utf-8")
        super().__init__(encoded)
        self.headers = {"Content-Length": str(declared_length if declared_length is not None else len(encoded))}
        self.status = 200


class MarketDataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.market = MarketData(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_real_daily_fixture_and_provenance_cache(self):
        raw = json.dumps(DAILY)
        with patch("market_data.urlopen", side_effect=lambda *args, **kwargs: Response(raw)) as opener:
            result = self.market.get("wti-202612")
            again = self.market.get("wti-202612")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(result["bars"][-1]["close"], 99.9)
        self.assertEqual(result["data_as_of"], "2026-10-27")
        self.assertFalse(result["contract"]["isDemo"])
        self.assertTrue(again["cache"]["hit"])
        self.assertEqual(result["source"]["provider_symbol"], "CLZ26")
        self.assertEqual(result["source"]["url"], "https://farmbucks.com/futures/energy/wti-crude")
        self.assertTrue(result["source"]["request_url"].endswith("/price-history"))
        entry = json.loads((Path(self.temporary.name) / "wti-202612-1d.json").read_text())
        self.assertEqual(entry["raw_response"], raw)
        self.assertEqual(entry["kind"], "real_market_response")
        self.assertEqual(entry["normalized"]["bars"], result["bars"])

    def test_null_hourly_slots_are_removed_not_interpolated(self):
        raw = copy.deepcopy(HOURLY)
        # A provider's asOf can describe a response slot beyond its last trade.
        raw["chartData"]["asOf"] = "2026-10-28T23:00:00-04:00"
        with patch("market_data.urlopen", side_effect=lambda *args, **kwargs: Response(json.dumps(raw))):
            result = self.market.get("wti-202612", "1h")
        self.assertEqual(len(result["bars"]), 3)
        self.assertEqual(result["bars"][0]["close"], 101.47)
        self.assertEqual(result["data_as_of"], "2026-10-28T09:00:00+00:00")
        self.assertEqual(result["source_as_of"], "2026-10-28T23:00:00-04:00")
        self.assertIn("未作插值", " ".join(result["notes"]))

    def test_sina_jsonp_and_shanghai_timezone(self):
        with patch("market_data.urlopen", return_value=Response(SINA_HOURLY)) as opener:
            result = self.market.get("sc-202701", "1h")
        self.assertEqual(result["bars"][0]["close"], 725.1)
        self.assertEqual(result["bars"][0]["open_interest"], 17371)
        self.assertEqual(result["data_as_of"], "2026-10-28T07:00:00+00:00")
        self.assertEqual(result["bars"][0]["trading_date"], "2026-10-28")
        self.assertIn("getFewMinLine", opener.call_args[0][0].full_url)
        self.assertIn("symbol=SC2701", opener.call_args[0][0].full_url)
        self.assertEqual(result["source"]["timezone"], "Asia/Shanghai")

    def test_saved_sina_raw_recovers_open_interest_without_network(self):
        with patch("market_data.urlopen", return_value=Response(SINA_HOURLY)):
            result = self.market.get("sc-202701", "1d")
        path = Path(self.temporary.name) / "sc-202701-1d.json"
        entry = json.loads(path.read_text())
        for bar in entry["normalized"]["bars"]:
            bar.pop("open_interest")
        path.write_text(json.dumps(entry))
        with patch("market_data.urlopen", side_effect=AssertionError("must reuse saved raw")):
            recovered = self.market.get("sc-202701", "1d")
        self.assertEqual(recovered["bars"][0]["open_interest"], 17371)
        self.assertEqual(recovered["fetched_at"], result["fetched_at"])
        self.assertTrue(recovered["cache"]["hit"])

    def test_daily_cache_dynamically_decorates_utc_sessions_without_moving_date_anchors(self):
        with patch("market_data.urlopen", return_value=Response(json.dumps(DAILY))):
            first = self.market.get("wti-202612")
        path = Path(self.temporary.name) / "wti-202612-1d.json"
        entry = json.loads(path.read_text())
        entry["normalized"].pop("session_rules")
        entry["normalized"].pop("display_timezone")
        for bar in entry["normalized"]["bars"]:
            bar["session"] = {"label": "old wrong metadata"}
        path.write_text(json.dumps(entry))
        with patch("market_data.urlopen", side_effect=AssertionError("must not fetch")):
            current = self.market.get("wti-202612")
        self.assertEqual(current["display_timezone"], "UTC")
        self.assertEqual(current["bars"][-1]["time"], first["bars"][-1]["time"])
        self.assertEqual(current["bars"][-1]["trading_date"], "2026-10-27")
        self.assertEqual(current["bars"][-1]["session"]["start_utc"], "2026-10-26T22:00:00Z")
        self.assertEqual(current["bars"][-1]["session"]["end_utc"], "2026-10-27T21:00:00Z")
        self.assertEqual(current["fetched_at"], first["fetched_at"])
        self.assertEqual(json.loads(path.read_text())["raw_response"], entry["raw_response"])

    def test_weekly_monthly_sessions_are_aggregated_and_old_cache_is_upgraded(self):
        with patch("market_data.urlopen", return_value=Response(json.dumps(DAILY))):
            weekly = self.market.get("wti-202612", "1w")
            monthly = self.market.get("wti-202612", "1M")
        self.assertEqual(weekly["bars"][-1]["session"]["kind"], "aggregated_schedule")
        self.assertEqual(weekly["bars"][-1]["session"]["end_utc"], "2026-10-27T21:00:00Z")
        self.assertEqual(monthly["bars"][0]["session"]["trading_date_end"], "2026-10-27")
        path = Path(self.temporary.name) / "wti-202612-1w.json"
        entry = json.loads(path.read_text())
        for bar in entry["normalized"]["bars"]:
            bar.pop("session")
        path.write_text(json.dumps(entry))
        with patch("market_data.urlopen", side_effect=AssertionError("must not fetch")):
            upgraded = self.market.get("wti-202612", "1w")
        self.assertEqual(upgraded["bars"][-1]["session"], weekly["bars"][-1]["session"])

    def test_open_interest_aggregation_is_final_snapshot_and_missing_stays_missing(self):
        bars, _, _ = parse_farmbucks(json.dumps(DAILY), "1d")
        bars[0]["open_interest"] = 100
        bars[1]["open_interest"] = 200
        bars[2]["open_interest"] = 150
        self.assertEqual(aggregate_bars(bars, "1M")[0]["open_interest"], 150)
        bars[-1]["open_interest"] = None
        self.assertIsNone(aggregate_bars(bars, "1M")[0]["open_interest"])

    def test_weekly_monthly_aggregation_preserves_actual_ohlcv(self):
        with patch("market_data.urlopen", side_effect=lambda *args, **kwargs: Response(json.dumps(DAILY))) as opener:
            weekly = self.market.get("wti-202612", "1w")
            monthly = self.market.get("wti-202612", "1M")
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(len(weekly["bars"]), 2)
        self.assertEqual(weekly["bars"][-1]["open"], 102.61)
        self.assertEqual(weekly["bars"][-1]["close"], 99.9)
        self.assertEqual(monthly["bars"][0]["high"], 103.92)
        self.assertEqual(monthly["bars"][0]["low"], 98.84)
        self.assertEqual(monthly["bars"][0]["volume"], 309751 + 246789 + 255150)
        self.assertTrue((Path(self.temporary.name) / "wti-202612-1w.json").exists())
        raw = copy.deepcopy(DAILY)
        raw["chartData"]["volume"][1] = None
        bars, _, _ = parse_farmbucks(json.dumps(raw), "1d")
        self.assertIsNone(aggregate_bars(bars, "1M")[0]["volume"])

    def test_stale_fallback_is_true_cached_data_and_errors_are_throttled(self):
        with patch("market_data.urlopen", return_value=Response(json.dumps(DAILY))):
            first = self.market.get("wti-202612")
        self.market.ttl_seconds = 0
        with patch("market_data.urlopen", side_effect=URLError("offline")) as opener:
            stale = self.market.get("wti-202612")
            again = self.market.get("wti-202612", refresh=True)
        self.assertEqual(opener.call_count, 1)
        self.assertTrue(stale["cache"]["stale"])
        self.assertEqual(stale["bars"], first["bars"])
        self.assertEqual(again["bars"], first["bars"])
        self.assertIn("不是最新行情", " ".join(stale["notes"]))

    def test_no_cache_failure_stays_explicit_and_is_not_a_price(self):
        with patch("market_data.urlopen", side_effect=URLError("offline")) as opener:
            for _ in range(2):
                with self.assertRaises(MarketDataError) as error:
                    self.market.get("sc-202702", "1d")
                self.assertEqual(error.exception.status, 502)
                self.assertEqual(error.exception.contract_id, "sc-202702")
                self.assertEqual(error.exception.source["name"], "新浪财经")
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(list(Path(self.temporary.name).glob("*.json")))

    def test_brent_uses_exact_nymex_month_not_ice_or_continuous(self):
        with patch("market_data.urlopen", return_value=Response(json.dumps(DAILY))) as opener:
            result = self.market.get("brent-202701")
        request = opener.call_args[0][0]
        self.assertEqual(result["contract"]["exchange"], "NYMEX")
        self.assertEqual(result["contract"]["code"], "BZF27")
        self.assertIn("/brent-crude/price-history", request.full_url)
        self.assertIn(b"contract=BZF27", request.data)
        self.assertIn(b"window=6M", request.data)

    def test_concurrent_first_reads_fetch_once(self):
        def delayed(*args, **kwargs):
            time.sleep(.03)
            return Response(json.dumps(DAILY))
        with patch("market_data.urlopen", side_effect=delayed) as opener:
            with ThreadPoolExecutor(max_workers=6) as pool:
                results = list(pool.map(lambda _: self.market.get("wti-202612"), range(6)))
        self.assertEqual(opener.call_count, 1)
        self.assertEqual(sum(not result["cache"]["hit"] for result in results), 1)

    def test_scheduled_refresh_fetches_every_real_series_even_with_fresh_cache(self):
        def fixture(source):
            if source["provider"] == "sina":
                return SINA_HOURLY
            return json.dumps(HOURLY if source["interval"] == "1h" else DAILY)

        with patch.object(self.market, "_request", side_effect=fixture) as requester:
            first = self.market.refresh_all()
            second = self.market.refresh_all()
        self.assertEqual(requester.call_count, 36)
        self.assertEqual(first["status"], "success")
        self.assertEqual(second["updated"], 18)
        self.assertEqual(second["contracts_updated"], 9)
        self.assertEqual(second["failures"], [])
        self.assertEqual({(item["contract_id"], item["period"]) for item in second["series"]},
                         {(contract, period) for contract in CONTRACTS for period in ("1d", "1h")})
        for item in second["series"]:
            self.assertEqual(item["status"], "updated")
            self.assertGreater(item["bars"], 0)
            self.assertTrue(item["fetched_at"])

    def test_scheduled_refresh_marks_stale_as_failure_and_continues_other_series(self):
        with patch.object(self.market, "_request", return_value=json.dumps(DAILY)):
            previous = self.market.get("wti-202612")
        path = Path(self.temporary.name) / "wti-202612-1d.json"
        raw_cache = path.read_bytes()

        def partly_offline(source):
            if source["provider_symbol"] == "CLZ26":
                raise MarketDataError("供应商不可用")
            if source["provider"] == "sina":
                return SINA_HOURLY
            return json.dumps(HOURLY if source["interval"] == "1h" else DAILY)

        with patch.object(self.market, "_request", side_effect=partly_offline):
            result = self.market.refresh_all()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["updated"], 16)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["contracts_updated"], 8)
        self.assertEqual(path.read_bytes(), raw_cache)
        self.assertEqual(result["failures"][0]["fetched_at"], previous["fetched_at"])
        self.assertIn("真实缓存", result["failures"][0]["error"])
        self.assertEqual(result["failures"][1]["error"], "供应商不可用")

    def test_scheduled_refresh_reports_total_failure_without_writing_prices(self):
        with patch.object(self.market, "_request", side_effect=MarketDataError("离线")):
            result = self.market.refresh_all()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed"], 18)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["contracts_updated"], 0)
        self.assertFalse(list(Path(self.temporary.name).glob("*.json")))

    def test_new_daily_cache_rebuilds_weekly_and_monthly_without_another_fetch(self):
        changed = copy.deepcopy(DAILY)
        changed["chartData"]["data"][-1] = 100.0
        with patch.object(self.market, "_request", return_value=json.dumps(DAILY)):
            weekly = self.market.get("wti-202612", "1w")
            monthly = self.market.get("wti-202612", "1M")
        with patch.object(self.market, "_request", return_value=json.dumps(changed)) as requester:
            self.market.get("wti-202612", "1d", refresh=True)
            new_weekly = self.market.get("wti-202612", "1w")
            new_monthly = self.market.get("wti-202612", "1M")
        self.assertEqual(requester.call_count, 1)
        self.assertEqual(weekly["bars"][-1]["close"], 99.9)
        self.assertEqual(monthly["bars"][-1]["close"], 99.9)
        self.assertEqual(new_weekly["bars"][-1]["close"], 100.0)
        self.assertEqual(new_monthly["bars"][-1]["close"], 100.0)
        self.assertFalse(new_weekly["cache"]["stale"])
        stored = json.loads((Path(self.temporary.name) / "wti-202612-1w.json").read_text())
        self.assertEqual(json.loads(stored["raw_response"])["chartData"]["data"][-1], 100.0)

    def test_rejects_invalid_input_and_invalid_provider_payloads(self):
        with patch("market_data.urlopen") as opener:
            with self.assertRaises(MarketDataError):
                self.market.get("../../arbitrary")
            with self.assertRaises(MarketDataError):
                self.market.get("wti-202612", "5m")
            opener.assert_not_called()
        with self.assertRaises(MarketDataError):
            parse_sina('var x=({"__ERROR":3});', "1d")
        invalid = copy.deepcopy(DAILY)
        invalid["chartData"]["interval"] = "1h"
        with self.assertRaises(MarketDataError):
            parse_farmbucks(json.dumps(invalid), "1d")
        with patch("market_data.urlopen", return_value=Response("{}", declared_length=MAX_RESPONSE_BYTES + 1)):
            with self.assertRaises(MarketDataError) as error:
                self.market.get("wti-202612")
        self.assertIn("10 MB", str(error.exception))


if __name__ == "__main__":
    unittest.main()
