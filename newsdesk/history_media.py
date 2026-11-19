"""Resumable public media history, with explicit limits on coverage.

This module only collects candidates. Every candidate must still go through the
existing Codex CLI reviewer before it can enter the news archive. A complete
result refers to the configured public channel, never the publisher's whole site.
"""

import hashlib
import json
import re
import time
import uuid
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from newsdesk.ingest import has_text_content, write_json
from newsdesk.news_sources import SOURCES as FLASH_SOURCES, ThsArticleText, candidate, parse_response, text_only
from newsdesk.additional_sources import parse_article as parse_additional_article, parse_links as additional_links

TZ = ZoneInfo("Asia/Shanghai")
SOURCES = {**FLASH_SOURCES,
           "aastocks": {"name": "阿斯达克研报", "page": "https://www.aastocks.com/sc/stocks/news/aafn"},
           "caixin": {"name": "财新", "page": "https://www.caixin.com/energy/"}}
CAIXIN_FEED = "https://gateway.caixin.com/api/extapi/homeInterface.jsp"
THS_CHANNEL = "https://futures.10jqka.com.cn/gzqhyw_list/"
AA_TERMS = ("原油", "石油", "OPEC", "WTI", "Brent", "炼油", "成品油", "战略储备", "油轮",
            "霍尔木兹", "石油制裁", "美联储", "美元", "利率", "需求", "飓风")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class HistoryFetcher:
    """Bounded ordinary public GETs; no cookies, login or subscriber endpoints."""
    def __init__(self, output_dir):
        self.directory = Path(output_dir) / "raw"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last_request = 0

    def get(self, source_id, url):
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = {
            "cls": {"www.cls.cn"}, "jin10": {"flash-api.jin10.com"},
            "jiemian": {"papi.jiemian.com"}, "stcn": {"www.stcn.com"},
            "caixin": {"www.caixin.com", "gateway.caixin.com"},
            "aastocks": {"www.aastocks.com"},
        }
        accepted = host.endswith(".10jqka.com.cn") if source_id == "ths" else host in allowed[source_id]
        if parsed.scheme != "https" or not accepted:
            raise ValueError("历史采集链接不属于已配置公开信源")
        delay = .4 - (time.monotonic() - self.last_request)
        if delay > 0:
            time.sleep(delay)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; OilNewsResearch/1.0)",
                   "Accept": "application/json,text/html,*/*", "Referer": SOURCES[source_id]["page"],
                   "X-Requested-With": "XMLHttpRequest"}
        if source_id == "jin10":
            headers.update({"x-app-id": "bVBF4FyRTn5NJF5n", "x-version": "1.0.0"})
        self.last_request = time.monotonic()
        with urlopen(Request(url, headers=headers), timeout=18) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError("历史响应超过 4 MiB")
            final_host = urlsplit(response.url).hostname or ""
            same_publisher_redirect = source_id == "ths" and final_host.endswith(".10jqka.com.cn")
            if urlsplit(response.url).scheme != "https" or (final_host != host and not same_publisher_redirect):
                raise ValueError("公开历史入口跳转到不同主机，未继续")
            encoding = response.headers.get_content_charset() or ("gbk" if source_id == "ths" else "utf-8")
            final_url = response.url
        text = raw.decode(encoding, errors="replace")
        stamp = now()
        path = self.directory / (str(time.time_ns()) + "-" + uuid.uuid4().hex[:6] + ".json")
        write_json(path, {"request_url": url, "response_url": final_url, "collected_at": stamp,
                          "encoding": encoding, "sha256": hashlib.sha256(raw).hexdigest(), "raw_response": text})
        return text, path, stamp


def publication_day(row):
    value = row.get("published_at") or row.get("published_date")
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(TZ).date() if parsed.tzinfo else parsed.date()
    except (ValueError, TypeError):
        return None


