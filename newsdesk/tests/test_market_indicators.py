"""Indicator arithmetic, incomplete data and contract/scope regressions."""

import copy
from datetime import datetime, timedelta, timezone
import unittest

from newsdesk.market_data import CONTRACTS, MarketDataError
from newsdesk.market_indicators import MarketIndicators


def daily_payload(size=12):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    bars = [{"time": int((start + timedelta(days=i)).timestamp() * 1000),
             "trading_date": (start + timedelta(days=i)).date().isoformat(),
             "open": 100 + i, "high": 102 + i, "low": 98 + i,
             "close": 101 + i, "volume": 1000 + 100 * i,
             "open_interest": 10000 + 200 * i} for i in range(size)]
    return {"contract": copy.deepcopy(CONTRACTS["sc-202612"]), "bars": bars,
            "period": "1d", "data_as_of": bars[-1]["trading_date"],
            "source": {"name": "测试真实数据接口", "url": "https://example.com/contract"},
            "cache": {"hit": True, "stale": False}, "fetched_at": "2026-10-13T00:00:00+00:00"}


class Market:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return copy.deepcopy(self.payload)


class Supplement:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def get(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return copy.deepcopy(self.result)


class HistorySupplement(Supplement):
    def __init__(self, snapshots=None, latest=None):
        super().__init__(latest or {})
        self.snapshots = snapshots or []
        self.history_calls = []

    def get_history(self, *args, **kwargs):
        self.history_calls.append((args, kwargs))
        return {"snapshots": copy.deepcopy(self.snapshots), "notes": []}


class MarketIndicatorTests(unittest.TestCase):
    def make(self, payload=None, supplemental=None):
        market = Market(payload or daily_payload())
        result = MarketIndicators(market, supplemental).get("sc-202612", refresh=True)
        return result, {row["id"]: row for row in result["rows"]}, market

    def history(self, payload=None, supplemental=None, days=30):
        market = Market(payload or daily_payload())
        result = MarketIndicators(market, supplemental).get("sc-202612", history=True, days=days)
        return result, {row["id"]: row for row in result["rows"]}

    def test_daily_arithmetic_and_exact_contract_are_explicit(self):
        result, rows, market = self.make()
        self.assertEqual(market.calls, [(("sc-202612", "1d"), {"refresh": True})])
        self.assertEqual(result["contract"]["code"], "SC2612")
        self.assertEqual(len(rows), 18)
        self.assertEqual(rows["price_trend"]["value"], "上行 +8.74%")
        self.assertIn("最近10根日K首尾收盘", rows["price_trend"]["detail"])
        self.assertIn("日变动+1.00（+0.90%）", rows["price_trend"]["detail"])
        self.assertEqual(rows["volume"]["value"], "2,100 手")
        self.assertIn("近10日日均量1,650手", rows["volume"]["detail"])
        self.assertEqual(rows["price_volume"]["value"], "放量上涨")
        self.assertEqual(rows["open_interest"]["value"], "12,200 手")
        self.assertIn("+200.00（+1.67%）", rows["open_interest"]["detail"])
        # Last ten bars span low=100, high=113; latest close=112.
        self.assertEqual(rows["range_position"]["value"], "92.3%")
        self.assertEqual(rows["range_position"]["status"], "available")
        self.assertEqual(rows["member_ratio"]["value"], "暂无数据")
        self.assertEqual(rows["rsi14"]["status"], "unavailable")

    def test_member_sample_and_pcr_have_independent_dates_and_scope(self):
        source = {"name": "INE", "url": "https://www.ine.cn/"}
        supplement = Supplement({
            "member_positions": {"long": 150, "short": 100, "as_of": "2026-10-10",
                                 "scope": "SC2612前20名会员", "source": source},
            "pcr": {"puts": 40, "calls": 100, "basis": "持仓量", "as_of": "2026-10-11",
                    "scope": "SC2612对应期权全行权价", "source": source}})
        result, rows, _ = self.make(supplemental=supplement)
        self.assertEqual(supplement.calls[0][0][0]["code"], "SC2612")
        self.assertEqual(supplement.calls[0][1], {"as_of": result["as_of"], "refresh": True})
        self.assertEqual(rows["member_ratio"]["value"], "1.500")
        self.assertEqual(rows["member_ratio"]["as_of"], "2026-10-10")
        self.assertIn("前20名会员", rows["member_ratio"]["detail"])
        self.assertEqual(rows["member_net"]["value"], "+50 手（净多）")
        self.assertEqual(rows["pcr"]["value"], "0.400")
        self.assertIn("持仓量口径", rows["pcr"]["detail"])
        self.assertEqual(rows["pcr"]["source"], source)

    def test_missing_values_are_not_zero_and_no_partial_window_is_claimed_complete(self):
        payload = daily_payload(3)
        payload["bars"][-1]["volume"] = None
        payload["bars"][-1]["open_interest"] = None
        _, rows, _ = self.make(payload)
        self.assertEqual(rows["volume"]["value"], "暂无数据")
        self.assertEqual(rows["open_interest"]["value"], "暂无数据")
        self.assertEqual(rows["price_volume"]["status"], "unavailable")
        self.assertEqual(rows["price_trend"]["status"], "partial")
        self.assertEqual(rows["range_position"]["status"], "partial")
        self.assertIn("不足10根", rows["range_position"]["detail"])

    def test_zero_denominators_and_zero_numerators_are_distinct(self):
        supplement = Supplement({"member_positions": {"long": 0, "short": 100},
                                 "pcr": {"puts": 0, "calls": 100, "basis": "成交量"}})
        _, rows, _ = self.make(supplemental=supplement)
        self.assertEqual(rows["member_ratio"]["value"], "0.000")
        self.assertEqual(rows["member_net"]["value"], "-100 手（净空）")
        self.assertEqual(rows["pcr"]["value"], "0.000")
        supplement.result["member_positions"]["short"] = 0
        supplement.result["pcr"]["calls"] = 0
        _, rows, _ = self.make(supplemental=supplement)
        self.assertEqual(rows["member_ratio"]["value"], "无法计算")
        self.assertEqual(rows["member_net"]["value"], "+0 手（多空相等）")
        self.assertEqual(rows["pcr"]["status"], "unavailable")
        self.assertIn("为0", rows["pcr"]["detail"])

    def test_pcr_requires_a_known_basis_and_future_reports_are_not_used(self):
        supplement = Supplement({"member_positions": {"long": 150, "short": 100, "as_of": "2026-10-20"},
                                 "pcr": {"puts": 40, "calls": 100}})
        _, rows, _ = self.make(supplemental=supplement)
        self.assertEqual(rows["member_ratio"]["status"], "unavailable")
        self.assertIn("晚于行情日期", rows["member_ratio"]["detail"])
        self.assertEqual(rows["pcr"]["status"], "unavailable")
        self.assertIn("未明确", rows["pcr"]["detail"])

    def test_official_oi_fills_only_a_missing_daily_value_and_keeps_its_date(self):
        payload = daily_payload()
        payload["bars"][-1]["open_interest"] = None
        supplement = Supplement({"open_interest": {"value": 900, "previous": 1000,
                                                   "as_of": "2026-10-11", "detail": "交易所日结数据",
                                                   "source": {"name": "INE", "url": "https://www.ine.cn/"}}})
        _, rows, _ = self.make(payload, supplement)
        self.assertEqual(rows["open_interest"]["value"], "900 手")
        self.assertEqual(rows["open_interest"]["as_of"], "2026-10-11")
        self.assertIn("-100.00（-10.00%）", rows["open_interest"]["detail"])
        self.assertEqual(rows["open_interest"]["source"]["name"], "INE")

    def test_unknown_supplementary_statistics_date_is_not_replaced_with_daily_date(self):
        payload = daily_payload()
        payload["bars"][-1]["open_interest"] = None
        supplement = Supplement({
            "open_interest": {"value": 900, "previous": 1000, "as_of": None,
                              "detail": "供应商未披露持仓量统计日期。"},
            "member_positions": {"long": 150, "short": 100, "as_of": None},
            "pcr": {"puts": 40, "calls": 100, "basis": "持仓量", "as_of": None}})
        result, rows, _ = self.make(payload, supplement)
        self.assertTrue(result["as_of"])
        for identifier in ("open_interest", "member_ratio", "member_net", "pcr"):
            self.assertIsNone(rows[identifier]["as_of"])
        self.assertIn("未披露", rows["open_interest"]["detail"])

    def test_negative_price_zero_volume_flat_range_and_stale_cache(self):
        payload = daily_payload(10)
        for bar in payload["bars"]:
            bar.update({"open": -10, "high": -10, "low": -10, "close": -10,
                        "volume": 0, "open_interest": 0})
        payload["cache"]["stale"] = True
        result, rows, _ = self.make(payload)
        self.assertEqual(rows["volume"]["value"], "0 手")
        self.assertEqual(rows["open_interest"]["value"], "0 手")
        self.assertEqual(rows["price_volume"]["value"], "平量持平")
        self.assertIn("前值为0", rows["volume"]["detail"])
        self.assertEqual(rows["range_position"]["value"], "无法计算")
        self.assertIn("绝对值", rows["price_trend"]["detail"])
        self.assertTrue(result["cache"]["stale"])
        self.assertTrue(any("上次成功缓存" in note for note in result["notes"]))

    def test_stale_supplementary_rows_keep_status_and_english_basis_is_rendered_chinese(self):
        payload = daily_payload()
        payload["bars"][-1]["open_interest"] = None
        supplement = Supplement({
            "open_interest": {"value": 900, "previous": 1000, "as_of": "2026-10-11",
                              "status": "stale", "cache": {"stale": True},
                              "detail": "刷新失败，使用缓存"},
            "member_positions": {"long": 150, "short": 100, "status": "stale"},
            "pcr": {"puts": 40, "calls": 100, "basis": "open_interest", "status": "stale"}})
        result, rows, _ = self.make(payload, supplement)
        for identifier in ("open_interest", "member_ratio", "member_net", "pcr"):
            self.assertEqual(rows[identifier]["status"], "stale")
        self.assertIn("持仓量口径", rows["pcr"]["detail"])
        self.assertNotIn("open_interest", rows["pcr"]["detail"])
        self.assertTrue(result["cache"]["stale"])

    def test_supplementary_failure_keeps_computed_daily_rows(self):
        class Offline:
            def get(self, *args, **kwargs):
                raise TimeoutError("provider unavailable")
        result, rows, _ = self.make(supplemental=Offline())
        self.assertEqual(rows["price_trend"]["status"], "available")
        self.assertEqual(rows["member_net"]["status"], "unavailable")
        self.assertTrue(any("TimeoutError" in note for note in result["notes"]))

    def test_history_is_optional_and_uses_aligned_daily_dates_and_normal_plot_types(self):
        supplemental = HistorySupplement()
        result, _, _ = self.make(supplemental=supplemental)
        self.assertNotIn("history", result)
        self.assertEqual(supplemental.history_calls, [])
        result, rows = self.history(supplemental=supplemental, days=5)
        expected_dates = [bar["trading_date"] for bar in daily_payload()["bars"][-5:]]
        self.assertEqual(result["history"]["dates"], expected_dates)
        self.assertEqual(supplemental.history_calls[0][1], {"dates": expected_dates, "refresh": False})
        for row in rows.values():
            for curve in row["series"]:
                self.assertEqual([point["date"] for point in curve["points"]], expected_dates)
                self.assertTrue(all(isinstance(point["time"], int) for point in curve["points"]))
        self.assertEqual(rows["price_trend"]["series"][0]["kind"], "line")
        self.assertEqual(rows["price_trend"]["series"][0]["unit"], "元 / 桶")
        self.assertEqual([curve["kind"] for curve in rows["price_volume"]["series"]], ["line", "bar"])
        self.assertEqual(rows["volume"]["series"][0]["kind"], "bar")
        self.assertEqual(rows["member_net"]["series"][0]["kind"], "bar")
        self.assertEqual(rows["price_trend"]["series"][0]["points"][-1]["value"], 112)

    def test_rolling_range_requires_ten_prior_known_bars_and_never_uses_future_prices(self):
        original = daily_payload()
        _, rows = self.history(original, days=5)
        points = rows["range_position"]["series"][0]["points"]
        self.assertEqual([point["value"] for point in points[:2]], [None, None])
        self.assertAlmostEqual(points[2]["value"], 100 * 12 / 13)
        self.assertEqual(rows["range_position"]["history_status"], "partial")
        changed = copy.deepcopy(original)
        changed["bars"][-1]["high"] = 500
        _, changed_rows = self.history(changed, days=5)
        changed_points = changed_rows["range_position"]["series"][0]["points"]
        self.assertEqual(changed_points[:-1], points[:-1])
        self.assertNotEqual(changed_points[-1]["value"], points[-1]["value"])

    def test_history_gaps_zero_values_and_same_day_oi_fallback(self):
        payload = daily_payload()
        for bar in payload["bars"][-3:]:
            bar["open_interest"] = None
        payload["bars"][-2]["volume"] = None
        payload["bars"][-1]["volume"] = 0
        days = [bar["trading_date"] for bar in payload["bars"][-3:]]
        supplemental = HistorySupplement([
            {"as_of": days[0], "open_interest": {"as_of": days[0], "value": 0}},
            {"as_of": days[1], "open_interest": {"as_of": None, "value": 999}},
            {"as_of": days[2], "open_interest": {"as_of": "2026-10-20", "value": 888}}])
        _, rows = self.history(payload, supplemental, days=3)
        self.assertEqual([point["value"] for point in rows["open_interest"]["series"][0]["points"]], [0, None, None])
        self.assertEqual([point["value"] for point in rows["volume"]["series"][0]["points"]][-2:], [None, 0])
        self.assertEqual(rows["open_interest"]["history_status"], "partial")

    def test_member_and_pcr_history_keep_zero_denominators_and_missing_dates_empty(self):
        payload = daily_payload()
        dates = [bar["trading_date"] for bar in payload["bars"][-3:]]
        supplemental = HistorySupplement([
            {"as_of": dates[0], "member_positions": {"as_of": dates[0], "long": 0, "short": 10},
             "pcr": {"as_of": dates[0], "puts": 0, "calls": 20, "basis": "open_interest"}},
            {"as_of": dates[1], "member_positions": {"as_of": dates[1], "long": 10, "short": 0},
             "pcr": {"as_of": dates[1], "puts": 10, "calls": 0, "basis": "open_interest"}}])
        _, rows = self.history(payload, supplemental, days=3)
        values = lambda identifier: [point["value"] for point in rows[identifier]["series"][0]["points"]]
        self.assertEqual(values("member_ratio"), [0, None, None])
        self.assertEqual(values("member_net"), [-10, 10, None])
        self.assertEqual(values("pcr"), [0, None, None])
        self.assertIn("持仓量", rows["pcr"]["series"][0]["label"])

    def test_pcr_history_does_not_mix_volume_and_open_interest_basis(self):
        dates = [bar["trading_date"] for bar in daily_payload()["bars"][-3:]]
        supplemental = HistorySupplement([
            {"as_of": day, "pcr": {"as_of": day, "puts": 20, "calls": 10,
                                  "basis": "volume" if index == 0 else "open_interest"}}
            for index, day in enumerate(dates)])
        _, rows = self.history(supplemental=supplemental, days=3)
        self.assertEqual([point["value"] for point in rows["pcr"]["series"][0]["points"]], [None, 2, 2])

    def test_current_unknown_date_oi_cannot_be_reused_as_history(self):
        payload = daily_payload()
        payload["contract"] = copy.deepcopy(CONTRACTS["wti-202612"])
        for bar in payload["bars"]:
            bar["open_interest"] = None
        supplemental = HistorySupplement(latest={"open_interest": {"value": 267773, "as_of": None}})
        _, rows = self.history(payload, supplemental)
        self.assertEqual(rows["open_interest"]["value"], "267,773 手")
        self.assertTrue(all(point["value"] is None for point in rows["open_interest"]["series"][0]["points"]))
        self.assertEqual(rows["open_interest"]["history_status"], "unavailable")
        self.assertIn("无法据此绘制历史曲线", rows["open_interest"]["history_detail"])

    def test_history_maximum_and_unknown_denominators_are_null(self):
        payload = daily_payload(40)
        result, rows = self.history(payload)
        self.assertEqual(result["history"]["days"], 30)
        self.assertEqual(len(rows["price_trend"]["series"][0]["points"]), 30)
        for bar in payload["bars"]:
            bar.update({"high": 100, "low": 100, "close": 100, "open": 100})
        _, rows = self.history(payload)
        self.assertTrue(all(point["value"] is None for point in rows["range_position"]["series"][0]["points"]))
        for invalid in (0, 31, True, 1.5):
            with self.assertRaises(MarketDataError) as caught:
                MarketIndicators(Market(payload)).get("sc-202612", history=True, days=invalid)
            self.assertEqual(caught.exception.status, 400)

    def test_visible_range_can_exceed_thirty_bars_and_uses_only_inclusive_requested_dates(self):
        payload = daily_payload(80)
        supplemental = HistorySupplement()
        start, end = payload["bars"][10]["trading_date"], payload["bars"][60]["trading_date"]
        result = MarketIndicators(Market(payload), supplemental).get("sc-202612", history=True, start=start, end=end)
        rows = {row["id"]: row for row in result["rows"]}
        expected = [bar["trading_date"] for bar in payload["bars"][10:61]]
        self.assertEqual(result["history"]["dates"], expected)
        self.assertEqual(result["history"]["days"], 51)
        self.assertEqual(result["history"]["from"], start)
        self.assertEqual(result["history"]["to"], end)
        self.assertEqual(supplemental.calls, [])  # No blocking current-snapshot request.
        self.assertEqual(supplemental.history_calls[0][1], {"dates": expected, "refresh": False, "background": True})
        self.assertEqual(rows["price_trend"]["series"][0]["points"][0]["value"], payload["bars"][10]["close"])
        self.assertAlmostEqual(rows["range_position"]["series"][0]["points"][0]["value"], 100 * 12 / 13)
        # A large later high lies outside this view and cannot affect it.
        payload["bars"][61]["high"] = 1000000
        changed = MarketIndicators(Market(payload)).get("sc-202612", history=True, start=start, end=end)
        self.assertEqual(next(row for row in changed["rows"] if row["id"] == "range_position")["series"], rows["range_position"]["series"])

    def test_visible_range_returns_pending_progress_and_keeps_available_local_curves(self):
        class Pending(HistorySupplement):
            def get_history(self, *args, **kwargs):
                return {"snapshots": [], "pending": True, "pending_count": 12,
                        "completed_dates": 0, "notes": ["历史补充数据加载中"]}
        result = MarketIndicators(Market(daily_payload()), Pending()).get(
            "sc-202612", history=True, start="2026-10-01", end="2026-10-12")
        rows = {row["id"]: row for row in result["rows"]}
        self.assertTrue(result["history"]["pending"])
        self.assertEqual(result["history"]["pending_count"], 12)
        self.assertEqual(result["history"]["completed_dates"], 0)
        self.assertEqual(rows["price_trend"]["history_status"], "available")
        self.assertEqual(rows["open_interest"]["history_status"], "available")

    def test_visible_range_outside_available_dates_is_empty_without_fabricated_points(self):
        supplemental = HistorySupplement()
        result = MarketIndicators(Market(daily_payload()), supplemental).get(
            "sc-202612", history=True, start="2030-01-01", end="2030-12-31")
        self.assertEqual(result["history"]["dates"], [])
        self.assertIsNone(result["history"]["from"])
        self.assertFalse(result["history"]["pending"])
        self.assertEqual(supplemental.history_calls, [])
        for row in result["rows"]:
            self.assertEqual(row["history_status"], "unavailable")
            self.assertTrue(all(curve["points"] == [] for curve in row["series"]))

    def test_visible_range_strict_iso_validation_precedes_market_network(self):
        market = Market(daily_payload())
        for start, end in [("2026-10-01", None), (None, "2026-10-12"),
                           ("20261001", "2026-10-12"), ("2026-02-30", "2026-10-12"),
                           ("2026-10-12", "2026-10-01"), (True, "2026-10-12")]:
            with self.subTest(start=start, end=end):
                with self.assertRaises(MarketDataError) as caught:
                    MarketIndicators(market).get("sc-202612", history=True, start=start, end=end)
                self.assertEqual(caught.exception.status, 400)
        self.assertEqual(market.calls, [])

    def test_history_points_carry_utc_sessions_without_reassigning_trading_dates(self):
        payload = daily_payload()
        payload["bars"] = [{**payload["bars"][-1], "trading_date": "2026-11-02", "time": 1793577600000}]
        result, rows = self.history(payload)
        self.assertEqual(result["display_timezone"], "UTC")
        self.assertFalse(result["session_rules"]["supplier_aggregation_verified"])
        for row in rows.values():
            for series in row["series"]:
                point = series["points"][0]
                self.assertEqual(point["date"], "2026-11-02")
                self.assertEqual(point["time"], 1793577600000)
                self.assertEqual(point["session"]["start_utc"], "2026-10-30T13:00:00Z")
                self.assertEqual(point["session"]["end_utc"], "2026-11-02T07:00:00Z")


if __name__ == "__main__":
    unittest.main()
