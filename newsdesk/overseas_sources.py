"""Public institutional research/insights, with publisher-scoped extraction.

No client login, access-control workaround, or search-result snippet is used.
Sitemap modification timestamps discover links but never become publication dates.
"""
import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from newsdesk.research_sources import declared_publication

MAX_BYTES = 12 * 1024 * 1024
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
SOURCES = {
    "institution_40": {"name": "瑞银", "host": "www.ubs.com", "enabled": False,
        "entry_urls": ["https://www.ubs.com/us/en/wealth-management/insights/market-news.html",
                       "https://www.ubs.com/global/en/wealthmanagement/insights/chief-investment-office/house-view/2026/weekly-key-messages.html"]},
    "institution_43": {"name": "伯恩斯坦", "host": "www.bernsteinresearch.com", "enabled": False,
        "entry_urls": ["https://www.bernsteinresearch.com/"]},
    "institution_45": {"name": "摩根士丹利", "host": "www.morganstanley.com", "enabled": True,
        "entry_urls": ["https://www.morganstanley.com/robots.txt", "https://www.morganstanley.com/sitemapindex.xml",
                       "https://www.morganstanley.com/sitemap.xml"]},
    "institution_46": {"name": "野村", "host": "www.nomuraconnects.com", "enabled": True,
        "entry_urls": ["https://www.nomuraconnects.com/Articles/"]},
    "institution_51": {"name": "摩根大通", "host": "www.jpmorgan.com", "enabled": True,
        "entry_urls": ["https://www.jpmorgan.com/robots.txt", "https://www.jpmorgan.com/sitemap.xml",
                       "https://www.jpmorgan.com/US/en/sitemap.xml"]},
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalized_url(url):
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path, "", ""))


def clean_text(text):
    return "\n".join(line for line in (re.sub(r"\s+", " ", part).strip() for part in text.splitlines()) if line)


class PublicHTML(HTMLParser):
    """Read metadata and only the publisher's real content containers."""
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, source_id):
        super().__init__(convert_charrefs=True)
        self.source_id = source_id
        self.meta = {}
        self.links = []
        self.stack = []
        self.capture = None
        self.blocks = []
        self.ld_parts = None
        self.jsonld = []
        self.ignored = 0
        self.h1_parts = []
        self.h1 = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = set(attrs.get("class", "").split())
        if tag == "meta":
            self.meta[attrs.get("property") or attrs.get("name")] = attrs.get("content", "")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.ld_parts = []
        if tag in {"script", "style", "noscript"}:
            self.ignored += 1
        if tag == "h1" and not self.h1_parts:
            self.h1 = True
        matched = (self.source_id == "institution_45" and "transcript_text" in classes
                   or self.source_id == "institution_46" and "wysiwyg" in classes
                   or self.source_id == "institution_51" and "cmp-text" in classes)
        if self.capture is None and matched:
            # MS pages include later "Up Next" podcast transcripts. Only the
            # first transcript belongs to this URL; do not capture recommendations.
            if self.source_id != "institution_45" or not self.blocks:
                self.capture = {"depth": len(self.stack), "parts": []}
        if self.capture is not None and tag in {"p", "div", "br", "li", "h1", "h2", "h3"}:
            self.capture["parts"].append("\n")
        if tag not in self.VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.ld_parts is not None:
            try:
                self.jsonld.append(json.loads("".join(self.ld_parts)))
            except (ValueError, TypeError):
                pass
            self.ld_parts = None
        if tag in {"script", "style", "noscript"}:
            self.ignored = max(0, self.ignored - 1)
        if tag == "h1":
            self.h1 = False
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]
        if self.capture is not None:
            if tag in {"p", "div", "li", "h1", "h2", "h3"}:
                self.capture["parts"].append("\n")
            if len(self.stack) <= self.capture["depth"]:
                text = clean_text("".join(self.capture["parts"]))
                if text:
                    self.blocks.append(text)
                self.capture = None

    def handle_data(self, data):
        if self.ld_parts is not None:
            self.ld_parts.append(data)
        if not self.ignored:
            if self.h1:
                self.h1_parts.append(data)
            if self.capture is not None:
                self.capture["parts"].append(data)


