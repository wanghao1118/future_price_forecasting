"""Durable public-history collection followed by the ordinary Codex reviewer.

This is a separate, resumable backfill; the thirty-minute latest-news scheduler
continues independently. A source work cap only yields its turn. It never means
that all of the requested year has been collected.
"""

import argparse
import fcntl
import hashlib
import heapq
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from zoneinfo import ZoneInfo

from newsdesk.ingest import DEFAULT_OUTPUT, canonical_url, prepare_candidate, write_json
from newsdesk.server import DEFAULT_DATABASE, NewsStore
from newsdesk.history_merge import merge_channel_reports

ROOT = Path(__file__).resolve().parent
TZ = ZoneInfo("Asia/Shanghai")
COUNTERS = ("created", "updated", "reactivated", "written", "unchanged", "exact_duplicates", "reviewed_count")
PROGRESS_VERSION = 3
ORIGINAL_BROKERS = {'institution_11','institution_18','institution_21','institution_23','institution_24','institution_49'}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Stopped(BaseException):
    pass


class ReviewServiceUnavailable(RuntimeError):
    """The review service cannot currently accept work; keep its queue intact."""


def service_unavailable(error):
    message = str(error).casefold()
    return isinstance(error, ReviewServiceUnavailable) or any(term in message for term in (
        "codex agent 调用失败", "未发现 codex cli", "rate limit", "rate_limit", "at capacity",
        "service unavailable", "temporarily unavailable", "usage limit", "quota exceeded",
        "not logged in", "unauthenticated", "authentication required"))


def publication_day(item):
    value = item.get("published_at") or item.get("published_date")
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return (parsed.astimezone(TZ) if parsed.tzinfo else parsed).date().isoformat()
    except ValueError:
        return None


def enabled_sources(registry=None):
    registry = registry or json.loads((ROOT / "source_registry.json").read_text(encoding="utf-8"))
    result = {}
    for item in registry["sources"]:
        if not item.get("enabled"):
            continue
        adapter = item.get("adapter")
        group = ("media" if adapter in {"news_sources", "additional_sources"} else
                 "overseas" if adapter in {"research_sources", "overseas_sources"} else
                 "companies" if adapter == "public_company_sources" else
                 "brokers" if adapter in {"domestic_brokers_a", "domestic_brokers_b", "xingye_source"} else None)
        result[item["id"]] = {"name": item["name"], "group": group}
    return result


def stop_checked(fetcher, stop):
    original = fetcher.get
    def checked_get(*args, **values):
        if stop.is_set():
            raise Stopped()
        return original(*args, **values)
    fetcher.get = checked_get
    return fetcher


def call_adapter(source_id, spec, start, end, output, stop):
    if not spec["group"]:
        return {"source_id": source_id, "status": "blocked", "errors": ["此信源尚无公开历史适配器"]}
    group = spec["group"]
    module = importlib.import_module("newsdesk.public_company_sources" if group == 'companies' else "newsdesk.history_" + group)
    directory = Path(output) / ("brokers" if group == "brokers" else "sources/" + source_id)
    if group == "media":
        fetcher, kwargs = module.HistoryFetcher(directory), {"max_pages": 10, "max_articles": 40}
    elif group == "overseas":
        fetcher, kwargs = module.Fetcher(directory / "raw", source_id), {"max_articles": 40, "max_discovery_pages": 5}
    elif group == 'companies':
        fetcher, kwargs = module.Fetcher(directory), {'max_pages':10,'max_articles':40}
    else:
        fetcher, kwargs = module.Fetcher(directory), {"max_days": 7}
    priority_months = spec.get('priority_months', [])
    if priority_months and (group == 'brokers' or source_id == 'ths'):
        kwargs['priority_months'] = priority_months
        if group == 'brokers':
            kwargs['max_days'] = max(7, len(priority_months))
    primary = module.run_source(source_id, start, end, directory, fetcher=stop_checked(fetcher, stop), **kwargs)
    channels = {'primary':primary}
    if source_id == 'cls':
        if priority_months:
            extra = importlib.import_module('newsdesk.history_priority_topics')
            extra_dir = Path(output)/'sources/cls-monthly-topics'
            channels['priority_monthly_topics'] = extra.run_monthly_topics(
                source_id,start,end,extra_dir,priority_months=priority_months,
                max_pages=18,max_articles=180,fetcher=stop_checked(extra.HistoryFetcher(extra_dir),stop))
            macro = importlib.import_module('newsdesk.history_cls_macro')
            macro_dir = Path(output)/'sources/cls-macro-topics'
            channels['priority_macro_topics'] = macro.run_source(source_id,start,end,macro_dir,
                priority_months=priority_months,max_pages=18,max_articles=180,
                fetcher=stop_checked(macro.HistoryFetcher(macro_dir),stop))
            # Preserve discoveries and coverage from the previous full-window
            # topic pass while the month-specific cursors fill older gaps.
            old_topics = Path(output)/'sources/cls-topics'
            for path in [old_topics/'report.json']:
                try:
                    previous = json.loads(path.read_text(encoding='utf-8'))
                except (ValueError, OSError):
                    continue
                if previous.get('candidates_path'):
                    channels['previous_public_topics'] = {
                        'source_id':source_id,'status':'complete','terminal':True,
                        'candidate_count':previous.get('candidate_count',0),
                        'candidates_path':previous['candidates_path'],
                        'original_report_path':str(path),
                        'original_coverage':previous.get('coverage',{}),
                        'coverage':{'complete':True,
                            'scope':'已保存候选快照；活动历史覆盖以逐月专题任务为准，快照完整不代表历史完整'}}
                    break
        else:
            extra = importlib.import_module('newsdesk.history_media_extra')
            extra_dir = Path(output)/'sources/cls-topics'
            channels['public_topics'] = extra.run_source(source_id,start,end,extra_dir,
                max_pages=10,max_articles=40,fetcher=stop_checked(extra.HistoryFetcher(extra_dir),stop))
    elif source_id == 'ths' and priority_months:
        extra = importlib.import_module('newsdesk.history_commodity_ths')
        extra_dir = Path(output)/'sources/ths-commodity'
        channels['commodity_futures_archive'] = extra.run_source(source_id,start,end,extra_dir,
            priority_months=priority_months,max_pages=18,max_articles=180,
            fetcher=stop_checked(extra.Fetcher(extra_dir),stop))
    elif source_id in ORIGINAL_BROKERS:
        extra = importlib.import_module('newsdesk.history_broker_channels')
        extra_dir = Path(output)/'broker-original'/source_id
        channels['original_channels'] = extra.run_original_channel(source_id,start,end,extra_dir,
            max_pages=2,metadata_only=False,fetcher=stop_checked(extra.Fetcher(extra_dir),stop))
    if group == 'brokers' and priority_months:
        extra = importlib.import_module('newsdesk.history_oil_research')
        extra_dir = Path(output)/'oil-research'
        industry = extra.run_source(source_id,start,end,extra_dir,
            priority_months=priority_months,max_pages=6,max_articles=20,
            fetcher=stop_checked(extra.Fetcher(extra_dir),stop))
        # Equal-content Sina IDs keep the richer discovery provenance while
        # retaining every original channel and its independent coverage gaps.
        channels = {'oil_industry_research':industry,**channels}
    if len(channels)>1:
        return merge_channel_reports(source_id,start,end,Path(output)/'combined'/source_id,channels)
    return primary


