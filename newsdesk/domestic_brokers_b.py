"""Public broker research summaries distributed by Sina Finance.

The report page explicitly names the originating broker. Sina is retained as
the collection platform; no broker homepage or media mention is a report.
"""
import hashlib
import json
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE = 'https://stock.finance.sina.com.cn'
LIST_PATH = '/stock/go.php/vReport_List/kind/search/index.phtml'
MAX_BYTES = 4 * 1024 * 1024
SOURCES = {
    'institution_26': {'name': '中金公司', 'publisher_names': ['中国国际金融股份有限公司']},
    'institution_27': {'name': '长江证券', 'publisher_names': ['长江证券股份有限公司']},
    'institution_29': {'name': '开源证券', 'publisher_names': ['开源证券股份有限公司']},
    'institution_30': {'name': '华西证券', 'publisher_names': ['华西证券股份有限公司']},
    'institution_32': {'name': '华福证券', 'publisher_names': ['华福证券股份有限公司', '华福证券有限责任公司']},
    'institution_33': {'name': '华源证券', 'publisher_names': ['华源证券股份有限公司']},
    'institution_34': {'name': '西南证券', 'publisher_names': ['西南证券股份有限公司']},
    'institution_35': {'name': '华鑫证券', 'publisher_names': ['华鑫证券有限责任公司']},
    'institution_36': {'name': '招商证券', 'publisher_names': ['招商证券股份有限公司']},
    'institution_37': {'name': '东方财富证券', 'publisher_names': ['东方财富证券股份有限公司']},
    'institution_41': {'name': '银河证券', 'publisher_names': ['中国银河证券股份有限公司']},
    'institution_42': {'name': '华安证券', 'publisher_names': ['华安证券股份有限公司']},
    'institution_44': {'name': '西部证券', 'publisher_names': ['西部证券股份有限公司']},
    'institution_47': {'name': '东方证券', 'publisher_names': ['东方证券股份有限公司']},
    'institution_49': {'name': '方正证券', 'publisher_names': ['方正证券股份有限公司']},
}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1
        if tag in {'p', 'br', 'li', 'div'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'li', 'div'}:
            self.parts.append('\n')

    def handle_data(self, value):
        if not self.hidden:
            self.parts.append(value)


def plain(value):
    parser = TextOnly()
    parser.feed(str(value or ''))
    lines = [re.sub(r'[\t \xa0\u3000]+', ' ', line).strip() for line in ''.join(parser.parts).splitlines()]
    return '\n'.join(line for line in lines if line)


def list_url(**params):
    return BASE + LIST_PATH + '?' + urlencode(params, encoding='gbk')


def parse_list(text):
    rows = []
    for block in re.findall(r'<tr\b[^>]*>(.*?)</tr>', text, re.S | re.I):
        link = re.search(r'<a\b([^>]*\bhref=["\'][^"\']*vReport_Show[^"\']*["\'][^>]*)>(.*?)</a>', block, re.S | re.I)
        if not link:
            continue
        href = re.search(r'\bhref=["\']([^"\']+)', link.group(1), re.I)
        title = re.search(r'\btitle=["\']([^"\']+)', link.group(1), re.I)
        publisher = re.search(r'class=["\']fname05["\'][^>]*>\s*<span[^>]*>(.*?)</span>', block, re.S | re.I)
        date = re.search(r'<td[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</td>', block, re.I)
        url = urljoin(BASE, unescape(href.group(1))) if href else ''
        if urlsplit(url).hostname != 'stock.finance.sina.com.cn' or not publisher:
            continue
        identifier = re.search(r'/rptid/(\d+)/', url)
        if not identifier:
            continue
        rows.append({'external_id': identifier.group(1), 'source_url': url,
                     'title': unescape(title.group(1)) if title else plain(link.group(2)),
                     'publisher_name': plain(publisher.group(1)), 'published_date': date.group(1) if date else ''})
    return rows


