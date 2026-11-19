"""History attempts for broker channels that predate the Sina supplement.

Only public responses are used.  A public index, a single stock window or a
login-gated history is never reported as an institution's complete year.
Metadata discoveries are separate from valid, still-unreviewed body candidates.
"""

import hashlib
import json
import re
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from newsdesk.domestic_brokers_a import SOURCES as BROKERS, parse_eastmoney_detail
from newsdesk.domestic_brokers_b import parse_founder_detail, plain
from newsdesk.history_brokers import atomic_candidates, atomic_json
from newsdesk.xingye_source import PROFILE, F10_PAGES, next_data, parse_article, parse_f10_reports


SOURCES = {key: BROKERS[key] for key in ("institution_11", "institution_18", "institution_21", "institution_23", "institution_24")}
SOURCES["institution_49"] = {"name": "方正证券", "publisher_names": ["方正证券股份有限公司"]}
FOUNDER_TYPES = ("Macro", "Strategy", "Industry", "Company", "ConstantReturn", "Engineering", "MartetDailyPaper")
ALLOWED_HOSTS = {"research.citics.com", "gwlink.foundersc.com", "reportapi.eastmoney.com", "data.eastmoney.com",
                 "m.gelonghui.com", "www.gelonghui.com", "basic.10jqka.com.cn", "news.10jqka.com.cn"}
MAX_BYTES = 8 * 1024 * 1024
_LOCK = threading.Lock()
_LAST = 0.0


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ChannelFetchError(OSError):
    def __init__(self, message, *, url, status=None, raw_path=None):
        super().__init__(message)
        self.url, self.status, self.raw_path = url, status, raw_path


class ArticleValidationError(ValueError):
    def __init__(self, message, *, url, raw_path):
        super().__init__(message)
        self.url, self.raw_path, self.status = url, str(raw_path), 200


