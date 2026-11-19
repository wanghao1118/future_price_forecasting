"""Resumable public-index history for the existing international publishers.

Every accepted candidate still requires the ordinary Codex review pipeline. This
module discovers full public indexes, never treats sitemap lastmod as publication,
and explicitly reports caps, unknown dates, and unavailable public pages.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

from newsdesk import overseas_sources, research_sources

SOURCES = {"goldman_sachs": {"name": "高盛", "host": "www.goldmansachs.com",
            "entry": "https://www.goldmansachs.com/sitemap.xml"},
           **{key: {**value, "entry": value["entry_urls"][0]} for key, value in overseas_sources.SOURCES.items()}}
SOURCES["institution_45"]["entry"] = "https://www.morganstanley.com/sitemap.xml"
SOURCES["institution_51"]["entry"] = "https://www.jpmorgan.com/US/en/sitemap.xml"
MAX_BYTES = 12 * 1024 * 1024
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
AGENT = "OilNewsResearch"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        temporary = Path(handle.name)
    temporary.replace(path)


def atomic_jsonl(path, rows):
    """Publish one complete candidate snapshot while reviewers read the old one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", delete=False) as handle:
            temporary = Path(handle.name)
            for item in rows:
                handle.write(json.dumps(item, ensure_ascii=False, allow_nan=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def normalized_url(url):
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, parts.query, ""))


def valid_url(source_id, url):
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname == SOURCES[source_id]["host"] and not parts.username and not parts.password


def article_url(source_id, url):
    if not valid_url(source_id, url) or urlsplit(url).path.lower().endswith((".pdf", ".jpg", ".png", ".xml")):
        return False
    path = urlsplit(url).path
    if source_id == "goldman_sachs":
        sections = path.strip("/").split("/")
        return len(sections) == 3 and sections[0] == "insights" and sections[1] in research_sources.PUBLIC_SECTIONS
    if source_id == "institution_45":
        return path.startswith(("/insights/articles/", "/insights/podcasts/thoughts-on-the-market/"))
    if source_id == "institution_46":
        return path.startswith("/focused-thinking-posts/")
    return source_id == "institution_51" and path.startswith("/insights/global-research/")


def sitemap_links(raw, source_id):
    """Return child indexes and article links; lastmod only orders discovery."""
    root = ET.fromstring(raw)
    if root.tag.rsplit("}", 1)[-1] not in {"sitemapindex", "urlset"}:
        raise ValueError("公开 sitemap 响应不是有效的索引或网址集合")
    children = [node.findtext("s:loc", "", NS) for node in root.findall("s:sitemap", NS)]
    children = [normalized_url(url) for url in children if valid_url(source_id, url)]
    rows = [(node.findtext("s:loc", "", NS), node.findtext("s:lastmod", "", NS))
            for node in root.findall("s:url", NS)]
    rows.sort(key=lambda row: (row[1], row[0]), reverse=True)
    return children, [normalized_url(url) for url, _ in rows if article_url(source_id, url)]


class Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])


def listing_links(raw, source_id, base):
    parser = Links()
    parser.feed(raw.decode("utf-8", errors="replace"))
    links = [normalized_url(urljoin(base, value)) for value in parser.links]
    pages = [url for url in links if valid_url(source_id, url)
             and re.fullmatch(r"/Articles/(?:p\d+/)?", urlsplit(url).path)]
    articles = list(dict.fromkeys(url for url in links if article_url(source_id, url)))
    if not articles:
        raise ValueError("公开列表没有文章链接，未将版式变化或访问提示当作空历史")
    return list(dict.fromkeys(pages)), articles


