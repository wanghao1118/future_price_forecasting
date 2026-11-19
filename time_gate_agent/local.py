"""Offline env01 gate over an immutable snapshot of already collected news."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
from zoneinfo import ZoneInfo


PROTOCOL = "env01-local-news-original-publication-v1"
UTC_PROTOCOL = "env01-local-news-original-publication-utc-v2"
RETRIEVAL_POLICY = "forecast-start-minus-2h-utc-v1"
CURRENT_RETRIEVAL_POLICY = "forecast-start-minus-30m-utc-v1"
RETRIEVAL_POLICY_MINUTES = {RETRIEVAL_POLICY: 120, CURRENT_RETRIEVAL_POLICY: 30}
SUPPORTED_RETRIEVAL_POLICIES = frozenset(RETRIEVAL_POLICY_MINUTES)


def _day(value):
    if not isinstance(value, str) or not value:
        raise ValueError("unknown_original_publication_date")
    result = date.fromisoformat(value[:10]).isoformat()
    if len(value) > 10:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            raise ValueError("publication_timestamp_without_timezone")
    return result


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _eligibility_day(value, timezone_name):
    original = _day(value)
    if len(value) <= 10:
        return original
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(ZoneInfo(timezone_name)).date().isoformat()


def _timestamp(value, reason):
    """Require an explicit offset and at least minute precision; never infer it."""
    if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})", value):
        raise ValueError(reason)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(reason)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise ValueError(reason)


def _utc_text(value):
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _exact_boundary(case):
    """New question windows are mandatory and fail closed when inconsistent.

    Absence of the new field deliberately retains the frozen legacy protocol.
    A present but empty/null/unconfirmed field never falls back to date rules.
    """
    result = {"enabled": "time_windows" in case, "cutoff": None, "error": ""}
    if not result["enabled"]:
        return result
    try:
        windows = case["time_windows"]
        if not isinstance(windows, dict) or windows.get("status") != "ready":
            raise ValueError("unconfirmed_forecast_time_windows")
        result["cutoff"] = _timestamp(windows.get("information_cutoff_utc"), "invalid_information_cutoff_utc")
        intervals = {}
        for name, expected_day in (("known", _day(case["input_date"])), ("forecast", _day(case["target_date"]))):
            window = windows.get(name)
            if (not isinstance(window, dict) or window.get("status") != "regular_schedule"
                    or window.get("calendar_status") != "verified_2026"):
                raise ValueError("unconfirmed_{}_time_window".format(name))
            if window.get("trading_date") != expected_day:
                raise ValueError("{}_trading_date_mismatch".format(name))
            start = _timestamp(window.get("start_utc"), "invalid_{}_start_utc".format(name))
            end = _timestamp(window.get("end_utc"), "invalid_{}_end_utc".format(name))
            if start >= end:
                raise ValueError("reversed_{}_time_window".format(name))
            segments = window.get("segments")
            if not isinstance(segments, list) or not segments:
                raise ValueError("missing_{}_time_segments".format(name))
            previous_end = None
            for index, segment in enumerate(segments):
                if not isinstance(segment, dict):
                    raise ValueError("invalid_{}_time_segments".format(name))
                segment_start = _timestamp(segment.get("start_utc"), "invalid_{}_segment_start_utc".format(name))
                segment_end = _timestamp(segment.get("end_utc"), "invalid_{}_segment_end_utc".format(name))
                if (segment_start >= segment_end or segment_start < start or segment_end > end
                        or previous_end is not None and segment_start < previous_end
                        or index == 0 and segment_start != start):
                    raise ValueError("inconsistent_{}_time_segments".format(name))
                previous_end = segment_end
            if previous_end != end:
                raise ValueError("inconsistent_{}_time_segments".format(name))
            intervals[name] = (start, end)
        if result["cutoff"] != intervals["known"][1]:
            raise ValueError("information_cutoff_known_end_mismatch")
        if intervals["known"][1] > intervals["forecast"][0]:
            raise ValueError("overlapping_known_forecast_time_windows")
    except (ValueError, TypeError, KeyError) as error:
        result["error"] = str(error) if isinstance(error, ValueError) else "invalid_forecast_time_windows"
    return result


class LocalTimeGate:
    """Date filtering without web requests, label reads or additional model calls.

    Historical backfills retain original publication dates. Collection after
    the forecast date is explicitly marked as an unverified historical version;
    it never substitutes for the original publication date.
    """

    name = "time gate agent"
    protocol = PROTOCOL

    def __init__(self, articles, max_articles=8):
        self.articles = json.loads(json.dumps(list(articles), ensure_ascii=False))
        self.snapshot_sha256 = _digest(self.articles)
        self.max_articles = max(0, min(30, int(max_articles)))

    @staticmethod
    def retrieval_boundary(case):
        """Resolve the explicitly frozen retrieval policy without date fallback."""
        result = {"allowed": False, "code": "unsupported_retrieval_time_policy",
                  "cutoff_utc": None, "forecast_start_utc": None,
                  "policy": case.get("retrieval_time_policy"), "timezone": "UTC",
                  "buffer_hours": 2,
                  "message": "题目未启用已知的UTC精确检索时间策略。"}
        if result["policy"] == CURRENT_RETRIEVAL_POLICY:
            result.pop("buffer_hours")
            result["buffer_minutes"] = 30
        if not isinstance(result["policy"], str) or result["policy"] not in SUPPORTED_RETRIEVAL_POLICIES:
            return result
        boundary = _exact_boundary(case)
        error = boundary["error"] or ("missing_forecast_time_windows" if not boundary["enabled"] else "")
        try:
            if not error and _day(case["input_date"]) >= _day(case["target_date"]):
                error = "forecast_input_must_precede_target"
        except (ValueError, TypeError, KeyError):
            error = "invalid_forecast_time_windows"
        if error:
            result.update(code=error, message="题目UTC时间区间缺失、未核实或不一致，不能释放检索内容。")
            return result
        start = _timestamp(case["time_windows"]["forecast"]["start_utc"], "invalid_forecast_start_utc")
        result.update(allowed=True, code="allowed", forecast_start_utc=_utc_text(start),
                      cutoff_utc=_utc_text(start - timedelta(minutes=RETRIEVAL_POLICY_MINUTES[result["policy"]])),
                      message=("检索截止为冻结预测区间开始前2小时，所有时间使用UTC。" if result["policy"] == RETRIEVAL_POLICY else
                               "检索截止为冻结预测区间开始前30分钟，所有时间使用UTC。"))
        return result

    @classmethod
    def validate_indicator_time_query(cls, case, start_time, end_time=None):
        """Validate an exact UTC interval before any indicator row is exposed."""
        boundary = cls.retrieval_boundary(case)
        result = dict(boundary, agent=cls.name, allowed_start_time="2026-01-01T00:00:00Z",
                      allowed_end_time=boundary["cutoff_utc"], start_time=None, end_time=None)
        if not boundary["allowed"]:
            result["retryable"] = False
            return result
        result["allowed"] = False
        try:
            start = _timestamp(start_time, "invalid_start_time")
            end = _timestamp(end_time if end_time is not None else boundary["cutoff_utc"], "invalid_end_time")
            result.update(start_time=_utc_text(start), end_time=_utc_text(end))
            cutoff = _timestamp(boundary["cutoff_utc"], "invalid_retrieval_cutoff_utc")
            earliest = _timestamp(result["allowed_start_time"], "invalid_history_start")
            if start > end:
                code, message = "reversed_time_range", "start_time 不能晚于 end_time。"
            elif end > cutoff:
                code, message = "future_time_range", "end_time 超过本题允许的UTC检索截止时间。"
            elif start < earliest:
                code, message = "before_history_start", "技术指标查询不能早于2026-01-01T00:00:00Z。"
            else:
                result.update(allowed=True, code="allowed", message="查询UTC时间范围合法。")
                return result
        except ValueError as error:
            code, message = str(error), "start_time 和 end_time 必须至少精确到时分且带有时区。"
        result.update(code=code, message=message + " 请调整时间后重新调用 search_indicators。", retryable=True)
        return result

    @classmethod
    def select_indicator_time_rows(cls, case, rows, start_time, end_time=None):
        """Gate explicit availability, observation and revision instants together.

        A calendar date or epoch number never implies a publication instant. If
        both ISO observation and availability times exist, the later one wins;
        a later source revision moves effective availability forward too.
        """
        query = cls.validate_indicator_time_query(case, start_time, end_time)
        if not query["allowed"]:
            return {"query": query, "rows": [], "checks": []}
        start = _timestamp(query["start_time"], "invalid_start_time")
        end = _timestamp(query["end_time"], "invalid_end_time")
        cutoff = _timestamp(query["cutoff_utc"], "invalid_retrieval_cutoff_utc")
        approved, checks = [], []
        for index, row in enumerate(rows):
            reason, available, normalized = "allowed", None, {}
            try:
                if not isinstance(row, dict):
                    raise ValueError("invalid_indicator_row")
                if row.get("availability_dependency_unverified"):
                    raise ValueError("indicator_dependency_time_unverified")
                instants = []
                for field in ("available_at", "timestamp"):
                    if field in row and row[field] is not None:
                        instant = _timestamp(row[field], "invalid_row_" + field)
                        instants.append(instant)
                        normalized[field] = _utc_text(instant)
                if not instants:
                    raise ValueError("missing_row_availability_time")
                for field in ("source_modified_at", "article_modified_at", "modified_at"):
                    if row.get(field):
                        modified = _timestamp(row[field], "source_revision_timestamp_unverified")
                        if modified > end:
                            raise ValueError("source_revision_after_requested_end_time")
                        instants.append(modified)
                        normalized[field] = _utc_text(modified)
                available = max(instants)
                if available > cutoff:
                    raise ValueError("row_after_retrieval_cutoff_utc")
                if not start <= available <= end:
                    raise ValueError("outside_requested_time_range")
            except ValueError as error:
                reason = str(error)
            checks.append({"row_index": index, "available_at": _utc_text(available),
                           "accepted": reason == "allowed", "reason": reason})
            if reason == "allowed":
                normalized["available_at"] = _utc_text(available)
                approved.append(dict(row, **normalized))
        return {"query": query,
                "rows": sorted(approved, key=lambda row: _timestamp(row["available_at"], "invalid_row_available_at")),
                "checks": checks}

    @staticmethod
    def validate_indicator_query(case, start_date, end_date):
        """Reject an illegal interval in full, before retrieving any observations."""
        input_day = date.fromisoformat(case["input_date"])
        target_day = date.fromisoformat(case["target_date"])
        if input_day >= target_day:
            raise ValueError("forecast_input_must_precede_target")
        latest = min(input_day, target_day - timedelta(days=1)).isoformat()
        earliest = "2026-01-01"
        result = {"agent": "time gate agent", "allowed": False,
                  "allowed_start_date": earliest, "allowed_end_date": latest,
                  "input_date": input_day.isoformat(), "target_date": target_day.isoformat()}
        boundary = _exact_boundary(case)
        if boundary["enabled"]:
            result["cutoff_utc"] = _utc_text(boundary["cutoff"])
            if boundary["error"]:
                result.update(code=boundary["error"], message="题目UTC时间区间缺失、未核实或不一致，不能释放技术指标。", retryable=False)
                return result
        try:
            for value in (start_date, end_date):
                if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
                    raise ValueError("invalid_date")
        except (ValueError, TypeError):
            code, message = "invalid_date", "start_date 和 end_date 必须是有效的 YYYY-MM-DD 日期。"
        else:
            if start_date > end_date:
                code, message = "reversed_date_range", "start_date 不能晚于 end_date。"
            elif end_date > latest:
                code, message = "future_date_range", "查询结束日期 {} 超过允许上限 {}；不能查询预测日 {} 或之后，也不能超过题目输入日 {}。".format(end_date, latest, target_day, input_day)
            elif start_date < earliest:
                code, message = "before_history_start", "技术历史查询从 {} 开始，请修改 start_date。".format(earliest)
            else:
                result.update(allowed=True, code="allowed", message="查询日期合法。")
                return result
        result.update(code=code, message=message + " 请调整日期后重新调用 search_indicators。", retryable=True)
        return result

    @classmethod
    def select_indicator_rows(cls, case, rows, start_date, end_date):
        """Validate the request, then independently check every recalled row."""
        query = cls.validate_indicator_query(case, start_date, end_date)
        if not query["allowed"]:
            return {"query": query, "rows": [], "checks": []}
        approved, checks = [], []
        for index, row in enumerate(rows):
            day = row.get("date") or row.get("trading_date")
            try:
                valid = isinstance(day, str) and date.fromisoformat(day).isoformat() == day
            except (ValueError, TypeError):
                valid = False
            reason = ("invalid_row_date" if not valid else
                      "after_input_or_target_date" if day > query["allowed_end_date"] else
                      "outside_requested_range" if not start_date <= day <= end_date else "allowed")
            checks.append({"row_index": index, "date": day, "accepted": reason == "allowed", "reason": reason})
            if reason == "allowed":
                approved.append(dict(row, date=day))
        return {"query": query, "rows": sorted(approved, key=lambda row: row["date"]), "checks": checks}

    def select(self, case):
        input_day = _day(case["input_date"])
        target_day = _day(case["target_date"])
        if input_day >= target_day:
            raise ValueError("forecast_input_must_precede_target")
        family = case.get("family")
        timezone_name = case.get("exchange_timezone") or ("Asia/Shanghai" if family == "SC" else "America/New_York")
        boundary = _exact_boundary(case)
        retrieval = self.retrieval_boundary(case) if "retrieval_time_policy" in case else None
        if retrieval is not None:
            boundary = {"enabled": True,
                        "cutoff": _timestamp(retrieval["cutoff_utc"], "invalid_retrieval_cutoff_utc") if retrieval["allowed"] else None,
                        "error": "" if retrieval["allowed"] else retrieval["code"]}
        accepted = []
        reasons = Counter()
        decisions = []
        for article in self.articles:
            reason = ""
            published = None
            eligibility_day = None
            historical_unverified = False
            published_utc = None
            try:
                if boundary["error"]:
                    raise ValueError(boundary["error"])
                if article.get("is_demo"):
                    raise ValueError("demonstration_record")
                if article.get("agent_review", {}).get("relevance") is False:
                    raise ValueError("excluded_by_archive_reviewer")
                if not article.get("ingestion"):
                    raise ValueError("not_a_crawled_record")
                if not str(article.get("body", "")).strip():
                    raise ValueError("missing_article_body")
                assets = article.get("assets") or []
                if assets and family not in assets:
                    raise ValueError("different_futures_category")
                metadata_day = article.get("ingestion", {}).get("published_date")
                if article["ingestion"].get("timezone_status") in {"unverified_timezone", "unverified_time"}:
                    raise ValueError("unverified_publication_time")
                precise = article.get("published_at")
                published = _day(precise or metadata_day)
                if precise and metadata_day and _day(metadata_day) != published:
                    raise ValueError("conflicting_original_publication_dates")
                if retrieval is None:
                    eligibility_day = _eligibility_day(precise or metadata_day, timezone_name)
                if boundary["enabled"]:
                    published_utc = _timestamp(precise or metadata_day, "original_publication_timestamp_unverified")
                    if retrieval is not None:
                        eligibility_day = published_utc.date().isoformat()
                    if published_utc > boundary["cutoff"]:
                        raise ValueError("publication_after_information_cutoff_utc")
                else:
                    if eligibility_day >= target_day:
                        raise ValueError("on_or_after_target_day")
                    if eligibility_day > input_day:
                        raise ValueError("after_input_day")
                # Database updated_at is an archive edit, not a website date.
                for field in ("source_modified_at", "article_modified_at", "modified_at"):
                    if article.get(field):
                        if boundary["enabled"]:
                            modified = _timestamp(article[field], "source_revision_timestamp_unverified")
                            if modified > boundary["cutoff"]:
                                raise ValueError("source_revision_after_information_cutoff_utc")
                        elif _eligibility_day(article[field], timezone_name) > input_day:
                            raise ValueError("source_revision_after_input_day")
                collected = article.get("collected_at")
                if boundary["enabled"]:
                    try:
                        historical_unverified = _timestamp(collected, "unknown_collection_timestamp") > boundary["cutoff"]
                    except ValueError:
                        historical_unverified = True
                else:
                    historical_unverified = not collected or _eligibility_day(collected, timezone_name) > input_day
            except (ValueError, TypeError, AttributeError) as exc:
                reason = str(exc) if isinstance(exc, ValueError) else "invalid_article_metadata"
            row = {"id": article.get("id"), "published_date": published,
                   "eligibility_date": eligibility_day, "exchange_timezone": timezone_name,
                   "decision": "deny" if reason else "eligible", "reason": reason,
                   "historical_version_unverified": historical_unverified}
            if boundary["enabled"]:
                row["published_at_utc"] = _utc_text(published_utc)
            decisions.append(row)
            if reason:
                reasons[reason] += 1
                continue
            public = {key: article.get(key, "") for key in
                      ("id", "title", "body", "source_name", "source_url", "published_at", "collected_at")}
            public["published_date"] = published
            public["publication_precision"] = article["ingestion"].get("publication_precision", "")
            public["timezone_status"] = article["ingestion"].get("timezone_status", "")
            public["eligibility_date"] = eligibility_day
            public["historical_version_unverified"] = historical_unverified
            if retrieval is not None:
                public["published_at"] = _utc_text(published_utc)
                public["published_at_utc"] = _utc_text(published_utc)
                for field in ("source_modified_at", "article_modified_at", "modified_at"):
                    if article.get(field):
                        public[field] = _utc_text(_timestamp(article[field], "source_revision_timestamp_unverified"))
            accepted.append(public)
        if retrieval is not None:
            accepted.sort(key=lambda item: (_timestamp(item["published_at"], "original_publication_timestamp_unverified"),
                                            item.get("id", "")), reverse=True)
        else:
            accepted.sort(key=lambda item: (item["published_date"], item.get("published_at", ""), item.get("id", "")), reverse=True)
        selected = accepted[:self.max_articles]
        selected_ids = {item["id"] for item in selected}
        for row in decisions:
            if row["decision"] == "eligible":
                row["decision"] = "allow" if row["id"] in selected_ids else "eligible_not_selected"
        audit = {"agent": self.name, "protocol": self.protocol, "case_id": case["case_id"],
                 "input_date": input_day, "cutoff_exclusive": target_day,
                 "exchange_timezone": timezone_name,
                 "news_snapshot_sha256": self.snapshot_sha256,
                 "examined": len(self.articles), "eligible": len(accepted),
                 "selected": len(selected), "rejected": sum(reasons.values()),
                 "rejection_counts": dict(reasons), "decisions": decisions,
                 "network_used": False,
                 "historical_version_unverified_count": sum(item["historical_version_unverified"] for item in selected)}
        if boundary["enabled"]:
            audit.update(protocol=UTC_PROTOCOL, cutoff_utc=_utc_text(boundary["cutoff"]),
                         time_windows_validated=not bool(boundary["error"]),
                         time_windows_error=boundary["error"])
        if retrieval is not None:
            audit.update(protocol=retrieval["policy"], retrieval_time_policy=retrieval["policy"],
                         retrieval_boundary=retrieval, forecast_start_utc=retrieval["forecast_start_utc"],
                         timezone="UTC")
            if retrieval["policy"] == CURRENT_RETRIEVAL_POLICY:
                audit["buffer_minutes"] = 30
            else:
                audit["buffer_hours"] = 2
        return {"articles": selected, "audit": audit}
