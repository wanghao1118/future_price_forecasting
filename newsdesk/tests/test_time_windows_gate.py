"""UTC input boundaries quarantine same-day future information without changing old questions."""

import copy
import unittest

from time_gate_agent.local import LocalTimeGate, PROTOCOL, UTC_PROTOCOL


def session(day, start, end, segments=None):
    return {"trading_date": day, "status": "regular_schedule", "calendar_status": "verified_2026",
            "start_utc": start, "end_utc": end,
            "segments": segments or [{"start_utc": start, "end_utc": end}]}


def question(exact=True):
    case = {"case_id": "sc-utc", "family": "SC", "input_date": "2026-10-30",
            "target_date": "2026-11-02", "exchange_timezone": "Asia/Shanghai"}
    if exact:
        known = session("2026-10-30", "2026-10-29T13:00:00Z", "2026-10-30T07:00:00Z", [
            {"start_utc": "2026-10-29T13:00:00Z", "end_utc": "2026-10-29T18:30:00Z"},
            {"start_utc": "2026-10-30T01:00:00Z", "end_utc": "2026-10-30T02:15:00Z"},
            {"start_utc": "2026-10-30T02:30:00Z", "end_utc": "2026-10-30T03:30:00Z"},
            {"start_utc": "2026-10-30T05:30:00Z", "end_utc": "2026-10-30T07:00:00Z"}])
        forecast = session("2026-11-02", "2026-10-30T13:00:00Z", "2026-11-02T07:00:00Z", [
            {"start_utc": "2026-10-30T13:00:00Z", "end_utc": "2026-10-30T18:30:00Z"},
            {"start_utc": "2026-11-02T01:00:00Z", "end_utc": "2026-11-02T07:00:00Z"}])
        case["time_windows"] = {"status": "ready", "known": known, "forecast": forecast,
                                "information_cutoff_utc": known["end_utc"]}
    return case


def article(identifier="news", published="2026-10-30T06:59:00Z", **changes):
    value = {"id": identifier, "title": "原油消息", "body": "原油供应事件原文。", "assets": ["SC"],
             "published_at": published, "collected_at": "2026-10-30T06:59:30Z",
             "ingestion": {"published_date": "2026-10-30", "timezone_status": "verified"},
             "agent_review": {"relevance": True}}
    value.update(changes)
    return value