def declared_date(raw, source_id):
    """Read publisher-declared dates, including date-only JSON-LD."""
    parser = research_sources.ArticleText() if source_id == "goldman_sachs" else overseas_sources.PublicHTML(source_id)
    parser.feed(raw.decode("utf-8", errors="replace"))
    dates = []

    def walk(value):
        if isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(kind in {"Article", "NewsArticle", "BlogPosting"} for kind in types):
                if value.get("datePublished"):
                    dates.append(str(value["datePublished"]))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(parser.jsonld)
    dates.extend(parser.meta.get(key, "") for key in ("article:published_time", "content_publishedAt", "publishDate", "datePublished"))
    for value in dates:
        if not value:
            continue
        try:
            return date.fromisoformat(value.strip()[:10]).isoformat()
        except ValueError:
            for pattern in ("%B %d, %Y", "%b %d, %Y"):
                try:
                    return datetime.strptime(value.strip(), pattern).date().isoformat()
                except ValueError:
                    continue
    return ""


def parse_candidate(raw, source_id, url, raw_path, collected_at):
    parser = research_sources.parse_article if source_id == "goldman_sachs" else overseas_sources.parse_article
    item = parser(raw, url, raw_path, collected_at) if source_id == "goldman_sachs" else parser(source_id, raw, url, raw_path, collected_at)
    if not item.get("published_date"):
        item["published_date"] = declared_date(raw, source_id)
        if item["published_date"] and not item.get("published_at"):
            item["published_at_precision"] = "date"
    item.setdefault("publisher_name", SOURCES[source_id]["name"])
    item.setdefault("collection_platform", "机构官网")
    return item


def publication_day(item):
    if item.get("published_at"):
        stamp = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is not None:
            return stamp.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
    value = item.get("published_date", "")
    return date.fromisoformat(value).isoformat() if value else ""


class SameHostRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).hostname != urlsplit(req.full_url).hostname:
            raise ValueError("机构公开入口跳转到未配置的域名；未继续访问")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """Per-source cached requests; the raw body and response metadata are retained."""
    def __init__(self, directory, source_id, min_interval=.5, retry_errors=False):
        self.directory, self.source_id = Path(directory), source_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.min_interval, self.last = max(.35, min_interval), 0
        self.retry_errors = retry_errors
        self.robot = None
        self.opener = build_opener(SameHostRedirect())

    def get(self, url):
        if not valid_url(self.source_id, url):
            raise ValueError("只访问该机构已配置的官方公开域名")
        if self.robot is not None and not self.robot.can_fetch(AGENT, url):
            raise PermissionError("机构 robots.txt 不允许采集此公开路径")
        digest = hashlib.sha256(url.encode()).hexdigest()
        path = self.directory / (digest + ".response")
        metadata_path = self.directory / (digest + ".json")
        if path.exists() and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata["http_status"] == 200:
                return path.read_bytes(), path, metadata["collected_at"], metadata["response_url"]
            if not self.retry_errors:
                raise HTTPError(url, metadata["http_status"], "此前公开请求失败（已留存原始响应）", {}, None)
        wait = self.min_interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; OilNewsResearch/1.0)",
                                       "Accept": "text/html,application/xml,text/plain"})
        error = None
        try:
            with self.opener.open(request, timeout=18) as response:
                raw, final, status = response.read(MAX_BYTES + 1), response.url, response.status
        except HTTPError as exception:
            raw, final, status = exception.read(MAX_BYTES + 1), exception.url, exception.code
            error = exception
        if len(raw) > MAX_BYTES:
            raise ValueError("机构公开响应超过 12 MiB，未继续处理")
        collected = now()
        path.write_bytes(raw)
        atomic_json(metadata_path, {"request_url": url, "response_url": final, "http_status": status,
                                   "collected_at": collected, "sha256": hashlib.sha256(raw).hexdigest()})
        if error:
            raise error
        return raw, path, collected, final

    def read_robots(self):
        url = "https://" + SOURCES[self.source_id]["host"] + "/robots.txt"
        try:
            raw, _, _, _ = self.get(url)
        except HTTPError as error:
            if error.code == 404:
                return "not_provided"
            raise
        self.robot = RobotFileParser(url)
        self.robot.parse(raw.decode("utf-8", errors="replace").splitlines())
        return "checked"


