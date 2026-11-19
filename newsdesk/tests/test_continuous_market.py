"""Vendor continuous OHLC parsing, completed sessions and honest cache fallback."""

from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.continuous_market import ContinuousMarketData, mark_complete, parse_ft
from newsdesk.market_data import MarketDataError


def ft_response(high="105.20"):
    return json.dumps({"data": {}, "html": '<tr><td><span class="mod-ui-hide-small-below">Wednesday, October 28, 2026</span><span>Oct 28</span></td>'
                       '<td>101.00</td><td>'+high+'</td><td>99.10</td><td>103.50</td>'
                       '<td><span class="mod-ui-hide-small-below">123,456</span><span>123.46k</span></td></tr>'})


class ContinuousMarketTests(unittest.TestCase):
    def test_ft_exact_ohlc_and_unshortened_volume(self):
        rows, notes = parse_ft(ft_response())
        self.assertEqual(rows[0]["trading_date"], "2026-10-28")
        self.assertEqual(rows[0]["volume"], 123456)
        self.assertEqual(rows[0]["close"], 103.5)
        self.assertEqual(notes, [])
        with self.assertRaises(MarketDataError):
            parse_ft(ft_response(high="100.0"))

    def test_running_session_not_promoted_to_completed_merely_because_bar_exists(self):
        bars = [{"trading_date":"2026-10-29"}]
        for family, before, after in [
            ("SC", "2026-10-29T07:29:59+00:00", "2026-10-29T07:30:00+00:00"),
            ("WTI", "2026-10-29T21:29:59+00:00", "2026-10-29T21:30:00+00:00"),
            ("Brent", "2026-10-29T23:29:59+00:00", "2026-10-29T23:30:00+00:00"),
        ]:
            self.assertFalse(mark_complete(bars, family, datetime.fromisoformat(before))[0][0]["is_complete"])
            self.assertTrue(mark_complete(bars, family, datetime.fromisoformat(after))[0][0]["is_complete"])

    def test_foreign_symbols_are_actual_vendor_futures_series_not_sina_cfd(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ContinuousMarketData(directory)
            for family in ("WTI", "Brent"):
                source = provider._source(family)
                self.assertEqual(source["provider"], "ft_continuous_futures")
                self.assertIn("markets.ft.com", source["endpoint"])
                self.assertNotIn("GlobalFuturesService", source["endpoint"])
            self.assertIn("symbol=SC0", provider._source("SC")["endpoint"])

    def test_failed_refresh_returns_only_marked_real_cache_and_detects_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            provider = ContinuousMarketData(directory)
            with patch.object(provider, "_request", return_value=ft_response()), patch("newsdesk.continuous_market.utc_now", return_value=datetime(2026,10,29,12,tzinfo=timezone.utc)):
                original = provider.get("WTI")
            with patch.object(provider, "_request", side_effect=MarketDataError("upstream unavailable")):
                stale = provider.get("WTI", refresh=True)
            self.assertTrue(stale["cache"]["stale"])
            self.assertEqual(stale["bars"], original["bars"])
            path = Path(directory) / "wti-main-1d.json"
            record = json.loads(path.read_text())
            record["normalized"]["bars"][0]["close"] = 999
            path.write_text(json.dumps(record))
            self.assertIsNone(provider._read("WTI"))


if __name__ == "__main__":
    unittest.main()
