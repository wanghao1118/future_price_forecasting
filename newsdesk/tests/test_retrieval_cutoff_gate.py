"""Exact retrieval boundaries preserve old frozen date and UTC protocols."""

import unittest

from time_gate_agent.local import LocalTimeGate, PROTOCOL, UTC_PROTOCOL, RETRIEVAL_POLICY


def window(day, start, end, segments=None):
    return {"status": "regular_schedule", "calendar_status": "verified_2026",
            "trading_date": day, "start_utc": start, "end_utc": end,
            "segments": segments or [{"start_utc": start, "end_utc": end}]}


def question(family="WTI"):
    case = {"case_id": "retrieval", "family": family, "input_date": "2026-10-29",
            "target_date": "2026-10-30", "retrieval_time_policy": RETRIEVAL_POLICY,
            "exchange_timezone": "America/New_York"}
    known = window("2026-10-29", "2026-10-28T22:00:00Z", "2026-10-29T21:00:00Z")
    forecast = window("2026-10-30", "2026-10-29T22:00:00Z", "2026-10-30T21:00:00Z")
    if family == "SC":
        case.update(input_date="2026-10-30", target_date="2026-11-02", exchange_timezone="Asia/Shanghai")
        known = window("2026-10-30", "2026-10-29T13:00:00Z", "2026-10-30T07:00:00Z")
        forecast = window("2026-11-02", "2026-10-30T13:00:00Z", "2026-11-02T07:00:00Z", [
            {"start_utc": "2026-10-30T13:00:00Z", "end_utc": "2026-10-30T18:30:00Z"},
            {"start_utc": "2026-11-02T01:00:00Z", "end_utc": "2026-11-02T07:00:00Z"}])
    case["time_windows"] = {"status": "ready", "known": known, "forecast": forecast,
                            "information_cutoff_utc": known["end_utc"]}
    return case


def article(identifier="news", published="2026-10-29T20:00:00Z", **extra):
    row = {"id": identifier, "body": "Frozen original article.", "published_at": published,
           "collected_at": "2026-11-01T00:00:00Z", "source_name": "Fixture",
           "ingestion": {"published_date": published[:10], "timezone_status": "verified_timezone",
                         "publication_precision": "second"}}
    row.update(extra)
    return row


class RetrievalBoundaryTest(unittest.TestCase):
    def test_boundary_uses_frozen_forecast_start_with_two_hour_buffer(self):
        boundary = LocalTimeGate.retrieval_boundary(question())
        self.assertTrue(boundary["allowed"])
        self.assertEqual(boundary["forecast_start_utc"], "2026-10-29T22:00:00Z")
        self.assertEqual(boundary["cutoff_utc"], "2026-10-29T20:00:00Z")
        self.assertEqual(boundary["timezone"], "UTC")
        self.assertEqual(boundary["buffer_hours"], 2)

    def test_offset_and_cross_midnight_are_normalized_to_utc(self):
        case = question()
        forecast = case["time_windows"]["forecast"]
        forecast["start_utc"] = "2026-10-30T01:00:00+02:00"
        forecast["segments"][0]["start_utc"] = forecast["start_utc"]
        boundary = LocalTimeGate.retrieval_boundary(case)
        self.assertEqual(boundary["forecast_start_utc"], "2026-10-29T23:00:00Z")
        self.assertEqual(boundary["cutoff_utc"], "2026-10-29T21:00:00Z")

    def test_sc_night_session_uses_friday_start_for_monday_trading_date(self):
        case = question("SC")
        self.assertEqual(LocalTimeGate.retrieval_boundary(case)["cutoff_utc"], "2026-10-30T11:00:00Z")
        result = LocalTimeGate([
            article("before", "2026-10-30T18:59:00+08:00"),
            article("equal", "2026-10-30T19:00:00+08:00"),
            article("after", "2026-10-30T19:01:00+08:00")]).select(case)
        self.assertEqual([row["id"] for row in result["articles"]], ["equal", "before"])

    def test_missing_or_inconsistent_windows_never_fall_back_to_dates(self):
        mutations = [lambda c: c.pop("time_windows"),
                     lambda c: c.update(time_windows=None),
                     lambda c: c["time_windows"].update(information_cutoff_utc="2026-10-29T20:00:00Z"),
                     lambda c: c["time_windows"]["forecast"].update(calendar_status="unconfirmed"),
                     lambda c: c["time_windows"]["known"].update(segments=[]),
                     lambda c: c["time_windows"]["forecast"].update(start_utc="2026-10-29"),
                     lambda c: c.update(retrieval_time_policy="unknown")]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                case = question()
                mutate(case)
                self.assertFalse(LocalTimeGate.retrieval_boundary(case)["allowed"])
                self.assertEqual(LocalTimeGate([article()]).select(case)["articles"], [])
                query = LocalTimeGate.validate_indicator_time_query(case, "2026-10-01T00:00:00Z")
                self.assertFalse(query["allowed"])
                self.assertFalse(query["retryable"])