def run_source(source_id, start_date, end_date, output_dir, *, max_articles=None,
               max_discovery_pages=None, min_interval=.5, retry_errors=False, fetcher=None):
    """Backfill one publisher; omitted caps exhaust the configured public index.

    Repeating the same call resumes cached indexes and per-article outcomes. The
    output folder belongs to this one source/window; changed dates are rejected.
    Caps count attempted new items, not accepted candidates, and always produce
    ``incomplete``. Index exhaustion is scoped evidence, not proof that a site has
    exposed every article it ever published.
    """
    if source_id not in SOURCES:
        raise ValueError("未知的海外历史信源")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    for value in (max_articles, max_discovery_pages):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError("历史采集上限必须为空或正整数")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "checkpoint.json"
    state = {"version": 1, "source_id": source_id, "start_date": start_date, "end_date": end_date,
             "discovery_done": [], "discovery_errors": {}, "pending": [SOURCES[source_id]["entry"]],
             "article_urls": []}
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if any(saved.get(key) != state[key] for key in ("version", "source_id", "start_date", "end_date")):
            raise ValueError("历史断点所属信源或日期范围不同，请使用独立输出目录")
        state = saved
    if retry_errors:
        state["pending"] = list(dict.fromkeys(state["pending"] + list(state["discovery_errors"])))
        state["discovery_errors"] = {}
    outcomes = output / "articles"
    outcomes.mkdir(exist_ok=True)
    fetcher = fetcher or Fetcher(output / "raw", source_id, min_interval, retry_errors)
    errors = []
    robots_status = "not_checked"
    discovery_this_run, attempted = 0, 0
    stop_reason = ""
    if source_id in {"institution_40", "institution_43"}:
        stop_reason = "configured_source_requires_access"
    else:
        try:
            robots_status = fetcher.read_robots()
        except (OSError, ValueError) as error:
            errors.append({"phase": "robots", "error": str(error)[:300]})
            stop_reason = "public_access_restricted" if isinstance(error, PermissionError) or getattr(error, "code", None) in {401, 403} else "rate_limited" if getattr(error, "code", None) == 429 else "public_access_unavailable"
    while state["pending"] and not stop_reason:
        if max_discovery_pages is not None and discovery_this_run >= max_discovery_pages:
            stop_reason = "discovery_page_cap"
            break
        url = state["pending"].pop(0)
        if url in state["discovery_done"]:
            continue
        discovery_this_run += 1
        try:
            raw, _, _, final = fetcher.get(url)
            if source_id == "institution_46":
                children, articles = listing_links(raw, source_id, final)
            else:
                children, articles = sitemap_links(raw, source_id)
            state["article_urls"] = list(dict.fromkeys(state["article_urls"] + articles))
            for child in children:
                if child not in state["discovery_done"] and child not in state["pending"]:
                    state["pending"].append(child)
            state["discovery_done"].append(url)
            state["discovery_errors"].pop(url, None)
        except (OSError, ValueError, ET.ParseError) as error:
            state["discovery_errors"][url] = str(error)[:300]
            if isinstance(error, PermissionError) or getattr(error, "code", None) in {401, 403, 429}:
                stop_reason = "rate_limited" if getattr(error, "code", None) == 429 else "public_access_restricted"
        atomic_json(checkpoint, state)
    # A discovery cap is resumable; process already-discovered articles now.
    if stop_reason == "discovery_page_cap":
        stop_reason = ""
    blocked_streak = 0
    for url in state["article_urls"]:
        if stop_reason:
            break
        result_path = outcomes / (hashlib.sha256(url.encode()).hexdigest() + ".json")
        if result_path.exists():
            previous = json.loads(result_path.read_text(encoding="utf-8"))
            if previous.get("status") != "failed" or not retry_errors:
                continue
        if max_articles is not None and attempted >= max_articles:
            stop_reason = "article_cap"
            break
        attempted += 1
        try:
            raw, path, collected, final = fetcher.get(url)
            item = parse_candidate(raw, source_id, final, path, collected)
            published = publication_day(item)
            status = "unknown_date" if not published else "accepted" if start_date <= published <= end_date else "outside_window"
            atomic_json(result_path, {"url": url, "status": status, "candidate": item,
                                      "publication_day": published})
            blocked_streak = 0
        except (OSError, ValueError) as error:
            code = getattr(error, "code", None)
            atomic_json(result_path, {"url": url, "status": "failed", "error": str(error)[:400], "http_status": code})
            blocked_streak = blocked_streak + 1 if code in {401, 403} else 0
            if code == 429 or blocked_streak >= 3:
                stop_reason = "rate_limited" if code == 429 else "public_access_restricted"
    records = [json.loads(path.read_text(encoding="utf-8")) for path in sorted(outcomes.glob("*.json"))]
    accepted = {row["candidate"]["candidate_id"]: row["candidate"] for row in records if row["status"] == "accepted"}
    unknown = [row for row in records if row["status"] == "unknown_date"]
    failures = [row for row in records if row["status"] == "failed"]
    candidates_path = output / "candidates.jsonl"
    atomic_jsonl(candidates_path, accepted.values())
    unknown_path = output / "unknown-dates.jsonl"
    atomic_jsonl(unknown_path, unknown)
    atomic_json(checkpoint, state)
    pending_articles = len(state["article_urls"]) - len(records)
    exhausted = not state["pending"] and not state["discovery_errors"] and not pending_articles and not stop_reason
    complete = exhausted and not failures and not unknown and not errors
    access_block = stop_reason in {"configured_source_requires_access", "public_access_restricted", "rate_limited"}
    status = "complete" if complete else "blocked" if access_block and not records else "failed" if stop_reason == "public_access_unavailable" and not records else "incomplete"
    report = {"source_id": source_id, "source_name": SOURCES[source_id]["name"], "status": status,
              "requested_start_date": start_date, "requested_end_date": end_date,
              "completed_at": now(), "candidate_count": len(accepted), "unknown_date_count": len(unknown),
              "outside_window_count": sum(row["status"] == "outside_window" for row in records),
              "failed_article_count": len(failures), "discovered_article_count": len(state["article_urls"]),
              "processed_article_count": len(records), "attempted_this_run": attempted,
              "discovery_pages_this_run": discovery_this_run, "discovery_pages_total": len(state["discovery_done"]),
              "discovery_errors": state["discovery_errors"], "pending_discovery_pages": len(state["pending"]),
              "pending_articles": pending_articles, "stop_reason": stop_reason,
              "public_index_exhausted": exhausted, "complete": complete,
              "coverage_scope": "已配置的官方公开研究栏目索引；不代表机构全部付费或未公开文章",
              "date_filter": "文章声明的首发日期；精确时间按北京时间换算，非 sitemap lastmod",
              "robots_status": robots_status, "errors": errors + [{"phase": "discovery", "url": key, "error": value} for key, value in state["discovery_errors"].items()] + failures,
              "candidates_path": str(candidates_path), "unknown_dates_path": str(unknown_path),
              "checkpoint_path": str(checkpoint), "raw_directory": str(output / "raw"),
              "earliest_publication_date": min((publication_day(row) for row in accepted.values()), default=None),
              "latest_publication_date": max((publication_day(row) for row in accepted.values()), default=None)}
    report["coverage"] = {"requested_start": start_date, "requested_end": end_date,
                          "candidate_count": len(accepted), "public_index_exhausted": exhausted,
                          "complete": complete, "unknown_date_count": len(unknown),
                          "pending_articles": pending_articles, "pending_discovery_pages": len(state["pending"]),
                          "earliest_publication_date": report["earliest_publication_date"],
                          "latest_publication_date": report["latest_publication_date"],
                          "scope": report["coverage_scope"], "errors": report["errors"]}
    atomic_json(output / "coverage.json", report)
    return report
