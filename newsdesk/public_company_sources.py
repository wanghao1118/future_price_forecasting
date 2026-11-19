"""Public Zijin Mining news, with resumable publisher-scoped historical indexes.

These are candidates, never oil relevance decisions or category assignments. Only
ordinary public requests to the company's two explicitly linked official language
sites are made; dated articles, raw responses and scoped coverage remain auditable.
"""

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

SOURCES = {"zijin_mining": {
    "name": "紫金矿业", "host": "www.zjky.cn", "enabled": True,
    "hosts": ["www.zjky.cn", "www.zijinmining.com"],
    "entry_urls": ["https://www.zjky.cn/news/news_list.jsp",
                   "https://www.zjky.cn/news/media_focus.jsp",
                   "https://www.zijinmining.com/news/news_list.jsp",
                   "https://www.zijinmining.com/news/media_focus.jsp",
                   "https://www.zjky.cn/sitemap.xml"],
}}
AGENT = "OilNewsResearch"
MAX_BYTES = 12 * 1024 * 1024
CHANNELS = {
    "cn_news": (SOURCES["zijin_mining"]["entry_urls"][0], "紫金新闻", "zh"),
    "cn_media": (SOURCES["zijin_mining"]["entry_urls"][1], "媒体聚焦", "zh"),
    "en_news": (SOURCES["zijin_mining"]["entry_urls"][2], "News", "en"),
    "en_media": (SOURCES["zijin_mining"]["entry_urls"][3], "Media Coverage", "en"),
    "cn_sitemap": (SOURCES["zijin_mining"]["entry_urls"][4], "官网公开 sitemap 补充索引", "zh"),
}
SCOPE_LIMITS = [
    "覆盖中英文新闻、媒体聚焦及中文 robots 声明的公开 sitemap；不等同公司全部历史资料。",
    "BLOG 静态入口只有空的动态列表，未将其当作已穷尽的文章归档；未猜测未公开的接口。",
    "sitemap lastmod 仅用于发现链接，绝不当作文章发布日期。",
    "不同编号的中英文章仅在官网声明 canonical/语言对应关系或正文完全一致时合并；其余交给后续 Agent 判断事件重复。",
]
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
BLOCK = {"p", "div", "li", "br", "section", "article", "h1", "h2", "h3", "h4", "tr"}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def atomic_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def valid_url(url):
    parts = urlsplit(url)
    return (parts.scheme == "https" and parts.hostname in SOURCES["zijin_mining"]["hosts"]
            and not parts.username and not parts.password and parts.port in {None, 443})


def normalized_url(url):
    parts = urlsplit(url)
    if parts.scheme == "http" and parts.hostname in SOURCES["zijin_mining"]["hosts"]:
        parts = parts._replace(scheme="https")
    path = re.sub(r"/{2,}", "/", parts.path)
    query = parse_qs(parts.query)
    # The publisher sitemap declares both this legacy alias and its .htm URL.
    legacy = re.fullmatch(r"/news/([a-z_-]+)-detail\.jsp", path, re.I)
    if legacy and re.fullmatch(r"\d+", query.get("id", [""])[0]):
        path = "/news/" + legacy.group(1) + "-detail-" + query["id"][0] + ".htm"
        query = {}
    if re.fullmatch(r"/news/[a-z_-]+-detail-\d+\.htm", path, re.I):
        query = {}
    return urlunsplit((parts.scheme, parts.netloc.lower(), path,
                       urlencode([(key, value) for key, values in query.items() for value in values]), ""))


def article_identity(url):
    url = normalized_url(url)
    if not valid_url(url):
        return ""
    match = re.fullmatch(r"/news/([a-z_-]+)-detail-(\d+)\.htm", urlsplit(url).path, re.I)
    return match.group(1).lower() + ":" + match.group(2) if match else ""