def parse_detail(text, expected_publishers):
    heading = re.search(r'<h1\b[^>]*>(.*?)</h1>', text, re.S | re.I)
    author = re.search(r'机构：\s*<a\b[^>]*>(.*?)</a>', text, re.S | re.I)
    content = re.search(r'<div\b[^>]*class=["\']blk_container["\'][^>]*>(.*?)</div>', text, re.S | re.I)
    date = re.search(r'日期：\s*(\d{4}-\d{2}-\d{2})', text)
    title = plain(heading.group(1)) if heading else ''
    publisher = plain(author.group(1)) if author else ''
    body = plain(content.group(1)) if content else ''
    if publisher not in expected_publishers:
        raise ValueError('详情机构与来源不匹配：' + publisher)
    if not title or len(body) < 80 or body == title:
        raise ValueError('没有可读的公开研报摘要')
    return {'title': title[:400], 'body': body[:18000], 'publisher_name': publisher,
            'published_date': date.group(1) if date else '', 'published_at': '',
            'published_at_precision': 'date' if date else 'unknown'}


class Fetcher:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.last = 0
        self.counter = 0

    def get(self, url):
        if urlsplit(url).hostname != 'stock.finance.sina.com.cn':
            raise ValueError('Unexpected collection host')
        with self.lock:
            delay = .3 - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()
            self.counter += 1
            number = self.counter
        request = Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0)',
                                      'Accept': 'text/html', 'Referer': BASE + LIST_PATH})
        with urlopen(request, timeout=15) as response:
            if urlsplit(response.url).hostname != 'stock.finance.sina.com.cn':
                raise ValueError('Unexpected cross-host redirect')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Response exceeds 4 MiB')
            content_type = response.headers.get('Content-Type', '')
            final_url = response.url
        # The publisher explicitly declares gb2312; GB18030 is a superset.
        text = raw.decode('gb18030', errors='strict')
        collected = now_iso()
        path = self.directory / ('sina-' + str(number) + '.json')
        path.write_text(json.dumps({'request_url': url, 'response_url': final_url, 'collected_at': collected,
                                   'content_type': content_type, 'encoding': 'gb18030',
                                   'sha256': hashlib.sha256(raw).hexdigest(), 'raw_response': text}, ensure_ascii=False), encoding='utf-8')
        return text, path, collected


