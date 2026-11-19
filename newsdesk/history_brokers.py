"""Resume public, attributed broker-summary discovery by publication date.

Sina's institution search is a recent window, while its public date search can
reach January 2026.  Date-list responses are shared by every broker in the same
output directory; report authors are checked again on each public detail page.
Collected candidates are unreviewed.  This module never classifies or archives.
"""

import argparse
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from newsdesk.domestic_brokers_a import SOURCES as A_SOURCES
from newsdesk.domestic_brokers_b import BASE, LIST_PATH, SOURCES as B_SOURCES, list_url, parse_detail, parse_list


SOURCES = {**A_SOURCES, **B_SOURCES}
MAX_BYTES = 4 * 1024 * 1024
_FETCH_LOCK = threading.Lock()
_LAST_FETCH = 0.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def atomic_candidates(path, candidates):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = output.name
            for candidate in candidates.values():
                output.write(json.dumps(candidate, ensure_ascii=False, allow_nan=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class Fetcher:
    """One public host, modest rate, exact responses and resumable URL cache."""

    def __init__(self, output_dir, *, timeout=15, delay=.35, refresh=False):
        self.directory = Path(output_dir) / "raw" / "sina-history"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout, self.delay, self.refresh = timeout, delay, refresh

    def get(self, url):
        global _LAST_FETCH
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "stock.finance.sina.com.cn":
            raise ValueError("历史券商采集只允许已配置的新浪公开研报域名")
        url_key = hashlib.sha256(url.encode()).hexdigest()
        index = self.directory / (url_key + "-cache.json")
        if index.exists() and not self.refresh:
            indexed = json.loads(index.read_text(encoding="utf-8"))
            cached_path = indexed.get("raw_path") if indexed.get("valid") else None
            cached = json.loads(Path(cached_path).read_text(encoding="utf-8")) if cached_path else None
        else:
            cached = None
        if cached:
            if cached.get("request_url") != url:
                raise ValueError("历史响应缓存 URL 不匹配")
            return cached["raw_response"], Path(cached_path).resolve(), cached["collected_at"]
        with _FETCH_LOCK:
            delay = self.delay - (time.monotonic() - _LAST_FETCH)
            if delay > 0:
                time.sleep(delay)
            _LAST_FETCH = time.monotonic()
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; OilNewsResearch/1.0; local research)",
                                        "Accept": "text/html", "Referer": BASE + LIST_PATH})
        with urlopen(request, timeout=self.timeout) as response:
            if urlsplit(response.url).hostname != parsed.hostname:
                raise ValueError("历史公开研报出现非预期跨域跳转")
            raw = response.read(MAX_BYTES + 1)
            final_url = response.url
            content_type = response.headers.get("Content-Type", "")
        if len(raw) > MAX_BYTES:
            raise ValueError("历史公开研报响应超过 4 MiB")
        text = raw.decode("gb18030", errors="strict")
        collected = now_iso()
        path = self.directory / (url_key + "-" + hashlib.sha256(raw).hexdigest() + "-" + str(time.time_ns()) + ".json")
        atomic_json(path, {"request_url": url, "response_url": final_url, "collected_at": collected,
                           "content_type": content_type, "encoding": "gb18030",
                           "sha256": hashlib.sha256(raw).hexdigest(), "raw_response": text})
        atomic_json(index, {"valid": True, "request_url": url, "raw_path": str(path.resolve())})
        return text, path.resolve(), collected

    def invalidate(self, url):
        index = self.directory / (hashlib.sha256(url.encode()).hexdigest() + "-cache.json")
        # Preserve the exact failed response; a subsequent run can retry rather
        # than using a transient HTTP-200 challenge as a permanent empty page.
        if index.exists():
            record = json.loads(index.read_text(encoding="utf-8"))
            record["valid"] = False
            atomic_json(index, record)


def parse_date_list(text, requested_date):
    """Reject ignored date queries and HTTP-200 block/login pages as incomplete."""
    rows = parse_list(text)
    if not rows:
        if "没有找到相关内容" not in text:
            raise ValueError("日期列表没有有效数据或明确的空结果标识")
        return [], 1
    if any(row["published_date"] != requested_date for row in rows):
        raise ValueError("日期查询返回其他日期，不能据此声明历史覆盖完整")
    pages = [int(value) for value in re.findall(r"set_page_num\(['\"](\d+)['\"]\)", text)]
    return rows, max(pages or [1])