class Node:
    def __init__(self, tag="root", attrs=None, parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs or []), parent, []

    def walk(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.walk()

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()

    def text(self):
        def parts(node):
            if node.tag in {"script", "style", "noscript", "svg"}:
                return []
            values = []
            for child in node.children:
                if isinstance(child, str):
                    values.append(child)
                else:
                    if child.tag in BLOCK:
                        values.append("\n")
                    values.extend(parts(child))
                    if child.tag in BLOCK:
                        values.append("\n")
            return values
        return "\n".join(line for line in (re.sub(r"\s+", " ", line).strip()
                              for line in "".join(parts(self)).splitlines()) if line)


class Page(HTMLParser):
    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root = Node()
        self.stack = [self.root]
        self.feed(raw.decode("utf-8-sig", errors="replace"))

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def declared_day(value):
    match = re.search(r"(?<!\d)(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})(?:日)?", value or "")
    if not match:
        return ""
    try:
        return date(*map(int, match.groups())).isoformat()
    except ValueError:
        return ""


def parse_listing(raw, url):
    """Only real article anchors and a published next-page URL are followed."""
    page = Page(raw)
    nodes = list(page.root.walk())
    rows, seen = [], set()
    for node in nodes:
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        target = normalized_url(urljoin(url, node.attrs["href"]))
        identity = article_identity(target)
        if not identity or identity in seen:
            continue
        seen.add(identity)
        dates = [child.text() for child in node.walk() if child.has_class("date")]
        title = next((child.text() for child in node.walk() if child.has_class("tit")), "")
        # Dates may be the anchor's sibling, but never use a whole page's dates.
        if not dates and node.parent is not None and node.parent.tag == "li":
            dates = [child.text() for child in node.parent.walk() if child.has_class("date")]
        rows.append({"url": target, "listing_title": title or node.text().split("\n")[0],
                     "published_date": declared_day(dates[0]) if dates else "",
                     "published_time_raw": dates[0] if dates else ""})
    if not rows:
        raise ValueError("公开栏目没有可核实的文章链接，未把登录、动态空壳或版式变化当作历史穷尽")
    current = int(parse_qs(urlsplit(url).query).get("nowPage", ["1"])[0])
    next_url = ""
    for node in nodes:
        if node.tag != "a" or not node.attrs.get("href"):
            continue
        target = normalized_url(urljoin(url, node.attrs["href"]))
        if not valid_url(target) or urlsplit(target).path != urlsplit(url).path:
            continue
        query = parse_qs(urlsplit(target).query)
        number = query.get("nowPage", [""])[0]
        if not number.isdigit() or int(number) <= current:
            continue
        label = node.text().strip().lower()
        explicit = ("next" in node.attrs.get("rel", "").split() or node.has_class("next")
                    or label in {"next", "next page", "下一页", "下页", "›", ">"})
        if explicit:
            next_url = target
            break
    return rows, next_url


def sitemap_links(raw, url):
    root = ET.fromstring(raw)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"sitemapindex", "urlset"}:
        raise ValueError("官网 sitemap 不是有效公开网址索引")
    urls = [normalized_url(node.text or "") for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "loc"]
    urls = list(dict.fromkeys(value for value in urls if valid_url(value)))
    children = [value for value in urls if urlsplit(value).path.lower().endswith(".xml")] if kind == "sitemapindex" else []
    return children, [{"url": value, "listing_title": "", "published_date": "", "published_time_raw": ""}
                      for value in urls if article_identity(value)]