class RetrievalNewsTest(unittest.TestCase):
    def test_one_minute_before_equal_and_after_cutoff(self):
        result = LocalTimeGate([
            article("before", "2026-10-29T19:59:00Z"),
            article("equal", "2026-10-29T22:00:00+02:00"),
            article("after", "2026-10-29T20:01:00Z")]).select(question())
        self.assertEqual([row["id"] for row in result["articles"]], ["equal", "before"])
        self.assertEqual(result["articles"][0]["published_at"], "2026-10-29T20:00:00Z")
        self.assertEqual(result["articles"][0]["published_at_utc"], "2026-10-29T20:00:00Z")
        self.assertEqual(result["audit"]["cutoff_utc"], "2026-10-29T20:00:00Z")
        self.assertTrue(result["articles"][0]["historical_version_unverified"])

    def test_unverified_times_and_post_cutoff_revisions_are_rejected(self):
        rows = [article("date", "2026-10-29"), article("naive", "2026-10-29T19:00:00"),
                article("revised", source_modified_at="2026-10-29T20:01:00Z"),
                article("revision-date", modified_at="2026-10-29"),
                article("timezone", ingestion={"published_date": "2026-10-29", "timezone_status": "unverified_timezone"}),
                article("revision-equal", source_modified_at="2026-10-29T22:00:00+02:00")]
        result = LocalTimeGate(rows).select(question())
        self.assertEqual([row["id"] for row in result["articles"]], ["revision-equal"])
        self.assertEqual(result["articles"][0]["source_modified_at"], "2026-10-29T20:00:00Z")

    def test_utc_order_ignores_source_calendar_day_and_fractional_format(self):
        case = question()
        rows = [article("early", "2026-10-30T00:00:00+05:00"),
                article("later", "2026-10-29T19:00:00.100000Z")]
        self.assertEqual([row["id"] for row in LocalTimeGate(rows).select(case)["articles"]], ["later", "early"])

    def test_legacy_date_and_known_end_utc_results_remain_unchanged(self):
        case = question()
        case.pop("retrieval_time_policy")
        news = [article("after-new-cutoff", "2026-10-29T20:30:00Z"), article("date", "2026-10-29")]
        exact = LocalTimeGate(news).select(case)
        self.assertEqual([row["id"] for row in exact["articles"]], ["after-new-cutoff"])
        self.assertEqual(exact["audit"]["protocol"], UTC_PROTOCOL)
        self.assertEqual(exact["audit"]["cutoff_utc"], "2026-10-29T21:00:00Z")
        case.pop("time_windows")
        legacy = LocalTimeGate(news).select(case)
        self.assertEqual({row["id"] for row in legacy["articles"]}, {"after-new-cutoff", "date"})
        self.assertEqual(legacy["audit"]["protocol"], PROTOCOL)
        self.assertNotIn("retrieval_boundary", legacy["audit"])