def progress_signature(report):
    """Use coverage work, not timestamps or transient exception text."""
    counts = tuple(report.get(key, 0) for key in ("candidate_count", "completed_days", "pages",
        "processed_article_count", "discovery_pages_total", "discovered_article_count", "discovery_count", "attempted_days"))
    processed = report.get("processed_dates", [])
    return counts + (len(set(processed)) if isinstance(processed, list) else 0,
                     report.get("frontier_date"), report.get("retry_attempts_remaining", 0),
                     json.dumps(channel_progress(report.get('channels')),ensure_ascii=False,sort_keys=True))


def channel_progress(value):
    """Keep bounded channel work comparable across JSON checkpoint reloads."""
    fields = ('candidate_count','pages','next_page','cursor','offset','completed_days','attempted_days',
              'processed_article_count','discovery_count','completed_topics','terminal','complete','frontier_date',
              'retry_pending','retry_attempts_remaining')
    if isinstance(value,list):
        return {str(item.get('channel',index)):channel_progress(item) for index,item in enumerate(value) if isinstance(item,dict)}
    if isinstance(value,dict):
        result = {key:value[key] for key in fields if key in value}
        if 'channels' in value:
            result['channels'] = channel_progress(value['channels'])
        if not any(key in value for key in fields):
            result.update({str(key):channel_progress(item) for key,item in value.items() if isinstance(item,(dict,list))})
        return result
    return None


def bounded_retry_remaining(report):
    remaining = report.get("retry_attempts_remaining")
    return remaining if (report.get("retry_pending") is True and type(remaining) is int and 0 < remaining <= 2) else 0


def month_keys(start, end):
    current, last = date.fromisoformat(start).replace(day=1), date.fromisoformat(end).replace(day=1)
    values = []
    while current <= last:
        values.append(current.strftime("%Y-%m"))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return values


def candidate_distribution(path, start, end):
    months = {key: 0 for key in month_keys(start, end)}
    dates, unknown, seen = [], 0, set()
    if path and Path(path).exists():
        with Path(path).open(encoding="utf-8") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    identity = row.get("candidate_id") or row.get("source_url")
                    if identity in seen:
                        continue
                    seen.add(identity)
                    day = publication_day(row)
                    if not day:
                        unknown += 1
                    elif start <= day <= end:
                        dates.append(day)
                        months[day[:7]] += 1
                except (ValueError, TypeError, AttributeError):
                    unknown += 1
    return {"candidate_counts_by_month": months, "earliest_publication_date": min(dates, default=None),
            "latest_publication_date": max(dates, default=None), "unknown_date_candidates": unknown}


def review_channel(item):
    """Schedule verified publisher channels; never decide oil relevance here."""
    if item.get('source_id') == 'cls' and str(item.get('discovery_topic_id')) in {
            '1583','1101','10986','11394','1515',
            '3140','7381','1622','1605','1350','2565','2223','1225','1086',
            '1556','1741','11424','11425'}:
        return 'oil_topics'
    if (str(item.get('source_id','')).startswith('institution_')
            and item.get('discovery_research_channel') == 'sina_public_oil_industries'
            and item.get('discovery_industry_code') in {'sw2_750100','sw2_750200','sw2_750300'}):
        return 'oil_topics'
    if item.get('source_id') == 'ths' and str(item.get('source_listing_url','')).startswith(
            'https://goodsfu.10jqka.com.cn/qhkx_list/'):
        return 'commodity_archive'
    return 'other_public_sources'


