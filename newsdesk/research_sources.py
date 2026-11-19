"""Crawl public institutional articles; no client research login or PDF scraping.

Goldman Sachs URLs are discovered from its public robots.txt sitemap. Every
collected topic is forwarded to the classification agent without keyword filtering.
"""
import argparse
import hashlib
import json
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

BASE = 'https://www.goldmansachs.com'
SOURCE_ID = 'goldman_sachs'
MAX_BYTES = 8 * 1024 * 1024
PUBLIC_SECTIONS = {'articles', 'goldman-sachs-exchanges', 'the-markets', 'videos', 'talks-at-gs'}
NS = {'s': 'http://www.sitemaps.org/schemas/sitemap/0.9'}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def get_public(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != 'www.goldmansachs.com':
        raise ValueError('Only the configured official source host is allowed')
    request = Request(url, headers={'User-Agent': 'OilNewsResearch/1.0', 'Accept': 'text/html,application/xml,text/plain'})
    with urlopen(request, timeout=15) as response:
        if urlsplit(response.url).hostname != parsed.hostname:
            raise ValueError('Unexpected cross-host redirect')
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Response exceeds size limit')
        return raw


def sitemap_articles(raw):
    """Return real article URLs ordered by sitemap modification, not publication."""
    root = ET.fromstring(raw)
    found = {}
    for node in root.findall('s:url', NS):
        url = node.findtext('s:loc', '', NS)
        parts = urlsplit(url)
        path = parts.path.strip('/').split('/')
        if (parts.scheme == 'https' and parts.hostname == 'www.goldmansachs.com'
                and len(path) == 3 and path[0] == 'insights' and path[1] in PUBLIC_SECTIONS
                and not path[2].lower().endswith('.pdf')):
            found[url] = node.findtext('s:lastmod', '', NS)
    return sorted(found, key=lambda url: (found[url], url), reverse=True)


class ArticleText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta = {}
        self.jsonld = []
        self.ld_parts = None
        self.ignored = 0
        self.started = False
        self.fragments = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta':
            key = attrs.get('property') or attrs.get('name')
            if key:
                self.meta[key] = attrs.get('content', '')
        if tag == 'script' and attrs.get('type') == 'application/ld+json':
            self.ld_parts = []
        if tag in {'script', 'style', 'noscript'}:
            self.ignored += 1
        if tag == 'h1':
            self.started = True
        if self.started and tag in {'p', 'li', 'h1', 'h2', 'h3', 'div', 'br'}:
            self.fragments.append('\n')

    def handle_endtag(self, tag):
        if tag == 'script' and self.ld_parts is not None:
            try:
                self.jsonld.append(json.loads(''.join(self.ld_parts)))
            except (ValueError, TypeError):
                pass
            self.ld_parts = None
        if tag in {'script', 'style', 'noscript'}:
            self.ignored = max(0, self.ignored - 1)
        if self.started and tag in {'p', 'li', 'h1', 'h2', 'h3', 'div'}:
            self.fragments.append('\n')

    def handle_data(self, text):
        if self.ld_parts is not None:
            self.ld_parts.append(text)
        if self.started and not self.ignored:
            self.fragments.append(text)

    def body(self):
        text = ''.join(self.fragments)
        for marker in ['Subscribe to Briefings', 'Related Tags', 'Related Insights']:
            text = text.split(marker, 1)[0]
        lines = [re.sub(r'\s+', ' ', line).strip() for line in text.splitlines()]
        return '\n'.join(line for line in lines if line)[:18000]


def declared_publication(jsonld):
    candidates = []
    def walk(value):
        if isinstance(value, dict):
            types = value.get('@type', [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(kind in {'NewsArticle', 'Article', 'BlogPosting'} for kind in types if isinstance(kind, str)) and value.get('datePublished'):
                candidates.append(str(value['datePublished']))
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(jsonld)
    # A publisher-declared timezone is preserved. A naive date is never assigned
    # a timezone or replaced by the collection time.
    for value in candidates:
        normalized = re.sub(r'([+-])(\d):(\d\d)$', r'\g<1>0\2:\3', value).replace('Z', '+00:00')
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            return parsed.isoformat(timespec='seconds'), 'date' if parsed.hour == parsed.minute == parsed.second == 0 else 'timestamp'
    return '', 'date' if candidates else 'unknown'


def parse_article(raw, url, raw_path, collected_at):
    parser = ArticleText()
    parser.feed(raw.decode('utf-8', errors='replace'))
    title = parser.meta.get('og:title', '').strip()
    body = parser.body() or parser.meta.get('description', '').strip()
    if not title or not body:
        raise ValueError('No public article title/text found')
    published_at, precision = declared_publication(parser.jsonld)
    external_id = hashlib.sha256(url.encode()).hexdigest()[:24]
    return {
        'candidate_id': SOURCE_ID + ':' + external_id,
        'source_id': SOURCE_ID, 'source_name': '高盛', 'external_id': external_id,
        'source_url': url, 'title': title, 'body': body,
        'published_at': published_at, 'published_at_precision': precision,
        'collected_at': collected_at, 'raw_path': str(Path(raw_path).resolve()),
        'content_scope': 'public_article_excerpt', 'title_origin': 'source',
    }


def crawl_research_sources(output_dir, limit_per_source=20):
    """Collect up to 20 latest-modified public articles across all topics."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    started = now_iso()
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-research'
    raw_dir = output / 'raw' / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = {'run_id': run_id, 'started_at': started, 'sources': [], 'candidates_path': str(output / 'research-candidates.jsonl')}
    source = {'source_id': SOURCE_ID, 'source_name': '高盛', 'status': 'failed', 'count': 0, 'errors': []}
    rows = []
    try:
        robots = get_public(BASE + '/robots.txt')
        (raw_dir / 'robots.txt').write_bytes(robots)
        # Respect the publisher's stated sitemap and do not enter disallowed
        # client research / Top of Mind paths.
        sitemap_url = next((line.split(':', 1)[1].strip() for line in robots.decode().splitlines() if line.lower().startswith('sitemap:')), '')
        if not sitemap_url:
            raise ValueError('Publisher did not provide a sitemap')
        index_raw = get_public(sitemap_url)
        (raw_dir / 'sitemap.xml').write_bytes(index_raw)
        root = ET.fromstring(index_raw)
        sitemaps = [node.text for node in root.findall('s:sitemap/s:loc', NS)]
        urls = []
        for number, url in enumerate(sitemaps[:3]):
            time.sleep(.25)
            raw = get_public(url)
            (raw_dir / ('sitemap-' + str(number) + '.xml')).write_bytes(raw)
            urls.extend(sitemap_articles(raw))
        for index, url in enumerate(dict.fromkeys(urls)):
            if len(rows) >= max(1, min(int(limit_per_source), 20)):
                break
            if index >= 30:
                break
            try:
                time.sleep(.3)
                raw = get_public(url)
                collected = now_iso()
                raw_path = raw_dir / ('goldman_sachs-' + str(index) + '.html')
                raw_path.write_bytes(raw)
                rows.append(parse_article(raw, url, raw_path, collected))
            except (OSError, ValueError, ET.ParseError) as error:
                source['errors'].append({'url': url, 'error': type(error).__name__ + ': ' + str(error)[:160]})
        source['count'] = len(rows)
        source['status'] = 'ok' if rows and not source['errors'] else 'partial' if rows else 'failed'
    except (OSError, ValueError, ET.ParseError) as error:
        source['errors'].append({'error': type(error).__name__ + ': ' + str(error)[:160]})
    with open(report['candidates_path'], 'w', encoding='utf-8') as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    report.update({'completed_at': now_iso(), 'candidate_count': len(rows)})
    report['sources'].append(source)
    report_path = output / ('research-run-' + run_id + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    command = argparse.ArgumentParser(description=__doc__)
    command.add_argument('--output-dir', default=str(Path(__file__).resolve().parent / 'data' / 'ingestion'))
    command.add_argument('--limit', type=int, default=20)
    args = command.parse_args()
    print(json.dumps(crawl_research_sources(args.output_dir, args.limit), ensure_ascii=False, indent=2))
