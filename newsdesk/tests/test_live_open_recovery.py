"""Recover already published live QA across midnight without moving cutoff."""

import copy
import unittest

from newsdesk.evaluation import _validate_live_open


def live_case(family, input_date, published_at, forecast_start, zone):
    return {"case_id": "published-" + family, "family": family, "input_date": input_date,
            "exchange_timezone": zone, "live_status": "ready", "input_is_today": True,
            "input_ready_at": published_at, "forecast_deadline": forecast_start,
            "time_windows": {"timezone": "UTC", "forecast": {"start_utc": forecast_start}}}


class LiveOpenRecoveryTests(unittest.TestCase):
    def brent(self):
        return live_case("Brent", "2026-11-04", "2026-11-04T23:35:00Z",
                         "2026-11-05T01:00:00Z", "Europe/London")

    def wti_weekend(self):
        return live_case("WTI", "2026-11-06", "2026-11-06T22:35:00Z",
                         "2026-11-08T23:00:00Z", "America/New_York")

    def test_brent_can_resume_after_london_midnight_before_cutoff(self):
        case = self.brent()
        before = copy.deepcopy(case)
        _validate_live_open(case, "2026-11-05T00:15:00Z")
        self.assertEqual(case, before, "Frozen publication evidence must not be rewritten")
        self.assertIs(case["input_is_today"], True)

    def test_wti_friday_question_can_resume_on_saturday_and_sunday(self):
        case = self.wti_weekend()
        for at in ("2026-11-07T12:00:00Z", "2026-11-08T22:29:59Z"):
            with self.subTest(at=at):
                _validate_live_open(case, at)

    def test_equal_or_later_than_strict_cutoff_is_rejected(self):
        for case, at in ((self.brent(), "2026-11-05T00:30:00Z"),
                         (self.brent(), "2026-11-05T00:45:00Z"),
                         (self.wti_weekend(), "2026-11-08T22:30:00Z")):
            with self.subTest(family=case["family"], at=at), self.assertRaisesRegex(ValueError, "截止时间"):
                _validate_live_open(case, at)

    def test_old_input_without_valid_original_publication_cannot_resume(self):
        for published in (None, "", "invalid", "2026-11-04T23:35:00", "2026-11-03T23:35:00Z"):
            case = self.brent()
            case["input_ready_at"] = published
            case["published_at"] = "2026-11-04T23:35:00Z"
            with self.subTest(published=published), self.assertRaises(ValueError):
                _validate_live_open(case, "2026-11-05T00:15:00Z")

    def test_waiting_previews_and_false_frozen_readiness_are_rejected(self):
        for state in (None, "pending", "waiting_new_completed_bar", "forecast_closed"):
            case = self.brent()
            case["live_status"] = state
            with self.subTest(state=state), self.assertRaises(ValueError):
                _validate_live_open(case, "2026-11-05T00:15:00Z")
        case = self.brent()
        case["input_is_today"] = False
        with self.assertRaises(ValueError):
            _validate_live_open(case, "2026-11-05T00:15:00Z")

    def test_future_publication_cannot_be_used_even_on_the_same_input_day(self):
        case = self.brent()
        for at in ("2026-11-04T23:34:59Z", "2026-11-04T23:00:00Z"):
            with self.subTest(at=at), self.assertRaisesRegex(ValueError, "尚未发布"):
                _validate_live_open(case, at)
        case["input_ready_at"] = "2026-11-05T00:20:00Z"
        with self.assertRaisesRegex(ValueError, "尚未发布"):
            _validate_live_open(case, "2026-11-05T00:15:00Z")

    def test_timezone_is_used_for_publication_day_and_equal_publication_is_allowed(self):
        case = self.wti_weekend()
        case["input_ready_at"] = "2026-11-07T00:15:00Z"  # Still Friday in New York.
        _validate_live_open(case, "2026-11-07T00:15:00Z")
        _validate_live_open(case, "2026-11-08T20:00:00Z")
        case["input_ready_at"] = "2026-11-07T05:00:00Z"  # Saturday in New York.
        with self.assertRaises(ValueError):
            _validate_live_open(case, "2026-11-08T20:00:00Z")

    def test_same_day_legacy_without_publication_remains_compatible(self):
        case = self.brent()
        case.pop("input_ready_at")
        case.pop("live_status")
        _validate_live_open(case, "2026-11-04T23:40:00Z")
        with self.assertRaises(ValueError):
            _validate_live_open(case, "2026-11-05T00:15:00Z")


if __name__ == "__main__":
    unittest.main()
