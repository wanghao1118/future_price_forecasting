"""Refresh daily bars, publish tomorrow's live QA and settle retained live QA."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from newsdesk.continuous_market import SPECS, mark_complete


FAMILIES = ("WTI", "Brent", "SC")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _message(error):
    return (str(error) or type(error).__name__)[:1000]


def _calendar_date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _completion_details(family, manifest, snapshot, questions):
    """Assess only the near edge of the actual captured complete daily bars.

    Weekdays without observed bars remain unverified gaps, including possible
    holidays. The report never promotes an unfinished bar or fills a gap.
    """
    fingerprint = manifest.get("source_fingerprints", {}).get(family, {})
    selected = fingerprint.get("selected_bars", [])
    complete_days = [_calendar_date(bar.get("trading_date")) for bar in selected
                     if isinstance(bar, dict)]
    if not any(complete_days):
        complete_days = [_calendar_date(question.get("input_date")) for question in questions
                         if question.get("family") == family]
    latest = max((day for day in complete_days if day is not None), default=None)
    fetched_at = snapshot.get("fetched_at")
    result = {"latest_completed_date": latest.isoformat() if latest else None,
              "fetched_at": fetched_at, "recent_unverified_weekday_gaps": [],
              "historical_unverified_gap_count": snapshot.get("data_gap_unverified_count"),
              "completeness": "unknown", "needs_daily_retry": True}
    if latest is None:
        result.update(completeness="no_completed_bar", reason="尚未取得可核验的完整主连日K")
        return result
    try:
        fetched = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
        if fetched.tzinfo is None:
            raise ValueError("missing timezone")
        zone = snapshot.get("exchange_timezone") or snapshot.get("source", {}).get("timezone") or SPECS[family]["timezone"]
        local = fetched.astimezone(ZoneInfo(zone))
    except (AttributeError, TypeError, ValueError, KeyError):
        result["reason"] = "缺少可核验的主连抓取时间或市场时区，完整日K新鲜度待确认"
        return result
    if latest > local.date():
        result["reason"] = "完整日K日期晚于此次抓取日期，完整性待确认"
        return result
    # Reuse the provider's existing close-buffer rule, rather than assigning a
    # second definition of when today's source bar can be complete.
    today_complete = mark_complete([{"trading_date": local.date().isoformat()}], family, fetched)[0][0]["is_complete"]
    through = local.date() if today_complete else local.date() - timedelta(days=1)
    result["freshness_checked_through"] = through.isoformat()
    day = latest + timedelta(days=1)
    while day <= through:
        if day.weekday() < 5:
            result["recent_unverified_weekday_gaps"].append(day.isoformat())
        day += timedelta(days=1)
    gaps = result["recent_unverified_weekday_gaps"]
    if gaps:
        result.update(completeness="recent_gap_unverified",
                      reason="完整主连日K仅到 {}；近端 {} 个工作日缺少已观测完整日线，休市或供应商延迟尚未核实".format(latest.isoformat(), len(gaps)))
    else:
        result.update(completeness="current", needs_daily_retry=False)
    return result


def _continuous_summary(manifest, snapshots=None, questions=None):
    families = {item["id"]: item for item in manifest.get("families", [])}
    errors = manifest.get("errors", {})
    snapshots, questions = snapshots or {}, questions or []
    series = []
    for family in FAMILIES:
        contracts = families.get(family, {}).get("contracts", [])
        entry = contracts[0] if contracts else {}
        error = entry.get("error") or errors.get(family, {}).get("message")
        if entry.get("source_stale"):
            error = error or "主连刷新失败，仅保留此前真实缓存"
        if not entry.get("source_name"):
            error = error or "未获得主连数据来源"
        completion = _completion_details(family, manifest, snapshots.get(family, {}), questions)
        row = {"family": family, "status": "failed" if error else "waiting" if completion["needs_daily_retry"] else "updated",
               "data_as_of": entry.get("data_as_of"), "source_stale": bool(entry.get("source_stale")), **completion}
        if error:
            row["error"] = str(error)[:1000]
            row.update(completeness="unavailable", needs_daily_retry=True)
        series.append(row)
    # Keep the existing fetch-success count, and report complete daily-bar
    # readiness separately so a fresh HTTP response cannot mask an old close.
    updated = sum(item["status"] != "failed" for item in series)
    waiting = sum(item["status"] == "waiting" for item in series)
    return {"total": len(FAMILIES), "updated": updated, "failed": len(FAMILIES) - updated,
            "waiting": waiting, "completed_updated": sum(item["status"] == "updated" for item in series),
            "status": "success" if updated == len(FAMILIES) and not waiting else "partial" if updated else "failed",
            "series": series}


def _settlement_snapshots(snapshots, continuous):
    """Reuse only this run's complete, fresh captures; never fetch during scoring."""
    ready = {row["family"] for row in continuous["series"]
             if row["status"] == "updated" and not row.get("source_stale")}
    result = {}
    for family in ready:
        captured = snapshots.get(family, {})
        history = captured.get("history_bars", [])
        if not history:
            continue
        result[family] = {**captured, "source_stale": False,
                          "ordered": sorted((bar["trading_date"], bar) for bar in history)}
    return result


