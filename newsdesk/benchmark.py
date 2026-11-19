"""Frozen daily continuous-futures QA and delayed, separately retained live truth."""

import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import threading
from zoneinfo import ZoneInfo

from newsdesk.market_data import CONTRACTS, FAMILY_SPECS
from newsdesk.trading_sessions import (confirmed_closed_day, confirmed_no_separate_trade_date, continuous_daily_session,
                                      next_expected_trading_date, no_separate_trade_date_assignment)
from newsdesk.trading_environment import ENV_V1, environment_injection, project_environment_question

VERSION = "futures-continuous-direction-range-v3-utc-windows"
BACKTEST_V2_VERSION = "futures-continuous-direction-range-v3-utc-windows-profile-v2"
BACKTEST_V2_DATE_RANGE = {"start": "2026-08-01", "end": "2026-09-30", "inclusive": True,
                          "fields": ["input_date", "target_date"]}
BACKTEST_V2_SUPPORTED_DATE_RANGES = (
    {"start": "2026-09-01", "end": "2026-09-30", "inclusive": True, "fields": ["input_date", "target_date"]},
    copy.deepcopy(BACKTEST_V2_DATE_RANGE),
)
BACKTEST_V2_SELECTION_POLICY = "all_verified_adjacent_pairs_both_dates_in_fixed_range-v1"
LEGACY_VERSION = "futures-next-trading-day-close-qa-v1"
PRICE_BASIS = "provider_continuous_unadjusted_daily_ohlc"
DATASET_ID = re.compile(r"^qa-[0-9a-f]{20}$")
MODES = ("backtest", "live")
FAMILIES = ("WTI", "Brent", "SC")
OUTPUTS = ["direction", "return_pct", "high", "low"]
METRICS = {"direction": "accuracy", "return_pct": "MAE_percentage_points", "high": "MAE", "low": "MAE"}
EVALUATOR_ID = ENV_V1
EVALUATOR_INJECTION = environment_injection(ENV_V1)


def project_evaluator_question(case):
    """Expose evaluator-owned output requirements without rewriting frozen data."""
    return project_environment_question(case, ENV_V1)
LIVE_SUBMISSION_POLICY = "live-submission-forecast-start-minus-30m-utc-v1"
LEGACY_SUBMISSION_POLICY = "legacy-frozen-live-deadline-v1"


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _current_time(zone):
    return datetime.now(ZoneInfo(zone))


