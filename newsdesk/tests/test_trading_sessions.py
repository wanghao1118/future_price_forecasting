"""UTC schedules, DST, exchange holidays and trading-date anchor regressions."""

from datetime import datetime
import unittest

from newsdesk.trading_sessions import (aggregate_sessions, confirmed_closed_day, confirmed_no_separate_trade_date,
                                      continuous_daily_session, daily_session, next_expected_trading_date,
                                      no_separate_trade_date_assignment, session_rules)


class TradingSessionTests(unittest.TestCase):
    def test_wti_summer_session_starts_previous_utc_date(self):
        result = daily_session("WTI", "2026-10-30")
        self.assertEqual(result["start_utc"], "2026-10-29T22:00:00Z")
        self.assertEqual(result["end_utc"], "2026-10-30T21:00:00Z")
        self.assertIn("前一日", result["label"])
        self.assertEqual(result["exchange_timezone"], "America/Chicago")
        self.assertFalse(result["supplier_aggregation_verified"])

    def test_us_dst_fall_change_is_evaluated_at_actual_session_date(self):
        friday = daily_session("WTI", "2026-10-30")
        monday = daily_session("WTI", "2026-11-02")
        self.assertEqual(monday["start_utc"], "2026-11-01T23:00:00Z")
        self.assertEqual(monday["end_utc"], "2026-11-02T22:00:00Z")
        self.assertNotEqual(friday["start_utc"][11:16], monday["start_utc"][11:16])
        self.assertEqual((datetime.fromisoformat(monday["end_utc"].replace("Z", "+00:00")) -
                          datetime.fromisoformat(monday["start_utc"].replace("Z", "+00:00"))).total_seconds(), 23 * 3600)

    def test_us_dst_spring_change(self):
        before = daily_session("WTI", "2026-03-06")
        after = daily_session("WTI", "2026-03-09")
        self.assertEqual(before["start_utc"], "2026-03-05T23:00:00Z")
        self.assertEqual(after["start_utc"], "2026-03-08T22:00:00Z")

    def test_brent_bz_uses_nymex_chicago_regular_schedule(self):
        result = daily_session("Brent", "2026-10-30")
        self.assertEqual(result["start_utc"], "2026-10-29T22:00:00Z")
        self.assertEqual(result["end_utc"], "2026-10-30T21:00:00Z")
        self.assertIn("brent-crude-oil-last-day", result["source_url"])
        self.assertIn("到期", " ".join(result["notes"]))

    def test_cme_holiday_period_has_no_claimed_regular_boundaries(self):
        for day in ("2026-01-01", "2026-01-19", "2026-04-03", "2026-11-26", "2026-12-25"):
            with self.subTest(day=day):
                result = daily_session("WTI", day)
                self.assertEqual(result["kind"], "unconfirmed")
                self.assertIsNone(result["start_utc"])
                self.assertIsNone(result["end_utc"])
                self.assertEqual(result["segments"], [])
                self.assertIn("待核实", result["label"])

    def test_sc_weekday_night_and_three_daily_segments_are_utc(self):
        result = daily_session("SC", "2026-10-30")
        self.assertEqual(result["start_utc"], "2026-10-29T13:00:00Z")
        self.assertEqual(result["end_utc"], "2026-10-30T07:00:00Z")
        self.assertEqual([(row["start_utc"], row["end_utc"]) for row in result["segments"]], [
            ("2026-10-29T13:00:00Z", "2026-10-29T18:30:00Z"),
            ("2026-10-30T01:00:00Z", "2026-10-30T02:15:00Z"),
            ("2026-10-30T02:30:00Z", "2026-10-30T03:30:00Z"),
            ("2026-10-30T05:30:00Z", "2026-10-30T07:00:00Z"),
        ])
        self.assertIn("分段交易", result["label"])

    def test_sc_monday_night_belongs_to_friday_not_sunday(self):
        result = daily_session("SC", "2026-11-02")
        self.assertEqual(result["start_utc"], "2026-10-30T13:00:00Z")
        self.assertEqual(result["segments"][0]["end_utc"], "2026-10-30T18:30:00Z")
        self.assertEqual(result["end_utc"], "2026-11-02T07:00:00Z")
        self.assertIn("前3日", result["label"])
        self.assertFalse(any("2026-11-01" in row["start_utc"] for row in result["segments"]))

    def test_sc_first_day_after_holidays_has_only_daily_session(self):
        for day in ("2026-01-05", "2026-02-24", "2026-04-07", "2026-05-06", "2026-06-22", "2026-09-28", "2026-10-08"):
            with self.subTest(day=day):
                result = daily_session("SC", day)
                self.assertEqual(result["night_session_status"], "cancelled_for_holiday")
                self.assertEqual(len(result["segments"]), 3)
                self.assertEqual(result["start_utc"], day + "T01:00:00Z")
                self.assertEqual(result["end_utc"], day + "T07:00:00Z")
                self.assertIn("起于当日", result["label"])

    def test_sc_second_day_after_holiday_has_prior_evening_session(self):
        result = daily_session("SC", "2026-10-09")
        self.assertEqual(result["night_session_status"], "regular_schedule")
        self.assertEqual(result["start_utc"], "2026-10-08T13:00:00Z")

    def test_weekends_and_sc_holidays_do_not_create_sessions(self):
        for family, day in (("SC", "2026-10-01"), ("SC", "2026-02-14"),
                            ("SC", "2026-10-10"), ("WTI", "2026-11-01")):
            with self.subTest(family=family, day=day):
                result = daily_session(family, day)
                self.assertEqual(result["kind"], "closed")
                self.assertIsNone(result["start_utc"])
                self.assertEqual(result["segments"], [])

    def test_unknown_year_does_not_invent_sc_night_or_cme_special_hours(self):
        sc = daily_session("SC", "2027-01-04")
        self.assertEqual(sc["calendar_status"], "unconfirmed")
        self.assertEqual(sc["night_session_status"], "unconfirmed")
        self.assertTrue(all(row["label"] == "日盘" for row in sc["segments"]))
        self.assertIn("未确认", sc["label"])
        cme = daily_session("WTI", "2027-01-04")
        self.assertEqual(cme["kind"], "unconfirmed")
        self.assertEqual(cme["segments"], [])

    def test_invalid_dates_and_unknown_family_remain_missing(self):
        for value in (None, "", "2026-02-30", "20261030", "2026-10-30T00:00:00", True):
            self.assertIsNone(daily_session("SC", value))
        self.assertIsNone(daily_session("unknown", "2026-10-30"))

    def test_weekly_aggregation_includes_last_day_and_all_segments(self):
        first = daily_session("SC", "2026-10-26")
        last = daily_session("SC", "2026-10-30")
        result = aggregate_sessions([first, last], "1w")
        self.assertEqual(result["kind"], "aggregated_schedule")
        self.assertEqual(result["start_utc"], first["start_utc"])
        self.assertEqual(result["end_utc"], last["end_utc"])
        self.assertEqual(result["trading_date_end"], "2026-10-30")
        self.assertEqual(len(result["segments"]), 8)
        self.assertIn("周K", result["label"])
        self.assertFalse(result["supplier_aggregation_verified"])

    def test_monthly_aggregation_retains_unknown_special_day_as_partial(self):
        result = aggregate_sessions([daily_session("WTI", "2026-11-25"),
                                     daily_session("WTI", "2026-11-26")], "1M")
        self.assertEqual(result["status"], "partial")
        self.assertIn("月K", result["label"])
        self.assertIn("待核实", result["label"])
        self.assertEqual(result["trading_date_end"], "2026-11-26")
        self.assertIsNone(result["end_utc"])
        self.assertIsNone(aggregate_sessions([], "1w"))

    def test_rules_distinguish_trading_date_anchor_and_provider_aggregation(self):
        rules = session_rules("WTI")
        self.assertEqual(rules["timezone"], "UTC")
        self.assertEqual(rules["date_kind"], "exchange_trading_date")
        self.assertIn("分类锚点", rules["time_anchor"])
        self.assertFalse(rules["supplier_aggregation_verified"])

    def test_ice_continuous_and_nymex_bz_contracts_use_separate_schedules(self):
        ice = continuous_daily_session({"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}, "2026-10-30")
        bz = continuous_daily_session({"familyId": "Brent", "code": "BZZ26", "exchange": "NYMEX"}, "2026-10-30")
        self.assertEqual(ice["exchange"], "ICE")
        self.assertEqual(ice["start_utc"], "2026-10-30T00:00:00Z")
        self.assertEqual(ice["end_utc"], "2026-10-30T22:00:00Z")
        self.assertEqual(bz["start_utc"], "2026-10-29T22:00:00Z")
        self.assertEqual(bz["end_utc"], "2026-10-30T21:00:00Z")
        self.assertIn("ice.com", ice["source_url"])
        self.assertIn("cmegroup.com", bz["source_url"])

    def test_ice_monday_session_uses_sunday_evening_and_us_dst(self):
        contract = {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}
        before = continuous_daily_session(contract, "2026-10-26")
        after = continuous_daily_session(contract, "2026-11-02")
        tuesday = continuous_daily_session(contract, "2026-11-03")
        self.assertEqual((before["start_utc"], before["end_utc"]), ("2026-10-25T22:00:00Z", "2026-10-26T22:00:00Z"))
        self.assertEqual((after["start_utc"], after["end_utc"]), ("2026-11-01T23:00:00Z", "2026-11-02T23:00:00Z"))
        self.assertEqual((tuesday["start_utc"], tuesday["end_utc"]), ("2026-11-03T01:00:00Z", "2026-11-03T23:00:00Z"))

    def test_ice_special_and_closed_dates_remain_explicit_without_guessed_hours(self):
        contract = {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}
        self.assertEqual(continuous_daily_session(contract, "2026-01-01")["status"], "closed")
        self.assertEqual(continuous_daily_session(contract, "2026-11-26")["status"], "unconfirmed")
        self.assertIsNone(continuous_daily_session(contract, "2026-11-26")["end_utc"])
        unknown = continuous_daily_session({"familyId": "Brent", "code": "unknown"}, "2026-10-30")
        self.assertEqual(unknown["status"], "unconfirmed")
        self.assertIsNone(unknown["start_utc"])

    def test_ice_brent_august_td_dates_keep_regular_product_specific_hours(self):
        contract = {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}
        friday = continuous_daily_session(contract, "2026-08-07")
        monday = continuous_daily_session(contract, "2026-08-31")
        self.assertEqual(friday["status"], "regular_schedule")
        self.assertEqual((friday["start_utc"], friday["end_utc"]), ("2026-08-07T00:00:00Z", "2026-08-07T22:00:00Z"))
        self.assertEqual(monday["status"], "regular_schedule")
        self.assertEqual((monday["start_utc"], monday["end_utc"]), ("2026-08-30T22:00:00Z", "2026-08-31T22:00:00Z"))
        # The correction is limited to these TD dates, without treating other
        # early closes or the independent CME calendar as ordinary sessions.
        for day in ("2026-07-03", "2026-11-26", "2026-11-27"):
            self.assertEqual(continuous_daily_session(contract, day)["status"], "unconfirmed")

    def test_wti_labor_day_trades_share_september_eighth_date_in_two_verified_segments(self):
        result = daily_session("WTI", "2026-09-08")
        self.assertEqual(result["status"], "regular_schedule")
        self.assertEqual(result["calendar_status"], "verified_2026")
        self.assertEqual(result["schedule_type"], "verified_extended_session")
        self.assertEqual(result["calendar_exception"]["no_separate_trade_dates"], ["2026-09-07"])
        self.assertEqual([(item["start_utc"], item["end_utc"]) for item in result["segments"]], [
            ("2026-09-06T22:00:00Z", "2026-09-07T18:30:00Z"),
            ("2026-09-07T22:00:00Z", "2026-09-08T21:00:00Z"),
        ])
        self.assertEqual(result["start_utc"], "2026-09-06T22:00:00Z")
        self.assertEqual(result["end_utc"], "2026-09-08T21:00:00Z")
        self.assertIn("劳动节延长", result["label"])
        self.assertIn("pre-open", " ".join(result["notes"]))
        self.assertIn("tradeDate=2026-09-08", result["source_url"])
        self.assertFalse(result["supplier_aggregation_verified"])

    def test_wti_september_seventh_has_no_own_daily_label_without_claiming_physical_closure(self):
        result = daily_session("WTI", "2026-09-07")
        self.assertEqual(result["status"], "no_separate_trade_date")
        self.assertEqual(result["assigned_trading_date"], "2026-09-08")
        self.assertEqual(result["segments"], [])
        self.assertIsNone(result["start_utc"])
        self.assertFalse(confirmed_closed_day("WTI", "2026-09-07"))
        self.assertTrue(confirmed_no_separate_trade_date("WTI", "2026-09-07"))
        self.assertEqual(no_separate_trade_date_assignment("WTI", "2026-09-07")["assigned_trading_date"], "2026-09-08")
        self.assertFalse(confirmed_no_separate_trade_date("Brent", "2026-09-07"))
        contract = {"familyId": "WTI", "code": "CL.1:NYM", "exchange": "NYMEX"}
        self.assertEqual(next_expected_trading_date(contract, "2026-09-04"), "2026-09-08")

    def test_ice_labor_day_early_close_is_verified_and_september_eighth_remains_regular(self):
        contract = {"familyId": "Brent", "code": "IB.1:IEU", "exchange": "ICE"}
        seventh = continuous_daily_session(contract, "2026-09-07")
        eighth = continuous_daily_session(contract, "2026-09-08")
        self.assertEqual(seventh["status"], "regular_schedule")
        self.assertEqual(seventh["schedule_type"], "verified_early_close")
        self.assertEqual(seventh["calendar_exception"]["type"], "early_close")
        self.assertEqual((seventh["start_utc"], seventh["end_utc"]), ("2026-09-06T22:00:00Z", "2026-09-07T17:30:00Z"))
        self.assertEqual((eighth["start_utc"], eighth["end_utc"]), ("2026-09-08T00:00:00Z", "2026-09-08T22:00:00Z"))
        self.assertIn("提前收盘", seventh["label"])
        self.assertEqual(seventh["source_url"], "https://www.ice.com/publicdocs/Trading_Schedule.pdf")
        self.assertEqual(continuous_daily_session(contract, "2026-11-26")["status"], "unconfirmed")

    def test_aggregation_preserves_verified_labor_day_segments_and_exception_metadata(self):
        result = aggregate_sessions([daily_session("WTI", "2026-09-08"), daily_session("WTI", "2026-09-09")], "1w")
        self.assertEqual(result["status"], "regular_schedule")
        self.assertEqual(len(result["segments"]), 3)
        self.assertEqual(result["start_utc"], "2026-09-06T22:00:00Z")
        self.assertEqual(result["end_utc"], "2026-09-09T21:00:00Z")
        self.assertEqual(result["calendar_exceptions"][0]["assigned_trading_date"], "2026-09-08")

    def test_next_expected_sc_date_skips_confirmed_holidays_without_assuming_future_calendar(self):
        sc = {"familyId": "SC", "code": "SC0", "exchange": "INE"}
        self.assertEqual(next_expected_trading_date(sc, "2026-09-30"), "2026-10-08")
        self.assertEqual(next_expected_trading_date(sc, "2026-02-13"), "2026-02-24")
        self.assertEqual(next_expected_trading_date(sc, "2026-10-30"), "2026-11-02")
        self.assertTrue(confirmed_closed_day("SC", "2026-10-01"))
        self.assertFalse(confirmed_closed_day("WTI", "2026-10-01"))
        self.assertFalse(confirmed_closed_day("SC", "2027-10-01"))


if __name__ == "__main__":
    unittest.main()