def _settle_history(benchmarks, snapshots, evaluations=None):
    """Settle every effective publication, including older refresh versions.

    Same-session refreshes may have their own locked predictions, so settling
    only the canonical display row would strand those runs. Missing or failed
    markets are absent from ``snapshots`` and cannot cause another upstream
    request through the settlement path.
    """
    history = benchmarks.live_history()
    questions = history["questions"]
    errors = {"history:" + key: value for key, value in history["summary"].get("errors", {}).items()}
    before_settled = sum(item.get("actual") is not None for item in questions)
    datasets = {}
    for question in questions:
        for version in question.get("versions", []):
            datasets.setdefault(version["dataset_id"], {})[version["case_id"]] = question
    checked, settled_versions, updated_runs = 0, 0, 0
    for identifier, members in datasets.items():
        try:
            dataset = benchmarks.load_dataset(identifier, mode="live")
            due = [case_id for case_id, question in members.items()
                   if case_id not in dataset["labels"] and any(
                       day > question["input_date"] for day, _ in snapshots.get(question["family"], {}).get("ordered", []))]
            if not due:
                continue
            result = benchmarks.settle_live(identifier, refresh=False, snapshots=snapshots, case_ids=due)
            checked += 1
            settled_versions += sum(case_id not in dataset["labels"] for case_id in result.get("items", {}) if case_id in due)
            for case_id, issue in result.get("errors", {}).items():
                errors[identifier + ":" + case_id] = issue
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            errors[identifier] = {"type": type(error).__name__, "message": _message(error)}
    if evaluations is not None:
        try:
            runs = evaluations.runs()
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            runs = []
            errors["runs"] = {"type": type(error).__name__, "message": _message(error)}
        for run in runs:
            if (run.get("mode") != "live" or not run.get("pending")
                    or run.get("status") in {"queued", "running"}):
                continue
            try:
                # settle() verifies prediction locks and only scores those
                # outputs. It never starts or repeats a model prediction.
                result = evaluations.settle(run["run_id"], refresh=False, snapshots=snapshots)
                updated_runs += int(result.get("succeeded", 0) > run.get("succeeded", 0))
            except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
                errors["run:" + str(run.get("run_id"))] = {"type": type(error).__name__, "message": _message(error)}
    after = benchmarks.live_history()["summary"]
    return {"status": "partial" if errors else "success", "datasets_checked": checked,
            "settled_versions": settled_versions,
            "newly_settled_questions": max(0, after.get("settled_questions", 0) - before_settled),
            "settled_questions": after.get("settled_questions", 0),
            "pending_questions": after.get("pending_questions", 0),
            "runs_updated": updated_runs, "errors": errors}