def parse_article(raw, url, raw_path, collected_at, listing=None):
    page = Page(raw)
    nodes = list(page.root.walk())
    detail = next((node for node in nodes if node.has_class("news_detail01")), page.root)
    body_node = next((node for node in detail.walk() if node.has_class("myart")), None)
    title = next((node.text() for node in detail.walk() if node.has_class("title")), "")
    meta = {node.attrs.get("property", node.attrs.get("name", "")): node.attrs.get("content", "")
            for node in nodes if node.tag == "meta"}
    title = title or meta.get("og:title", "").strip()
    body = body_node.text() if body_node else ""
    if not title or not body:
        raise ValueError("没有官方文章标题或可读公开正文容器 .myart")
    stamp = next((node.text() for node in detail.walk() if node.has_class("time")), "")
    published_at = ""
    published_date = declared_day(stamp)
    date_origin = "source_article" if published_date else ""
    for value in (meta.get("article:published_time", ""), meta.get("datePublished", "")):
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            published_at = parsed.isoformat()
            published_date = parsed.date().isoformat()
            date_origin = "source_article_metadata"
            stamp = value
            break
        if not published_date and declared_day(value):
            published_date, stamp, date_origin = declared_day(value), value, "source_article_metadata"
    if not published_date and listing and listing.get("published_date"):
        published_date = listing["published_date"]
        stamp = listing.get("published_time_raw", published_date)
        date_origin = "source_listing"
    canonical = normalized_url(url)
    aliases = []
    for node in nodes:
        if node.tag != "link" or not node.attrs.get("href"):
            continue
        target = normalized_url(urljoin(url, node.attrs["href"]))
        if not article_identity(target):
            continue
        rel = node.attrs.get("rel", "").split()
        if "canonical" in rel:
            canonical = target
        elif "alternate" in rel and node.attrs.get("hreflang"):
            aliases.append(target)
    identity = article_identity(canonical) or article_identity(url)
    kind, number = identity.split(":")
    row = {"candidate_id": "zijin_mining:" + kind + ":" + number,
           "source_id": "zijin_mining", "source_name": "紫金矿业", "publisher_name": "紫金矿业",
           "external_id": kind + "-" + number, "source_url": normalized_url(url), "canonical_url": canonical,
           "title": title, "body": body, "published_at": published_at, "published_date": published_date,
           "published_at_precision": "timestamp" if published_at else "date" if published_date else "unknown",
           "published_time_raw": stamp, "published_date_origin": date_origin, "collected_at": collected_at,
           "raw_path": str(Path(raw_path).resolve()), "content_scope": "public_article", "title_origin": "source",
           "collection_platform": "紫金矿业官网", "language": "zh" if urlsplit(url).hostname == "www.zjky.cn" else "en",
           "declared_translation_urls": list(dict.fromkeys(aliases))}
    if listing and listing.get("listing_raw_path"):
        row["listing_raw_path"] = listing["listing_raw_path"]
    return row


class OfficialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not valid_url(newurl):
            raise ValueError("公司公开页面跳转到未配置域名或非 HTTPS，未继续访问")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    """get(url) returns (bytes, raw_path, collected_at, final_url); no login/cookies."""
    def __init__(self, output_dir, min_interval=.4):
        self.directory = Path(output_dir)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.min_interval, self.last = max(.35, min_interval), 0
        self.opener = build_opener(OfficialRedirect())
        self.robots = {}
        self.robots_status = {}

    def _request(self, url):
        if not valid_url(url):
            raise ValueError("只允许紫金矿业两个已核实的官方 HTTPS 域名")
        digest = hashlib.sha256(url.encode()).hexdigest()
        path = self.directory / (digest + ".response")
        metadata_path = self.directory / (digest + ".json")
        if path.exists() and metadata_path.exists():
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if metadata["http_status"] != 200:
                raise HTTPError(url, metadata["http_status"], "已审计的公开请求错误", {}, None)
            return path.read_bytes(), path, metadata["collected_at"], metadata["response_url"]
        wait = self.min_interval - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)
        self.last = time.monotonic()
        error = None
        try:
            with self.opener.open(Request(url, headers={"User-Agent": "OilNewsResearch/1.0 (+public-company-source)",
                                                       "Accept": "text/html,application/xml,text/plain"}), timeout=14) as response:
                raw, final, status = response.read(MAX_BYTES + 1), response.url, response.status
        except HTTPError as exception:
            raw, final, status, error = exception.read(MAX_BYTES + 1), exception.url, exception.code, exception
        if len(raw) > MAX_BYTES:
            raise ValueError("公开响应超过 12 MiB，未处理截断内容")
        if not valid_url(final):
            raise ValueError("公司公开响应来自未配置域名")
        collected = now()
        path.write_bytes(raw)
        atomic_json(metadata_path, {"request_url": url, "response_url": final, "http_status": status,
                    "collected_at": collected, "sha256": hashlib.sha256(raw).hexdigest()})
        if error:
            raise error
        return raw, path, collected, final

    def get(self, url):
        if not valid_url(url):
            raise ValueError("只允许紫金矿业两个已核实的官方 HTTPS 域名")
        host = urlsplit(url).hostname
        if host not in self.robots:
            robot_url = "https://" + host + "/robots.txt"
            robot = RobotFileParser(robot_url)
            try:
                raw, _, _, _ = self._request(robot_url)
                robot.parse(raw.decode("utf-8-sig", errors="replace").splitlines())
                self.robots_status[host] = "checked"
            except HTTPError as error:
                if error.code != 404:
                    self.robots_status[host] = "HTTP " + str(error.code)
                    raise
                robot.parse(["User-agent: *", "Disallow:"])
                self.robots_status[host] = "not_provided"
            self.robots[host] = robot
        if not self.robots[host].can_fetch(AGENT, url):
            raise PermissionError("公司 robots.txt 不允许采集此路径")
        return self._request(url)