class RetrievalIndicatorTest(unittest.TestCase):
    def test_query_default_end_and_offsets_are_exact_utc(self):
        query = LocalTimeGate.validate_indicator_time_query(question(), "2026-10-29T20:00+02:00")
        self.assertTrue(query["allowed"])
        self.assertEqual(query["start_time"], "2026-10-29T18:00:00Z")
        self.assertEqual(query["end_time"], "2026-10-29T20:00:00Z")
        self.assertEqual(query["allowed_start_time"], "2026-01-01T00:00:00Z")
        self.assertEqual(query["allowed_end_time"], query["cutoff_utc"])

    def test_invalid_or_out_of_range_query_is_rejected_in_full(self):
        ranges = [("2026-10-29", None), ("2026-10-29T18:00:00", None),
                  ("2025-12-31T23:59:00Z", None),
                  ("2026-10-29T19:00:00Z", "2026-10-29T20:01:00Z"),
                  ("2026-10-29T19:00:00Z", "2026-10-29T18:00:00Z")]
        for start, end in ranges:
            with self.subTest(start=start, end=end):
                result = LocalTimeGate.select_indicator_time_rows(question(),
                    [{"available_at": "2026-10-29T19:30:00Z"}], start, end)
                self.assertFalse(result["query"]["allowed"])
                self.assertEqual(result["rows"], [])
                self.assertEqual(result["checks"], [])

    def test_availability_boundary_missing_times_and_epoch_are_independently_gated(self):
        rows = [{"id": "before", "available_at": "2026-10-29T19:59:00Z"},
                {"id": "equal", "timestamp": "2026-10-29T22:00:00+02:00"},
                {"id": "after", "available_at": "2026-10-29T20:01:00Z"},
                {"id": "date", "date": "2026-10-29"},
                {"id": "date-time", "available_at": "2026-10-29"},
                {"id": "naive", "available_at": "2026-10-29T19:00:00"},
                {"id": "epoch", "timestamp": 1793296800},
                {"id": "mixed-epoch", "available_at": "2026-10-29T19:00:00Z", "timestamp": "1793296800"}]
        result = LocalTimeGate.select_indicator_time_rows(question(), rows, "2026-10-29T00:00:00Z")
        self.assertEqual([row["id"] for row in result["rows"]], ["before", "equal"])
        self.assertEqual(result["rows"][1]["available_at"], "2026-10-29T20:00:00Z")
        self.assertEqual(len(result["checks"]), len(rows))

    def test_later_observation_and_revision_times_control_availability(self):
        rows = [{"id": "observation-later", "available_at": "2026-10-29T18:00:00Z", "timestamp": "2026-10-29T19:00:00Z"},
                {"id": "publication-later", "available_at": "2026-10-29T19:10:00Z", "timestamp": "2026-10-29T18:00:00Z"},
                {"id": "future-observation", "available_at": "2026-10-29T18:00:00Z", "timestamp": "2026-10-29T20:01:00Z"},
                {"id": "revision-later", "available_at": "2026-10-29T18:00:00Z", "source_modified_at": "2026-10-29T19:20:00Z"},
                {"id": "future-revision", "available_at": "2026-10-29T18:00:00Z", "modified_at": "2026-10-29T19:31:00Z"}]
        result = LocalTimeGate.select_indicator_time_rows(question(), rows,
            "2026-10-29T18:30:00Z", "2026-10-29T19:30:00Z")
        self.assertEqual([row["id"] for row in result["rows"]], ["observation-later", "publication-later", "revision-later"])
        self.assertEqual(result["rows"][0]["available_at"], "2026-10-29T19:00:00Z")
        self.assertEqual(result["rows"][2]["available_at"], "2026-10-29T19:20:00Z")
        self.assertEqual(result["checks"][-1]["reason"], "source_revision_after_requested_end_time")


if __name__ == "__main__":
    unittest.main()