def _load_candidates(path):
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").split('\n') if line.strip()]
    return {row["candidate_id"]: row for row in rows}


def _positive_limit(value, name):
    if value is not None and (type(value) is not int or value < 1):
        raise ValueError(name + " 必须为空或正整数")


def _calendar_order(first, last, priority_months):
    """Interleave days of target months without changing the requested scope."""
    if isinstance(priority_months, str):
        raise ValueError("priority_months 必须为 YYYY-MM 月份序列")
    months = list(dict.fromkeys(priority_months or ()))
    for month in months:
        if not isinstance(month, str) or not re.fullmatch(r"\d{4}-\d{2}", month):
            raise ValueError("priority_months 必须为 YYYY-MM 月份序列")
        date.fromisoformat(month + "-01")
    positions = {month: index for index, month in enumerate(months)}
    days = [(first + timedelta(days=offset)).isoformat() for offset in range((last - first).days + 1)]
    if months:
        days.sort(key=lambda day: (0, int(day[-2:]), positions[day[:7]]) if day[:7] in positions else (1, day, 0))
    return days, months


def run_source(source_id, start_date, end_date, output_dir, *, max_days=None, max_pages=None,
               max_candidates=None, resume=True, fetcher=None, metadata_only=False, priority_months=None):
    """Walk every public date page in a range for one explicitly named broker.

    There are no implicit per-source sample limits. Explicit safety limits leave
    coverage.complete=False. Resume retries partial days, uses saved responses
    and skips already captured IDs; it does not mark unfinished work as done.
    Never-attempted dates run before failed dates, so a blocked January prefix
    cannot starve the rest of a bounded, round-robin annual discovery job.
    max_pages limits each publication day's pages; max_days bounds the number
    of dates attempted in this call. Use both for a bounded discovery probe.
    priority_months interleaves the first day of each target month, then the
    second day, etc.; it can be changed when resuming the same date scope.
    Completeness only describes the publisher's public Sina-distributed corpus,
    not access to a broker's private portal or all original institutional work.
    """
    if source_id not in SOURCES:
        raise ValueError("未知历史券商来源：" + source_id)
    first, last = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if first > last:
        raise ValueError("开始日期晚于结束日期")
    if last > datetime.now(timezone(timedelta(hours=8))).date():
        raise ValueError("结束日期不能晚于当前日期")
    for value, name in [(max_days, "max_days"), (max_pages, "max_pages"), (max_candidates, "max_candidates")]:
        _positive_limit(value, name)
    requested_dates, priority_months = _calendar_order(first, last, priority_months)
    spec = SOURCES[source_id]
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    range_key = first.isoformat() + "_" + last.isoformat()
    destination = output / (source_id + "-" + range_key + "-candidates.jsonl")
    discovery_path = output / (source_id + "-" + range_key + "-discoveries.jsonl")
    report_path = output / (source_id + "-" + range_key + ("-metadata" if metadata_only else "") + "-coverage.json")
    candidates = _load_candidates(destination) if resume else {}
    discoveries = _load_candidates(discovery_path) if resume else {}
    old = json.loads(report_path.read_text(encoding="utf-8")) if resume and report_path.exists() else {}
    days = old.get("days", {})
    report = {"source_id": source_id, "source_name": spec["name"], "started_at": old.get("started_at") or now_iso(),
              "start_date": first.isoformat(), "end_date": last.isoformat(), "collection_platform": "新浪财经研报",
              "scope": "按实际发布日期遍历新浪公开分发、明确署名匹配的该机构研究摘要；不代表机构私有门户的全部研究",
              "classification_status": "listing_metadata_only" if metadata_only else "unreviewed",
              "candidates_path": None if metadata_only else str(destination), "discoveries_path": str(discovery_path),
              "metadata_only": metadata_only, "report_path": str(report_path),
              "publisher_names": spec["publisher_names"], "days": days, "errors": [], "stop_reason": ""}
    report["limits"] = {"max_days": max_days, "max_pages_per_day": max_pages, "max_candidates": max_candidates}
    report["priority_months"] = priority_months
    fetcher = fetcher or Fetcher(output)
    attempted_days = 0

    def save():
        dates = sorted(row["published_date"] for row in (discoveries if metadata_only else candidates).values() if row.get("published_date"))
        expected = (last - first).days + 1
        complete_days = sum(day.get("status") == "complete" for day in days.values())
        discovery_complete = complete_days == expected
        report.update(candidate_count=0 if metadata_only else len(candidates), discovery_count=len(discoveries), checked_at=now_iso(),
                      completed_days=complete_days, attempted_days=len(days),
                      attempts_total=sum(day.get("attempt", 1) for day in days.values()), requested_days=expected,
                      coverage={"complete": discovery_complete and not metadata_only,
                                "discovery_complete": discovery_complete,
                                "earliest_publication_date": dates[0] if dates else "",
                                "latest_publication_date": dates[-1] if dates else ""})
        report["status"] = "discovered" if metadata_only and discovery_complete else "complete" if report["coverage"]["complete"] else "partial" if candidates or complete_days else "incomplete"
        if not metadata_only:
            atomic_candidates(destination, candidates)
        atomic_candidates(discovery_path, discoveries)
        atomic_json(report_path, report)

    pending_dates = ([day for day in requested_dates if day not in days]
                     + [day for day in requested_dates if day in days and days[day].get("status") != "complete"])
    if priority_months:
        # Stable partition preserves interleaved dates and the unseen-before-
        # retry rule within each group, while keeping targets ahead of October.
        pending_dates.sort(key=lambda day: day[:7] not in priority_months)
    for day in pending_dates:
        if max_days is not None and attempted_days >= max_days:
            report["stop_reason"] = "max_days"
            break
        if max_candidates is not None and len(discoveries if metadata_only else candidates) >= max_candidates:
            report["stop_reason"] = "max_candidates"
            break
        attempted_days += 1
        previous = days.get(day)
        previous_attempts = list(previous.get("previous_attempts", [])) if previous else []
        if previous:
            # Keep the old response URLs and failure reasons without nesting
            # the complete audit history inside each successive snapshot.
            previous_attempts.append({key: value for key, value in previous.items() if key != "previous_attempts"})
        state = {"status": "incomplete", "pages": 0, "list_rows": 0, "matching_rows": 0, "errors": [],
                 "started_at": now_iso(), "attempt": len(previous_attempts) + 1,
                 "previous_attempts": previous_attempts}
        days[day] = state
        page, total_pages = 1, 1
        stop_all = False
        while page <= total_pages:
            if max_pages is not None and page > max_pages:
                state["stop_reason"] = report["stop_reason"] = "max_pages"
                break
            url = list_url(t1="6", pubdate=day, p=page)
            try:
                text, list_path, _ = fetcher.get(url)
                rows, observed_pages = parse_date_list(text, day)
                total_pages = max(total_pages, observed_pages)
                state["pages"] = page
                state["list_rows"] += len(rows)
                for row in rows:
                    if row["publisher_name"] not in spec["publisher_names"]:
                        continue
                    state["matching_rows"] += 1
                    identifier = source_id + ":sina:" + row["external_id"]
                    if identifier in (discoveries if metadata_only else candidates):
                        continue
                    if max_candidates is not None and len(discoveries if metadata_only else candidates) >= max_candidates:
                        state["stop_reason"] = report["stop_reason"] = "max_candidates"
                        stop_all = True
                        break
                    discoveries[identifier] = dict(row, candidate_id=identifier, source_id=source_id,
                        source_name=spec["name"], external_id="sina:" + row["external_id"],
                        discovery_raw_path=str(Path(list_path).resolve()), source_listing_url=url,
                        collection_platform="新浪财经研报", content_scope="listing_metadata_only",
                        publisher_verification="listing_only", requires_detail=True)
                    if metadata_only:
                        continue
                    try:
                        detail, raw_path, collected = fetcher.get(row["source_url"])
                        parsed = parse_detail(detail, spec["publisher_names"])
                        if parsed["published_date"] and parsed["published_date"] != day:
                            raise ValueError("详情与日期列表的首发日期不一致")
                        # A date printed in the source list remains a source
                        # date; no clock time or timezone is fabricated.
                        parsed["published_date"] = parsed["published_date"] or day
                        parsed["published_at_precision"] = "date"
                        candidates[identifier] = dict(parsed, candidate_id=identifier, source_id=source_id,
                            source_name=spec["name"], external_id="sina:" + row["external_id"], source_url=row["source_url"],
                            collected_at=collected, raw_path=str(Path(raw_path).resolve()),
                            discovery_raw_path=str(Path(list_path).resolve()), source_listing_url=url,
                            collection_platform="新浪财经研报", content_scope="public_research_summary",
                            discovery="按实际发布日期回溯公开机构研报")
                    except (OSError, ValueError, UnicodeError) as error:
                        if isinstance(fetcher, Fetcher):
                            fetcher.invalidate(row["source_url"])
                        state["errors"].append({"url": row["source_url"], "error": type(error).__name__ + ": " + str(error)[:250]})
                save()
                if stop_all:
                    break
                if page >= total_pages:
                    state["status"] = "partial" if state["errors"] else "complete"
                page += 1
            except (OSError, ValueError, UnicodeError) as error:
                if isinstance(fetcher, Fetcher):
                    fetcher.invalidate(url)
                state["errors"].append({"url": url, "error": type(error).__name__ + ": " + str(error)[:250]})
                state["status"] = "partial" if state["pages"] else "incomplete"
                break
        state["finished_at"] = now_iso()
        save()
        if stop_all:
            break
    report["errors"] = [dict(error, date=day) for day, state in days.items() for error in state.get("errors", [])]
    save()
    return report


