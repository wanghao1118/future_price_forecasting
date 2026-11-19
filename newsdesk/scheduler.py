"""Persistent, server-owned update scheduling for the local research desk.

The browser only edits intervals; crawling and review continue while it is closed.
News runs in a separate process so HTTP requests remain responsive, and still uses
our normal Codex CLI reviewer and the shared per-candidate review cache.
"""

import argparse
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, time as day_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
INTERVAL_FIELDS = {"news": "news_interval_minutes", "contracts": "contract_interval_minutes"}
DEFAULT_INTERVALS = {"news_interval_minutes": 30, "contract_interval_minutes": 1440}
MAX_INTERVAL_MINUTES = 10080
DAILY_CONTRACT_POLICY = "exchange_close_daily_v1"
# Match the daily OHLC completeness buffers, with five extra minutes for
# vendor publication. Each market gets a daily opportunity before its next
# session, including across US/UK daylight-saving transitions.
DAILY_CONTRACT_TIMES = (
    ("WTI", "America/New_York", "17:35"),
    ("Brent", "Europe/London", "23:35"),
    ("SC", "Asia/Shanghai", "15:35"),
)
PUBLICATION_RETRY_SECONDS = 5 * 60
PUBLICATION_RETRY_LIMIT = 12
PUBLICATION_WINDOW_SECONDS = 60 * 60


def next_update_time(name, current, interval_minutes):
    if name != "contracts" or interval_minutes != 1440:
        return iso_time(current + interval_minutes * 60)
    candidates = []
    for _, timezone_name, clock_time in DAILY_CONTRACT_TIMES:
        zone = ZoneInfo(timezone_name)
        local = datetime.fromtimestamp(current, zone)
        for offset in range(8):
            day = local.date() + timedelta(days=offset)
            if day.weekday() >= 5:
                continue
            candidate = datetime.combine(day, day_time.fromisoformat(clock_time), tzinfo=zone).timestamp()
            if candidate > current:
                candidates.append(candidate)
                break
    return iso_time(min(candidates))


def contract_publication_retry(summary, current, previous=None):
    """Bound a late daily-bar retry to its original post-close opportunity.

    The ordinary contract job owns these retries. A waiting market whose close
    has not happened cannot create a loop, nor can an old historical data gap.
    """
    from newsdesk.benchmark import _deadline, _next_weekday

    continuous = summary.get("continuous", {}) if isinstance(summary, dict) else {}
    rows = continuous.get("series", []) if isinstance(continuous, dict) else []
    rows = rows if isinstance(rows, list) else []
    rows = {row.get("family"): row for row in rows if isinstance(row, dict)}
    for family, timezone_name, clock_time in DAILY_CONTRACT_TIMES:
        zone = ZoneInfo(timezone_name)
        local = datetime.fromtimestamp(current, zone)
        day = local.date()
        if day.weekday() >= 5:
            continue
        started = datetime.combine(day, day_time.fromisoformat(clock_time), tzinfo=zone).timestamp()
        deadline = timestamp(_deadline(family, day.isoformat(), _next_weekday(day.isoformat()), timezone_name))
        midnight = datetime.combine(day + timedelta(days=1), day_time(), tzinfo=zone).timestamp()
        expires = min(started + PUBLICATION_WINDOW_SECONDS, deadline, midnight)
        if not started <= current < expires:
            continue
        row = rows.get(family, {})
        if row.get("needs_daily_retry") is not True:
            continue
        completed = row.get("latest_completed_date")
        try:
            completed_day = datetime.fromisoformat(completed).date() if isinstance(completed, str) and len(completed) == 10 else None
        except ValueError:
            completed_day = None
        if completed_day is not None and completed_day >= day:
            continue
        same_cycle = (isinstance(previous, dict) and previous.get("family") == family
                      and previous.get("trading_date") == day.isoformat())
        attempts = previous.get("attempts", 0) if same_cycle else 0
        attempts = attempts if type(attempts) is int and attempts >= 0 else 0
        attempts += 1
        next_retry = current + PUBLICATION_RETRY_SECONDS
        allowed = attempts <= PUBLICATION_RETRY_LIMIT and next_retry < expires
        return {"family": family, "trading_date": day.isoformat(), "attempts": attempts,
                "window_started_at": iso_time(started), "expires_at": iso_time(expires),
                "next_run_at": iso_time(next_retry) if allowed else None,
                "status": "waiting" if allowed else "exhausted"}
    return None