def _update_live(benchmarks, evaluations=None, on_live_published=None):
    # build(refresh=True) already refreshes all three continuous series. Keep
    # question publication close to market close, independently of slow months.
    manifest = benchmarks.build(mode="live", refresh=True, preserve_current_on_failure=True)
    if not isinstance(manifest, dict):
        raise RuntimeError("实盘题目更新未返回有效报告")
    # build() returns a public summary that intentionally omits fingerprints.
    # Read only this just-built, immutable, integrity-checked dataset to retain
    # the actual capture time and complete selected bar dates in the report.
    dataset = benchmarks.load_dataset(manifest.get("dataset_id"), mode="live")
    # Wake the separate inference worker as soon as publication is committed;
    # neither settlement nor slow month-contract requests should delay it.
    prediction_notification = None
    if on_live_published is not None:
        try:
            prediction_notification = on_live_published()
        except Exception as error:
            prediction_notification = {"status": "failed", "error": _message(error)}
    snapshots = json.loads((benchmarks.root / "datasets" / dataset["manifest"]["dataset_id"] / "source-snapshots.json").read_text(encoding="utf-8"))
    questions = benchmarks.questions(mode="live")
    continuous = _continuous_summary(dataset["manifest"], snapshots, questions)
    ready = [question for question in questions if question.get("live_status") == "ready"
             and question.get("actual") is None]
    ready_families = {question["family"] for question in ready}
    errors = [item["family"] + "：" + (item.get("error") or item["reason"])
              for item in continuous["series"] if item.get("error") or item.get("reason")]
    status = ("failed" if not continuous["updated"] else "partial" if continuous["failed"] or continuous["waiting"]
              else "updated" if ready else "waiting")
    live = {"status": status, "dataset_id": manifest.get("dataset_id"),
            "total_questions": len(questions), "ready_questions": len(ready),
            "unavailable_families": [family for family in FAMILIES if family not in ready_families],
            "error": "；".join(errors)}
    if prediction_notification is not None:
        live["model_predictions"] = prediction_notification
    try:
        settlement = _settle_history(benchmarks, _settlement_snapshots(snapshots, continuous), evaluations)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        settlement = {"status": "failed", "errors": {"history": {"type": type(error).__name__, "message": _message(error)}}}
    return continuous, live, settlement


def run_futures_update(market_data, benchmarks, evaluations=None, on_live_published=None):
    """Run independent price stages and report incomplete updates honestly.

    Only the live dataset is rebuilt; existing benchmark versions and locked
    predictions remain in their immutable directories. The optional callback
    notifies the independent prediction dispatcher without waiting for models.
    """
    started = _now()
    month, continuous, live, settlement, errors = {}, None, None, None, []
    with ThreadPoolExecutor(max_workers=2) as executor:
        month_future = executor.submit(market_data.refresh_all)
        live_future = executor.submit(_update_live, benchmarks, evaluations, on_live_published)
        try:
            month = month_future.result()
            if not isinstance(month, dict) or month.get("status") not in {"success", "partial", "failed"}:
                raise RuntimeError("月份行情更新未返回有效报告")
        except Exception as error:
            month = {"status": "failed", "error": _message(error)}
        if month["status"] != "success":
            detail = month.get("error") or "；".join(str(item.get("error") or "刷新失败")
                for item in month.get("failures", [])[:3]) or "部分或全部月份行情未更新"
            errors.append("月份行情：" + detail)
        try:
            continuous, live, settlement = live_future.result()
        except Exception as error:
            detail = _message(error)
            continuous = {"total": len(FAMILIES), "updated": 0, "failed": len(FAMILIES), "waiting": 0, "completed_updated": 0,
                          "status": "failed", "series": [
                              {"family": family, "status": "failed", "latest_completed_date": None,
                               "data_as_of": None, "fetched_at": None, "source_stale": None,
                               "recent_unverified_weekday_gaps": [], "historical_unverified_gap_count": None,
                               "completeness": "unavailable", "needs_daily_retry": True, "error": detail}
                              for family in FAMILIES], "error": detail}
            live = {"status": "failed", "dataset_id": None, "total_questions": 0, "ready_questions": 0,
                    "unavailable_families": list(FAMILIES), "error": detail}
            settlement = {"status": "waiting", "errors": {}, "reason": "本轮未取得可验证的新行情，保留原实盘结算状态"}
        if continuous["status"] != "success":
            errors.append("主连与实盘题目：" + (live["error"] or "主连数据未全部更新"))
        if settlement["status"] in {"partial", "failed"}:
            errors.append("历史实盘结算：" + "；".join(
                str(value.get("message") or value.get("type") or value) if isinstance(value, dict) else str(value)
                for value in settlement.get("errors", {}).values())[:1000])
    statuses = [month["status"], continuous["status"]]
    status = ("success" if all(item == "success" for item in statuses)
              else "failed" if all(item == "failed" for item in statuses) else "partial")
    if status == "success" and settlement["status"] in {"partial", "failed"}:
        status = "partial"
    # Preserve the existing month counters and series for API/UI compatibility.
    return {**month, "started_at": started, "finished_at": _now(), "status": status,
            "month_contracts": month, "continuous": continuous, "live_benchmark": live,
            "live_settlement": settlement,
            "error": "；".join(errors)[:2000]}
