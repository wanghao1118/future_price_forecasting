"""UTC reference sessions without rewriting vendor trading-date identifiers.

These are exchange schedules, not verified vendor OHLC aggregation boundaries.
Known holidays are treated separately; unknown exceptions never become exact
session claims. All UTC instants are explicit ISO strings ending in Z.
"""

import copy
from datetime import date, datetime, time, timedelta, timezone
import re
from zoneinfo import ZoneInfo


SOURCES = {
    "WTI": "https://www.cmegroup.com/cn-s/markets/energy/crude-oil/light-sweet-crude.contractSpecs.html",
    "Brent": "https://www.cmegroup.com/cn-s/markets/energy/crude-oil/brent-crude-oil-last-day.contractSpecs.html",
    "SC": "https://www.ine.com.cn/eng/reports/calendarholidays/tradingtime/index.html",
}
INE_HOLIDAY_SOURCE = "https://www.ine.cn/services/calenderandholidays/holiday/"
CME_HOLIDAY_SOURCE = "https://www.cmegroup.com/trading-hours.html"
CME_LABOR_DAY_2026_SOURCE = CME_HOLIDAY_SOURCE + "#tradeDate=2026-09-08"
CME_COMBINED_TRADE_DATES = {
    "WTI": {"2026-09-08": {"no_separate_trade_dates": ["2026-09-07"],
                            "source_url": CME_LABOR_DAY_2026_SOURCE}},
}
ICE_BRENT_SOURCE = "https://www.ice.com/products/219/Brent-Crude-Oil-Futures"
ICE_HOLIDAY_SOURCE = "https://www.ice.com/publicdocs/Trading_Schedule.pdf"
ICE_DST_SOURCE = "https://www.ice.com/publicdocs/futures/Trading_Schedule_Temporary_Trading_Hours_for_DST.pdf"
ICE_VERIFIED_EARLY_CLOSES = {"2026-09-07": {"reason": "2026_labor_day_type_C", "source_url": ICE_HOLIDAY_SOURCE}}
ICE_CLOSED_2026 = frozenset(("2026-01-01", "2026-04-03", "2026-12-25"))
# Product-specific early closes are not normalized here; do not advertise an
# exact full-session window for these published holiday/special dates.
# August 7 and 31 are TD entries whose adjustments concern other products;
# ICE Brent retains its regular schedule on both dates.
ICE_SPECIAL_2026 = frozenset(("2026-01-02", "2026-01-19", "2026-02-16", "2026-04-02", "2026-04-06",
                              "2026-04-30", "2026-05-04", "2026-05-25", "2026-05-29", "2026-06-19",
                              "2026-07-03", "2026-09-07", "2026-11-06",
                              "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-28", "2026-12-31"))
INE_CLOSED_2026 = (
    ("2026-01-01", "2026-01-03"), ("2026-02-15", "2026-02-23"),
    ("2026-04-04", "2026-04-06"), ("2026-05-01", "2026-05-05"),
    ("2026-06-19", "2026-06-21"), ("2026-09-25", "2026-09-27"),
    ("2026-10-01", "2026-10-07"),
)
INE_NO_NIGHT_STARTS = frozenset(("2025-12-31", "2026-02-13", "2026-04-03", "2026-04-30",
                                 "2026-06-18", "2026-09-24", "2026-09-30"))
# CME publishes these holiday periods. Exact product-specific opening/early
# close times were not verified here, so affected dates remain unconfirmed.
CME_SPECIAL_PERIODS = (
    ("2025-12-31", "2026-01-02"), ("2026-01-18", "2026-01-20"),
    ("2026-02-15", "2026-02-17"), ("2026-04-02", "2026-04-04"),
    ("2026-05-24", "2026-05-26"), ("2026-06-18", "2026-06-19"),
    ("2026-07-03", "2026-07-05"), ("2026-09-06", "2026-09-08"),
    ("2026-11-26", "2026-11-28"), ("2026-12-24", "2026-12-26"),
    ("2026-12-31", "2027-01-01"),
)


def _day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _inside(day, intervals):
    return any(start <= day.isoformat() <= end for start, end in intervals)