def _today(zone):
    return _current_time(zone).date()


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def _finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _day(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("Invalid daily trading date")
    return date.fromisoformat(value)


def _mode(value):
    if value not in MODES:
        raise ValueError("mode 必须是 backtest 或 live")
    return value


def _profile(mode, value):
    if value not in (None, "v1", "v2") or mode != "backtest" and value is not None:
        raise ValueError("profile 仅支持 backtest 的 v1 或 v2")
    return value or "v1"


def _next_weekday(value):
    value = _day(value) + timedelta(days=1)
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value.isoformat()


def _missing_weekdays(input_day, target_day, family=None):
    """Missing business dates are unverified gaps, not assumed exchange holidays.

    Weekends, verified 2026 INE closures and verified dates without a separate
    exchange trading-date label are safe to skip. Other absent weekdays block
    the pair. Passing no family retains the original frozen gap behavior.
    """
    current, target = _day(input_day) + timedelta(days=1), _day(target_day)
    missing = []
    while current < target:
        if current.weekday() < 5 and not confirmed_closed_day(family, current.isoformat()) and not confirmed_no_separate_trade_date(family, current.isoformat()):
            missing.append(current.isoformat())
        current += timedelta(days=1)
    return missing


def _deadline(family, input_day, target_day, zone, contract=None):
    contract = contract or {"familyId": family, "exchange": "ICE" if family == "Brent" and zone == "Europe/London" else "INE" if family == "SC" else "NYMEX"}
    session = continuous_daily_session(contract, target_day)
    if not session or not session.get("start_utc"):
        raise ValueError("目标交易时段尚未核实，不能生成精确预测截止时间")
    return (datetime.fromisoformat(session["start_utc"].replace("Z", "+00:00"))
            - timedelta(minutes=30)).astimezone(ZoneInfo(zone)).isoformat()


def _time_windows(contract, input_day, target_day):
    known = continuous_daily_session(contract, input_day)
    forecast = continuous_daily_session(contract, target_day)
    ready = all(item and item.get("status") == "regular_schedule" and item.get("calendar_status") == "verified_2026"
                and item.get("start_utc") and item.get("end_utc") for item in (known, forecast))
    if ready:
        ready = known["end_utc"] < forecast["start_utc"]
    result = {"timezone": "UTC", "display_timezone": "UTC+0", "basis": "exchange_schedule_reference",
            "status": "ready" if ready else "unconfirmed", "supplier_aggregation_verified": False,
            "known": known, "forecast": forecast,
            "information_cutoff_utc": known.get("end_utc") if known else None,
            "notes": ["区间为对应交易日的交易所常规时段参考，供应商OHLC精确聚合边界未经核实。",
                      "仅使用已知区间结束时或更早的信息；预测区间的价格与事后信息不得用作输入。",
                      "SC逐段列出夜盘和日盘，首末时刻之间的休市间隔不表示持续交易。"]}
    if any(item and item.get("calendar_exception") for item in (known, forecast)):
        result["notes"][0] = "区间为交易所交易时段参考，包含已核实的节假日交易日归属或提前收盘安排；供应商OHLC精确聚合边界未经核实。"
    return result


def _window_text(session):
    if not session or not session.get("start_utc") or not session.get("end_utc"):
        return "UTC+0时段待核实"
    def stamp(value):
        return value[:16].replace("T", " ")
    segments = session.get("segments") or []
    if len(segments) > 1:
        return "UTC+0 " + "；".join("{} → {}（{}）".format(stamp(item["start_utc"]), stamp(item["end_utc"]), item.get("label", "交易")) for item in segments)
    return "UTC+0 {} → {}".format(stamp(session["start_utc"]), stamp(session["end_utc"]))


def display_time_windows(question, manifest=None):
    """Add safe display context without altering an immutable historical case.

    The caller decides where to attach the returned copy. This helper never
    changes the question text, source files, identities, hashes or scores.
    """
    if not isinstance(question, dict):
        return None
    if isinstance(question.get("time_windows"), dict):
        return copy.deepcopy(question["time_windows"])
    family = question.get("family")
    if family not in FAMILIES:
        return None
    input_day, target_day = question.get("input_date"), question.get("target_date")
    try:
        _day(input_day)
        _day(target_day)
    except (ValueError, TypeError):
        return None
    manifest = manifest if isinstance(manifest, dict) else {}
    fingerprints = manifest.get("source_fingerprints")
    fingerprint = fingerprints.get(family, {}) if isinstance(fingerprints, dict) else {}
    saved_contract = fingerprint.get("contract") if isinstance(fingerprint, dict) else None
    contract = copy.deepcopy(saved_contract) if isinstance(saved_contract, dict) else {}
    contract.update(familyId=family)
    contract.setdefault("code", question.get("contract_code", ""))
    if question.get("exchange"):
        contract.setdefault("exchange", question["exchange"])
    windows = _time_windows(contract, input_day, target_day)
    windows["display_only"] = True
    windows["notes"].append("历史冻结题的原始问句与模型输入保持原样；此时段信息仅供页面展示，不追溯修改既有成绩。")
    return windows


def _aware_utc(value):
    if not isinstance(value, str):
        raise ValueError("实盘预测截止时间缺失")
    point = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if point.tzinfo is None or point.utcoffset() is None:
        raise ValueError("实盘预测截止时间必须包含时区")
    return point.astimezone(timezone.utc)


def live_submission_deadline(case, *, legacy=False):
    """Return an aware UTC submission boundary, independent of retrieval policy.

    New submissions use the verified forecast start minus thirty minutes.
    Historical locks can explicitly retain their original frozen protocol.
    Records with no usable window retain their explicit original deadline;
    callers expose that fallback rather than inventing a trading session.
    """
    if not legacy:
        for name in ("time_windows", "display_time_windows"):
            windows = case.get(name)
            forecast = windows.get("forecast") if isinstance(windows, dict) else None
            start = forecast.get("start_utc") if isinstance(forecast, dict) else None
            if start is not None:
                return _aware_utc(start) - timedelta(minutes=30)
    return _aware_utc(case.get("frozen_forecast_deadline") or case.get("forecast_deadline") or case.get("live_deadline_at"))


def live_submission_metadata(case):
    window_name = next((name for name in ("time_windows", "display_time_windows")
                        if isinstance(case.get(name), dict) and isinstance(case[name].get("forecast"), dict)
                        and case[name]["forecast"].get("start_utc") is not None), None)
    return {"submission_deadline_utc": live_submission_deadline(case).isoformat(),
            "submission_deadline_policy": LIVE_SUBMISSION_POLICY if window_name else LEGACY_SUBMISSION_POLICY,
            "submission_deadline_source": window_name + ".forecast.start_utc" if window_name else "frozen_deadline_fallback",
            "submission_buffer_minutes": 30 if window_name else None,
            "submission_deadline_fallback": window_name is None}


def project_live_submission(case, manifest=None):
    """Project the current boundary for display/new runs without rewriting input."""
    result = copy.deepcopy(case)
    if not isinstance(result.get("time_windows"), dict) and not isinstance(result.get("display_time_windows"), dict):
        windows = display_time_windows(result, manifest)
        if windows is not None:
            result["display_time_windows"] = windows
    metadata = live_submission_metadata(result)
    original = result.get("frozen_forecast_deadline") or result.get("forecast_deadline") or result.get("live_deadline_at")
    if original:
        result["frozen_forecast_deadline"] = original
    result.update(metadata, forecast_deadline=metadata["submission_deadline_utc"], live_deadline_at=metadata["submission_deadline_utc"])
    return result


def _outcome(current, target):
    if not _finite_number(current["close"]) or current["close"] == 0:
        raise ValueError("零收盘价无法构造百分比变化")
    change = (target["close"] / current["close"] - 1) * 100
    if not math.isfinite(change):
        raise ValueError("Nonfinite return")
    return {"direction": "up" if target["close"] > current["close"] else "down" if target["close"] < current["close"] else "flat",
            "return_pct": change, "open": target["open"], "high": target["high"], "low": target["low"],
            "close": target["close"], "target_date": target["trading_date"]}


def _backtest_v2_pairs(snapshot, family):
    """Select every verified pair in the current range without bridging gaps.

    Filtering occurs on both sides of the original adjacent-observation pair.
    A pair crossing the range boundary is excluded; the August/September
    boundary inside the range remains eligible. There is no fixed pair quota.
    """
    start, end = BACKTEST_V2_DATE_RANGE["start"], BACKTEST_V2_DATE_RANGE["end"]
    ordered = snapshot["ordered"]
    in_range = [(day, bar) for day, bar in ordered if start <= day <= end]
    candidates = [((left_day, left), (right_day, right))
                  for (left_day, left), (right_day, right) in zip(ordered, ordered[1:])
                  if start <= left_day <= end and start <= right_day <= end]
    pairs, exclusions = [], []
    counts = {key: 0 for key in ("invalid_ohlc", "zero_baseline", "data_gap_unverified", "unconfirmed_time_windows")}
    for (left_day, left), (right_day, right) in candidates:
        missing = []
        if left is None or right is None:
            reason = "invalid_ohlc"
        elif left["close"] == 0:
            reason = "zero_baseline"
        else:
            missing = _missing_weekdays(left_day, right_day, family)
            reason = "data_gap_unverified" if missing else "unconfirmed_time_windows" if _time_windows(
                snapshot["contract"], left_day, right_day)["status"] != "ready" else None
        if reason:
            counts[reason] += 1
            record = {"input_date": left_day, "target_date": right_day, "reason": reason}
            if missing:
                record["missing_weekdays"] = missing
            exclusions.append(record)
        else:
            pairs.append((left, right))
    observed = {day for day, _ in in_range}
    confirmed_holidays, missing_weekdays, no_separate_dates, assignments = [], [], [], []
    day, last = _day(start), _day(end)
    while day <= last:
        if day.weekday() < 5:
            label = day.isoformat()
            if confirmed_closed_day(family, label):
                confirmed_holidays.append(label)
            elif confirmed_no_separate_trade_date(family, label):
                no_separate_dates.append(label)
                assignments.append(no_separate_trade_date_assignment(family, label))
            elif label not in observed:
                missing_weekdays.append(label)
        day += timedelta(days=1)
    spans = bool(ordered and ordered[0][0] <= start and ordered[-1][0] >= end)
    complete = bool(pairs) and spans and not missing_weekdays and not any(bar is None for _, bar in in_range)
    return pairs, {"candidate_pairs": len(candidates), "eligible_pairs": len(pairs),
                   "observed_bar_count": len(in_range), "valid_bar_count": sum(bar is not None for _, bar in in_range),
                   "excluded_counts": counts, "excluded_pairs": exclusions,
                   "unverified_missing_weekdays": missing_weekdays, "confirmed_holiday_dates": confirmed_holidays,
                   "no_separate_trade_dates": no_separate_dates, "trading_date_assignments": assignments,
                   "source_spans_range": spans, "coverage_status": "complete" if complete else "partial",
                   "coverage_basis": "actual_verified_pairs_and_observed_range; no_fixed_question_quota"}


class BenchmarkStore:
    def __init__(self, root, market_data=None, continuous_market=None):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.market_data = market_data
        self.continuous_market = continuous_market
        self._lock = threading.RLock()
        # History is polled with the research catalog. Revalidate immutable
        # snapshots only when a file changes; settlement is part of the key.
        self._history_dataset_cache = {}

    def _provider(self):
        if self.continuous_market is None:
            from newsdesk.continuous_market import ContinuousMarketData
            self.continuous_market = ContinuousMarketData(cache_dir=self.root.parent / "continuous-market")
        return self.continuous_market

    def _snapshot(self, family, refresh):
        payload = self._provider().get(family, refresh=refresh)
        if not isinstance(payload, dict) or payload.get("period") != "1d":
            raise ValueError("Benchmark requires real daily continuous OHLC")
        contract, source = payload.get("contract", {}), payload.get("source", {})
        if (contract.get("familyId") != family or contract.get("isDemo") is True
                or contract.get("series_type", source.get("series_type")) != "dominant_continuous"):
            raise ValueError("不能用具体月份合约或演示价格代替主连")
        zone = source.get("timezone") or ("Asia/Shanghai" if family == "SC" else "America/New_York")
        today, ordered = _today(zone), {}
        rows = payload.get("bars")
        if not isinstance(rows, list):
            raise ValueError("Daily response has no bar array")
        invalid, unfinished = 0, 0
        for row in rows:
            try:
                day = _day(row.get("trading_date"))
            except (ValueError, TypeError, AttributeError):
                invalid += 1
                continue
            completed_through = payload.get("completed_through", "")
            explicit_complete = row.get("is_complete") is True or (isinstance(completed_through, str) and completed_through >= day.isoformat())
            if day > today or (day == today and not explicit_complete):
                unfinished += 1
                continue
            values = [row.get(key) for key in ("open", "high", "low", "close")]
            valid = all(_finite_number(value) for value in values)
            if valid:
                opening, high, low, close = values
                valid = high >= max(opening, close) and low <= min(opening, close) and low <= high
            if not valid:
                ordered[day.isoformat()] = None
                invalid += 1
            else:
                ordered[day.isoformat()] = {"trading_date": day.isoformat(),
                    **{key: row[key] for key in ("open", "high", "low", "close")},
                    "volume": row.get("volume") if _finite_number(row.get("volume")) else None,
                    "open_interest": row.get("open_interest") if _finite_number(row.get("open_interest")) else None}
                # Preserve supplier availability/revision instants for the
                # retrieval gate. The chart's date/epoch anchor is not one.
                ordered[day.isoformat()].update({key: copy.deepcopy(row[key]) for key in
                    ("available_at", "timestamp", "source_modified_at", "article_modified_at", "modified_at")
                    if key in row})
        ordered = [(day, ordered[day]) for day in sorted(ordered)]
        gaps = [{"input_date": left, "next_observed_date": right, "reason": "data_gap_unverified",
                 "missing_weekdays": _missing_weekdays(left, right, family)}
                for (left, _), (right, _) in zip(ordered, ordered[1:]) if _missing_weekdays(left, right, family)]
        return {"contract": copy.deepcopy(contract), "source": copy.deepcopy(source), "exchange_timezone": zone,
                "fetched_at": payload.get("fetched_at"), "data_as_of": payload.get("data_as_of"),
                "source_stale": bool(payload.get("cache", {}).get("stale")), "raw_response_sha256": payload.get("raw_response_sha256"),
                "raw_response": payload.get("raw_response"), "normalized_sha256": _digest({"contract": contract, "source": source, "bars": rows}),
                "invalid_rows": invalid, "ignored_unfinished_bars": unfinished, "ordered": ordered,
                "eligible_bar_count": sum(bar is not None for _, bar in ordered),
                "data_gap_unverified_count": len(gaps), "data_gaps": gaps,
                "notes": list(payload.get("notes", [])) + (["未核实为交易所休市日的缺失工作日区间，跳过回测并阻止实盘结算。"] if gaps else [])}

    def _question(self, family, snapshot, current, target, mode):
        contract, input_day = snapshot["contract"], current["trading_date"]
        target_day = target["trading_date"] if target else next_expected_trading_date(contract, input_day)
        windows = _time_windows(contract, input_day, target_day)
        if windows["status"] != "ready":
            raise ValueError("已知或预测区间的交易所常规时段尚未核实，未生成缺少精确UTC时分的题目")
        deadline = _deadline(family, input_day, target_day, snapshot["exchange_timezone"], contract)
        input_is_today = input_day == _today(snapshot["exchange_timezone"]).isoformat()
        timely = _current_time(snapshot["exchange_timezone"]) < datetime.fromisoformat(deadline)
        live_status = "ready" if input_is_today and timely and not snapshot["source_stale"] else "waiting_new_completed_bar"
        case_id = "{}__{}__{}__{}".format(mode, contract["id"], input_day, target_day)
        if mode == "live":
            case_id += "__" + _digest({"current": current, "source": snapshot["source"], "capture": snapshot["fetched_at"]})[:8]
        question = ("已知 {known_window}（交易日 {input_day}）对应的 {family} 主连（{code}）完整日线："
                    "开盘 {open:g}、最高 {high:g}、最低 {low:g}、收盘 {close:g} {unit}。"
                    "请预测 {forecast_window}（下一交易日 {target_day}{estimated}）对应日线的行情表现。"
                    "上述区间为交易所常规交易时段参考，供应商日线的精确OHLC聚合边界未经核实；分段之间的休市时间不算交易。"
                    "可用信息截止于 {information_cutoff} UTC+0，不得使用之后或预测区间内的信息。").format(
            input_day=input_day, family=family, code=contract["code"], unit=contract["unit"], target_day=target_day,
            known_window=_window_text(windows["known"]), forecast_window=_window_text(windows["forecast"]),
            information_cutoff=windows["information_cutoff_utc"][:16].replace("T", " "),
            estimated="，预期日期，以实际下一交易日为准" if mode == "live" else "", **current)
        if any(window and window.get("calendar_exception") for window in (windows["known"], windows["forecast"])):
            question = question.replace("上述区间为交易所常规交易时段参考", "上述区间为交易所交易时段参考，包含已核实的节假日特殊安排")
        result = {"case_id": case_id, "family": family, "contract_id": contract["id"], "contract_code": contract["code"],
                  "currency": contract["currency"], "unit": contract["unit"], "input_date": input_day, "target_date": target_day,
                  "current_price": current["close"], "current_bar": {key: current[key] for key in ("open", "high", "low", "close")},
                  "question": question, "environment_id": EVALUATOR_ID, "environment_injection": EVALUATOR_INJECTION,
                  "evaluator_id": "eval-v1", "evaluator_injection": EVALUATOR_INJECTION,
                  "news_cutoff_exclusive": target_day, "exchange_timezone": snapshot["exchange_timezone"],
                  "exchange": windows["known"].get("exchange", contract.get("exchange")), "time_windows": windows,
                  "price_basis": PRICE_BASIS, "series_type": "dominant_continuous", "mode": mode,
                  "continuous_method": snapshot["source"].get("continuous_method", contract.get("continuous_method", "provider_continuous"))}
        if mode == "live":
            result.update(live_status=live_status, forecast_status=live_status, input_is_today=input_is_today,
                          forecast_deadline=deadline, live_deadline_at=deadline, input_ready_at=snapshot["fetched_at"],
                          target_date_is_expected=True, live_target_date_note="依据已核实休市安排排除闭市日期；仍以实际下一交易日确认")
            result = project_live_submission(result)
        return result

    def build(self, days=30, refresh=False, mode="backtest", preserve_current_on_failure=False, profile=None):
        mode = _mode(mode)
        profile = _profile(mode, profile)
        bounded = profile == "v2"
        if type(days) is not int or not 1 <= days <= 366:
            raise ValueError("days must be an integer between 1 and 366")
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        if type(preserve_current_on_failure) is not bool:
            raise ValueError("preserve_current_on_failure must be a boolean")
        with self._lock:
            snapshots, errors = {}, {}
            self._provider()
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {family: executor.submit(self._snapshot, family, refresh) for family in FAMILIES}
                for family, future in futures.items():
                    try:
                        snapshots[family] = future.result()
                    except Exception as error:
                        errors[family] = {"type": type(error).__name__, "message": str(error)[:1000]}
            if preserve_current_on_failure and not any(not item["source_stale"] for item in snapshots.values()):
                detail = "；".join(family + "：" + errors.get(family, {}).get("message", "刷新失败，仅有过期缓存")
                                 for family in FAMILIES)
                raise RuntimeError("本轮未获得新的主连行情，保留上次题目。" + detail)
            questions, labels, sources, fingerprints, families = [], {}, {}, {}, []
            expected = None if bounded else days if mode == "backtest" else 1
            for family, name, exchange, currency, unit, _ in FAMILY_SPECS:
                snapshot = snapshots.get(family)
                info = {"id": family.lower() + "-main", "code": snapshot["contract"]["code"] if snapshot else family + " 主连",
                        "count": 0, "expected_count": expected, "series_type": "dominant_continuous"}
                if snapshot:
                    ordered = snapshot["ordered"]
                    if bounded:
                        pairs, selection = _backtest_v2_pairs(snapshot, family)
                        skipped_windows = selection["excluded_counts"]["unconfirmed_time_windows"]
                        info.update(expected_count=len(pairs), selection=selection)
                    elif mode == "backtest":
                        candidates = [(left, right) for (_, left), (_, right) in zip(ordered, ordered[1:])
                                 if left is not None and right is not None and left["close"] != 0
                                 and not _missing_weekdays(left["trading_date"], right["trading_date"], family)]
                    else:
                        latest = ordered[-1][1] if ordered else None
                        candidates = [(latest, None)] if latest is not None and latest["close"] != 0 else []
                    if not bounded:
                        pairs = [(current, target) for current, target in candidates if _time_windows(
                            snapshot["contract"], current["trading_date"], target["trading_date"] if target else
                            next_expected_trading_date(snapshot["contract"], current["trading_date"]))["status"] == "ready"]
                        skipped_windows = len(candidates) - len(pairs)
                        pairs = pairs[-days:] if mode == "backtest" else pairs
                    used = {bar["trading_date"]: bar for pair in pairs for bar in pair if bar is not None}
                    selected = [used[key] for key in sorted(used)]
                    sources[family] = {key: value for key, value in snapshot.items() if key != "ordered"}
                    sources[family]["selected_bars"] = selected
                    history = [bar for day, bar in ordered if bar is not None and day >= "2026-01-01"]
                    if bounded:
                        history = [bar for bar in history if bar["trading_date"] <= BACKTEST_V2_DATE_RANGE["end"]]
                    sources[family]["history_bars"] = history
                    fingerprints[family] = {"contract": snapshot["contract"], "source": snapshot["source"],
                        "normalized_sha256": snapshot["normalized_sha256"], "raw_response_sha256": snapshot["raw_response_sha256"], "selected_bars": selected, "history_bars": history}
                    for current, target in pairs:
                        item = self._question(family, snapshot, current, target, mode)
                        questions.append(item)
                        if target is not None:
                            labels[item["case_id"]] = _outcome(current, target)
                    info.update(count=len(pairs), source_name=snapshot["source"].get("name", ""),
                                continuous_method=snapshot["source"].get("continuous_method", "provider_continuous"),
                                source_stale=snapshot["source_stale"], invalid_rows=snapshot["invalid_rows"],
                                ignored_unfinished_bars=snapshot["ignored_unfinished_bars"], data_as_of=snapshot["data_as_of"],
                                data_gap_unverified_count=snapshot["data_gap_unverified_count"],
                                unconfirmed_time_windows_skipped=skipped_windows)
                    if mode == "live" and not pairs and skipped_windows:
                        info["error"] = "已知或目标交易日存在未核实的特殊交易时段，暂不发布缺少精确UTC时分的预测题。"
                else:
                    fingerprints[family] = {"unavailable": errors[family]["type"]}
                    info["error"] = errors[family]["message"]
                info["status"] = (info["selection"]["coverage_status"] if snapshot else "failed") if bounded else (
                    "complete" if info["count"] == expected else "partial" if snapshot else "failed")
                actual_exchange = snapshot["contract"].get("exchange", exchange) if snapshot else exchange
                families.append({"id": family, "name": name, "exchange": actual_exchange, "currency": currency, "unit": unit,
                                 "count": info["count"], "contracts": [info], "series_type": "dominant_continuous"})
            version = BACKTEST_V2_VERSION if bounded else VERSION
            build_days = None if bounded else days
            identity = {"version": version, "mode": mode, "days_per_contract": build_days,
                        "questions": questions, "labels": labels, "sources": fingerprints}
            if bounded:
                identity.update(profile="v2", date_range=copy.deepcopy(BACKTEST_V2_DATE_RANGE), selection_policy=BACKTEST_V2_SELECTION_POLICY)
            content_hash = _digest(identity)
            dataset_id = "qa-" + content_hash[:20]
            dates = lambda field: [item[field] for item in questions]
            complete = all(family["contracts"][0]["status"] == "complete" for family in families) if bounded else all(family["count"] == expected for family in families)
            manifest = {"dataset_id": dataset_id, "version": version, "mode": mode, "sha256": content_hash,
                "created_at": _now(), "status": "ready" if questions else "empty", "series_type": "dominant_continuous",
                "total_questions": len(questions), "expected_questions": len(questions) if bounded else expected * len(FAMILIES),
                "days_per_contract": None if bounded else days if mode == "backtest" else 1, "build_days": build_days, "period": "1d", "horizon": "next_actual_trading_day",
                "input_start": min(dates("input_date"), default=None), "input_end": max(dates("input_date"), default=None),
                "target_start": min(dates("target_date"), default=None), "target_end": max(dates("target_date"), default=None),
                "families": families, "coverage": "完整" if complete else "部分", "coverage_status": "complete" if complete else "partial",
                "price_basis": PRICE_BASIS, "price_basis_description": "供应商未复权主连日线，不用月份合约拼接或冒充官方结算价",
                "sampling_rule": "主连相邻完整日线；实盘仅发布待观测下一交易日的四项预测", "outputs": OUTPUTS, "metrics": METRICS,
                "data_gap_policy": "除已核实的2026年INE休市日外，缺失工作日仍标记data_gap_unverified并跳过；不把未核实观测缺口当下一交易日",
                "time_window_policy": "仅构建已知与预测常规时段均可确定UTC日期时分的题目；节假日特殊时段未核实的日对跳过，历史冻结题仅附展示信息",
                "display_timezone": "UTC", "time_windows_schema": "exchange-session-utc-v1",
                "return_pct_unit": "percent; errors in percentage points", "flat_rule": "目标收盘价与输入收盘价相等时方向为 flat",
                "questions_sha256": _digest(questions), "labels_sha256": _digest(labels), "source_snapshots_sha256": _digest(sources),
                "source_fingerprints": fingerprints, "errors": errors}
            if bounded:
                manifest.update(profile="v2", date_range=copy.deepcopy(BACKTEST_V2_DATE_RANGE), selection_policy=BACKTEST_V2_SELECTION_POLICY,
                    sampling_rule="{}至{}范围内的全部可核验相邻主连完整日线；输入日与目标日均在起止日期内，不设固定题数".format(
                        BACKTEST_V2_DATE_RANGE["start"], BACKTEST_V2_DATE_RANGE["end"]),
                    data_gap_policy="已核实休市日期与已核实无独立交易日标签分别记录；WTI 2026-09-07交易归属2026-09-08，不补造单独日K；其余未核实缺失工作日仍跳过",
                    time_window_policy="接受已核实的常规、合并延长及提前收盘交易时段；已知与预测区间均须有明确UTC时分，未核实特殊安排仍跳过",
                    coverage_basis="按实际可核验日对计数；少于30题本身不表示缺失，未核实缺口与无效行情另记覆盖状态")
            manifest["manifest_sha256"] = _digest(manifest)
            datasets = self.root / "datasets"
            datasets.mkdir(mode=0o700, exist_ok=True)
            destination = datasets / dataset_id
            if not destination.exists():
                temporary = Path(tempfile.mkdtemp(prefix=".building-", dir=datasets))
                try:
                    for filename, value in (("questions", questions), ("labels", labels), ("source-snapshots", sources), ("manifest", manifest)):
                        _write(temporary / (filename + ".json"), value)
                    try:
                        temporary.rename(destination)
                    except OSError:
                        if not destination.is_dir():
                            raise
                finally:
                    if temporary.exists():
                        shutil.rmtree(temporary)
            self.load_dataset(dataset_id, mode=mode)
            _write(self._pointer(mode, profile="v2" if bounded else None), {"dataset_id": dataset_id, "sha256": content_hash, "updated_at": _now()})
            return self.summary(mode=mode, profile="v2" if bounded else None)

    def _pointer(self, mode, profile=None):
        mode = _mode(mode)
        selected = _profile(mode, profile)
        return self.root / ("current-backtest-v2.json" if selected == "v2" else "current-" + mode + ".json")

    def load_dataset(self, dataset_id=None, mode="backtest", profile=None):
        mode = _mode(mode)
        _profile(mode, profile)
        with self._lock:
            if dataset_id is None:
                dataset_id = _read(self._pointer(mode, profile))["dataset_id"]
            if not isinstance(dataset_id, str) or not DATASET_ID.fullmatch(dataset_id):
                raise ValueError("Invalid dataset identifier")
            path = self.root / "datasets" / dataset_id
            manifest = _read(path / "manifest.json")
            questions, labels, sources = [_read(path / (name + ".json")) for name in ("questions", "labels", "source-snapshots")]
            unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
            if (manifest.get("dataset_id") != dataset_id or manifest.get("manifest_sha256") != _digest(unsigned)
                    or manifest.get("questions_sha256") != _digest(questions) or manifest.get("labels_sha256") != _digest(labels)
                    or manifest.get("source_snapshots_sha256") != _digest(sources)):
                raise ValueError("Frozen benchmark integrity check failed")
            identity = {"version": manifest["version"], "days_per_contract": manifest.get("build_days", manifest["days_per_contract"]),
                        "questions": questions, "labels": labels, "sources": manifest["source_fingerprints"]}
            if manifest.get("version") != LEGACY_VERSION:
                identity["mode"] = manifest["mode"]
            if manifest.get("version") == BACKTEST_V2_VERSION or manifest.get("profile") == "v2":
                if (manifest.get("version") != BACKTEST_V2_VERSION or manifest.get("mode") != "backtest"
                        or manifest.get("profile") != "v2" or manifest.get("date_range") not in BACKTEST_V2_SUPPORTED_DATE_RANGES
                        or manifest.get("selection_policy") != BACKTEST_V2_SELECTION_POLICY):
                    raise ValueError("Frozen benchmark v2 profile integrity check failed")
                # Old September-only publications retain their own date range
                # and content address after the active profile expands.
                bounds = manifest["date_range"]
                if any(not isinstance(case, dict) or any(not isinstance(case.get(field), str)
                        or not bounds["start"] <= case[field] <= bounds["end"]
                        for field in bounds["fields"]) for case in questions):
                    raise ValueError("Frozen benchmark v2 question date-range integrity check failed")
                identity.update(profile=manifest["profile"], date_range=manifest["date_range"], selection_policy=manifest["selection_policy"])
            value_hash = _digest(identity)
            if value_hash != manifest.get("sha256") or dataset_id != "qa-" + value_hash[:20]:
                raise ValueError("Frozen benchmark content hash does not match its version")
            result = {"manifest": manifest, "questions": questions, "labels": labels, "settlements": {}}
            if manifest.get("mode") == "live":
                settlement = self._read_settlements(path, manifest, questions)
                result["settlements"] = settlement.get("items", {})
                result["labels"] = {**labels, **{key: item["actual"] for key, item in result["settlements"].items()}}
            return result

    def one_day(self, dataset_id=None, mode="backtest", input_date=None):
        """Return a deterministic one-session slice for multi-model runs.

        The frozen benchmark remains immutable. With no date supplied, the
        latest input date represented by every primary continuous family is
        selected, yielding one question per WTI, Brent and SC. ``input_date``
        can replay a specific shared session. The returned values are copies;
        this helper never writes a benchmark pointer or alters the dataset.
        """
        mode = _mode(mode)
        dataset = self.load_dataset(dataset_id, mode=mode)
        questions = dataset["questions"]
        if not questions:
            raise ValueError("benchmark 没有题目")
        by_date = {}
        for question in questions:
            day, family = question.get("input_date"), question.get("family")
            if isinstance(day, str) and family in FAMILIES:
                by_date.setdefault(day, {})[family] = question
        if input_date is not None:
            _day(input_date)
            selected_date = input_date
        else:
            complete = [day for day, members in by_date.items()
                        if all(family in members for family in FAMILIES)]
            if not complete:
                raise ValueError("benchmark 没有覆盖全部主连的共同交易日")
            selected_date = max(complete)
        members = by_date.get(selected_date, {})
        selected = [members[family] for family in FAMILIES if family in members]
        if len(selected) != len(FAMILIES):
            raise ValueError("指定日期没有覆盖全部主连")
        selected_ids = {item["case_id"] for item in selected}
        labels = {case_id: copy.deepcopy(value) for case_id, value in dataset["labels"].items()
                  if case_id in selected_ids}
        if mode == "backtest" and len(labels) != len(selected):
            raise ValueError("指定日期缺少完整真实答案")
        manifest = copy.deepcopy(dataset["manifest"])
        manifest.update({"one_day": True, "one_day_input_date": selected_date,
                         "one_day_source_dataset_id": manifest.get("dataset_id"),
                         "total_questions": len(selected), "expected_questions": len(selected)})
        return {"manifest": manifest, "questions": copy.deepcopy(selected), "labels": labels,
                "sources": copy.deepcopy(dataset.get("sources", {})), "settlements": {}}

    def _read_settlements(self, path, manifest, questions):
        file = path / "settlement.json"
        if not file.exists():
            return {"items": {}}
        record = _read(file)
        unsigned = {key: value for key, value in record.items() if key != "settlement_sha256"}
        if (record.get("dataset_id") != manifest["dataset_id"] or record.get("questions_sha256") != manifest["questions_sha256"]
                or record.get("settlement_sha256") != _digest(unsigned)):
            raise ValueError("Live settlement integrity check failed")
        if not isinstance(record.get("items"), dict) or set(record["items"]) - {item["case_id"] for item in questions}:
            raise ValueError("Live settlement case binding failed")
        return record

    def settle_live(self, dataset_id=None, refresh=True, *, snapshots=None, case_ids=None):
        if type(refresh) is not bool:
            raise ValueError("refresh must be a boolean")
        if snapshots is not None and not isinstance(snapshots, dict):
            raise ValueError("snapshots must be a family mapping")
        if case_ids is not None and (not isinstance(case_ids, (list, tuple, set, frozenset))
                                     or any(not isinstance(value, str) for value in case_ids)):
            raise ValueError("case_ids must be a collection of case identifiers")
        with self._lock:
            dataset = self.load_dataset(dataset_id, mode="live")
            manifest, questions = dataset["manifest"], dataset["questions"]
            if manifest.get("mode") != "live":
                raise ValueError("Only live datasets can receive delayed settlements")
            if case_ids is not None and set(case_ids) - {item["case_id"] for item in questions}:
                raise ValueError("Settlement case identifiers do not belong to the dataset")
            path = self.root / "datasets" / manifest["dataset_id"]
            previous = self._read_settlements(path, manifest, questions)
            items, errors = copy.deepcopy(previous.get("items", {})), {}
            for case in questions:
                key = case["case_id"]
                if key in items or case_ids is not None and key not in case_ids:
                    continue
                if snapshots is not None and snapshots.get(case["family"]) is None:
                    continue
                try:
                    if snapshots is None:
                        snapshot = self._snapshot(case["family"], refresh=refresh)
                    else:
                        snapshot = snapshots[case["family"]]
                    if snapshot["source_stale"]:
                        raise ValueError("待最新行情确认，过期缓存不能新增实盘真值")
                    later = [(day, bar) for day, bar in snapshot["ordered"] if day > case["input_date"]]
                    if not later or later[0][1] is None:
                        continue
                    if _missing_weekdays(case["input_date"], later[0][0], case["family"] if case.get("time_windows") else None):
                        raise ValueError("data_gap_unverified：输入日至下一观测日间缺少工作日，等待交易日历或缺失行情核验")
                    actual = _outcome({"close": case["current_price"]}, later[0][1])
                    proof = {"source": snapshot["source"], "fetched_at": snapshot["fetched_at"], "raw_response_sha256": snapshot["raw_response_sha256"],
                             "normalized_sha256": snapshot["normalized_sha256"], "target_bar": later[0][1]}
                    items[key] = {"case_id": key, "status": "settled", "actual": actual, "settled_at": _now(),
                                  "source_proof": proof, "source_proof_sha256": _digest(proof)}
                except Exception as error:
                    errors[key] = {"type": type(error).__name__, "message": str(error)[:500]}
            record = {"dataset_id": manifest["dataset_id"], "questions_sha256": manifest["questions_sha256"],
                      "updated_at": _now(), "items": items, "errors": errors}
            record["settlement_sha256"] = _digest(record)
            _write(path / "settlement.json", record)
            return {"dataset_id": manifest["dataset_id"], "mode": "live", "settled": len(items),
                    "pending": len(questions) - len(items), "items": copy.deepcopy(items), "errors": errors}

    def summary(self, mode="backtest", profile=None):
        mode = _mode(mode)
        selected_profile = _profile(mode, profile)
        if not self._pointer(mode, profile).is_file():
            result = {"status": "empty", "mode": mode, "dataset_id": None, "version": VERSION, "sha256": None, "created_at": None,
                    "total_questions": 0, "days_per_contract": 30 if mode == "backtest" else 1, "period": "1d", "input_start": None,
                    "input_end": None, "target_start": None, "target_end": None, "families": [], "series_type": "dominant_continuous",
                    "coverage": "尚未构建", "coverage_status": "empty", "outputs": OUTPUTS, "metrics": METRICS,
                    "settled_questions": 0, "pending_questions": 0}
            if selected_profile == "v2":
                result.update(profile="v2", version=BACKTEST_V2_VERSION, date_range=copy.deepcopy(BACKTEST_V2_DATE_RANGE),
                              selection_policy=BACKTEST_V2_SELECTION_POLICY, days_per_contract=None, expected_questions=0)
            return result
        dataset = self.load_dataset(mode=mode, profile=profile)
        private = {"source_fingerprints", "source_snapshots_sha256", "labels_sha256"}
        result = copy.deepcopy({key: value for key, value in dataset["manifest"].items() if key not in private})
        result.update(settled_questions=len(dataset["labels"]), pending_questions=len(dataset["questions"]) - len(dataset["labels"]))
        if mode == "live":
            result["submission_deadline_policy"] = LIVE_SUBMISSION_POLICY
            result["ready_questions"] = sum(self._live_status(project_live_submission(item, dataset["manifest"])) == "ready"
                                             for item in dataset["questions"] if item["case_id"] not in dataset["labels"])
        for family in result["families"]:
            members = [item for item in dataset["questions"] if item["family"] == family["id"]]
            family["settled"] = sum(item["case_id"] in dataset["labels"] for item in members)
            family["pending"] = len(members) - family["settled"]
        return result

    def _live_status(self, question):
        if question.get("live_status") != "ready":
            return "waiting_new_completed_bar"
        if _current_time(question["exchange_timezone"]) >= live_submission_deadline(question):
            return "waiting_new_completed_bar"
        return "ready"

    def catalog(self):
        return {**{mode: self.summary(mode) for mode in MODES},
                "backtest_v2": self.summary("backtest", profile="v2"),
                "live_history": self.live_history_summary()}

    @staticmethod
    def _history_signature(path):
        signature = []
        for name in ("manifest.json", "questions.json", "labels.json", "source-snapshots.json", "settlement.json"):
            try:
                info = (path / name).stat()
                signature.append((name, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))
            except FileNotFoundError:
                signature.append((name, None))
        return tuple(signature)

    def _history_dataset(self, path):
        signature = self._history_signature(path)
        cached = self._history_dataset_cache.get(path.name)
        if cached and cached[0] == signature:
            if cached[2]:
                error_type, message = cached[2]
                raise error_type(message)
            return cached[1]
        try:
            manifest = _read(path / "manifest.json")
            unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
            if (manifest.get("dataset_id") != path.name
                    or manifest.get("manifest_sha256") != _digest(unsigned)):
                raise ValueError("Frozen benchmark manifest integrity check failed")
            # A verified non-live manifest is sufficient to exclude unrelated
            # backtests; every live dataset receives the full source/hash check.
            result = self.load_dataset(path.name, mode="live") if manifest.get("mode") == "live" else None
            if signature != self._history_signature(path):
                raise ValueError("Benchmark files changed during history verification; retry reading")
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            # Cache only stable failures; a concurrent atomic update is retried
            # by the next request and is always surfaced to the caller.
            if signature == self._history_signature(path):
                self._history_dataset_cache[path.name] = (signature, None, (type(error), str(error)[:500]))
            raise
        self._history_dataset_cache[path.name] = (signature, result, None)
        return result

    @staticmethod
    def _history_instant(value):
        if not isinstance(value, str):
            raise ValueError("Live history timestamp is missing")
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None or instant.utcoffset() is None:
            raise ValueError("Live history timestamp requires an explicit timezone")
        return instant

    @staticmethod
    def _history_question_key(item):
        if (item.get("family") not in FAMILIES or not isinstance(item.get("contract_id"), str)
                or not item["contract_id"] or not isinstance(item.get("case_id"), str) or not item["case_id"]):
            raise ValueError("Live history question identity is incomplete")
        _day(item.get("input_date"))
        _day(item.get("target_date"))
        return tuple(item[key] for key in ("family", "contract_id", "input_date", "target_date"))

    def live_history(self):
        """Read verified, timely live publications without changing frozen data.

        A market refresh may freeze the same trading-day pair several times.
        The first timely ready publication owns the displayed input and truth;
        all timely versions remain available for matching locked predictions.
        """
        with self._lock:
            errors, datasets, groups, seen = {}, {}, {}, set()
            current_id = None
            if self._pointer("live").exists():
                try:
                    current_id = _read(self._pointer("live"))["dataset_id"]
                    if not isinstance(current_id, str) or not DATASET_ID.fullmatch(current_id):
                        raise ValueError("Invalid current live dataset identifier")
                except (OSError, ValueError, KeyError, TypeError) as error:
                    errors["current-live"] = {"type": type(error).__name__, "message": str(error)[:500]}
                    current_id = None
            paths = sorted((self.root / "datasets").glob("qa-*"))
            existing = {path.name for path in paths if path.is_dir() and DATASET_ID.fullmatch(path.name)}
            for dataset_id in set(self._history_dataset_cache) - existing:
                self._history_dataset_cache.pop(dataset_id, None)
            for path in paths:
                if path.name not in existing:
                    continue
                try:
                    dataset = self._history_dataset(path)
                    if dataset is None:
                        continue
                    manifest = dataset["manifest"]
                    published = self._history_instant(manifest.get("created_at"))
                    candidates, keys = [], set()
                    for item in dataset["questions"]:
                        key = self._history_question_key(item)
                        if key in keys:
                            raise ValueError("Duplicate live question trading-day identity")
                        keys.add(key)
                        deadline = self._history_instant(item.get("forecast_deadline"))
                        if item.get("live_status") == "ready" and published < deadline:
                            candidates.append((key, item))
                    datasets[path.name] = dataset
                    seen.update(keys)
                    for key, item in candidates:
                        groups.setdefault(key, []).append((published, path.name, item))
                except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                    errors[path.name] = {"type": type(error).__name__, "message": str(error)[:500]}
            current = datasets.get(current_id)
            if current_id and current is None:
                errors.setdefault(current_id, {"type": "ValueError", "message": "Current live dataset is missing or invalid"})
            current_questions = {self._history_question_key(item): item for item in current["questions"]} if current else {}
            questions = []
            for key, versions in sorted(groups.items(), key=lambda pair: (FAMILIES.index(pair[0][0]), pair[0][2], pair[0][3], pair[0][1])):
                versions.sort(key=lambda value: (value[0], value[1], value[2]["case_id"]))
                published, dataset_id, frozen = versions[0]
                item = copy.deepcopy(frozen)
                item.update(dataset_id=dataset_id, published_at=datasets[dataset_id]["manifest"]["created_at"],
                            is_current=key in current_questions,
                            versions=[{"dataset_id": version_id, "case_id": question["case_id"],
                                       "published_at": datasets[version_id]["manifest"]["created_at"]}
                                      for _, version_id, question in versions])
                if not isinstance(item.get("time_windows"), dict):
                    windows = display_time_windows(item, datasets[dataset_id]["manifest"])
                    if windows is not None:
                        item["display_time_windows"] = windows
                item = project_live_submission(item, datasets[dataset_id]["manifest"])
                # Delayed truth is bound to this exact baseline and frozen
                # version. A later revision's settlement must not leak across.
                actual = copy.deepcopy(datasets[dataset_id]["labels"].get(item["case_id"]))
                item["actual"] = actual
                item["settlement_status"] = "settled" if actual is not None else "pending"
                if isinstance(actual, dict):
                    item.update({"actual_" + field: actual[field] for field in OUTPUTS})
                    item["actual_target_date"] = actual["target_date"]
                deadline = live_submission_deadline(item)
                current_question = current_questions.get(key)
                current_is_published = current_question is not None and any(
                    version_id == current_id and question["case_id"] == current_question["case_id"]
                    for _, version_id, question in versions)
                if actual is not None:
                    state = "settled"
                elif _current_time("UTC") >= deadline:
                    state = "forecast_closed"
                elif current_is_published and self._live_status(project_live_submission(current_question, current["manifest"])) == "ready":
                    state = "ready"
                else:
                    state = "waiting_new_completed_bar"
                item["live_status"] = item["forecast_status"] = state
                questions.append(project_evaluator_question(item))
            families = []
            for family, name, exchange, currency, unit, _ in FAMILY_SPECS:
                members = [item for item in questions if item["family"] == family]
                settled = sum(item["actual"] is not None for item in members)
                families.append({"id": family, "name": name, "exchange": exchange, "currency": currency, "unit": unit,
                                 "count": len(members), "settled": settled, "pending": len(members) - settled,
                                 "ready": sum(item["live_status"] == "ready" for item in members),
                                 "excluded": sum(key[0] == family for key in seen - groups.keys())})
            dates = lambda field: [item[field] for item in questions]
            settled = sum(item["actual"] is not None for item in questions)
            summary = {"mode": "live", "history": True, "scope": "live_history", "dataset_id": current_id,
                       "current_dataset_id": current_id, "version": VERSION, "series_type": "dominant_continuous", "period": "1d",
                       "status": "partial" if errors else "ready" if questions else "empty",
                       "coverage": "累计有效发布", "coverage_status": "partial" if errors else "complete",
                       "total_questions": len(questions), "families": families,
                       "input_start": min(dates("input_date"), default=None), "input_end": max(dates("input_date"), default=None),
                       "target_start": min(dates("target_date"), default=None), "target_end": max(dates("target_date"), default=None),
                       "created_at": max(dates("published_at"), default=None),
                       "settled_questions": settled, "pending_questions": len(questions) - settled,
                       "ready_questions": sum(item["live_status"] == "ready" for item in questions),
                       "excluded_questions": len(seen - groups.keys()), "errors": errors,
                       "outputs": OUTPUTS, "metrics": METRICS, "submission_deadline_policy": LIVE_SUBMISSION_POLICY}
            summary["history_revision"] = _digest({"summary": summary, "questions": questions})
            return {"summary": summary, "questions": questions}

    def live_history_summary(self):
        return self.live_history()["summary"]

    def live_history_questions(self, family=None):
        if family is not None and family not in FAMILIES:
            raise ValueError("Unknown futures family")
        return [item for item in self.live_history()["questions"] if family is None or item["family"] == family]

    def questions(self, family=None, mode="backtest", profile=None):
        mode = _mode(mode)
        _profile(mode, profile)
        if family is not None and family not in FAMILIES:
            raise ValueError("Unknown futures family")
        if not self._pointer(mode, profile).is_file():
            return []
        dataset, result = self.load_dataset(mode=mode, profile=profile), []
        for question in dataset["questions"]:
            if family is not None and question["family"] != family:
                continue
            item = copy.deepcopy(question)
            if not isinstance(item.get("time_windows"), dict):
                windows = display_time_windows(item, dataset["manifest"])
                if windows is not None:
                    item["display_time_windows"] = windows
            actual = copy.deepcopy(dataset["labels"].get(question["case_id"]))
            item["actual"] = actual
            item["settlement_status"] = "settled" if actual is not None else "pending"
            if isinstance(actual, dict):
                item.update({"actual_" + field: actual[field] for field in OUTPUTS})
                item["actual_target_date"] = actual["target_date"]
            if mode == "live":
                item = project_live_submission(item, dataset["manifest"])
                item["live_status"] = item["forecast_status"] = self._live_status(item)
            result.append(project_evaluator_question(item))
        return result