def review_subprocess(candidate_file, database, cache, stop, *, workers=1):
    directory = Path(candidate_file).parent
    command = [sys.executable, "-m", "newsdesk.ingest", "review", "--candidates", str(candidate_file),
               "--database", str(database), "--output", str(cache), "--workers", str(workers), "--batch-size", "30"]
    with (directory / "review.log").open("a", encoding="utf-8") as log:
        child = subprocess.Popen(command, cwd=ROOT.parent, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while child.poll() is None:
                if stop.wait(1):
                    raise Stopped()
        finally:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    child.wait(timeout=4)
    path = directory / "pipeline.json"
    report = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if child.returncode or report.get("status") != "complete":
        raise RuntimeError(report.get("error") or "Codex 审核未完成；详情见 " + str(directory / "review.log"))
    return report


class Backfill:
    def __init__(self, start, end, output, database=DEFAULT_DATABASE, sources=None, *,
                 adapter=call_adapter, processor=review_subprocess, poll_seconds=3, retry_reviews=False,
                 retry_sources=False, retry_delay=5, service_backoff_base=60, service_backoff_max=1800,
                 priority_months=None, monthly_target=None, review_workers=None, reference_month=None):
        first, last = date.fromisoformat(start), date.fromisoformat(end)
        if first > last or last > datetime.now(TZ).date():
            raise ValueError("历史日期范围无效或结束日期晚于今天")
        self.start, self.end = start, end
        self.output, self.database = Path(output).resolve(), Path(database).resolve()
        self.output.mkdir(parents=True, exist_ok=True)
        self.path, self.ledger_path = self.output / "manifest.json", self.output / "review-ledger.json"
        self.adapter, self.processor, self.poll_seconds = adapter, processor, poll_seconds
        self.retry_delay = retry_delay
        if service_backoff_base <= 0 or service_backoff_max < service_backoff_base:
            raise ValueError("审核服务退避间隔无效")
        self.service_backoff_base, self.service_backoff_max = service_backoff_base, service_backoff_max
        self.stop, self.collected = threading.Event(), threading.Event()
        self.lock = threading.RLock()
        self.specs = {key:dict(value) for key,value in (sources if sources is not None else enabled_sources()).items()}
        saved = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None
        if saved and any(saved.get(key) != value for key, value in
                         (("start_date", start), ("end_date", end), ("database", str(self.database)))):
            raise ValueError("输出目录中的历史任务范围或数据库不同，请使用另一目录")
        self.state = saved or {"version": 1, "start_date": start, "end_date": end,
            "database": str(self.database), "started_at": now(), "sources": {}, "completed_chunks": [],
            "counts": {key: 0 for key in COUNTERS}}
        requested_months = list(dict.fromkeys(priority_months if priority_months is not None
                                             else self.state.get('priority_months', [])))
        if any(month not in month_keys(start,end) for month in requested_months):
            raise ValueError('优先月份必须属于本次历史日期范围，格式为 YYYY-MM')
        target = monthly_target if monthly_target is not None else self.state.get('monthly_target')
        if target is not None and (type(target) is not int or target < 1 or not requested_months):
            raise ValueError('每月覆盖目标必须是正整数且指定优先月份')
        reference = reference_month if reference_month is not None else self.state.get('reference_month')
        if reference is not None and (reference not in month_keys(start,end) or not target):
            raise ValueError('参照月份必须属于历史日期范围且设置每月覆盖目标')
        workers = review_workers if review_workers is not None else self.state.get('review_workers',1)
        if type(workers) is not int or not 1 <= workers <= 24:
            raise ValueError('审核并发数必须为 1 至 24')
        self.priority_months, self.monthly_target, self.reference_month = requested_months, target, reference
        previous_chunk_size = self.state.get('review_chunk_size',60)
        chunk_size = max(60,workers*30)
        if self.state.get('review_chunk_limit',previous_chunk_size) == previous_chunk_size:
            self.state['review_chunk_limit'] = chunk_size
        self.state.update(priority_months=requested_months,monthly_target=target,
                          review_workers=workers,review_chunk_size=chunk_size,reference_month=reference)
        if processor is review_subprocess:
            self.processor = partial(review_subprocess,workers=workers)
        for spec in self.specs.values():
            spec['priority_months'] = requested_months
        self.state.setdefault("existing_record_ids", [])
        self.existing_record_ids = set(self.state["existing_record_ids"])
        self.state.update(status="running", last_error="")
        self.state.pop("review_worker_error", None)
        self.state.pop("finished_at", None)
        self.state.setdefault("review_service_status", "ready")
        self.state.setdefault("next_review_retry_at", None)
        self.state.setdefault("last_review_error", "")
        self.state.setdefault("review_service_retries", 0)
        self.state.setdefault("consecutive_service_failures", 0)
        self.state.setdefault('review_chunk_limit',60)
        self.state.setdefault('review_partition_successes',0)
        self.ledger = json.loads(self.ledger_path.read_text(encoding="utf-8")) if self.ledger_path.exists() else {}
        if retry_reviews:
            self.ledger = {key: value for key, value in self.ledger.items() if value != "review_failed"}
        for identifier, spec in self.specs.items():
            self.state["sources"].setdefault(identifier, {"name": spec["name"], "status": "pending", "stalled": 0})
            old = self.state["sources"][identifier]
            if (identifier=='cls' or identifier in ORIGINAL_BROKERS) and not old.get('report',{}).get('channels'):
                # A newly added public channel can extend an earlier feed-only
                # boundary; its availability must be checked before stopping.
                old.update(terminal=False,stalled=0)
            if old.get("progress_version", 0) != PROGRESS_VERSION:
                # Older coordinators could stop a calendar source while only
                # counting accepted articles. Reopen only that coordinator stop,
                # never a genuine public-access or exhausted-index terminal.
                report = old.get("report", {})
                if (old.get("stalled", 0) >= 2 and old.get("status") in {"partial", "incomplete"}
                        and not report.get("terminal") and report.get("status") not in {"blocked", "failed", "complete"}):
                    old.update(terminal=False)
                    old.pop("stop_reason", None)
                old["stalled"] = 0
            if retry_sources and self.state["sources"][identifier].get("status") != "complete":
                self.state["sources"][identifier].update(terminal=False, stalled=0)
            if old.get('report') and not old.get('date_distribution'):
                old['date_distribution']=candidate_distribution(old['report'].get('candidates_path'),start,end)
        self.state["active_sources"] = list(self.specs)
        self._recover_chunks()
        self._save()

    def _recover_chunks(self):
        """A completed chunk is a write-ahead result for a crashed coordinator."""
        changed = False
        for path in sorted((self.output / "review").glob("*/pipeline.json")):
            report = json.loads(path.read_text(encoding="utf-8"))
            if report.get("status") != "complete":
                continue
            if path.parent.name not in self.state["completed_chunks"]:
                for field in COUNTERS:
                    self.state["counts"][field] += int(report.get(field, 0))
                self.state["completed_chunks"].append(path.parent.name)
            candidate_path = path.parent / "candidates.jsonl"
            for line_number, line in enumerate(candidate_path.read_text(encoding="utf-8").split("\n"), 1):
                if not line.strip():
                    continue
                try:
                    item = prepare_candidate(json.loads(line))
                except (ValueError, TypeError) as error:
                    # A formerly accepted row may fail today's validation.
                    # Preserve its old journal/counts, quarantine the physical
                    # row exactly as the pending scanner does, and recover the
                    # other completed decisions without invoking the model.
                    key = 'invalid:' + hashlib.sha256(line.strip().encode()).hexdigest()
                    if key not in self.ledger:
                        self._audit('invalid-candidates', {'candidate_file':str(candidate_path),
                            'line':line_number,'error':str(error),'raw_line':line})
                        self.ledger[key] = 'invalid'
                        changed = True
                    continue
                self.ledger[item["candidate_id"] + ":" + item["content_hash"]] = "reviewed"
                changed = True
        if changed:
            write_json(self.ledger_path, self.ledger)

    def _save(self):
        self.state["updated_at"] = now()
        self.state["coverage_overview"] = self._coverage_overview()
        write_json(self.path, self.state)

    def _coverage_overview(self):
        months = month_keys(self.start, self.end)
        by_month = {month: {"captured_candidates": 0,
                           "current_archived_news": self.state.get("archive_distribution", {}).get("by_month", {}).get(month),
                           "sources_with_candidates": []} for month in months}
        for month in self.priority_months:
            archived = by_month[month]['current_archived_news']
            by_month[month].update(target=self.monthly_target,
                                  shortfall=max(0,self.monthly_target-archived)
                                  if self.monthly_target is not None and archived is not None else None)
        sources = {}
        for identifier, source in self.state["sources"].items():
            report, distribution = source.get("report", {}), source.get("date_distribution", {})
            coverage = report.get("coverage", {})
            gaps = set(coverage.get("gap_dates", []))
            gaps.update(day for day, detail in report.get("days", {}).items() if detail.get("status") != "complete")
            earliest = (distribution.get("earliest_publication_date") or coverage.get("earliest_publication_date")
                        or coverage.get("observed_earliest") or report.get("earliest_publication_date"))
            latest = (distribution.get("latest_publication_date") or coverage.get("latest_publication_date")
                      or coverage.get("observed_latest") or report.get("latest_publication_date"))
            sources[identifier] = {"name": source.get("name"), "status": source.get("status"),
                "terminal": source.get("terminal", False), "earliest_publication_date": earliest,
                "latest_publication_date": latest, "frontier_date": report.get("frontier_date"),
                "attempted_days": report.get("attempted_days"), "processed_days": report.get("completed_days"),
                "gap_dates": sorted(gaps), "candidate_counts_by_month": distribution.get("candidate_counts_by_month", {}),
                "scope": coverage.get("scope") or report.get("scope") or report.get("coverage_scope"),
                "complete": source.get("status") == "complete"}
            for month, count in distribution.get("candidate_counts_by_month", {}).items():
                if month in by_month:
                    by_month[month]["captured_candidates"] += count
                    if count:
                        by_month[month]["sources_with_candidates"].append(identifier)
        return {"requested_start": self.start, "requested_end": self.end, "months": by_month, "sources": sources,
                "note": "候选数量是已抓取公开内容，已入库数量来自当前正式新闻；0条不证明该日或该月未发生新闻。"}

    def _update_archive_distribution(self, records):
        distribution = {"by_month": {month: 0 for month in month_keys(self.start, self.end)},
                        "dated_records_in_window": 0, "visible_records_with_unknown_date": 0}
        for record in records:
            if record.get("is_demo") or record.get("agent_review", {}).get("relevance") is False:
                continue
            day = publication_day({"published_at": record.get("published_at"),
                                   "published_date": record.get("ingestion", {}).get("published_date")})
            if not day:
                distribution["visible_records_with_unknown_date"] += 1
            elif self.start <= day <= self.end:
                distribution["by_month"][day[:7]] += 1
                distribution["dated_records_in_window"] += 1
        with self.lock:
            previous = {key: value for key, value in self.state.get("archive_distribution", {}).items() if key != "checked_at"}
            target_changed = False
            if self.reference_month:
                count = distribution['by_month'].get(self.reference_month,0)
                target = max(self.monthly_target,((count+99)//100)*100)
                target_changed = target != self.monthly_target
                self.monthly_target = target
                self.state.update(monthly_target=target,reference_month_count=count)
            if previous != distribution or target_changed:
                self.state["archive_distribution"] = {**distribution, "checked_at": now()}
                self._save()

    def _audit(self, kind, record):
        with (self.output / (kind + ".jsonl")).open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _collect(self, identifier):
        try:
            report = self.adapter(identifier, self.specs[identifier], self.start, self.end, self.output, self.stop)
            if not isinstance(report, dict):
                raise ValueError("历史适配器没有返回有效报告")
        except Stopped:
            return
        except Exception as error:
            # A failed advance must not erase the durable queue discovered by
            # previous successful calls. Keep those candidates reviewable while
            # recording the collection failure and its public coverage gap.
            with self.lock:
                report = dict(self.state['sources'][identifier].get('report',{}))
            previous_errors = list(report.get('errors',[]))
            report.update(source_id=identifier,status='failed',terminal=True,
                errors=previous_errors+[{'phase':'collector_exception','error':str(error)[:1500]}],
                collection_error=str(error)[:1500])
        distribution = candidate_distribution(report.get("candidates_path"), self.start, self.end)
        with self.lock:
            previous = self.state["sources"][identifier]
            signature = progress_signature(report)
            stalled = previous.get("stalled", 0) + 1 if tuple(previous.get("progress", [])) == signature else 0
            complete = report.get("status") == "complete" or report.get("coverage", {}).get("complete") is True
            # Calendar adapters may need a bounded second attempt before moving
            # past a transient error. Never let the coordinator's no-progress
            # check stop that declared retry before the next date is attempted.
            retry_grace = (previous.get("retry_grace", 0) + 1
                           if stalled and bounded_retry_remaining(report) else 0)
            stalled_out = stalled >= 2 and not (bounded_retry_remaining(report) and retry_grace <= 2)
            terminal = complete or report.get("status") in {"blocked", "failed"} or report.get("terminal") is True or stalled_out
            self.state["sources"][identifier] = {"name": self.specs[identifier]["name"],
                "status": "complete" if complete else report.get("status", "incomplete"),
                "terminal": terminal, "stalled": stalled, "progress": list(signature), "report": report,
                "progress_version": PROGRESS_VERSION, "retry_grace": retry_grace,
                "date_distribution": distribution}
            if stalled_out and not complete:
                self.state["sources"][identifier]["stop_reason"] = "连续两轮没有历史覆盖进展，保留公开访问缺口"
            self._save()
        print("历史采集 {}：{}，候选 {}".format(identifier, report.get("status"), report.get("candidate_count", 0)), flush=True)

    def _pending(self, limit=60):
        rows, keys = [], []
        changed = False
        monthly_queues, pending_counts, source_ordinals, seen_pending = {}, {}, {}, set()
        channel_counts = {}
        channel_cycle = ('oil_topics', 'oil_topics', 'oil_topics',
                         'commodity_archive', 'commodity_archive', 'other_public_sources')
        # Selection is a proposal. Failed reviews and smaller retry partitions
        # must start from the last successfully committed scheduling position.
        self._pending_review_channel_positions = {}
        self._pending_review_stream_positions = {}
        with self.lock:
            channel_positions = dict(self.state.get('review_channel_positions', {}))
            stream_positions = {month: dict(channels) for month, channels in
                                self.state.get('review_stream_positions', {}).items()}
            source_positions = {source_id: index for index, source_id in
                                enumerate(key for key in self.state['sources'] if key in self.specs)}

        def stream_key(item, channel):
            # review_channel has already checked publisher identity and the
            # official discovery enum. These are scheduling streams, never a
            # semantic relevance or final news category decision.
            detail = 0
            if channel == 'oil_topics':
                if item['source_id'] == 'cls':
                    detail = int(item['discovery_topic_id'])
                else:
                    detail = int(item['discovery_industry_code'].removeprefix('sw2_'))
            return item['source_id'], detail

        def stream_order(stream):
            return source_positions.get(stream[0], len(source_positions)), stream[1]
        # Backfill fills gaps. Never replace a visible existing article or human
        # edits with an older, shorter public history summary. Excluded records
        # remain eligible for a fresh semantic review.
        identities, urls = {}, {}
        records = NewsStore(self.database, seed_demo=False).all() if self.database.exists() else []
        self._update_archive_distribution(records)
        if records:
            for record in records:
                if record.get("is_demo") or record.get("agent_review", {}).get("relevance") is False:
                    continue
                ingestion = record.get("ingestion", {})
                identity = (ingestion.get("source_id"), ingestion.get("external_id"))
                if all(identity):
                    identities[identity] = record["id"]
                if ingestion.get("source_page_kind") != "collection":
                    value = ingestion.get("canonical_url") or record.get("source_url")
                    if value:
                        try:
                            urls[canonical_url(value)] = record["id"]
                        except ValueError:
                            pass
        with self.lock:
            reports = [dict(value.get("report", {})) for key, value in self.state["sources"].items() if key in self.specs]
        for source_index, report in enumerate(reports):
            path = report.get("candidates_path")
            if not path or not Path(path).exists():
                continue
            with Path(path).open(encoding="utf-8") as source:
                for line_number, line in enumerate(source, 1):
                    if not line.strip():
                        continue
                    digest = hashlib.sha256(line.strip().encode()).hexdigest()
                    try:
                        raw = json.loads(line)
                        item = prepare_candidate(raw)
                        key = item["candidate_id"] + ":" + item["content_hash"]
                    except (ValueError, TypeError) as error:
                        key = "invalid:" + digest
                        if key not in self.ledger:
                            self._audit("invalid-candidates", {"candidate_file": path, "line": line_number,
                                "error": str(error), "raw_line": line.rstrip()})
                            self.ledger[key] = "invalid"
                            changed = True
                        continue
                    existing_id = identities.get((item["source_id"], item.get("external_id")))
                    if not existing_id and not item.get("url_is_collection"):
                        existing_id = urls.get(item["canonical_url"])
                    # A previously protected item can become eligible if the
                    # visible record was later deleted or explicitly excluded.
                    if self.ledger.get(key) == "existing_record" and not existing_id:
                        del self.ledger[key]
                        changed = True
                    if existing_id and self.ledger.get(key) != "reviewed":
                        if self.ledger.get(key) != "existing_record" or existing_id not in self.existing_record_ids:
                            self.ledger[key] = "existing_record"
                            self.existing_record_ids.add(existing_id)
                            changed = True
                        continue
                    if key in self.ledger or key in keys or key in seen_pending:
                        continue
                    day = publication_day(item)
                    if not day or not self.start <= day <= self.end:
                        reason = "unknown_date" if not day else "outside_window"
                        self._audit("excluded-dates", {"reason": reason, "candidate": item})
                        self.ledger[key] = reason
                        changed = True
                        continue
                    if self.priority_months:
                        month = day[:7]
                        seen_pending.add(key)
                        pending_counts[month] = pending_counts.get(month,0)+1
                        channel = review_channel(item)
                        channel_counts[channel] = channel_counts.get(channel,0)+1
                        stream = stream_key(item, channel)
                        ordinal_key = (month, channel, stream)
                        ordinal = source_ordinals.get(ordinal_key,0)
                        source_ordinals[ordinal_key] = ordinal+1
                        position = stream_order(stream)
                        previous_stream = stream_positions.get(month, {}).get(channel)
                        wrapped = int(previous_stream is not None and
                                      position <= stream_order(previous_stream))
                        # Keep at most limit candidates for EACH channel. A
                        # large oil backlog must not evict the next commodity
                        # or general turn before the weighted cycle runs.
                        # Within a channel take each stream's first candidate
                        # before any stream's second, continuing after the last
                        # successful stream when a chunk is smaller than a round.
                        queue = monthly_queues.setdefault(month,{}).setdefault(channel,[])
                        entry = (-ordinal,-wrapped,-position[0],-position[1],
                                 -source_index,-line_number,key,item)
                        if len(queue) < limit:
                            heapq.heappush(queue,entry)
                        elif entry > queue[0]:
                            heapq.heapreplace(queue,entry)
                    else:
                        rows.append(item)
                        keys.append(key)
                        if len(rows) >= limit:
                            break
            if not self.priority_months and len(rows) >= limit:
                break
        if self.priority_months:
            available = monthly_queues
            for channels in available.values():
                for queue in channels.values():
                    queue.sort(reverse=True)
            archive_counts = self.state['archive_distribution']['by_month']
            selected = {}
            while len(rows) < limit:
                months = [month for month in self.priority_months
                          if any(available.get(month,{}).values())]
                if months:
                    month = min(months,key=lambda m:(archive_counts.get(m,0)+selected.get(m,0),m))
                elif self.collected.is_set():
                    others = [month for month,channels in available.items() if any(channels.values())]
                    if not others:
                        break
                    month = min(others)
                else:
                    # The latest-news scheduler handles October. Wait for old
                    # discoveries instead of filling a history chunk with it.
                    break
                cursor = channel_positions.get(month, 0)
                for offset in range(len(channel_cycle)):
                    slot = (cursor + offset) % len(channel_cycle)
                    channel = channel_cycle[slot]
                    if available[month].get(channel):
                        entry = available[month][channel].pop(0)
                        channel_positions[month] = (slot + 1) % len(channel_cycle)
                        stream_positions.setdefault(month,{})[channel] = list(stream_key(entry[-1],channel))
                        break
                rows.append(entry[-1]); keys.append(entry[-2])
                selected[month] = selected.get(month,0)+1
            self._pending_review_channel_positions = channel_positions
            self._pending_review_stream_positions = stream_positions
            with self.lock:
                self.state['review_queue_counts_by_month'] = dict(sorted(pending_counts.items()))
                self.state['review_queue_checked_at'] = now()
                self.state['review_chunk_months'] = selected
                self.state['review_queue_policy'] = 'oil_topics_3_commodity_2_other_1'
                self.state['review_queue_channel_counts'] = channel_counts
                self._save()
        if changed:
            write_json(self.ledger_path, self.ledger)
            with self.lock:
                self.state["existing_record_ids"] = sorted(self.existing_record_ids)
                self.state["existing_records"] = len(self.existing_record_ids)
                self._save()
        return rows, keys

    def _review(self):
        while not self.stop.is_set():
            rows, keys = self._pending(limit=self.state['review_chunk_limit'])
            next_channel_positions = dict(getattr(self, '_pending_review_channel_positions', {}))
            next_stream_positions = {month: dict(channels) for month, channels in
                                     getattr(self, '_pending_review_stream_positions', {}).items()}
            if not rows:
                if self.collected.is_set():
                    return
                self.stop.wait(self.poll_seconds)
                continue
            digest = hashlib.sha256("\n".join(sorted(keys)).encode()).hexdigest()[:24]
            directory = self.output / "review" / ("history-" + digest)
            directory.mkdir(parents=True, exist_ok=True)
            candidate_file = directory / "candidates.jsonl"
            candidate_file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
            report, unavailable_error = None, None
            for attempt in range(3):
                try:
                    completed = directory / "pipeline.json"
                    previous = json.loads(completed.read_text(encoding="utf-8")) if completed.exists() else {}
                    report = previous if previous.get("status") == "complete" else self.processor(candidate_file, self.database, DEFAULT_OUTPUT, self.stop)
                    if not isinstance(report, dict) or report.get("status") != "complete":
                        raise RuntimeError("审核流程未返回完成报告")
                    # Injected processors and ordinary subprocesses share the
                    # same recoverable completed-chunk contract.
                    write_json(completed, report)
                    break
                except Stopped:
                    return
                except Exception as error:
                    self._audit("review-errors", {"chunk": directory.name, "attempt": attempt + 1, "error": str(error), "time": now()})
                    report = None
                    if service_unavailable(error):
                        unavailable_error = str(error)
                        break
                    if attempt < 2 and self.stop.wait(self.retry_delay):
                        return
            if unavailable_error is not None:
                with self.lock:
                    self.state["consecutive_service_failures"] += 1
                    self.state["review_service_retries"] += 1
                    exponent = min(self.state["consecutive_service_failures"] - 1, 12)
                    delay = min(self.service_backoff_base * 2 ** exponent, self.service_backoff_max)
                    self.state.update(review_service_status="waiting", last_review_error=unavailable_error,
                        next_review_retry_at=(datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="seconds"))
                    self._save()
                print("历史审核服务暂不可用，保留待审核队列，{} 秒后重试；采集继续。".format(delay), flush=True)
                if self.stop.wait(delay):
                    return
                continue
            if report is not None:
                with self.lock:
                    self.state.update(review_service_status="ready", last_review_error="",
                                      next_review_retry_at=None, consecutive_service_failures=0)
                    if self.state['review_chunk_limit']<self.state['review_chunk_size']:
                        self.state['review_partition_successes']+=len(rows)
                        if self.state['review_partition_successes']>=self.state['review_chunk_size']:
                            self.state.update(review_chunk_limit=self.state['review_chunk_size'],review_partition_successes=0)
                    if directory.name not in self.state["completed_chunks"]:
                        for field in COUNTERS:
                            self.state["counts"][field] += int(report.get(field, 0))
                        self.state["completed_chunks"].append(directory.name)
                    self.ledger.update({key: "reviewed" for key in keys})
                    write_json(self.ledger_path, self.ledger)
                    if self.priority_months:
                        self.state['review_channel_positions'] = next_channel_positions
                        self.state['review_stream_positions'] = next_stream_positions
                    self._save()
                print("历史审核 {} 条，本块新增 {} 条，累计新增 {} 条".format(len(rows), report.get("created", 0), self.state["counts"]["created"]), flush=True)
            elif len(rows)>1:
                # A malformed review for one candidate must not discard every
                # unrelated candidate in a sixty-record history block. Narrow
                # that block until the actual failing item is isolated.
                with self.lock:
                    if self.state['review_chunk_limit']==self.state['review_chunk_size']:
                        self.state['review_partition_successes']=0
                    self.state['review_chunk_limit']=max(1,len(rows)//2)
                    self._save()
                self._audit('review-splits',{'chunk':directory.name,'candidates':len(rows),
                                            'next_chunk_limit':self.state['review_chunk_limit'],'time':now()})
            else:
                self.ledger.update({key: "review_failed" for key in keys})
                write_json(self.ledger_path, self.ledger)
                with self.lock:
                    self.state.update(review_chunk_limit=self.state['review_chunk_size'],review_partition_successes=0)
                    self._save()

    def _collect_sources(self, identifiers, workers):
        """Round-robin a disjoint source set with bounded collector concurrency."""
        if not identifiers:
            return
        with ThreadPoolExecutor(max_workers=workers) as pool:
            while not self.stop.is_set():
                with self.lock:
                    pending = [key for key in identifiers if not self.state['sources'][key].get('terminal')]
                if not pending:
                    return
                jobs = [pool.submit(self._collect, key) for key in pending]
                try:
                    for job in as_completed(jobs):
                        if not job.cancelled():
                            job.result()
                except BaseException:
                    self.stop.set()
                    for job in jobs:
                        job.cancel()
                    raise

    def _collect_all(self):
        """Keep the oil-history lane advancing while broker rounds continue.

        The sets are disjoint and each lane finishes one source's call before
        rescheduling it.  One plus two collector workers preserves the existing
        three-call limit without making fast media wait for the broker barrier.
        """
        fast = [key for key, spec in self.specs.items()
                if self.priority_months and key in {'cls', 'ths'} and spec.get('group') == 'media']
        if not fast:
            self._collect_sources(list(self.specs), 3)
            return
        ordinary = [key for key in self.specs if key not in fast]
        failures = []

        def fast_lane():
            try:
                self._collect_sources(fast, 1)
            except BaseException as error:
                failures.append(error)
                self.stop.set()

        collector = threading.Thread(target=fast_lane, name='history-priority-collection')
        collector.start()
        try:
            self._collect_sources(ordinary, 2)
        except BaseException:
            self.stop.set()
            raise
        finally:
            # Collected is set by run() only after both lanes have drained.
            # The reviewer must not release deferred months while one lane is
            # still discovering priority-month content.
            collector.join()
        if failures:
            raise failures[0]

    def run(self):
        def review_guard():
            try:
                self._review()
            except BaseException as error:
                with self.lock:
                    self.state["review_worker_error"] = str(error) or type(error).__name__
                    self.state["last_error"] = self.state["review_worker_error"]
                    self._save()
                self.stop.set()
        reviewer = threading.Thread(target=review_guard, name="history-review", daemon=True)
        reviewer.start()
        try:
            self._collect_all()
        finally:
            self.collected.set()
            reviewer.join()
            with self.lock:
                failed = sum(value == "review_failed" for value in self.ledger.values())
                self.state["review_failed_candidates"] = failed
                self.state["skipped_dates"] = sum(value in {"unknown_date", "outside_window"} for value in self.ledger.values())
                self.state["invalid_candidates"] = sum(value == "invalid" for value in self.ledger.values())
                self.state["existing_record_ids"] = sorted(self.existing_record_ids)
                self.state["existing_records"] = len(self.existing_record_ids)
                all_complete = all(value.get("status") == "complete" for value in self.state["sources"].values())
                unknown = sum(value == "unknown_date" for value in self.ledger.values())
                archive_counts = self.state.get('archive_distribution',{}).get('by_month',{})
                target_unmet = [month for month in self.priority_months
                                if self.monthly_target and archive_counts.get(month,0) < self.monthly_target]
                self.state['target_unmet_months'] = target_unmet
                self.state["status"] = ("failed" if self.state.get("review_worker_error") else
                    "interrupted" if self.stop.is_set() else
                    "complete" if all_complete and not failed and not unknown and not self.state["invalid_candidates"] and not target_unmet else "partial")
                self.state["finished_at"] = now()
                self._save()
        return self.state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", default=datetime.now(TZ).date().isoformat())
    parser.add_argument("--output", type=Path, default=ROOT / "data/history-2026")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--sources", help="逗号分隔来源 ID；默认所有已接入来源")
    parser.add_argument("--retry-reviews", action="store_true")
    parser.add_argument("--retry-sources", action="store_true")
    parser.add_argument('--priority-months',help='优先补采、审核的 YYYY-MM 月份，以逗号分隔；未指定时沿用断点设置')
    parser.add_argument('--monthly-target',type=int,help='各优先月份的正式入库覆盖目标，只统计真实审核收录记录')
    parser.add_argument('--review-workers',type=int,choices=range(1,25),help='Codex 审核并发数；默认沿用断点或 1')
    parser.add_argument('--reference-month',help='以该月当前正式新闻数向上取整至百条，动态提高每月覆盖目标')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.exit(1, "同一历史任务目录已有进程运行，未启动重复补采。\n")
        sources = enabled_sources()
        if args.sources:
            selected = [key.strip() for key in args.sources.split(",") if key.strip()]
            if any(key not in sources for key in selected):
                parser.error("仅能选择已接入信源")
            sources = {key: sources[key] for key in selected}
        runner = Backfill(args.start_date, args.end_date, args.output, args.database, sources,
                          retry_reviews=args.retry_reviews, retry_sources=args.retry_sources,
                          priority_months=[m.strip() for m in args.priority_months.split(',') if m.strip()]
                          if args.priority_months is not None else None,
                          monthly_target=args.monthly_target,review_workers=args.review_workers,
                          reference_month=args.reference_month)
        def stop(signum, frame):
            runner.stop.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        result = runner.run()
        print(json.dumps({"status": result["status"], "counts": result["counts"], "manifest": str(runner.path)}, ensure_ascii=False), flush=True)
        if result["status"] == "failed":
            parser.exit(1)


if __name__ == "__main__":
    main()