def parse_article(source_id, raw, url, raw_path, collected_at):
    parser = PublicHTML(source_id)
    parser.feed(raw.decode("utf-8", errors="replace"))
    title = clean_text("".join(parser.h1_parts)) or parser.meta.get("content_title") or parser.meta.get("og:title", "")
    body = clean_text("\n".join(parser.blocks))
    scope = "public_podcast_transcript" if source_id == "institution_45" and body else "public_article"
    if source_id == "institution_46":
        body = body.split("This content has been prepared by Nomura", 1)[0]
    if not body:
        body = parser.meta.get("og:description") or parser.meta.get("description", "")
        scope = "public_article_lead"
    body = clean_text(body)
    if not title or len(body) < 100:
        raise ValueError("未取得足够的公开文章正文或实质性导语")
    if len(body) > 18000:
        scope = "public_podcast_transcript_excerpt" if scope == "public_podcast_transcript" else "public_article_excerpt"
    published, precision = declared_publication(parser.jsonld)
    if not published and source_id == "institution_45":
        value = parser.meta.get("content_publishedAt", "")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                published, precision = stamp.isoformat(timespec="seconds"), "timestamp"
        except ValueError:
            pass
    published_date_text = parser.meta.get("publishDate", "")
    published_date = published[:10] if published else ""
    if published_date_text and not published:
        precision = "date"
        for pattern in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d"):
            try:
                published_date = datetime.strptime(published_date_text.strip(), pattern).date().isoformat()
                break
            except ValueError:
                continue
    # Never infer a time zone from country, URL, sitemap lastmod or collection.
    canonical = normalized_url(parser.meta.get("og:url") or url)
    if urlsplit(canonical).hostname != SOURCES[source_id]["host"]:
        canonical = normalized_url(url)
    external_id = hashlib.sha256(canonical.encode()).hexdigest()[:24]
    return {"candidate_id": source_id + ":" + external_id, "source_id": source_id,
            "source_name": SOURCES[source_id]["name"], "external_id": external_id,
            "source_url": canonical, "title": title[:400], "body": body[:18000],
            "published_at": published, "published_at_precision": precision,
            "published_date": published_date, "publisher_name": SOURCES[source_id]["name"],
            "collection_platform": "机构官网",
            "publication_date_text": published_date_text, "collected_at": collected_at,
            "raw_path": str(Path(raw_path).resolve()), "content_scope": scope}


def sitemap_urls(raw, source_id):
    root = ET.fromstring(raw)
    result = []
    for node in root.findall("s:url", NS):
        url = node.findtext("s:loc", "", NS)
        parsed = urlsplit(url)
        if parsed.hostname != SOURCES[source_id]["host"]:
            continue
        if source_id == "institution_45":
            valid = parsed.path.startswith(("/insights/articles/", "/insights/podcasts/thoughts-on-the-market/"))
        else:
            valid = parsed.path.startswith("/insights/global-research/")
        if valid:
            result.append((url, node.findtext("s:lastmod", "", NS)))
    # Topic discovery is not classification. Every fetched article still goes
    # to the agent; this prioritizes the institution's energy/macroeconomic feed.
    topics = ("oil", "energy", "commodit", "iran", "fed", "inflation", "bond", "rates", "economy")
    result.sort(key=lambda item: (item[1], any(word in item[0].lower() for word in topics)), reverse=True)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
    recent = [item for item in result if item[1][:10] >= cutoff]
    # Put recent energy/research topics before other recent public insights.
    recent.sort(key=lambda item: (any(word in item[0].lower() for word in topics), item[1]), reverse=True)
    return [url for url, _ in recent] or [url for url, _ in result]


class Fetcher:
    def __init__(self, directory, source_id):
        self.directory, self.source_id = Path(directory), source_id
        self.directory.mkdir(parents=True, exist_ok=True)
        self.count = 0
        self.last = 0

    def get(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != SOURCES[self.source_id]["host"]:
            raise ValueError("公开爬虫只访问已配置的官方机构域名")
        wait = .3 - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        self.count += 1
        path = self.directory / (self.source_id + "-" + str(self.count) + ".html")
        request = Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; OilNewsResearch/1.0)",
                                       "Accept": "text/html,application/xml,text/plain"})
        try:
            with urlopen(request, timeout=18) as response:
                if urlsplit(response.url).hostname != parsed.hostname:
                    raise ValueError("机构入口发生未配置的跨域重定向")
                raw, final_url, status = response.read(MAX_BYTES + 1), response.url, response.status
        except HTTPError as error:
            raw, final_url, status = error.read(MAX_BYTES + 1), error.url, error.code
            path.write_bytes(raw[:MAX_BYTES])
            path.with_suffix(".json").write_text(json.dumps({"request_url": url, "response_url": final_url,
                "http_status": status, "collected_at": now()}, ensure_ascii=False))
            raise
        if len(raw) > MAX_BYTES:
            raise ValueError("机构公开响应超过 12 MiB")
        path.write_bytes(raw)
        collected = now()
        path.with_suffix(".json").write_text(json.dumps({"request_url": url, "response_url": final_url,
            "http_status": status, "collected_at": collected, "sha256": hashlib.sha256(raw).hexdigest()}, ensure_ascii=False))
        return raw, path, collected, final_url