def _new_state(source_id, start, end):
    return {"version": 1, "source_id": source_id, "start_date": start, "end_date": end,
            "channels": {key: {"pending": [value[0]], "done": [], "status": "pending", "last_day": ""}
                         for key, value in CHANNELS.items()},
            "pending_articles": {key: [] for key in CHANNELS}, "seen_articles": {}, "candidates": [],
            "outcomes": [], "gaps": [], "duplicates": [], "pages": 0, "processed_article_count": 0,
            "discovery_turn": 0, "article_turn": 0}


def _blocked(error):
    return isinstance(error, PermissionError) or getattr(error, "code", None) in {401, 403, 429}


def run_source(source_id, start_date, end_date, output_dir, max_pages=10, max_articles=40,
               resume=True, fetcher=None):
    """Resume all public columns independently; caps apply to this invocation only."""
    if source_id not in SOURCES:
        raise ValueError("未知的上市公司信源")
    start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    if end < start:
        raise ValueError("历史日期范围倒置")
    for value in (max_pages, max_articles):
        if value is not None and (not isinstance(value, int) or value < 0):
            raise ValueError("单轮上限须为非负整数或 None")
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint = output / "checkpoint.json"
    state = _new_state(source_id, start.isoformat(), end.isoformat())
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if not resume:
            raise ValueError("输出目录已有断点；重新采集请使用独立目录")
        if any(saved.get(key) != state[key] for key in ("version", "source_id", "start_date", "end_date")):
            raise ValueError("历史断点属于其他信源或日期范围，请使用独立目录")
        state = saved
    # A crash after recording an attempted request must not lose its later retry.
    for row in state.pop("deferred_articles", []):
        state["pending_articles"][row["channel"]].append(row)
    state["deferred_articles"] = []
    fetcher = fetcher or Fetcher(output / "raw")
    keys = list(CHANNELS)
    pages_this_run, articles_this_run = 0, 0
    blocked_hosts = set()

    def save():
        atomic_jsonl(output / "public-company-candidates.jsonl", state["candidates"])
        atomic_jsonl(output / "gaps.jsonl", state["gaps"])
        atomic_jsonl(output / "duplicates.jsonl", state["duplicates"])
        atomic_json(checkpoint, state)

    def gap(phase, url, reason, **extra):
        state["gaps"].append({"phase": phase, "url": url, "reason": reason, "recorded_at": now(), **extra})

    # Round robin columns: a long news archive never starves the media column or English supplements.
    while ((max_pages is None or pages_this_run < max_pages)
           and any(channel["pending"] for channel in state["channels"].values())):
        key = keys[state["discovery_turn"] % len(keys)]
        state["discovery_turn"] += 1
        channel = state["channels"][key]
        if not channel["pending"]:
            continue
        url = channel["pending"].pop(0)
        host = urlsplit(url).hostname
        if host in blocked_hosts:
            channel["status"] = "restricted"
            gap("listing", url, "同官方域名已返回访问限制，未继续请求")
            continue
        pages_this_run += 1
        state["pages"] += 1
        try:
            raw, raw_path, collected, final = fetcher.get(url)
            if not valid_url(final):
                raise ValueError("公开索引重定向到未配置域名")
            if key.endswith("sitemap"):
                children, rows = sitemap_links(raw, final)
                next_pages = [child for child in children if child not in channel["done"] and child != url]
            else:
                rows, next_page = parse_listing(raw, final)
                next_pages = [next_page] if next_page else []
            days = [row["published_date"] for row in rows if row["published_date"]]
            boundary = bool(days) and len(days) == len(rows) and max(days) < start.isoformat()
            if boundary:
                next_pages = []
                channel["status"] = "before_start_date"
            else:
                channel["status"] = "pending" if next_pages else "public_index_exhausted"
            channel["last_day"] = min(days) if days else channel.get("last_day", "")
            channel["done"].append(url)
            channel["pending"].extend(page for page in next_pages if page not in channel["done"] and page not in channel["pending"])
            for row in rows:
                identity = article_identity(row["url"])
                if identity in state["seen_articles"]:
                    continue
                # A dated list can rule out older/future articles without guessing a body date.
                day = row.get("published_date", "")
                if day and not (start.isoformat() <= day <= end.isoformat()):
                    state["seen_articles"][identity] = "outside_listing_window"
                    continue
                state["seen_articles"][identity] = row["url"]
                state["pending_articles"][key].append({**row, "listing_url": final,
                    "listing_raw_path": str(Path(raw_path).resolve()), "channel": key})
        except (OSError, ValueError, ET.ParseError) as error:
            attempts = channel.setdefault("failed_attempts", {})
            attempts[url] = attempts.get(url, 0) + 1
            permanent = _blocked(error) or getattr(error, "code", None) == 404 or attempts[url] >= 3
            if _blocked(error):
                blocked_hosts.add(host)
            if permanent:
                channel["status"] = "restricted" if _blocked(error) else "gap"
                gap("listing", url, type(error).__name__ + ": " + str(error)[:400], attempts=attempts[url])
            else:
                # At most one retry per call; other columns continue immediately.
                channel["status"] = "retry_pending"
                channel["retry_next_call"] = url
            channel["last_error"] = type(error).__name__ + ": " + str(error)[:400]
        save()
    for channel in state["channels"].values():
        if channel.get("retry_next_call"):
            channel["pending"].append(channel.pop("retry_next_call"))

    aliases = set()
    body_hashes = set()
    for row in state["candidates"]:
        aliases.update(article_identity(value) for value in [row["source_url"], row["canonical_url"], *row.get("declared_translation_urls", [])])
        body_hashes.add(hashlib.sha256(row["body"].encode()).hexdigest())
    while max_articles is None or articles_this_run < max_articles:
        if all(not rows for rows in state["pending_articles"].values()):
            break
        key = keys[state["article_turn"] % len(keys)]
        state["article_turn"] += 1
        if not state["pending_articles"][key]:
            continue
        listing = state["pending_articles"][key].pop(0)
        url = listing["url"]
        if urlsplit(url).hostname in blocked_hosts:
            gap("article", url, "同官方域名已返回访问限制，未继续请求")
            state["outcomes"].append({"url": url, "status": "restricted"})
            continue
        articles_this_run += 1
        state["processed_article_count"] += 1
        try:
            raw, raw_path, collected, final = fetcher.get(url)
            if not valid_url(final):
                raise ValueError("公开文章重定向到未配置域名")
            row = parse_article(raw, final, raw_path, collected, listing)
            row["source_section"] = CHANNELS[key][1]
            if not row["published_date"]:
                gap("article", url, "文章与其列表均无可核实发布日期", raw_path=str(raw_path))
                state["outcomes"].append({"url": url, "status": "unknown_date"})
            elif not (start.isoformat() <= row["published_date"] <= end.isoformat()):
                state["outcomes"].append({"url": url, "status": "outside_window", "published_date": row["published_date"]})
            else:
                identities = {article_identity(value) for value in [row["source_url"], row["canonical_url"], *row["declared_translation_urls"]]}
                digest = hashlib.sha256(row["body"].encode()).hexdigest()
                if identities.intersection(aliases) or digest in body_hashes:
                    state["duplicates"].append({"url": url, "canonical_url": row["canonical_url"],
                        "reason": "official_identity_or_declared_language_equivalent_or_exact_body", "raw_path": row["raw_path"]})
                    state["outcomes"].append({"url": url, "status": "duplicate"})
                else:
                    state["candidates"].append(row)
                    state["outcomes"].append({"url": url, "status": "accepted"})
                aliases.update(identities)
                body_hashes.add(digest)
        except (OSError, ValueError) as error:
            if _blocked(error):
                blocked_hosts.add(urlsplit(url).hostname)
            attempts = listing.get("attempts", 0) + 1
            permanent = (isinstance(error, ValueError) or _blocked(error)
                         or getattr(error, "code", None) == 404 or attempts >= 3)
            reason = type(error).__name__ + ": " + str(error)[:400]
            if permanent:
                gap("article", url, reason, attempts=attempts)
                state["outcomes"].append({"url": url, "status": "failed"})
            else:
                # Network failures are retried on a subsequent invocation, after
                # other columns have had their turn, never in a tight loop.
                state["deferred_articles"].append({**listing, "attempts": attempts})
                state.setdefault("transient_errors", []).append({"url": url, "reason": reason,
                                                                "attempts": attempts, "recorded_at": now()})
                state["outcomes"].append({"url": url, "status": "retry_pending"})
        save()
    for row in state["deferred_articles"]:
        state["pending_articles"][row["channel"]].append(row)
    state["deferred_articles"] = []
    save()
    pending_pages = sum(len(value["pending"]) for value in state["channels"].values())
    pending_articles = sum(len(rows) for rows in state["pending_articles"].values())
    terminal = pending_pages == 0 and pending_articles == 0
    complete = terminal and not state["gaps"]
    observed = sorted(row["published_date"] for row in state["candidates"])
    report = {"source_id": source_id, "source_name": SOURCES[source_id]["name"],
              "status": "complete" if complete else "incomplete", "complete": complete, "terminal": terminal,
              "candidates_path": str(output / "public-company-candidates.jsonl"), "checkpoint_path": str(checkpoint),
              "candidate_count": len(state["candidates"]), "count": len(state["candidates"]), "pages": state["pages"],
              "discovery_pages_total": state["pages"], "discovered_article_count": len(state["seen_articles"]),
              "processed_article_count": state["processed_article_count"], "pages_this_run": pages_this_run,
              "articles_this_run": articles_this_run, "pending_discovery_pages": pending_pages,
              "pending_article_count": pending_articles, "gap_count": len(state["gaps"]),
              "unknown_date_count": sum(row["status"] == "unknown_date" for row in state["outcomes"]),
              "transient_error_count": len(state.get("transient_errors", [])),
              "duplicate_count": len(state["duplicates"]), "errors": state["gaps"][-20:],
              "stop_reason": "public_indexes_finished" if terminal else "round_cap_or_retry_pending",
              "coverage": {"requested_start": start.isoformat(), "requested_end": end.isoformat(),
                  "observed_earliest": observed[0] if observed else "", "observed_latest": observed[-1] if observed else "",
                  "scope": "紫金矿业中英文官网新闻、媒体聚焦公开分页及中文 sitemap 补充索引",
                  "scope_limits": SCOPE_LIMITS, "channels": state["channels"],
                  "public_index_exhausted": terminal and all(row["status"] in {"before_start_date", "public_index_exhausted"}
                                                            for row in state["channels"].values()),
                  "robots": getattr(fetcher, "robots_status", {}), "gaps_path": str(output / "gaps.jsonl")},
              "updated_at": now()}
    atomic_json(output / "coverage.json", report)
    return report