def _utc(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _segment(start_day, start_clock, end_day, end_clock, zone, label):
    tz = ZoneInfo(zone)
    return {"start_utc": _utc(datetime.combine(start_day, time(*start_clock), tzinfo=tz)),
            "end_utc": _utc(datetime.combine(end_day, time(*end_clock), tzinfo=tz)), "label": label}


def _label(day, segments):
    beginning, ending = segments[0]["start_utc"], segments[-1]["end_utc"]
    start_day, end_day = date.fromisoformat(beginning[:10]), date.fromisoformat(ending[:10])
    delta = (day - start_day).days
    relative = "起于前一日" if delta == 1 else "起于前{}日".format(delta) if delta > 1 else "起于当日" if delta == 0 else "起于次日"
    if end_day > day:
        relative += "，止于次日" if (end_day - day).days == 1 else "，止于后{}日".format((end_day - day).days)
    if len(segments) > 1:
        relative += "；分段交易，含休市间隔"
    return "{} → {}（UTC+0；{}）".format(beginning[:16].replace("T", " "), ending[:16].replace("T", " "), relative)


def _ine_open(day):
    return day.weekday() < 5 and not _inside(day, INE_CLOSED_2026)


def session_rules(family):
    return {"timezone": "UTC", "display_timezone": "UTC+0", "date_kind": "exchange_trading_date",
            "time_anchor": "日K time 为交易日分类锚点，不代表UTC 00:00开盘。",
            "description": "时段为交易所常规时间表的UTC换算；供应商日K的精确OHLC聚合边界未经独立核实，节假日、到期及临时调整以交易所公告为准。",
            "supplier_aggregation_verified": False, "source_url": SOURCES.get(family, ""),
            "calendar_source_url": INE_HOLIDAY_SOURCE if family == "SC" else CME_HOLIDAY_SOURCE}


def no_separate_trade_date_assignment(family, trading_date):
    """Return verified trade-date reassignment, independent of physical closure."""
    for assigned, exception in CME_COMBINED_TRADE_DATES.get(family, {}).items():
        if trading_date in exception["no_separate_trade_dates"]:
            return {"date": trading_date, "assigned_trading_date": assigned,
                    "reason": "verified_exchange_calendar_combined_trade_date", "source_url": exception["source_url"]}
    return None


def confirmed_no_separate_trade_date(family, trading_date):
    return no_separate_trade_date_assignment(family, trading_date) is not None


def daily_session(family, trading_date):
    day = _day(trading_date)
    if day is None or family not in SOURCES:
        return None
    rule = session_rules(family)
    result = {"timezone": "UTC", "trading_date": trading_date, "kind": "regular_schedule",
              "status": "regular_schedule", "calendar_status": "verified_2026" if day.year == 2026 else "unconfirmed",
              "start_utc": None, "end_utc": None, "segments": [], "label": "",
              "source_url": rule["source_url"], "calendar_source_url": rule["calendar_source_url"],
              "supplier_aggregation_verified": False, "notes": [rule["description"]]}
    assigned = no_separate_trade_date_assignment(family, trading_date)
    if assigned:
        result.update({"kind": "no_separate_trade_date", "status": "no_separate_trade_date",
                       "assigned_trading_date": assigned["assigned_trading_date"], "calendar_exception": assigned,
                       "source_url": assigned["source_url"], "calendar_source_url": assigned["source_url"],
                       "label": "{}的交易归属{}交易日，无独立日K日期。".format(trading_date, assigned["assigned_trading_date"]),
                       "notes": ["交易所能源交易日历把这些交易时段合并到后一交易日；该自然日仍包含交易，并非全天停市。"]})
        return result
    exception = CME_COMBINED_TRADE_DATES.get(family, {}).get(trading_date)
    if exception:
        segments = [
            _segment(date(2026, 9, 6), (17, 0), date(2026, 9, 7), (13, 30), "America/Chicago", "劳动节交易段1"),
            _segment(date(2026, 9, 7), (17, 0), date(2026, 9, 8), (16, 0), "America/Chicago", "劳动节交易段2"),
        ]
        result.update({"exchange_timezone": "America/Chicago", "schedule_type": "verified_extended_session",
                       "calendar_exception": {"type": "combined_trade_date", "assigned_trading_date": trading_date,
                                              "no_separate_trade_dates": list(exception["no_separate_trade_dates"]),
                                              "source_url": exception["source_url"]},
                       "start_utc": segments[0]["start_utc"], "end_utc": segments[-1]["end_utc"], "segments": segments,
                       "label": "劳动节延长交易日 · " + _label(day, segments),
                       "source_url": exception["source_url"], "calendar_source_url": exception["source_url"],
                       "contract_source_url": SOURCES[family],
                       "notes": ["按CME 2026年劳动节能源交易日历，两个交易片段均归属2026-09-08交易日。",
                                 "中间pre-open不计入交易；供应商日线的精确OHLC聚合边界仍未经独立核实。"]})
        return result
    if day.weekday() >= 5 or family == "SC" and day.year == 2026 and not _ine_open(day):
        result.update({"kind": "closed", "status": "closed", "label": "{} 为常规周末或官方休市日，无已确认交易时段。".format(trading_date)})
        return result
    if family in {"WTI", "Brent"}:
        if day.year != 2026 or _inside(day, CME_SPECIAL_PERIODS):
            result.update({"kind": "unconfirmed", "status": "unconfirmed", "calendar_status": "unconfirmed",
                           "label": "{} 节假日或特殊交易时段待核实，未将常规时间表当作实际边界。".format(trading_date)})
            return result
        result["exchange_timezone"] = "America/Chicago"
        segments = [_segment(day - timedelta(days=1), (17, 0), day, (16, 0), "America/Chicago", "常规交易时段")]
    else:
        result["exchange_timezone"] = "Asia/Shanghai"
        segments = []
        if day.year == 2026:
            previous = day - timedelta(days=1)
            while not _ine_open(previous):
                previous -= timedelta(days=1)
            if previous.isoformat() not in INE_NO_NIGHT_STARTS:
                segments.append(_segment(previous, (21, 0), previous + timedelta(days=1), (2, 30), "Asia/Shanghai", "夜盘"))
                result["night_session_status"] = "regular_schedule"
            else:
                result["night_session_status"] = "cancelled_for_holiday"
                result["notes"].append("官方节假日前一交易日晚不进行夜盘；节后首交易日从日盘开始。")
        else:
            result["night_session_status"] = "unconfirmed"
            result["notes"].append("该年度休市与节前夜盘安排尚未核实，未生成夜盘时段；仅列常规日盘参考。")
        segments.extend(_segment(day, opening, day, closing, "Asia/Shanghai", "日盘")
                        for opening, closing in (((9, 0), (10, 15)), ((10, 30), (11, 30)), ((13, 30), (15, 0))))
    result.update({"start_utc": segments[0]["start_utc"], "end_utc": segments[-1]["end_utc"],
                   "segments": segments, "label": _label(day, segments)})
    if family == "SC" and day.year != 2026:
        result["label"] += "；夜盘及休市安排未确认"
    return result


def continuous_daily_session(contract, trading_date):
    """Choose a continuous contract's exchange, keeping NYMEX BZ separate.

    ICE energy sessions are anchored to US Eastern time, including the weeks
    when US and UK clock changes differ. A Monday trading day opens Sunday
    18:00 New York; Tuesday-Friday sessions open the previous day at 20:00.
    """
    if not isinstance(contract, dict):
        return None
    family = contract.get("familyId") or contract.get("family")
    code = str(contract.get("code") or contract.get("contract_code") or "")
    exchange = str(contract.get("exchange") or "").upper()
    is_ice = family == "Brent" and (exchange in {"ICE", "IFEU", "ICE FUTURES EUROPE"}
                                         or not exchange and (code == "IB.1:IEU" or code.startswith("BRN")))
    if not is_ice:
        if family == "Brent" and exchange != "NYMEX" and not code.startswith("BZ"):
            return {"timezone": "UTC", "trading_date": trading_date, "kind": "unconfirmed", "status": "unconfirmed",
                    "calendar_status": "unconfirmed", "start_utc": None, "end_utc": None, "segments": [],
                    "label": "Brent来源交易所无法确认，未推测交易时段。", "supplier_aggregation_verified": False,
                    "source_url": "", "notes": ["需确认是ICE Brent还是NYMEX BZ后才能换算交易时段。"]}
        result = daily_session(family, trading_date)
        if result is not None:
            result["exchange"] = "INE" if family == "SC" else "NYMEX"
        return result
    day = _day(trading_date)
    if day is None:
        return None
    rule = session_rules("Brent")
    result = {"timezone": "UTC", "trading_date": trading_date, "exchange": "ICE",
              "exchange_timezone": "Europe/London", "schedule_anchor_timezone": "America/New_York",
              "kind": "regular_schedule", "status": "regular_schedule", "calendar_status": "verified_2026",
              "start_utc": None, "end_utc": None, "segments": [], "label": "",
              "source_url": ICE_BRENT_SOURCE, "calendar_source_url": ICE_HOLIDAY_SOURCE,
              "dst_source_url": ICE_DST_SOURCE, "supplier_aggregation_verified": False,
              "notes": [rule["description"], "ICE Brent主连使用ICE时段；纽约时间开收盘锚点随美国夏令时换算UTC，不套用NYMEX BZ。"]}
    if day.weekday() >= 5 or trading_date in ICE_CLOSED_2026:
        result.update({"kind": "closed", "status": "closed", "label": "{} 为周末或ICE官方休市日，无交易时段。".format(trading_date)})
    elif trading_date in ICE_VERIFIED_EARLY_CLOSES:
        segment = {"start_utc": _utc(datetime(2026, 9, 6, 18, tzinfo=ZoneInfo("America/New_York"))),
                   "end_utc": _utc(datetime(2026, 9, 7, 18, 30, tzinfo=ZoneInfo("Europe/London"))),
                   "label": "ICE劳动节提前收盘时段"}
        result.update({"schedule_type": "verified_early_close",
                       "calendar_exception": {"type": "early_close", "reason": "2026_labor_day_type_C",
                                              "close_local": "18:30", "timezone": "Europe/London", "source_url": ICE_HOLIDAY_SOURCE},
                       "start_utc": segment["start_utc"], "end_utc": segment["end_utc"], "segments": [segment],
                       "label": "ICE劳动节提前收盘 · " + _label(day, [segment]),
                       "source_url": ICE_HOLIDAY_SOURCE, "contract_source_url": ICE_BRENT_SOURCE,
                       "notes": ["按ICE 2026年交易日历C类安排，Brent于伦敦时间18:30提前收盘；开盘沿用星期日晚纽约18:00。",
                                 "供应商日线的精确OHLC聚合边界仍未经独立核实。"]})
    elif day.year != 2026 or trading_date in ICE_SPECIAL_2026:
        result.update({"kind": "unconfirmed", "status": "unconfirmed", "calendar_status": "unconfirmed",
                       "label": "{} ICE节假日或特殊交易时段待核实，未使用常规时间冒充实际边界。".format(trading_date)})
    else:
        segment = _segment(day - timedelta(days=1), (18 if day.weekday() == 0 else 20, 0),
                           day, (18, 0), "America/New_York", "ICE常规交易时段")
        result.update({"start_utc": segment["start_utc"], "end_utc": segment["end_utc"],
                       "segments": [segment], "label": _label(day, [segment])})
    return result


def confirmed_closed_day(family, trading_date):
    """Only positively known closures may bridge a missing-observation gap."""
    day = _day(trading_date)
    return day is not None and (day.weekday() >= 5 or family == "SC" and day.year == 2026 and not _ine_open(day))


def next_expected_trading_date(contract, trading_date):
    day = _day(trading_date)
    if day is None:
        raise ValueError("Invalid daily trading date")
    family = contract.get("familyId") or contract.get("family")
    day += timedelta(days=1)
    while confirmed_closed_day(family, day.isoformat()) or confirmed_no_separate_trade_date(family, day.isoformat()) or (
            family == "Brent" and continuous_daily_session(contract, day.isoformat()).get("kind") == "closed"):
        day += timedelta(days=1)
    return day.isoformat()


def aggregate_sessions(sessions, period):
    """Describe all included daily schedules, including gaps and unknown days."""
    values = [session for session in sessions if isinstance(session, dict)]
    if not values:
        return None
    segments = [copy.deepcopy(segment) for session in values for segment in session.get("segments", [])]
    unknown = any(session.get("kind") != "regular_schedule" or session.get("calendar_status") == "unconfirmed" for session in values)
    start, end = values[0]["trading_date"], values[-1]["trading_date"]
    result = {"timezone": "UTC", "kind": "aggregated_schedule", "status": "partial" if unknown else "regular_schedule",
              "period": period, "trading_date": start, "trading_date_start": start, "trading_date_end": end,
              "start_utc": values[0].get("start_utc"),
              "end_utc": values[-1].get("end_utc"), "segments": segments,
              "source_url": values[0].get("source_url"), "calendar_source_url": values[0].get("calendar_source_url"),
              "supplier_aggregation_verified": False,
              "notes": ["按所包含日K的常规交易时段汇总；跨度内含休市间隔，非连续交易。"]}
    if unknown:
        result["notes"].append("包含未核实特殊时段或休市日期，已确认片段不代表完整周期边界。")
    exceptions = [copy.deepcopy(session["calendar_exception"]) for session in values if session.get("calendar_exception")]
    if exceptions:
        result["calendar_exceptions"] = exceptions
        result["notes"].append("包含已核实的节假日延长交易或提前收盘安排，按各交易片段汇总。")
    result["label"] = "交易日{} → {}的{}汇总（UTC+0；分段交易{}）".format(
        start, end, "周K" if period == "1w" else "月K", "；含待核实时段" if unknown else "")
    return result