class ExactUtcTimeGateTests(unittest.TestCase):
    def test_same_input_day_news_after_close_is_denied_and_boundary_is_inclusive(self):
        news = [article("before"), article("boundary", "2026-10-30T15:00:00+08:00"),
                article("after", "2026-10-30T07:00:00.000001Z"),
                article("forecast-night", "2026-10-30T21:01:00+08:00")]
        exact = LocalTimeGate(news).select(question())
        self.assertEqual({item["id"] for item in exact["articles"]}, {"before", "boundary"})
        self.assertEqual(exact["audit"]["cutoff_utc"], "2026-10-30T07:00:00Z")
        self.assertEqual(exact["audit"]["protocol"], UTC_PROTOCOL)
        self.assertTrue(exact["audit"]["time_windows_validated"])
        self.assertEqual(exact["audit"]["rejection_counts"], {"publication_after_information_cutoff_utc": 2})
        legacy = LocalTimeGate(news).select(question(exact=False))
        self.assertEqual(len(legacy["articles"]), 4)
        self.assertEqual(legacy["audit"]["protocol"], PROTOCOL)
        self.assertNotIn("cutoff_utc", legacy["audit"])

    def test_date_only_publication_is_not_assigned_an_invented_minute(self):
        news = [article("date-only", published=""), article("naive", "2026-10-30T06:00:00")]
        exact = LocalTimeGate(news).select(question())
        self.assertEqual(exact["articles"], [])
        self.assertIn("original_publication_timestamp_unverified", exact["audit"]["rejection_counts"])
        self.assertEqual([item["id"] for item in LocalTimeGate(news).select(question(False))["articles"]], ["date-only"])

    def test_each_source_revision_requires_verified_time_before_cutoff(self):
        for field in ("source_modified_at", "article_modified_at", "modified_at"):
            with self.subTest(field=field):
                news = [article("safe", **{field: "2026-10-30T15:00:00+08:00"}),
                        article("late", **{field: "2026-10-30T07:01:00Z"}),
                        article("date-only", **{field: "2026-10-30"})]
                result = LocalTimeGate(news).select(question())
                self.assertEqual([item["id"] for item in result["articles"]], ["safe"])
                self.assertEqual(result["audit"]["rejection_counts"], {
                    "source_revision_after_information_cutoff_utc": 1, "source_revision_timestamp_unverified": 1})

    def test_archive_edit_is_not_a_source_revision_and_late_collection_is_flagged(self):
        news = [article("late-collection", collected_at="2026-10-30T07:01:00Z", updated_at="2026-11-03T00:00:00Z"),
                article("unknown-collection", collected_at="2026-10-30")]
        result = LocalTimeGate(news).select(question())
        self.assertEqual(len(result["articles"]), 2)
        self.assertEqual(result["audit"]["historical_version_unverified_count"], 2)

    def test_present_invalid_windows_cannot_fall_back_to_legacy_date_filter(self):
        mutations = [
            lambda c: c.update(time_windows=None),
            lambda c: c.update(time_windows={}),
            lambda c: c["time_windows"].update(status="unconfirmed"),
            lambda c: c["time_windows"].pop("forecast"),
            lambda c: c["time_windows"].pop("information_cutoff_utc"),
            lambda c: c["time_windows"]["known"].update(status="closed"),
            lambda c: c["time_windows"]["forecast"].update(calendar_status="unconfirmed"),
            lambda c: c["time_windows"]["known"].update(trading_date="2026-10-29"),
            lambda c: c["time_windows"]["forecast"].update(trading_date="2026-11-03"),
            lambda c: c["time_windows"]["known"].update(start_utc=None),
            lambda c: c["time_windows"]["known"].update(start_utc="2026-10-29T13:00:00"),
            lambda c: c["time_windows"].update(information_cutoff_utc="2026-10-30T08:00:00Z"),
            lambda c: c["time_windows"]["forecast"].update(segments=[]),
            lambda c: c["time_windows"]["forecast"]["segments"][0].update(start_utc="2026-10-30T12:59:00Z"),
            lambda c: c["time_windows"]["known"]["segments"][1].update(end_utc="2026-10-30T03:00:00Z"),
        ]
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                case = question()
                mutate(case)
                result = LocalTimeGate([article()]).select(case)
                self.assertEqual(result["articles"], [])
                self.assertFalse(result["audit"]["time_windows_validated"])
                self.assertTrue(result["audit"]["time_windows_error"])
                self.assertIn("cutoff_utc", result["audit"])
                selected = LocalTimeGate.select_indicator_rows(case, [{"date": "2026-10-30", "close": 10}],
                                                                "2026-10-29", "2026-10-30")
                self.assertFalse(selected["query"]["allowed"])
                self.assertEqual(selected["rows"], [])

    def test_overlapping_or_reversed_windows_fail_closed(self):
        case = question()
        forecast = case["time_windows"]["forecast"]
        forecast.update(start_utc="2026-10-30T06:00:00Z")
        forecast["segments"][0]["start_utc"] = forecast["start_utc"]
        result = LocalTimeGate([article()]).select(case)
        self.assertEqual(result["articles"], [])
        self.assertEqual(result["audit"]["time_windows_error"], "overlapping_known_forecast_time_windows")
        case = question()
        case["time_windows"]["known"]["start_utc"] = "2026-10-30T08:00:00Z"
        result = LocalTimeGate([article()]).select(case)
        self.assertEqual(result["audit"]["time_windows_error"], "reversed_known_time_window")

    def test_indicator_queries_validate_exact_windows_and_still_reject_target_dates(self):
        good = LocalTimeGate.select_indicator_rows(question(), [
            {"date": "2026-10-29", "close": 1}, {"date": "2026-10-30", "close": 2},
            {"date": "2026-11-02", "close": 999}], "2026-10-29", "2026-10-30")
        self.assertTrue(good["query"]["allowed"])
        self.assertEqual(good["query"]["cutoff_utc"], "2026-10-30T07:00:00Z")
        self.assertEqual([item["date"] for item in good["rows"]], ["2026-10-29", "2026-10-30"])
        self.assertFalse(LocalTimeGate.validate_indicator_query(question(), "2026-10-29", "2026-11-02")["allowed"])

    def test_display_only_windows_do_not_change_frozen_legacy_protocol(self):
        legacy = question(False)
        legacy["display_time_windows"] = {"status": "unconfirmed"}
        result = LocalTimeGate([article(published="")]).select(legacy)
        self.assertEqual(len(result["articles"]), 1)
        self.assertEqual(result["audit"]["protocol"], PROTOCOL)
        self.assertNotIn("time_windows_validated", result["audit"])

    def test_empty_archive_still_records_invalid_time_boundary(self):
        case = question()
        case["time_windows"] = None
        result = LocalTimeGate([]).select(case)
        self.assertEqual(result["articles"], [])
        self.assertFalse(result["audit"]["time_windows_validated"])
        self.assertEqual(result["audit"]["cutoff_utc"], None)


if __name__ == "__main__":
    unittest.main()