def restore_publication_retry(job, current):
    """Keep a valid retry after restart; expire it back to the regular plan."""
    retry = job.get("publication_retry")
    if not isinstance(retry, dict) or not retry.get("next_run_at"):
        return
    try:
        expires = timestamp(retry["expires_at"])
        retry_at = timestamp(retry["next_run_at"])
        regular = retry["regular_next_run_at"]
        timestamp(regular)
        if retry_at >= expires:
            raise ValueError("重试时间已超出发布窗口")
    except (KeyError, TypeError, ValueError):
        job["publication_retry"] = None
        job["next_run_at"] = next_update_time("contracts", current, 1440)
        return
    if expires <= current:
        if job["next_run_at"] == retry["next_run_at"] or job["running"]:
            job["next_run_at"] = regular
        job["publication_retry"] = None
    elif job["running"]:
        job["next_run_at"] = iso_time(min(timestamp(regular), retry_at))


def iso_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("更新任务时间必须包含时区")
    return parsed.timestamp()


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, prefix=".update-", delete=False) as output:
            temporary = output.name
            json.dump(data, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def validate_intervals(data):
    if not isinstance(data, dict) or any(key not in DEFAULT_INTERVALS for key in data):
        raise ValueError("设置仅支持新闻与期货日K及实盘题目的更新间隔")
    for key, value in data.items():
        if type(value) is not int or not 1 <= value <= MAX_INTERVAL_MINUTES:
            raise ValueError("更新间隔必须是 1–10080 分钟的整数")
    return data


class UpdateScheduler:
    def __init__(self, path, runners, clock=time.time):
        self.path = Path(path)
        self.runners = runners
        self.clock = clock
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._workers = {}
        current = self.clock()
        self._data = {"version": 1, **DEFAULT_INTERVALS, "jobs": {}}
        for name, field in INTERVAL_FIELDS.items():
            self._data["jobs"][name] = {
                "running": False, "status": "idle", "last_started_at": None,
                "last_finished_at": None, "last_success_at": None,
                "next_run_at": next_update_time(name, current, DEFAULT_INTERVALS[field]),
                "error": "", "summary": None,
            }
            if name == "contracts":
                self._data["jobs"][name]["schedule_policy"] = DAILY_CONTRACT_POLICY
                self._data["jobs"][name]["publication_retry"] = None
        if self.path.exists():
            try:
                saved = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(saved, dict) or saved.get("version") != 1:
                    raise ValueError("无效的设置版本")
                intervals = {field: saved[field] for field in DEFAULT_INTERVALS}
                validate_intervals(intervals)
                self._data.update(intervals)
                for name, field in INTERVAL_FIELDS.items():
                    previous = saved.get("jobs", {}).get(name, {})
                    job = self._data["jobs"][name]
                    job.update({key: previous[key] for key in job if key in previous})
                    for key in ("next_run_at", "last_started_at", "last_finished_at", "last_success_at"):
                        if job[key]:
                            timestamp(job[key])
                    if not job["next_run_at"]:
                        job["next_run_at"] = next_update_time(name, current, intervals[field])
                    if name == "contracts":
                        policy = DAILY_CONTRACT_POLICY if intervals[field] == 1440 else "interval"
                        # Migrate the former 24-hour schedule once. A saved
                        # exchange-aware due time survives restarts for catchup.
                        if policy == DAILY_CONTRACT_POLICY and previous.get("schedule_policy") != policy:
                            job["next_run_at"] = next_update_time(name, current, intervals[field])
                        job["schedule_policy"] = policy
                        if policy == DAILY_CONTRACT_POLICY:
                            restore_publication_retry(job, current)
                        else:
                            job["publication_retry"] = None
                    if job["running"]:
                        job.update(running=False, status="interrupted", last_finished_at=iso_time(current),
                                   error="上次更新因本地服务退出而中断，将在下一次计划时间重试。")
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
                raise ValueError("本地更新设置文件无法读取：{}".format(error))
        self._persist()

    def _persist(self):
        atomic_json(self.path, self._data)

    def snapshot(self):
        with self._lock:
            result = copy.deepcopy(self._data)
            result.pop("version", None)
            result["server_time"] = iso_time(self.clock())
            result["contract_update_schedule"] = {
                "mode": "exchange_close_daily" if result["contract_interval_minutes"] == 1440 else "interval",
                "markets": [{"family": family, "timezone": zone, "time": at}
                            for family, zone, at in DAILY_CONTRACT_TIMES],
            }
            return result

    def configure(self, values):
        validate_intervals(values)
        with self._lock:
            before = copy.deepcopy(self._data)
            current = self.clock()
            for name, field in INTERVAL_FIELDS.items():
                if field in values and values[field] != self._data[field]:
                    self._data[field] = values[field]
                    self._data["jobs"][name]["next_run_at"] = next_update_time(name, current, values[field])
                    if name == "contracts":
                        self._data["jobs"][name]["schedule_policy"] = DAILY_CONTRACT_POLICY if values[field] == 1440 else "interval"
                        self._data["jobs"][name]["publication_retry"] = None
            try:
                self._persist()
            except OSError:
                self._data = before
                raise
        self._wake.set()
        return self.snapshot()

    def start(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="oil-update-scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=2)
        # The news subprocess sees _stop within one second. Market requests have
        # bounded network timeouts and may finish after server_close returns.
        for worker in list(self._workers.values()):
            worker.join(timeout=2)

    def trigger(self, name):
        if name not in INTERVAL_FIELDS:
            raise ValueError("未知的更新任务")
        with self._lock:
            job = self._data["jobs"][name]
            if self._stop.is_set() or job["running"]:
                return False
            before = copy.deepcopy(job)
            current = self.clock()
            job.update(running=True, status="running", last_started_at=iso_time(current), error="",
                       next_run_at=next_update_time(name, current, self._data[INTERVAL_FIELDS[name]]))
            try:
                self._persist()
            except OSError:
                self._data["jobs"][name] = before
                raise
            worker = threading.Thread(target=self._execute, args=(name,), name="oil-update-" + name, daemon=True)
            self._workers[name] = worker
            worker.start()
            return True

    def run_due(self):
        """One scheduling pass, also useful for deterministic offline tests."""
        with self._lock:
            current = self.clock()
            contracts = self._data["jobs"]["contracts"]
            if self._data["contract_interval_minutes"] == 1440 and not contracts["running"]:
                before = copy.deepcopy(contracts)
                restore_publication_retry(contracts, current)
                if contracts != before:
                    try:
                        self._persist()
                    except OSError:
                        self._data["jobs"]["contracts"] = before
                        raise
            due = [name for name, job in self._data["jobs"].items()
                   if not job["running"] and timestamp(job["next_run_at"]) <= current]
        for name in due:
            self.trigger(name)

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.run_due()
            except OSError as error:
                # A transient disk error must not permanently stop future runs.
                print("自动更新调度暂时失败：{}".format(error), file=sys.stderr, flush=True)
            self._wake.wait(timeout=1)
            self._wake.clear()

    def _execute(self, name):
        result, error, status = None, "", "success"
        try:
            result = self.runners[name]()
            if not isinstance(result, dict):
                raise RuntimeError("更新流程没有返回有效报告")
            status = result.get("status", "success")
            if status == "complete":
                status = "success"
            if status not in {"success", "partial", "failed", "interrupted"}:
                raise RuntimeError("更新流程返回了无效状态")
            error = str(result.get("error") or "")
        except Exception as exception:
            status = "interrupted" if self._stop.is_set() else "failed"
            error = str(exception)[:2000] or type(exception).__name__
            result = getattr(exception, "summary", None)
        with self._lock:
            current = self.clock()
            job = self._data["jobs"][name]
            job.update(running=False, status=status, last_finished_at=iso_time(current), error=error, summary=result)
            if status == "success":
                job["last_success_at"] = iso_time(current)
            # A run longer than its interval does not produce a backlog or an
            # immediate crawl loop. Changes saved during a run are respected.
            if timestamp(job["next_run_at"]) <= current:
                job["next_run_at"] = next_update_time(name, current, self._data[INTERVAL_FIELDS[name]])
            if name == "contracts":
                retry = (contract_publication_retry(result, current, job.get("publication_retry"))
                         if self._data["contract_interval_minutes"] == 1440 else None)
                job["publication_retry"] = retry
                if retry:
                    retry["regular_next_run_at"] = job["next_run_at"]
                    if retry["next_run_at"] and timestamp(retry["next_run_at"]) < timestamp(job["next_run_at"]):
                        job["next_run_at"] = retry["next_run_at"]
            try:
                self._persist()
            except OSError as exception:
                print("无法保存自动更新结果：{}".format(exception), file=sys.stderr, flush=True)
        self._wake.set()


def source_health(manifest, source_ids):
    """Summarize primary feeds and supplements without counting a source twice."""
    records = {identifier: [] for identifier in source_ids}

    def walk(value):
        if isinstance(value, dict):
            identifier = value.get("source_id")
            if identifier in records and "status" in value:
                records[identifier].append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(manifest)
    failed, partial = [], []
    failed_states = {"failed", "error", "requires_access", "needs_access", "no_public"}
    for identifier, entries in records.items():
        if not entries or all(item["status"] in failed_states for item in entries):
            failed.append(identifier)
        elif any(item["status"] in failed_states | {"partial"} for item in entries):
            partial.append(identifier)
    return {"sources_total": len(records), "sources_failed": len(failed), "sources_partial": len(partial),
            "failed_sources": failed, "partial_sources": partial}


def news_worker(database, output, run_dir):
    """Runs in its own process; exceptions leave collection/review audit intact."""
    from newsdesk.ingest import collect, process
    registry = json.loads((ROOT / "source_registry.json").read_text(encoding="utf-8"))
    source_ids = [item["id"] for item in registry["sources"] if item.get("enabled")]
    if not source_ids:
        raise RuntimeError("没有已接通的新闻信源")
    manifest = collect(run_dir, source_ids=source_ids)
    from newsdesk.source_maintenance import update_results
    from newsdesk.ingest import read_candidates
    update_results(Path(database).parent / 'source-health', manifest,
                   read_candidates(Path(run_dir) / 'candidates.jsonl'), source_ids)
    health = source_health(manifest, source_ids)
    report = process(Path(run_dir) / "candidates.jsonl", output=output, database=database)
    report.update(health)
    no_valid_content = bool(report.get("invalid_count")) and not report.get("candidate_count")
    report["status"] = "failed" if health["sources_failed"] == len(source_ids) else "partial" if health["sources_failed"] or health["sources_partial"] or no_valid_content else "success"
    warnings = []
    if health["sources_failed"] or health["sources_partial"]:
        warnings.append("{} 个信源抓取失败，{} 个信源仅部分完成".format(health["sources_failed"], health["sources_partial"]))
    if no_valid_content:
        warnings.append("本轮内容均未通过完整性校验，未获得可审核新闻")
    report["error"] = "；".join(warnings)
    atomic_json(Path(run_dir) / "scheduled-summary.json", report)
    return report


def run_news_update(database, output, stop_event=None):
    output = Path(output)
    run_dir = output / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-auto-" + uuid.uuid4().hex[:6])
    run_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "scheduled.log"
    command = [sys.executable, "-m", "newsdesk.scheduler", "--news-worker", "--database", str(Path(database).resolve()),
               "--output", str(output.resolve()), "--run-dir", str(run_dir.resolve())]
    with log_path.open("w", encoding="utf-8") as log:
        child = subprocess.Popen(command, cwd=str(ROOT.parent), stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while True:
                try:
                    code = child.wait(timeout=1)
                    break
                except subprocess.TimeoutExpired:
                    if stop_event is not None and stop_event.is_set():
                        raise RuntimeError("本地服务停止，新闻自动更新已中断；候选与审核缓存已保留。")
        finally:
            if child.poll() is None:
                # Includes the Codex reviewer children started by the worker.
                import signal
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                    child.wait(timeout=3)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=3)
    summary_path = run_dir / "scheduled-summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        summary.update(log_path=str(log_path.resolve()), run_directory=str(run_dir.resolve()))
        return summary
    raise RuntimeError("新闻更新流程未完成（退出码 {}）。详情：{}".format(code, log_path.resolve()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news-worker", action="store_true", required=True)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        news_worker(args.database, args.output, args.run_dir)
    except Exception as error:
        # Preserve collection/review counts and any committed archive counts.
        # Absence of archive counts means the new-event total is unconfirmed.
        summary = {"run_id": args.run_dir.name}
        for filename in ("collection.json", "pipeline.json"):
            try:
                saved = json.loads((args.run_dir / filename).read_text(encoding="utf-8"))
                for key in ("candidate_count", "reviewed_count", "relevant_count", "rejected_count",
                            "created", "updated", "reactivated", "written", "unchanged", "exact_duplicates",
                            "invalid_count", "invalid_by_source", "invalid_candidates_path",
                            "time_skipped_count", "time_skipped_by_source", "archive_time_skipped_count"):
                    if key in saved:
                        summary[key] = saved[key]
            except (OSError, ValueError, TypeError):
                continue
        summary.update(status="failed", error=str(error)[:2000])
        atomic_json(args.run_dir / "scheduled-summary.json", summary)
        raise


if __name__ == "__main__":
    main()
