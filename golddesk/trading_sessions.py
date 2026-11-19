"""Gold futures reference sessions with explicit UTC labels."""
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

SOURCE = "https://www.shfe.com.cn/statements/marketdata/"


def _utc(day, clock):
    return datetime.combine(day, time(*clock), tzinfo=ZoneInfo("Asia/Shanghai")).astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def session_rules(family):
    return {
        "timezone": "UTC", "display_timezone": "UTC+0",
        "date_kind": "exchange_trading_date",
        "time_anchor": "日K time 为交易日分类锚点，不代表 UTC 00:00 开盘。",
        "description": "上海期货交易所黄金常规时段按 Asia/Shanghai 换算为 UTC+0；节假日和临时调整以交易所公告为准。",
        "supplier_aggregation_verified": False, "source_url": SOURCE,
        "calendar_source_url": SOURCE,
    }


def daily_session(family, trading_date):
    if family != "AU":
        return None
    try:
        day = date.fromisoformat(trading_date)
    except (TypeError, ValueError):
        return None
    if day.weekday() >= 5:
        return {"timezone": "UTC", "trading_date": trading_date, "kind": "closed", "status": "closed",
                "calendar_status": "unconfirmed", "start_utc": None, "end_utc": None, "segments": [],
                "label": "周末无常规交易时段。", "source_url": SOURCE, "calendar_source_url": SOURCE,
                "supplier_aggregation_verified": False, "notes": []}
    previous = day - timedelta(days=1)
    segments = [{"start_utc": _utc(previous, (21, 0)), "end_utc": _utc(day, (2, 30)), "label": "夜盘"},
                {"start_utc": _utc(day, (9, 0)), "end_utc": _utc(day, (15, 0)), "label": "日盘（含休市间隔）"}]
    return {"timezone": "UTC", "trading_date": trading_date, "kind": "regular_schedule",
            "status": "regular_schedule", "calendar_status": "unconfirmed",
            "exchange_timezone": "Asia/Shanghai", "start_utc": segments[0]["start_utc"],
            "end_utc": segments[-1]["end_utc"], "segments": segments,
            "label": "上海黄金常规交易时段（UTC+0；节假日安排待交易所公告确认）",
            "source_url": SOURCE, "calendar_source_url": SOURCE,
            "supplier_aggregation_verified": False, "notes": [session_rules(family)["description"]]}


def aggregate_sessions(sessions, period):
    confirmed = [s for s in sessions if isinstance(s, dict) and s.get("timezone") == "UTC" and s.get("start_utc") and s.get("end_utc")]
    if not confirmed:
        return None
    return {"timezone": "UTC", "kind": "aggregated_schedule",
            "start_utc": min(s["start_utc"] for s in confirmed),
            "end_utc": max(s["end_utc"] for s in confirmed),
            "label": "{}覆盖{}个交易日的上海黄金常规时段".format("周K" if period == "1w" else "月K", len(confirmed)),
            "trading_dates": [s.get("trading_date") for s in sessions if s.get("trading_date")],
            "segments": [part for s in confirmed for part in s.get("segments", [])],
            "source_url": SOURCE}