def crawl_one(source_id, raw_dir, limit):
    spec = SOURCES[source_id]
    fetcher = Fetcher(raw_dir, source_id)
    status = {"source_id": source_id, "source_name": spec["name"], "status": "failed", "count": 0,
              "errors": [], "entry_urls": spec["entry_urls"], "candidates": []}
    try:
        if source_id == "institution_43":
            _, _, _, final = fetcher.get(spec["entry_urls"][0])
            status["status"] = "requires_access" if "login" in final.lower() else "no_public_research"
            status["errors"].append("Bernstein Research 入口转向客户登录；未用同名财富管理机构替代。")
            return status
        if source_id == "institution_40":
            for url in spec["entry_urls"]:
                try:
                    fetcher.get(url)
                except (OSError, ValueError) as error:
                    status["errors"].append({"url": url, "error": type(error).__name__ + ": " + str(error)[:200]})
            return status
        if source_id == "institution_46":
            raw, _, _, final = fetcher.get(spec["entry_urls"][0])
            parser = PublicHTML(source_id)
            parser.feed(raw.decode("utf-8", errors="replace"))
            links = list(dict.fromkeys(normalized_url(urljoin(final, link)) for link in parser.links
                                      if urlsplit(urljoin(final, link)).path.startswith("/focused-thinking-posts/")))
        else:
            # Discovery uses the robots-declared sitemap chain stored alongside articles.
            for entry in spec["entry_urls"]:
                raw, _, _, _ = fetcher.get(entry)
            links = sitemap_urls(raw, source_id)
        for url in links[:max(limit * 2, 12)]:
            if len(status["candidates"]) >= limit:
                break
            try:
                raw, path, collected, final = fetcher.get(url)
                item = parse_article(source_id, raw, final, path, collected)
                status["candidates"].append(item)
            except (OSError, ValueError) as error:
                status["errors"].append({"url": url, "error": type(error).__name__ + ": " + str(error)[:200]})
        status["count"] = len(status["candidates"])
        status["status"] = "ok" if status["count"] and not status["errors"] else "partial" if status["count"] else "failed"
    except (OSError, ValueError, ET.ParseError) as error:
        status["errors"].append({"error": type(error).__name__ + ": " + str(error)[:200]})
    return status


def crawl_overseas_sources(output_dir, source_ids=None, limit_per_source=8):
    selected = list(dict.fromkeys(source_ids or SOURCES))
    if any(source not in SOURCES for source in selected):
        raise ValueError("未知的海外机构信源")
    if not isinstance(limit_per_source, int) or not 1 <= limit_per_source <= 30:
        raise ValueError("limit_per_source 必须是1至30")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "-overseas"
    raw_dir = output / "raw" / run_id
    started = now()
    with ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(lambda source: crawl_one(source, raw_dir, limit_per_source), selected))
    rows = []
    for run in runs:
        rows.extend(run.pop("candidates"))
    unique = {item["candidate_id"]: item for item in rows}
    path = output / "overseas-candidates.jsonl"
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in unique.values()), encoding="utf-8")
    report = {"run_id": run_id, "started_at": started, "completed_at": now(), "candidate_count": len(unique),
              "sources": runs, "candidates_path": str(path), "raw_directory": str(raw_dir)}
    report_path = output / ("overseas-run-" + run_id + ".json")
    report["report_path"] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "data" / "ingestion")
    parser.add_argument("--limit-per-source", type=int, default=8)
    args = parser.parse_args()
    print(json.dumps(crawl_overseas_sources(args.output_dir, limit_per_source=args.limit_per_source), ensure_ascii=False, indent=2))