def crawl_public_company_sources(output_dir, source_ids=None, limit_per_source=5):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    selected = list(SOURCES) if source_ids is None else list(source_ids)
    if any(source_id not in SOURCES for source_id in selected):
        raise ValueError("未知的上市公司信源")
    limit = max(1, min(100, int(limit_per_source)))
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-company-" + uuid.uuid4().hex[:6]
    candidates, reports = [], []
    for source_id in selected:
        directory = output / "raw" / run_id / source_id
        report = run_source(source_id, "0001-01-01", datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                            directory, max_pages=len(CHANNELS), max_articles=limit)
        # A normal latest-news sample is successful when public candidates were
        # obtained; its deliberately capped historical scope is reported apart.
        reports.append({**report, "history_status": report["status"],
                        "status": "ok" if report["candidate_count"] and not report["gap_count"] else
                                  "partial" if report["candidate_count"] else
                                  "failed" if report["gap_count"] else "empty"})
        path = Path(report["candidates_path"])
        candidates.extend(json.loads(line) for line in path.read_text(encoding="utf-8").split('\n') if line.strip())
    candidate_path = output / "public-company-candidates.jsonl"
    atomic_jsonl(candidate_path, candidates)
    report = {"run_id": run_id, "completed_at": now(), "candidate_count": len(candidates),
              "sources": reports, "candidates_path": str(candidate_path),
              "report_path": str(output / ("public-company-run-" + run_id + ".json"))}
    atomic_json(report["report_path"], report)
    return report