def fetch_selected(source_id, discoveries_path, selected_ids, output_dir, *, fetcher=None):
    """Fetch final public text for Agent-shortlisted/uncertain metadata records.

    A metadata Agent rejection or acceptance never enters the archive here.
    Successful bodies must still go through the normal per-item review pipeline.
    """
    if source_id not in SOURCES:
        raise ValueError("未知历史券商来源：" + source_id)
    spec = SOURCES[source_id]
    discoveries = _load_candidates(Path(discoveries_path))
    selected_ids = list(dict.fromkeys(selected_ids))
    if any(identifier not in discoveries or discoveries[identifier].get("source_id") != source_id for identifier in selected_ids):
        raise ValueError("待抓正文 ID 必须来自该机构已保存的公开发现列表")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    destination = output / (source_id + "-selected-candidates.jsonl")
    candidates = _load_candidates(destination)
    fetcher = fetcher or Fetcher(output)
    errors = []
    for identifier in selected_ids:
        if identifier in candidates:
            continue
        row = discoveries[identifier]
        try:
            if row.get("publisher_name") not in spec["publisher_names"]:
                raise ValueError("发现列表署名与该机构不匹配")
            detail, raw_path, collected = fetcher.get(row["source_url"])
            parsed = parse_detail(detail, spec["publisher_names"])
            if parsed["published_date"] and parsed["published_date"] != row["published_date"]:
                raise ValueError("详情与发现列表首发日期不一致")
            parsed["published_date"] = parsed["published_date"] or row["published_date"]
            parsed["published_at_precision"] = "date"
            candidates[identifier] = dict(parsed, candidate_id=identifier, source_id=source_id,
                source_name=spec["name"], external_id=row["external_id"], source_url=row["source_url"],
                collected_at=collected, raw_path=str(Path(raw_path).resolve()),
                discovery_raw_path=row["discovery_raw_path"], source_listing_url=row["source_listing_url"],
                collection_platform="新浪财经研报", content_scope="public_research_summary",
                discovery="Agent 对公开标题元数据初筛后抓取全文，仍待正文审核")
        except (OSError, ValueError, UnicodeError) as error:
            if isinstance(fetcher, Fetcher):
                fetcher.invalidate(row["source_url"])
            errors.append({"candidate_id": identifier, "url": row["source_url"], "error": type(error).__name__ + ": " + str(error)[:250]})
        atomic_candidates(destination, candidates)
    atomic_candidates(destination, candidates)
    report = {"source_id": source_id, "source_name": spec["name"], "status": "partial" if errors else "complete",
              "classification_status": "unreviewed", "candidate_count": len(candidates),
              "selected_count": len(selected_ids), "candidates_path": str(destination), "errors": errors,
              "coverage": {"complete": False, "scope": "Agent初筛选择的发现记录，非该机构全年全部公开研究"},
              "completed_at": now_iso()}
    atomic_json(output / (source_id + "-selected-report.json"), report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_id", choices=sorted(SOURCES))
    parser.add_argument("--start-date", default="2026-01-01")
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-days", type=int)
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--max-candidates", type=int)
    parser.add_argument("--metadata-only", action="store_true")
    args = parser.parse_args()
    result = run_source(args.source_id, args.start_date, args.end_date, args.output,
                        max_days=args.max_days, max_pages=args.max_pages, max_candidates=args.max_candidates,
                        metadata_only=args.metadata_only)
    print(json.dumps({key: value for key, value in result.items() if key != "days"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