def crawl_sina_brokers(output_dir, specs, limit_per_source=5, filename='broker-b-candidates.jsonl'):
    """Collect explicit broker-authored public summaries, including other topics.

    A topic query chooses a research channel, never decides relevance. General
    newest reports fill remaining slots. The classification agent reviews all.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-sina-' + uuid.uuid4().hex[:6]
    fetcher = Fetcher(output / 'raw' / run_id)
    report = {'run_id': run_id, 'started_at': now_iso(), 'collection_platform': '新浪财经研报', 'sources': []}
    limit = max(1, min(int(limit_per_source), 20))
    topics, topic_errors = [], []
    for query in ['原油', '石油', '宏观', '炼化', '航运']:
        url = list_url(t1='5', title=query)
        try:
            text, raw_path, collected = fetcher.get(url)
            topics.extend(parse_list(text))
        except (OSError, ValueError, UnicodeError) as error:
            topic_errors.append({'url': url, 'error': type(error).__name__ + ': ' + str(error)[:180]})
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()

    def one(entry):
        source_id, spec = entry
        names = spec['publisher_names']
        status = {'source_id': source_id, 'source_name': spec['name'], 'status': 'empty', 'count': 0,
                  'errors': [], 'entry_urls': [], 'collection_platform': '新浪财经研报',
                  'publisher_names': names, 'notes': ['发布时间仅有日期，published_at 留空，不伪造时分。']}
        found = [dict(row, discovery='公开专题检索') for row in topics if row['publisher_name'] in names]
        for publisher in names:
            url = list_url(t1='1', orgname=publisher)
            status['entry_urls'].append(url)
            try:
                text, _, _ = fetcher.get(url)
                found.extend(dict(row, discovery='机构最新研报列表') for row in parse_list(text) if row['publisher_name'] in names)
            except (OSError, ValueError, UnicodeError) as error:
                status['errors'].append({'url': url, 'error': type(error).__name__ + ': ' + str(error)[:180]})
        unique = {}
        for row in found:
            unique.setdefault(row['external_id'], row)
        def order(row):
            try:
                age = (today - datetime.strptime(row['published_date'], '%Y-%m-%d').date()).days
            except ValueError:
                age = 10000
            # Prefer current topic material, then current general reports. Older
            # real research can prove source coverage but is disclosed as history.
            return (age < 0 or age > 30, row['discovery'] != '公开专题检索', -int(row['published_date'].replace('-', '') or '0'))
        selected = sorted(unique.values(), key=order)
        candidates = []
        for row in selected[:max(12, limit * 3)]:
            if len(candidates) >= limit:
                break
            try:
                if row['published_date'] and (today - datetime.strptime(row['published_date'], '%Y-%m-%d').date()).days > 180:
                    continue
                text, raw_path, collected = fetcher.get(row['source_url'])
                parsed = parse_detail(text, names)
                candidate = dict(parsed, candidate_id=source_id + ':sina:' + row['external_id'], source_id=source_id,
                                 source_name=spec['name'], external_id='sina:' + row['external_id'], source_url=row['source_url'],
                                 collected_at=collected, raw_path=str(raw_path.resolve()),
                                 collection_platform='新浪财经研报', content_scope='public_research_summary', discovery=row['discovery'])
                candidates.append(candidate)
            except (OSError, ValueError, UnicodeError) as error:
                status['errors'].append({'url': row['source_url'], 'error': type(error).__name__ + ': ' + str(error)[:180]})
        status['count'] = len(candidates)
        status['status'] = 'ok' if candidates and not status['errors'] else 'partial' if candidates else 'error' if status['errors'] else 'empty'
        dates = sorted(row['published_date'] for row in candidates if row['published_date'])
        status['coverage'] = {'earliest_publication_date': dates[0] if dates else '', 'latest_publication_date': dates[-1] if dates else '',
                              'recent_30_days': sum(0 <= (today - datetime.strptime(d, '%Y-%m-%d').date()).days <= 30 for d in dates)}
        return status, candidates

    with ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(one, specs.items()))
    candidates = []
    for status, rows in runs:
        report['sources'].append(status)
        candidates.extend(rows)
    destination = output / filename
    destination.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in candidates), encoding='utf-8')
    report.update(completed_at=now_iso(), candidate_count=len(candidates), candidates_path=str(destination),
                  raw_directory=str(fetcher.directory), topic_query_errors=topic_errors)
    report_path = output / (Path(filename).stem.replace('-candidates', '') + '-run-' + run_id + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def parse_founder_detail(text):
    def fragment(name):
        match = re.search(r'<div\b[^>]*class=["\']' + name + r'["\'][^>]*>(.*?)</div>', text, re.S | re.I)
        return plain(match.group(1)) if match else ''
    title = fragment('k-infor_d-2')
    body = fragment('k-infor_d-3')
    date = re.search(r'时间：\s*(\d{4}-\d{2}-\d{2})', text)
    if '方正证券研究所' not in text or not title or len(body) < 80:
        raise ValueError('方正官网没有公开可读的研究摘要或归属声明')
    return {'title': title[:400], 'body': body[:18000], 'publisher_name': '方正证券股份有限公司',
            'published_at': '', 'published_date': date.group(1) if date else '',
            'published_at_precision': 'date' if date else 'unknown'}


def crawl_founder_public(output_dir, limit=5):
    """Official Founder public summaries; no client report area is entered."""
    limit = max(1, min(int(limit), 20))
    output = Path(output_dir).resolve()
    raw_dir = output / 'raw' / ('founder-' + uuid.uuid4().hex[:12])
    raw_dir.mkdir(parents=True, exist_ok=True)
    origin = 'https://gwlink.foundersc.com'
    listing = origin + '/infoResearchMacro/index.jhtml'
    status = {'source_id': 'institution_49', 'source_name': '方正证券', 'status': 'empty', 'count': 0,
              'errors': [], 'entry_urls': [listing], 'collection_platform': '方正证券官网',
              'publisher_names': ['方正证券股份有限公司'], 'notes': ['公开研究摘要，单日期不虚构发布时间。']}
    counter = 0
    def fetch(url):
        nonlocal counter
        if urlsplit(url).hostname != 'gwlink.foundersc.com':
            raise ValueError('Unexpected Founder source host')
        time.sleep(.3)
        with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0)',
                                          'Referer': listing}), timeout=15) as response:
            if urlsplit(response.url).hostname != 'gwlink.foundersc.com':
                raise ValueError('Unexpected Founder redirect')
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Response exceeds 4 MiB')
        text = raw.decode('utf-8')
        counter += 1
        path = raw_dir / ('founder-' + str(counter) + '.json')
        collected = now_iso()
        path.write_text(json.dumps({'request_url': url, 'collected_at': collected, 'encoding': 'utf-8',
                                    'sha256': hashlib.sha256(raw).hexdigest(), 'raw_response': text}, ensure_ascii=False), encoding='utf-8')
        return text, path, collected
    candidates = []
    try:
        text, _, _ = fetch(listing)
        links = {}
        for href, title in re.findall(r'<a\b[^>]*href=["\'](/infoResearchMacro/\d+\.jhtml)["\'][^>]*>(.*?)</a>', text, re.S | re.I):
            links.setdefault(href, plain(title))
        # Topic ordering chooses a source research channel, not acceptance. All
        # captured summaries still require independent agent relevance review.
        ordered = sorted(links, key=lambda href: not bool(re.search('宏观|政策|能源|原油|石油|化工|利率|经济|中期选举|航运', links[href])))
        for href in ordered:
            if len(candidates) >= limit:
                break
            try:
                url = origin + href
                content, path, collected = fetch(url)
                data = parse_founder_detail(content)
                identifier = re.search(r'/(\d+)\.jhtml', href).group(1)
                candidates.append(dict(data, candidate_id='institution_49:founder:' + identifier,
                                       source_id='institution_49', source_name='方正证券', external_id='founder:' + identifier,
                                       source_url=url, collected_at=collected, raw_path=str(path.resolve()),
                                       collection_platform='方正证券官网', content_scope='public_research_summary'))
            except (OSError, ValueError, UnicodeError) as error:
                status['errors'].append({'url': href, 'error': type(error).__name__ + ': ' + str(error)[:180]})
    except (OSError, ValueError, UnicodeError) as error:
        status['errors'].append({'url': listing, 'error': type(error).__name__ + ': ' + str(error)[:180]})
    status['count'] = len(candidates)
    status['status'] = 'ok' if candidates and not status['errors'] else 'partial' if candidates else 'error' if status['errors'] else 'empty'
    dates = sorted(row['published_date'] for row in candidates if row['published_date'])
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    status['coverage'] = {'earliest_publication_date': dates[0] if dates else '', 'latest_publication_date': dates[-1] if dates else '',
                          'recent_30_days': sum(0 <= (today - datetime.strptime(d, '%Y-%m-%d').date()).days <= 30 for d in dates)}
    return status, candidates


def crawl_domestic_brokers_b(output_dir, source_ids=None, limit_per_source=5):
    selected = list(dict.fromkeys(source_ids if source_ids is not None else SOURCES))
    unknown = [source for source in selected if source not in SOURCES]
    if unknown:
        raise ValueError('未知券商 ID：' + ', '.join(unknown))
    report = crawl_sina_brokers(output_dir, {key: SOURCES[key] for key in selected if key != 'institution_49'}, limit_per_source)
    if 'institution_49' in selected:
        status, candidates = crawl_founder_public(output_dir, limit_per_source)
        report['sources'].append(status)
        with Path(report['candidates_path']).open('a', encoding='utf-8') as destination:
            for candidate in candidates:
                destination.write(json.dumps(candidate, ensure_ascii=False) + '\n')
        report['candidate_count'] += len(candidates)
        report['completed_at'] = now_iso()
        Path(report['report_path']).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