class Fetcher:
    """Keep normal public HTTP attempts, including blocked response evidence."""
    def __init__(self, output_dir, delay=.35, timeout=15):
        self.root = Path(output_dir) / "raw" / "original-broker-channels"
        self.root.mkdir(parents=True, exist_ok=True)
        self.delay, self.timeout = delay, timeout

    def get(self, url, post=None, encoding="utf-8"):
        global _LAST
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError("原渠道请求必须使用已核验的公开来源域名")
        key = hashlib.sha256(json.dumps([url, post], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        index = self.root / (key + "-cache.json")
        if index.exists():
            cached = json.loads(index.read_text())
            if cached.get("valid"):
                path = Path(cached["raw_path"])
                record = json.loads(path.read_text(encoding="utf-8"))
                return record["raw_response"], path, record["collected_at"]
        with _LOCK:
            wait = self.delay - (time.monotonic() - _LAST)
            if wait > 0:
                time.sleep(wait)
            _LAST = time.monotonic()
        headers = {"User-Agent": "Mozilla/5.0 (compatible; OilNewsResearch/1.0; public research)",
                   "Accept": "application/json,text/html", "Referer": "https://data.eastmoney.com/report/macresearch.jshtml" if "eastmoney.com" in parsed.hostname else url}
        payload = None
        if post is not None:
            headers["Content-Type"] = "application/json"
            payload = json.dumps(post).encode()
        request = Request(url, data=payload, headers=headers)
        status, raw, error = None, b"", None
        final = url
        try:
            try:
                response = urlopen(request, timeout=self.timeout)
            except HTTPError as exception:
                response = exception
                error = exception
            with response:
                status, final = response.status, response.url
                if urlsplit(final).hostname not in ALLOWED_HOSTS:
                    raise ValueError("原渠道出现非预期跨域跳转")
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError("原渠道响应超过8MiB")
            text = raw.decode(encoding, errors="strict")
        except (OSError, ValueError, UnicodeError) as exception:
            error = exception
            text = raw[:MAX_BYTES].decode(encoding, errors="replace")
        path = self.root / (key + "-" + str(time.time_ns()) + ".json")
        collected = now()
        atomic_json(path, {"request_url": url, "response_url": final, "request_method": "POST" if post is not None else "GET",
                           "request_body": post, "http_status": status, "collected_at": collected, "encoding": encoding,
                           "sha256": hashlib.sha256(raw).hexdigest(), "raw_response": text,
                           "error": str(error)[:500] if error else ""})
        atomic_json(index, {"valid": error is None and status == 200, "raw_path": str(path.resolve())})
        if error is not None or status != 200:
            raise ChannelFetchError("公开请求未取得有效响应：HTTP {} {}".format(status, str(error)[:200]),
                                    url=url, status=status, raw_path=str(path.resolve()))
        return text, path.resolve(), collected

    def invalidate(self, url, post=None):
        key = hashlib.sha256(json.dumps([url, post], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        index = self.root / (key + "-cache.json")
        if index.exists():
            value = json.loads(index.read_text())
            value["valid"] = False
            atomic_json(index, value)


def _load(path):
    return {row["candidate_id"]: row for row in (json.loads(line) for line in path.read_text().split('\n') if line.strip())} if path.exists() else {}


def _day(item):
    if item.get("published_at"):
        value = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("原发布时分没有时区")
        return value.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    day = str(item.get("published_date") or "")[:10]
    if day:
        date.fromisoformat(day)
    return day


def _within(item, start, end):
    day = _day(item)
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", day)) and start <= day <= end


def _base(source_id, channel, external_id, url):
    return {"candidate_id": source_id + ":" + external_id, "source_id": source_id, "source_name": SOURCES[source_id]["name"],
            "external_id": external_id, "source_url": url, "channel": channel}


def eastmoney_url(row):
    """The same report-type routes used by the publisher's public org table."""
    kind, code, encoded = row.get("columnType"), row.get("infoCode"), row.get("encodeUrl")
    if kind == "个股研报" and code:
        return "https://data.eastmoney.com/report/info/" + str(code) + ".html"
    if kind == "行业研报" and code:
        return "https://data.eastmoney.com/report/zw_industry.jshtml?" + urlencode({"infocode": code})
    path = {"券商晨会": "zw_brokerreport", "宏观研究": "zw_macresearch", "宏观策略": "zw_macresearch", "策略报告": "zw_strategy"}.get(kind)
    if not path or not encoded:
        raise ValueError("东方财富公开研报类型或详情链接缺失")
    return "https://data.eastmoney.com/report/" + path + ".jshtml?" + urlencode({"encodeUrl": encoded})


def parse_eastmoney_page(text, source_id, start, end, page, raw_path):
    value = json.loads(text)
    rows = value.get("data")
    if not isinstance(rows, list) or int(value.get("pageNo", -1)) != page:
        raise ValueError("东方财富公开机构历史列表未返回请求页")
    spec = SOURCES[source_id]
    discoveries = []
    for row in rows:
        if (str(row.get("orgCode")) != spec["eastmoney_org"] or row.get("orgSName") != spec["name"]
                or row.get("orgName") not in spec["publisher_names"]):
            raise ValueError("东方财富历史列表没有保持请求的机构署名过滤")
        day = str(row.get("publishDate") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not start <= day <= end:
            raise ValueError("东方财富历史列表没有保持请求的日期范围")
        identifier = str(row.get("id") or row.get("infoCode") or "")
        if not identifier or not row.get("title"):
            raise ValueError("东方财富历史列表缺少稳定ID或标题")
        discoveries.append(dict(_base(source_id, "eastmoney", "eastmoney:" + identifier, eastmoney_url(row)),
            title=row["title"][:400], publisher_name=row["orgName"], published_at="", published_date=day,
            published_at_precision="date", collection_platform="东方财富研报中心", report_type=row.get("columnType"),
            discovery_raw_path=str(raw_path), requires_detail=True, content_scope="listing_metadata_only"))
    pages = max(0, int(value.get("TotalPage", 0)))
    hits = int(value.get("hits", 0))
    if not rows and hits > 0 and page <= pages:
        raise ValueError("东方财富公开历史声称有记录却返回空页，不能当作已完整抓取")
    if rows and pages < page:
        raise ValueError("东方财富历史总页数无效")
    return discoveries, pages, hits


def parse_citic_public(text, raw_path, collected, start, end):
    value = json.loads(text)
    if value.get("code") != 0 or not isinstance(value.get("data"), dict):
        raise ValueError("中信公开首页没有返回有效结构化内容")
    found, gated = {}, False
    def walk(node):
        nonlocal gated
        if isinstance(node, dict):
            gated = gated or node.get("loginList") == 1
            identifier, title, body = node.get("id"), node.get("psTitle"), node.get("psCoreView")
            if identifier and isinstance(title, str) and isinstance(body, str) and len(body.strip()) >= 80:
                previous = found.get(str(identifier))
                if previous is None or len(body) > len(previous.get("psCoreView", "")):
                    found[str(identifier)] = node
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
    walk(value["data"])
    candidates = []
    for identifier, row in found.items():
        day = str(row.get("rptDate") or "")[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not start <= day <= end:
            continue
        candidates.append(dict(_base("institution_23", "citic_public_index", "citic:" + identifier,
            "https://research.citics.com/detail?" + urlencode({"rid": identifier})), title=row["psTitle"].strip()[:400],
            body=row["psCoreView"].strip()[:18000], publisher_name="中信证券", published_at="", published_date=day,
            published_at_precision="date", collected_at=collected, raw_path=str(raw_path),
            collection_platform="中信证券研究官网公开首页", content_scope="public_research_summary",
            source_listing_url="https://research.citics.com/home", detail_requires_login=True,
            content_note="仅无需登录已公开交付的核心观点摘要；不请求需要登录的历史列表或全文。"))
    return candidates, gated


def parse_founder_page(text, listing_url):
    channel_path = urlsplit(listing_url).path.rsplit("/", 1)[0]
    links = {}
    for attrs, content in re.findall(r"<a\b([^>]+)>(.*?)</a>", text, re.S | re.I):
        href = re.search(r"\bhref=[\"']([^\"']+)", attrs, re.I)
        if not href:
            continue
        url = urljoin(listing_url, href.group(1))
        if urlsplit(url).hostname != "gwlink.foundersc.com" or not re.fullmatch(re.escape(channel_path) + r"/\d+\.jhtml", urlsplit(url).path):
            continue
        heading = re.search(r"\btitle=[\"']([^\"']+)", attrs, re.I)
        title = plain(heading.group(1)) if heading else plain(content)
        if title:
            links[url] = title
    match = re.search(r"当前第\s*(\d+)\s*页(?:&nbsp;|\s)*共\s*(\d+)\s*页", text)
    if not match:
        # Some templates use a literal Unicode whitespace between the labels.
        match = re.search(r"当前第\s*(\d+)\s*页.{0,40}?共\s*(\d+)\s*页", text, re.S)
    if not links or not match:
        raise ValueError("方正公开研究列表缺少文章或明确分页，不能当空结果")
    return links, int(match.group(1)), int(match.group(2))


def _verify_gelonghui(profile):
    if profile.get("userId") != 624647 or "兴业证券研究所策略团队" not in str(profile.get("userBrief", "")):
        raise ValueError("格隆汇历史响应不是已核验的兴业认证策略团队")


def _initial_channels(source_id):
    if SOURCES[source_id].get("eastmoney_org"):
        names = ["eastmoney_all_reports"]
    elif source_id == "institution_23":
        names = ["citic_public_index"]
    elif source_id == "institution_49":
        names = ["founder_" + kind for kind in FOUNDER_TYPES]
    else:
        names = ["xingye_gelonghui", "xingye_ths_worth", "xingye_ths_news_archive"]
    return {name: {"channel": name, "attempted": False, "complete": False, "terminal": False,
                   "attempts": 0, "pages": 0, "next_page": 1, "errors": [], "article_errors": [],
                   "raw_paths": []} for name in names}


def _parse_discovery_body(row, text):
    channel, source_id = row["channel"], row["source_id"]
    if channel == "eastmoney":
        title, body = parse_eastmoney_detail(text, SOURCES[source_id]["name"])
        result = dict(row, title=title, body=body)
    elif channel.startswith("founder_"):
        result = dict(row, **parse_founder_detail(text))
    elif channel == "gelonghui":
        result = dict(row, **parse_article(text))
    elif channel == "ths_news_archive":
        # An article may be publicly readable but an unrelated body's mention
        # of the broker is insufficient to establish authorship.
        text_plain = plain(text)
        declared = re.search(r"(?:研究机构|来源)[：:]\s*(兴业证券(?:股份有限公司)?)", text_plain[:2500])
        heading = re.search(r"<h1\b[^>]*>(.*?)</h1>", text, re.S | re.I)
        content = re.search(r"<div\b[^>]*(?:id|class)=[\"'][^\"']*(?:atc-content|main-text|main_text)[^\"']*[\"'][^>]*>(.*?)</div>", text, re.S | re.I)
        body = plain(content.group(1)) if content else ""
        if not declared or not heading or len(body) < 80:
            raise ValueError("同花顺历史详情没有可二次核验的兴业署名及公开正文")
        result = dict(row, title=plain(heading.group(1)), body=body[:18000], publisher_name=declared.group(1))
    else:
        raise ValueError("未知原渠道正文类型")
    if row.get("published_date") and result.get("published_date") and row["published_date"] != result["published_date"]:
        raise ValueError("公开详情与发现列表的首发日期不一致")
    return result


def _body_for_discovery(row, fetcher):
    text, path, collected = fetcher.get(row["source_url"])
    try:
        result = _parse_discovery_body(row, text)
        if not isinstance(result.get("body"), str) or not result["body"].strip():
            raise ValueError("公开详情缺少可读取正文")
    except (ValueError, TypeError, KeyError, UnicodeError) as exception:
        raise ArticleValidationError(str(exception), url=row["source_url"], raw_path=path) from exception
    result.update(collected_at=collected, raw_path=str(path), requires_detail=False, content_scope="public_research_summary")
    return result


def _detail_url(url):
    parsed = urlsplit(str(url))
    return (parsed.hostname == "data.eastmoney.com" and parsed.path.startswith("/report/")
            or parsed.hostname == "gwlink.foundersc.com" and re.fullmatch(r"/infoResearch[^/]+/\d+\.jhtml", parsed.path)
            or parsed.hostname in {"m.gelonghui.com", "www.gelonghui.com"} and re.fullmatch(r"/(?:p|column/article)/\d+", parsed.path)
            or parsed.hostname == "news.10jqka.com.cn" and parsed.path.endswith(".shtml"))


def _migrate_article_terminals(state, discoveries, candidates):
    """Undo old whole-channel failures caused solely by one detail response.

    Old parse failures did not always retain a detail URL/ID. Such history is
    preserved without guessing an ID; the saved current index page is retried
    once and the new per-article isolation identifies the exact failed record.
    """
    body_messages = ("公开详情的作者机构", "没有可读取的公开研究正文", "方正官网没有公开可读的研究摘要",
                     "文章不属于平台认证的兴业证券研究团队", "文章有付费/未公开内容", "没有足够的公开研报文字",
                     "同花顺历史详情没有可二次核验", "公开详情与发现列表的首发日期不一致", "公开详情缺少可读取正文")
    for channel in state["channels"].values():
        article_errors = channel.setdefault("article_errors", [])
        index_errors = []
        migrated = False
        for error in channel.get("errors", []):
            actual_url = error.get("detail_url") or error.get("url", "")
            if error.get("raw_path"):
                try:
                    evidence = json.loads(Path(error["raw_path"]).read_text(encoding="utf-8"))
                    actual_url = evidence.get("request_url") or actual_url
                except (OSError, ValueError, TypeError):
                    pass
            is_article = error.get("phase") == "article" or _detail_url(actual_url)
            if not is_article and channel.get("stop_reason") == "public_request_failed":
                is_article = any(message in error.get("error", "") for message in body_messages)
            if not is_article:
                index_errors.append(error)
                continue
            migrated = True
            record = dict(error, phase="article", migrated_from_legacy_terminal=True)
            identifier = record.get("candidate_id") or next((identifier for identifier, row in discoveries.items()
                                                            if row.get("source_url") == actual_url), None)
            if identifier:
                record.update(candidate_id=identifier, url=discoveries.get(identifier, {}).get("source_url", actual_url))
                if not any(old.get("candidate_id") == identifier for old in article_errors):
                    article_errors.append(record)
            else:
                channel.setdefault("legacy_article_errors", []).append(record)
        channel["errors"] = index_errors
        if migrated and not index_errors and channel.get("stop_reason") == "public_request_failed":
            channel.setdefault("migration_history", []).append({"at": now(), "reason": "legacy_article_error_marked_index_terminal",
                                                               "resume_page": channel.get("next_page", 1), "resume_cursor": channel.get("cursor")})
            channel.update(terminal=False, complete=False, stop_reason="legacy_article_resume")
        unresolved = []
        for error in article_errors:
            if error.get("candidate_id") in candidates:
                channel.setdefault("resolved_article_errors", []).append(dict(error, resolved_at=now()))
            else:
                unresolved.append(error)
        channel["article_errors"] = unresolved


def run_original_channel(source_id, start_date, end_date, output_dir, *, max_pages=None, metadata_only=True,
                         max_candidates=None, resume=True, retry_failed=False, fetcher=None):
    """Resume original public channels alongside, never in place of, Sina.

    max_pages is a per-call global page budget. Seven Founder types and Xingye's
    separate original channels rotate fairly. terminal means exhausted or an
    explicit access limit; it does not mean complete annual institution coverage.
    Individual body failures are quarantined by final candidate ID; they never
    stop index pagination and leave final coverage incomplete until resolved.
    """
    if source_id not in SOURCES:
        raise ValueError("该机构没有本模块配置的原渠道")
    start, end = date.fromisoformat(str(start_date)).isoformat(), date.fromisoformat(str(end_date)).isoformat()
    if start > end:
        raise ValueError("开始日期晚于结束日期")
    if end > datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat():
        raise ValueError("历史结束日期不能晚于当前日期")
    for value in (max_pages, max_candidates):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("页面或候选预算必须为空或正整数")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    prefix = source_id + "-" + start + "_" + end + ("-metadata" if metadata_only else "-body")
    state_path = output / (prefix + "-original-state.json")
    report_path = output / (prefix + "-original-report.json")
    candidates_path = output / (source_id + "-original-candidates.jsonl")
    discoveries_path = output / (source_id + "-original-discoveries.jsonl")
    state = json.loads(state_path.read_text()) if resume and state_path.exists() else {"channels": _initial_channels(source_id)}
    candidates, discoveries = _load(candidates_path), _load(discoveries_path)
    fetcher = fetcher or Fetcher(output)
    _migrate_article_terminals(state, discoveries, candidates)
    if retry_failed:
        for channel in state["channels"].values():
            if channel["terminal"] and not channel["complete"]:
                channel.setdefault("previous_attempts", []).append({key: value for key, value in channel.items() if key != "previous_attempts"})
                channel.update(terminal=False, errors=[])
    spec = SOURCES[source_id]
    used, stop_reason = 0, ""
    article_errors_path = output / (prefix + "-original-article-errors.jsonl")
    failed_article_ids = {name: {error.get("candidate_id") for error in channel["article_errors"]}
                          for name, channel in state["channels"].items()}

    def quarantine_article(row, channel, exception, body=None):
        identifier = row["candidate_id"]
        if identifier in failed_article_ids[channel["channel"]]:
            return
        if isinstance(fetcher, Fetcher):
            fetcher.invalidate(row["source_url"])
        record = {"phase": "article", "candidate_id": identifier, "source_id": source_id,
                  "url": row["source_url"], "error": type(exception).__name__ + ": " + str(exception)[:450],
                  "discovery_raw_path": row.get("discovery_raw_path"), "candidate": row, "recorded_at": now()}
        raw_path = getattr(exception, "raw_path", None) or (body or {}).get("raw_path") or row.get("raw_path")
        if raw_path:
            record["raw_path"] = str(raw_path)
        if getattr(exception, "status", None) is not None:
            record["http_status"] = exception.status
        channel["article_errors"].append(record)
        failed_article_ids[channel["channel"]].add(identifier)

    def add_discovery(row, channel):
        identifier = row["candidate_id"]
        discoveries.setdefault(identifier, row)
        if metadata_only or identifier in candidates or identifier in failed_article_ids[channel["channel"]]:
            return
        if identifier in channel.get("outside_range", []):
            return
        body = None
        try:
            body = _body_for_discovery(row, fetcher)
            if not _day(body):
                raise ValueError("原渠道正文没有可确定的首发日期，不能归入年度范围")
            if not _within(body, start, end):
                channel.setdefault("outside_range", []).append(identifier)
                return
            candidates[identifier] = body
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exception:
            quarantine_article(row, channel, exception, body)

    def add_embedded_body(row, channel):
        identifier = row["candidate_id"]
        if identifier in candidates or identifier in failed_article_ids[channel["channel"]]:
            return
        try:
            if not isinstance(row.get("body"), str) or not row["body"].strip():
                raise ValueError("公开交付的单篇正文缺失")
            if not _day(row):
                raise ValueError("公开交付的单篇正文没有可确定的首发日期")
            if _within(row, start, end):
                candidates[identifier] = row
            elif identifier not in channel.setdefault("outside_range", []):
                channel["outside_range"].append(identifier)
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exception:
            quarantine_article(row, channel, exception, row)

    def save():
        channels = list(state["channels"].values())
        terminal = all(channel["terminal"] for channel in channels)
        indexes_complete = all(channel["complete"] for channel in channels)
        index_errors = [dict(error, channel=channel["channel"]) for channel in channels for error in channel["errors"]]
        article_errors = [dict(error, channel=channel["channel"]) for channel in channels for error in channel["article_errors"]]
        errors = index_errors + article_errors
        complete = indexes_complete and not metadata_only and not errors
        dates = sorted(_day(row) for row in candidates.values() if _day(row))
        report = {"source_id": source_id, "source_name": spec["name"], "start_date": start, "end_date": end,
                  "status": "complete" if complete else "discovered" if indexes_complete and metadata_only else "partial" if candidates or discoveries else "blocked" if terminal else "incomplete",
                  "original_channel_attempted": any(channel["attempted"] for channel in channels),
                  "original_channel_complete": complete, "metadata_only": metadata_only,
                  "classification_status": "unreviewed", "candidate_count": len(candidates), "discovery_count": len(discoveries),
                  "candidates_path": str(candidates_path), "discoveries_path": str(discoveries_path),
                  "article_errors_path": str(article_errors_path), "article_errors": article_errors,
                  "report_path": str(report_path), "state_path": str(state_path), "stop_reason": stop_reason,
                  "coverage": {"complete": complete, "discovery_complete": indexes_complete, "terminal": terminal,
                               "article_failures": len(article_errors),
                               "earliest_publication_date": dates[0] if dates else "", "latest_publication_date": dates[-1] if dates else ""},
                  "channels": channels, "errors": errors, "checked_at": now()}
        atomic_candidates(candidates_path, candidates)
        atomic_candidates(discoveries_path, discoveries)
        atomic_candidates(article_errors_path, {str(index): error for index, error in enumerate(article_errors)})
        atomic_json(state_path, state)
        atomic_json(report_path, report)
        return report

    while True:
        pending = [channel for channel in state["channels"].values() if not channel["terminal"]]
        if not pending:
            break
        if max_pages is not None and used >= max_pages:
            stop_reason = "max_pages"
            break
        if max_candidates is not None and len(set(discoveries) | set(candidates)) >= max_candidates:
            stop_reason = "max_candidates"
            break
        channel = min(pending, key=lambda item: item["attempts"])
        channel["attempted"] = True
        channel["attempts"] += 1
        channel.pop("stop_reason", None)
        used += 1
        name = channel["channel"]
        url, post, response_path = "", None, None
        try:
            if name == "eastmoney_all_reports":
                page = channel["next_page"]
                url = "https://reportapi.eastmoney.com/report/dg?" + urlencode({"orgCode": spec["eastmoney_org"], "pageNo": page,
                    "pageSize": 50, "beginTime": start, "endTime": end, "qType": "", "author": "*"})
                text, raw_path, _ = fetcher.get(url)
                response_path = str(raw_path)
                rows, total_pages, hits = parse_eastmoney_page(text, source_id, start, end, page, raw_path)
                channel["raw_paths"].append(str(raw_path))
                for row in rows:
                    row["source_listing_url"] = url
                    add_discovery(row, channel)
                channel.update(pages=page, next_page=page + 1, total_pages=total_pages, reported_hits=hits)
                if page >= total_pages:
                    channel.update(complete=True, terminal=True)
            elif name == "citic_public_index":
                url, post = "https://research.citics.com/default/api/index", {}
                text, raw_path, collected = fetcher.get(url, post=post)
                response_path = str(raw_path)
                rows, gated = parse_citic_public(text, raw_path, collected, start, end)
                channel["raw_paths"].append(str(raw_path))
                for row in rows:
                    add_embedded_body(row, channel)
                channel.update(pages=1, terminal=True, complete=False, stop_reason="history_requires_login" if gated else "public_index_window")
                channel["errors"].append({"url": url, "raw_path": str(raw_path),
                    "error": "官网公开首页可读取核心观点，但历史列表标明loginList=1；未请求受限历史或详情。" if gated else "公开首页没有可验证的全年历史分页。"})
            elif name.startswith("founder_"):
                kind = name[len("founder_"):]
                page = channel["next_page"]
                url = "https://gwlink.foundersc.com/infoResearch" + kind + ("/index.jhtml" if page == 1 else "/index_" + str(page) + ".jhtml")
                text, raw_path, _ = fetcher.get(url)
                response_path = str(raw_path)
                links, actual_page, total_pages = parse_founder_page(text, url)
                if page != actual_page:
                    raise ValueError("方正公开历史列表忽略了请求页数")
                channel["raw_paths"].append(str(raw_path))
                for detail_url, title in links.items():
                    identifier = re.search(r"/(\d+)\.jhtml", detail_url).group(1)
                    add_discovery(dict(_base(source_id, name, "founder:" + identifier, detail_url), title=title,
                        publisher_name="方正证券股份有限公司", published_at="", published_date="", published_at_precision="unknown",
                        source_listing_url=url, discovery_raw_path=str(raw_path), collection_platform="方正证券官网",
                        content_scope="listing_metadata_only", requires_detail=True,
                        date_note="公开列表没有发布日期，需抓正文核对2026范围，不能用标题年份代替。"), channel)
                channel.update(pages=page, next_page=page + 1, total_pages=total_pages)
                if page >= total_pages:
                    channel.update(complete=True, terminal=True)
            elif name == "xingye_gelonghui":
                if not channel.get("identity_verified"):
                    url = PROFILE
                    profile, path, _ = fetcher.get(url)
                    response_path = str(path)
                    _verify_gelonghui(next_data(profile)["articles"])
                    channel["raw_paths"].append(str(path))
                    channel["identity_verified"] = True
                cursor = channel.get("cursor", "")
                url = "https://m.gelonghui.com/api/community/dynamic/my-dynamics?" + urlencode({
                    "userId": 624647, "timestamp": cursor, "article": "true", "count": 15})
                text, raw_path, _ = fetcher.get(url)
                response_path = str(raw_path)
                payload = json.loads(text)
                if payload.get("statusCode") != 200 or not isinstance(payload.get("result"), dict):
                    raise ValueError("格隆汇公开历史接口未返回有效结果")
                profile = payload["result"]
                _verify_gelonghui(profile)
                rows = profile.get("myHomePageItemVOS")
                if not isinstance(rows, list):
                    raise ValueError("格隆汇公开历史缺少文章列表")
                channel["raw_paths"].append(str(raw_path))
                stamps = []
                for row in rows:
                    if row.get("userId") != 624647:
                        raise ValueError("格隆汇历史文章混入其他作者")
                    stamp = int(row["createTimestamp"])
                    stamps.append(stamp)
                    if row.get("type") != 3:
                        continue
                    published = datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")
                    discovery = dict(_base(source_id, "gelonghui", "gelonghui:" + str(row["id"]), "https://m.gelonghui.com/p/" + str(row["id"])),
                        title=str(row.get("title", ""))[:400], publisher_name="兴业证券研究所策略团队", published_at=published,
                        published_date="", published_at_precision="timestamp", source_listing_url=url,
                        discovery_raw_path=str(raw_path), collection_platform="格隆汇认证专栏", content_scope="listing_metadata_only", requires_detail=True)
                    if _within(discovery, start, end):
                        add_discovery(discovery, channel)
                if stamps != sorted(stamps, reverse=True):
                    raise ValueError("格隆汇时间游标列表不是降序，不能证明边界覆盖")
                channel["pages"] += 1
                if not stamps or datetime.fromtimestamp(min(stamps), ZoneInfo("Asia/Shanghai")).date().isoformat() < start:
                    channel.update(complete=True, terminal=True)
                elif cursor != "" and min(stamps) >= int(cursor):
                    raise ValueError("格隆汇时间游标未向历史推进")
                else:
                    channel["cursor"] = min(stamps)
            elif name in {"xingye_ths_worth", "xingye_ths_news_archive"}:
                code, worth_url = next(iter(F10_PAGES.items()))
                url = worth_url if name == "xingye_ths_worth" else urljoin(worth_url, "news.html")
                text, raw_path, collected = fetcher.get(url, encoding="gb18030")
                response_path = str(raw_path)
                channel["raw_paths"].append(str(raw_path))
                if name == "xingye_ths_worth":
                    for row in parse_f10_reports(text, code):
                        candidate = dict(row, **_base(source_id, "ths_worth", row["external_id"], url), collected_at=collected,
                                         raw_path=str(raw_path), url_is_collection=True)
                        add_embedded_body(candidate, channel)
                    channel.update(pages=1, terminal=True, complete=False, stop_reason="single_stock_current_window")
                    channel["errors"].append({"url": url, "raw_path": str(raw_path),
                        "error": "原同花顺入口仅宁德时代个股盈利预测窗口，不能声明兴业机构全年研究完整。"})
                else:
                    match = re.search(r'id=["\']report_list_contents["\'][^>]*>(.*?)</div>', text, re.S)
                    if not match:
                        raise ValueError("同花顺公开个股档案缺少历史报告数据")
                    rows = json.loads(match.group(1))
                    if not isinstance(rows, list):
                        raise ValueError("同花顺公开历史报告不是数组")
                    channel["available_archive_records"] = len(rows)
                    for row in rows:
                        if row.get("source") not in {"兴业证券", "兴业证券股份有限公司"}:
                            continue
                        report_url = str(row.get("url", "")).replace("http://", "https://", 1)
                        if urlsplit(report_url).hostname != "news.10jqka.com.cn":
                            raise ValueError("同花顺历史出现非预期原文域名")
                        identifier = re.search(r"/(\d+)\.shtml", report_url)
                        if not identifier:
                            raise ValueError("同花顺历史报告缺少稳定原文ID")
                        discovery = dict(_base(source_id, "ths_news_archive", "ths-report:" + code + ":" + identifier.group(1), report_url),
                            title=str(row.get("title", ""))[:400], publisher_name=row["source"], published_at="", published_date=str(row.get("date", "")),
                            published_at_precision="date", source_listing_url=url, discovery_raw_path=str(raw_path),
                            collection_platform="同花顺公开F10研报", content_scope="listing_metadata_only", requires_detail=True)
                        if _within(discovery, start, end):
                            add_discovery(discovery, channel)
                    channel.update(pages=1, terminal=True, complete=True, scope="仅原宁德时代个股公开档案")
            save()
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exception:
            if isinstance(fetcher, Fetcher) and url:
                fetcher.invalidate(url, post=post)
            evidence = {"phase": "index", "url": url, "error": type(exception).__name__ + ": " + str(exception)[:450]}
            raw_evidence = getattr(exception, "raw_path", None) or response_path
            if raw_evidence:
                evidence["raw_path"] = str(raw_evidence)
                if str(raw_evidence) not in channel["raw_paths"]:
                    channel["raw_paths"].append(str(raw_evidence))
            if getattr(exception, "status", None) is not None:
                evidence["http_status"] = exception.status
            channel["errors"].append(evidence)
            channel.update(terminal=True, complete=False, stop_reason="public_request_failed")
            save()
    return save()


def fetch_selected_original(source_id, discoveries_path, selected_ids, output_dir, *, start_date=None, end_date=None, fetcher=None):
    """Only returns source-verified public bodies; never classifies or archives."""
    if source_id not in SOURCES:
        raise ValueError("未知原渠道机构")
    records = _load(Path(discoveries_path))
    selected_ids = list(dict.fromkeys(selected_ids))
    if any(identifier not in records or records[identifier].get("source_id") != source_id for identifier in selected_ids):
        raise ValueError("正文ID必须来自该机构原渠道发现列表")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    path = output / (source_id + "-original-candidates.jsonl")
    candidates = _load(path)
    fetcher = fetcher or Fetcher(output)
    errors, outside_range = [], []
    for identifier in selected_ids:
        if identifier in candidates:
            continue
        row = records[identifier]
        try:
            candidate = _body_for_discovery(row, fetcher)
            if not _day(candidate):
                raise ValueError("原渠道正文没有可确定的首发日期，无法归入2026回溯范围")
            if start_date and end_date and not _within(candidate, str(start_date), str(end_date)):
                outside_range.append(identifier)
                continue
            candidates[identifier] = candidate
        except (OSError, ValueError, TypeError, KeyError, UnicodeError) as exception:
            if isinstance(fetcher, Fetcher):
                fetcher.invalidate(row["source_url"])
            errors.append({"candidate_id": identifier, "url": row["source_url"], "error": str(exception)[:500],
                           "raw_path": getattr(exception, "raw_path", None)})
        atomic_candidates(path, candidates)
    atomic_candidates(path, candidates)
    report = {"source_id": source_id, "status": "partial" if errors else "complete", "classification_status": "unreviewed",
              "candidate_count": len(candidates), "candidates_path": str(path), "selected_count": len(selected_ids),
              "outside_range": outside_range, "errors": errors, "coverage": {"complete": False, "scope": "Agent初筛选择的原渠道公开正文"},
              "completed_at": now()}
    atomic_json(output / (source_id + "-original-selected-report.json"), report)
    return report