def caixin_rows(raw, raw_path, collected_at):
    """Read only top-level public channel summaries, excluding related-story ads."""
    payload = json.loads(raw)
    rows = payload.get("datas")
    if not isinstance(rows, list):
        raise ValueError("财新公开栏目没有 datas 列表")
    result = []
    for row in rows:
        url = row.get("link", "")
        parsed = urlsplit(url)
        if parsed.hostname != "www.caixin.com" or not re.fullmatch(r"/\d{4}-\d{2}-\d{2}/\d+\.html", parsed.path):
            continue
        try:
            published = datetime.strptime(row.get("time", ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).isoformat()
        except (TypeError, ValueError):
            published = ""
        external = str(row.get("nid") or parsed.path.rsplit("/", 1)[-1].split(".")[0])
        result.append({"candidate_id": "caixin:" + external, "source_id": "caixin", "source_name": "财新",
                       "external_id": external, "source_url": url, "title": text_only(row.get("desc")),
                       "body": text_only(row.get("summ")), "published_at": published,
                       "collected_at": collected_at, "raw_path": str(Path(raw_path).resolve()),
                       "content_scope": "public_article_lead", "title_origin": "source",
                       "collection_platform": "财新能源栏目公开摘要", "source_listing_url": "https://www.caixin.com/energy/"})
    return rows, result


def ths_rows(raw, listing_url, raw_path, collected_at):
    title = re.search(r"<title[^>]*>(.*?)</title>", raw, re.S | re.I)
    if not title or "股指期货要闻" not in text_only(title.group(1)) or "view_otherday" not in raw:
        raise ValueError("同花顺日期页面未返回可核验的公开栏目，不能视为空新闻日")
    result = []
    for block in re.findall(r"<li\b[^>]*>(.*?)</li>", raw, re.S | re.I):
        if not re.search(r'class=["\'][^"\']*arc-title', block):
            continue
        match = re.search(r'<a\b([^>]+)>(.*?)</a>', block, re.S | re.I)
        if not match:
            continue
        href = re.search(r'href=["\']([^"\']+)', match.group(1))
        if not href:
            continue
        url = urljoin(listing_url, href.group(1)).replace("http://", "https://", 1)
        parts = urlsplit(url)
        identity = re.fullmatch(r"/(\d{8})/c(\d+)\.shtml", parts.path)
        if not (parts.hostname or "").endswith(".10jqka.com.cn") or not identity:
            continue
        summary = re.search(r'<a\b[^>]*class=["\'][^"\']*arc-cont[^"\']*["\'][^>]*>(.*?)</a>', block, re.S | re.I)
        title = text_only(match.group(2))
        published = ""
        clock = re.search(r"(\d{2})月(\d{2})日\s+(\d{2}:\d{2})", block)
        if clock:
            stamp = identity.group(1)[:4] + "-" + clock.group(1) + "-" + clock.group(2) + " " + clock.group(3)
            published = datetime.strptime(stamp, "%Y-%m-%d %H:%M").replace(tzinfo=TZ).isoformat()
        external = identity.group(2)
        result.append({"candidate_id": "ths:" + external, "source_id": "ths", "source_name": "同花顺",
                       "external_id": external, "source_url": url, "title": title,
                       "body": text_only(summary.group(1)) if summary else "", "published_at": published,
                       "collected_at": collected_at, "raw_path": str(Path(raw_path).resolve()),
                       "discovery_raw_path": str(Path(raw_path).resolve()),
                       "source_listing_url": listing_url,
                       "content_scope": "public_article_excerpt", "title_origin": "source"})
    following = re.search(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*class=["\'][^"\']*\bnext\b[^"\']*["\']', raw, re.I)
    next_url = urljoin(listing_url, following.group(1)).replace("http://", "https://", 1) if following else None
    if next_url and (urlsplit(next_url).hostname != "futures.10jqka.com.cn" or "/gzqhyw_list/" not in urlsplit(next_url).path):
        raise ValueError("同花顺历史分页跳出了已配置栏目")
    return result, next_url


def enrich_ths(row, fetcher):
    text, raw_path, collected = fetcher.get("ths", row["source_url"])
    parser = ThsArticleText()
    parser.feed(text)
    body = re.sub(r"\n[ \t]*\n+", "\n", "".join(parser.parts)).strip()
    if not has_text_content(body):
        raise ValueError("同花顺公开详情没有可用正文")
    result = dict(row, body=body[:20000], raw_path=str(Path(raw_path).resolve()), collected_at=collected,
                  content_scope="public_article")
    precise = re.search(r">(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})<", text)
    if precise:
        result["published_at"] = datetime.strptime(precise.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).isoformat()
    return result


def aa_search_rows(raw, url, path, collected):
    """Use the public search result container, never sidebar/related links."""
    content = re.search(r'<div\b[^>]*id=["\']aafn-search-c1["\'][^>]*>(.*?)<div\b[^>]*id=["\']checker_content_end', raw, re.S | re.I)
    if not content:
        raise ValueError("AAStocks公开检索未返回可核验的结果区")
    params = None
    for value in re.findall(r"\bvar\s+param\s*=\s*(\{[^{};]*\})\s*;", raw):
        try:
            decoded = json.loads(value)
            if "cat" in decoded and "newstime" in decoded and "newsid" in decoded:
                params = decoded
        except ValueError:
            continue
    if params is None:
        raise ValueError("AAStocks公开页面未声明可继续使用的检索游标")
    rows = []
    for link in additional_links(content.group(1).encode("utf-8"), "aastocks", url):
        external = re.search(r"/aafn-con/([^/]+)/", link).group(1)
        rows.append({"candidate_id": "aastocks:" + external, "source_id": "aastocks", "source_name": "阿斯达克研报",
                     "external_id": external, "source_url": link, "discovery_raw_path": str(Path(path).resolve()),
                     "collected_at": collected})
    return rows, params


def aa_more_rows(raw, path, collected):
    values = json.loads(raw)
    if values == "1" or values == "" or type(values) is int and values == 1:
        return [], None
    if values == "2" or type(values) is int and values == 2:
        raise ValueError("AAStocks公开检索分页返回 Datafeed System Error，不能作为空页结束")
    if not isinstance(values, list):
        raise ValueError("AAStocks公开检索分页没有返回列表")
    rows = []
    cursor = None
    for value in values:
        external = str(value.get("id", ""))
        source = str(value.get("s", ""))
        if not re.fullmatch(r"NOW\.\d+|IC\d+", external) or source not in {"AAFN", "INFOCAST"}:
            continue
        link = "https://www.aastocks.com/sc/stocks/news/aafn-con/" + external + "/top-news/" + source
        rows.append({"candidate_id": "aastocks:" + external, "source_id": "aastocks", "source_name": "阿斯达克研报",
                     "external_id": external, "source_url": link, "discovery_raw_path": str(Path(path).resolve()),
                     "collected_at": collected})
        if value.get("dtd") is not None:
            cursor = {"newsid": external, "newstime": value["dtd"]}
    return rows, cursor


def _flash_url(source_id, state):
    cursor = state["cursor"]
    if source_id == "cls":
        return "https://www.cls.cn/api/cache?" + urlencode({"rn": 20, "lastTime": cursor, "name": "telegraph"})
    if source_id == "jin10":
        value = datetime.fromtimestamp(cursor, TZ).strftime("%Y-%m-%d %H:%M:%S")
        return "https://flash-api.jin10.com/get_flash_list?" + urlencode({"channel": "-8200", "max_time": value})
    if source_id == "jiemian":
        # The timestamp cursor already moves to the previous batch. Increasing
        # page as well skips another offset within that older result window.
        return "https://papi.jiemian.com/page/api/kuaixun/getlistmore?" + urlencode({"cid": "1323kb", "start_time": cursor, "page": 1, "tagid": "1323"})
    return "https://www.stcn.com/article/list.html?" + urlencode({"type": "kx", **state.get("provider_cursor", {})})


def _persist(path, state):
    write_json(path, state)


def _read_jsonl(path):
    """Recover an interrupted final append without hiding interior corruption."""
    path = Path(path)
    if not path.exists():
        return []
    content = path.read_text(encoding="utf-8")
    # JSON strings may legally contain U+2028/U+2029/NEL. str.splitlines()
    # treats them as separators and would manufacture corrupt JSON records.
    # Preserve physical LF endings for the interrupted-append recovery below.
    parts = content.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    result = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            result.append(json.loads(line))
        except ValueError:
            if index != len(lines) - 1 or line.endswith("\n"):
                raise
            # Candidate append precedes checkpoint advancement. Keeping the old
            # pending item lets the next run safely fetch/write that item again.
            with path.with_suffix(path.suffix + ".partial-tail.txt").open("a", encoding="utf-8") as audit:
                audit.write(line + "\n")
            path.write_text("".join(lines[:index]), encoding="utf-8")
            break
    if path.stat().st_size and not path.read_bytes().endswith(b"\n"):
        with path.open("a", encoding="utf-8") as output:
            output.write("\n")
    return result


def _calendar_order(first, last, priority_months):
    """Interleave days of target months while retaining the original scope."""
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


def run_source(source_id, start_date, end_date, output_dir, *, max_pages=50, max_articles=None,
               resume=True, fetch_articles=True, fetcher=None, discovery_terms=None, priority_months=None):
    """Advance a durable source checkpoint; never interpret a work cap as coverage.

    All output stays under ``output_dir``. ``terminal`` distinguishes a public
    historical limit from a resumable per-call page/article cap. Candidates are
    accumulated in candidates.jsonl and are stable across retries.
    For THS, priority_months interleaves dates of the requested months and keeps
    every unfinished date's page cursor when changing an existing checkpoint.
    """
    if source_id not in SOURCES:
        raise ValueError("未知媒体历史信源")
    start, end = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if start > end or end > datetime.now(TZ).date():
        raise ValueError("历史日期范围无效或包含未来日期")
    if type(max_pages) is not int or max_pages < 1 or (max_articles is not None and (type(max_articles) is not int or max_articles < 1)):
        raise ValueError("max_pages/max_articles 必须是正整数")
    calendar_dates, priority_months = _calendar_order(start, end, priority_months)
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "checkpoint.json"
    destination = output / "candidates.jsonl"
    scope = {"source_id": source_id, "requested_start": str(start), "requested_end": str(end)}
    if checkpoint.exists() and resume:
        state = json.loads(checkpoint.read_text(encoding="utf-8"))
        if any(state.get(key) != value for key, value in scope.items()):
            raise ValueError("历史断点属于不同来源或日期范围，请使用不同输出目录")
    else:
        if destination.exists() and destination.stat().st_size:
            raise ValueError("非空历史输出目录不能无断点重新使用")
        end_cursor = int(datetime.combine(end + timedelta(days=1), day_time(), TZ).timestamp())
        state = {"version": 1, **scope, "started_at": now(), "pages": 0, "offset": 0,
                 "cursor": min(int(time.time()), end_cursor), "day": str(start), "next_url": None,
                 "pending": [], "errors": [], "invalid_count": 0, "terminal": False,
                 "finished": False, "stop_reason": "", "observed_earliest": None, "observed_latest": None,
                 "failed_attempts": {}, "page_fingerprints": []}
        if source_id == "aastocks":
            state.update(aa_terms=list(discovery_terms or AA_TERMS), aa_term_index=0, aa_cursor=None,
                         aa_query_pages=0, truncated_queries=[], discovery_only=True)
        destination.touch(exist_ok=True)
        _persist(checkpoint, state)
    fetcher = fetcher or HistoryFetcher(output)
    state.setdefault("processed_dates", {})
    state.setdefault("attempted_dates", [])
    state.setdefault("processed_article_count", 0)
    state.setdefault("date_cursors", {})
    state.setdefault("date_errors", {})
    calendar_scheduling = source_id == "ths" and bool(priority_months or state.get("calendar_scheduling"))
    if calendar_scheduling:
        state["calendar_scheduling"] = True
        # Migrate the previous single-day pagination cursor before selecting a
        # different month. Pending articles are drained before any new page.
        if state.get("next_url") and state["day"] not in state["processed_dates"]:
            state["date_cursors"][state["day"]] = state["next_url"]
        if state.get("last_error"):
            failed_day = re.search(r"/gzqhyw_list/(\d{8})/", state["last_error"].get("url", ""))
            failed_day = datetime.strptime(failed_day[1], "%Y%m%d").date().isoformat() if failed_day else None
            if failed_day and failed_day not in state["processed_dates"]:
                state["date_errors"][failed_day] = state["last_error"]
        state["priority_months"] = priority_months
    if source_id == "aastocks" and "aa_terms" not in state:
        state.update(aa_terms=list(discovery_terms or AA_TERMS), aa_term_index=0, aa_cursor=None,
                     aa_query_pages=0, truncated_queries=[], discovery_only=True, terminal=False,
                     finished=False, stop_reason="")
        _persist(checkpoint, state)
    saved = _read_jsonl(destination)
    seen = {row["candidate_id"] for row in saved}
    pages_this_call = articles_this_call = processed_this_call = 0
    invalid_path = output / "invalid-candidates.jsonl"
    existing_invalid = _read_jsonl(invalid_path)
    invalid_ids = {entry["candidate"]["candidate_id"] for entry in existing_invalid}
    content_gap_dates = {str(publication_day(entry['candidate'])) for entry in existing_invalid
                         if source_id == 'ths' and publication_day(entry['candidate'])
                         and not has_text_content(entry['candidate'].get('body'))}

    def choose_ths_date():
        remaining = [day for day in calendar_dates if day not in state["processed_dates"]]
        # A failing date or a long multipage date cannot consume every bounded
        # call before another target month receives even its first public page.
        remaining.sort(key=lambda day: (day[:7] not in priority_months,
                                         day in state["attempted_dates"]))
        if not remaining:
            state.update(day=str(end + timedelta(days=1)), next_url=None,
                         terminal=True, finished=True, stop_reason="已遍历请求范围内公开日期栏目")
            return False
        state["day"] = remaining[0]
        state["next_url"] = state["date_cursors"].get(state["day"])
        return True

    def keep(row):
        nonlocal articles_this_call
        day = publication_day(row)
        if day:
            value = str(day)
            state["observed_earliest"] = min(state["observed_earliest"] or value, value)
            state["observed_latest"] = max(state["observed_latest"] or value, value)
        if row["candidate_id"] in seen or (day and not start <= day <= end):
            return
        if not day or not row.get("title", "").strip() or not has_text_content(row.get("body")):
            if row["candidate_id"] not in invalid_ids:
                with invalid_path.open("a", encoding="utf-8") as target:
                    target.write(json.dumps({"reason": "首发日期无法核验" if not day else "缺少公开标题或有效正文，未用标题代替正文", "candidate": row}, ensure_ascii=False) + "\n")
                invalid_ids.add(row["candidate_id"])
            if source_id == 'ths' and day and not has_text_content(row.get('body')):
                content_gap_dates.add(str(day))
            state["invalid_count"] = len(invalid_ids)
            return
        with destination.open("a", encoding="utf-8") as target:
            target.write(json.dumps(row, ensure_ascii=False) + "\n")
        seen.add(row["candidate_id"])
        articles_this_call += 1

    while not state["terminal"] or state["pending"]:
        if state["pending"]:
            if max_articles is not None and processed_this_call >= max_articles:
                break
            row = state["pending"][0]
            processed_this_call += 1
            state["processed_article_count"] += 1
            if row["candidate_id"] in seen:
                state["pending"].pop(0)
                _persist(checkpoint, state)
                continue
            if source_id == "ths" and fetch_articles:
                try:
                    row = enrich_ths(row, fetcher)
                except Exception as error:
                    state["errors"].append({"url": row["source_url"], "error": str(error)[:300], "public_excerpt_retained": has_text_content(row.get("body"))})
            if source_id == "aastocks":
                try:
                    text, raw_path, collected = fetcher.get(source_id, row["source_url"])
                    parsed = parse_additional_article(text.encode("utf-8"), row["source_url"], source_id, raw_path, collected)
                    row = dict(parsed, discovery_raw_path=row["discovery_raw_path"],
                               discovery_query=row.get("discovery_query", ""))
                except Exception as error:
                    state["errors"].append({"url": row["source_url"], "error": str(error)[:300]})
            keep(row)
            state["pending"].pop(0)
            _persist(checkpoint, state)
            continue
        if state["terminal"]:
            break
        if state["finished"]:
            state["terminal"] = True
            _persist(checkpoint, state)
            break
        if source_id == "ths" and not calendar_scheduling and date.fromisoformat(state["day"]) > end:
            state.update(terminal=True, finished=True, stop_reason="已遍历请求范围内公开日期栏目")
            _persist(checkpoint, state)
            break
        if source_id == "aastocks" and state["aa_term_index"] >= len(state["aa_terms"]):
            state.update(terminal=True, finished=True, stop_reason="已遍历公开关键词查询；每查询最多300条，关键词发现不等于全年全站完整档案")
            _persist(checkpoint, state)
            break
        if pages_this_call >= max_pages or (max_articles is not None and processed_this_call >= max_articles):
            break
        if source_id == "aastocks":
            if state["aa_term_index"] >= len(state["aa_terms"]):
                state.update(terminal=True, finished=True, stop_reason="已遍历公开关键词查询；每查询最多300条，关键词发现不等于全年全站完整档案")
                _persist(checkpoint, state)
                break
            term = state["aa_terms"][state["aa_term_index"]]
            url = "https://www.aastocks.com/sc/resources/datafeed/getmorenews.ashx?" + urlencode(state["aa_cursor"]) if state["aa_cursor"] else "https://www.aastocks.com/sc/stocks/news/aafn/top-news/0?" + urlencode({"key": term})
        elif source_id == "caixin":
            url = CAIXIN_FEED + "?" + urlencode({"subject": "101426572", "type": 0, "count": 25, "picdim": "_266_177", "start": state["offset"]})
        elif source_id == "ths":
            if calendar_scheduling and not choose_ths_date():
                _persist(checkpoint, state)
                break
            if date.fromisoformat(state["day"]) > end:
                state.update(terminal=True, finished=True, stop_reason="已遍历请求范围内公开日期栏目")
                _persist(checkpoint, state)
                break
            url = state["next_url"] or THS_CHANNEL + state["day"].replace("-", "") + "/"
        else:
            url = _flash_url(source_id, state)
        if source_id == "ths" and state["day"] not in state["attempted_dates"]:
            state["attempted_dates"].append(state["day"])
        try:
            raw, path, collected = fetcher.get(source_id, url)
            digest = hashlib.sha256(raw.encode()).hexdigest()
            if source_id not in {"ths", "aastocks"} and digest in state["page_fingerprints"]:
                state.update(terminal=True, stop_reason="公开游标返回重复页面，未能继续回溯")
                _persist(checkpoint, state)
                break
            if source_id == "aastocks":
                if state["aa_cursor"]:
                    rows, following = aa_more_rows(raw, path, collected)
                    next_cursor = {**state["aa_cursor"], **following} if following else None
                else:
                    rows, next_cursor = aa_search_rows(raw, url, path, collected)
                upstream = rows
                state["aa_query_pages"] += 1
                for row in rows:
                    row["discovery_query"] = state["aa_terms"][state["aa_term_index"]]
                if len(rows) < 20 or next_cursor is None or state["aa_query_pages"] >= 15:
                    if state["aa_query_pages"] >= 15 and len(rows) >= 20:
                        state["truncated_queries"].append(state["aa_terms"][state["aa_term_index"]])
                    state["aa_term_index"] += 1
                    state["aa_query_pages"] = 0
                    state["aa_cursor"] = None
                else:
                    state["aa_cursor"] = next_cursor
            elif source_id == "caixin":
                upstream, rows = caixin_rows(raw, path, collected)
                state["offset"] += 25
            elif source_id == "ths":
                rows, following = ths_rows(raw, url, path, collected)
                upstream = rows
                state["date_errors"].pop(state["day"], None)
                if following:
                    state["next_url"] = following
                    state["date_cursors"][state["day"]] = following
                else:
                    state["processed_dates"][state["day"]] = {"status": "fetched"}
                    state["date_cursors"].pop(state["day"], None)
                    state["date_errors"].pop(state["day"], None)
                    state["day"] = str(date.fromisoformat(state["day"]) + timedelta(days=1))
                    state["next_url"] = None
            else:
                if source_id == "jiemian":
                    payload = json.loads(raw)
                    if str(payload.get("code")) != "0":
                        raise ValueError("界面公开历史接口返回错误")
                    value = payload.get("result")
                    upstream = value.get("list") if isinstance(value, dict) else value
                    if not isinstance(upstream, list):
                        raise ValueError("界面公开历史没有有效新闻列表")
                else:
                    upstream = parse_response(source_id, raw)
                rows = [row for row in (candidate(source_id, row, path, collected) for row in upstream) if row]
                previous_cursor = state["cursor"]
                previous_provider_cursor = state.get("provider_cursor", {})
                if source_id == "stcn":
                    payload = json.loads(raw)
                    state["provider_cursor"] = {key: payload[key] for key in ("page_time", "last_time") if payload.get(key) is not None}
                dated = [datetime.fromisoformat(row["published_at"]).timestamp() for row in rows if row.get("published_at")]
                if dated:
                    state["cursor"] = int(min(dated))
                if (source_id != "stcn" and dated and state["cursor"] >= previous_cursor) or (source_id == "stcn" and previous_provider_cursor and state.get("provider_cursor") == previous_provider_cursor):
                    state.update(terminal=True, stop_reason="公开游标未向历史推进，未能继续回溯")
            state["pages"] += 1
            pages_this_call += 1
            state.pop("last_error", None)
            state["page_fingerprints"].append(digest)
            state["pending"] = rows
            dates = [publication_day(row) for row in rows if publication_day(row)]
            if source_id not in {"ths", "aastocks"} and dates and min(dates) < start:
                # Pending in-range rows are flushed before the terminal result.
                state.update(finished=True, stop_reason="公开倒序列表已越过请求开始日期")
            elif source_id not in {"ths", "aastocks"} and not upstream:
                state.update(terminal=True, stop_reason="公开历史列表在到达请求开始日期前已无数据，不能证明全年覆盖")
            _persist(checkpoint, state)
        except Exception as error:
            attempts = state["failed_attempts"].get(url, 0) + 1
            state["failed_attempts"][url] = attempts
            state["last_error"] = {"url": url, "error": type(error).__name__ + ": " + str(error)[:300]}
            if calendar_scheduling:
                state["date_errors"][state["day"]] = state["last_error"]
            pages_this_call += 1
            missing_date = source_id == "ths" and isinstance(error, HTTPError) and error.code == 404
            if attempts >= 3 or missing_date:
                state["errors"].append(state["last_error"])
                if source_id == "ths":
                    state["processed_dates"][state["day"]] = {"status": "gap", **state["last_error"]}
                    state["date_cursors"].pop(state["day"], None)
                    state["date_errors"].pop(state["day"], None)
                    state["day"] = str(date.fromisoformat(state["day"]) + timedelta(days=1))
                    state["next_url"] = None
                    state["stop_reason"] = "部分日期公开页面不可访问，继续后续日期；不可访问日期保留为覆盖缺口"
                    state.pop("last_error", None)
                else:
                    state.update(terminal=True, stop_reason="公开历史请求连续失败，已保留断点及原始响应")
            _persist(checkpoint, state)
            if source_id == "ths" and (attempts >= 3 or missing_date):
                continue
            break
    if calendar_scheduling and not state["pending"] and not state["terminal"]:
        choose_ths_date()
        _persist(checkpoint, state)
    drained = not state["pending"]
    status = "incomplete" if not drained else "complete" if state["finished"] and not state["errors"] and not state.get("discovery_only") else "incomplete" if not state["terminal"] or state["finished"] else "blocked"
    if status == 'complete' and content_gap_dates:
        status = 'incomplete'
    retry_url = state.get("last_error", {}).get("url")
    attempts_remaining = max(0, 3 - state["failed_attempts"].get(retry_url, 3)) if retry_url and not state["terminal"] else 0
    if calendar_scheduling and not state["terminal"]:
        attempts_remaining = max([attempts_remaining] + [max(0, 3 - state["failed_attempts"].get(error["url"], 3))
                                  for error in state["date_errors"].values()])
    error_report = list(state["errors"])
    for error in state["date_errors"].values():
        if error not in error_report:
            error_report.append(error)
    if state.get("last_error") and state["last_error"] not in error_report:
        error_report.append(state["last_error"])
    result = {"source_id": source_id, "source_name": SOURCES[source_id]["name"], "status": status,
              "terminal": state["terminal"] and drained, "candidate_count": len(seen), "new_candidates": articles_this_call,
              "pages": state["pages"], "pages_this_call": pages_this_call, "invalid_count": state["invalid_count"],
              "errors": error_report, "retry_pending": bool(attempts_remaining),
              "retry_attempts_remaining": attempts_remaining,
              "processed_article_count": state["processed_article_count"],
              "priority_months": priority_months,
              "attempted_days": len(state["attempted_dates"]), "completed_days": len(state["processed_dates"]),
              "processed_dates": sorted(state["processed_dates"]), "frontier_date": state["day"] if source_id == "ths" else None,
              "candidates_path": str(destination), "checkpoint_path": str(checkpoint), "updated_at": now(),
              "coverage": {"requested_start": str(start), "requested_end": str(end),
                           "observed_earliest": state["observed_earliest"], "observed_latest": state["observed_latest"],
                           "candidate_count": len(seen), "reason": state["stop_reason"] or "本轮工作量上限，保留断点待续跑",
                           "scope": "财新能源栏目公开摘要" if source_id == "caixin" else "同花顺股指期货要闻日期栏目" if source_id == "ths" else "公开关键词检索发现，仍须Agent逐条判断" if source_id == "aastocks" else "已接入公开快讯列表",
                           "truncated_queries": state.get("truncated_queries", []),
                           "gap_dates": sorted({day for day, info in state["processed_dates"].items() if info["status"] == "gap"} | content_gap_dates),
                           "content_gap_dates": sorted(content_gap_dates),
                           "complete": status == "complete"}}
    write_json(output / "report.json", result)
    return result
