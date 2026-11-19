"""Attributed public broker research distributed by Eastmoney or Sina.

The collection platform is kept separately from the broker author. Only public
research text with an explicitly matching institution is emitted, never a broker
homepage, a mere media mention, a PDF preview or a login prompt.
"""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from newsdesk.domestic_brokers_b import crawl_sina_brokers

SOURCES = {
    'institution_11': {'name': '东吴证券', 'publisher_names': ['东吴证券股份有限公司'], 'eastmoney_org': '80000031'},
    'institution_12': {'name': '华创证券', 'publisher_names': ['华创证券有限责任公司']},
    'institution_13': {'name': '国泰海通', 'publisher_names': ['国泰海通证券股份有限公司']},
    'institution_14': {'name': '天风证券', 'publisher_names': ['天风证券股份有限公司']},
    'institution_15': {'name': '华泰证券', 'publisher_names': ['华泰证券股份有限公司']},
    'institution_16': {'name': '浙商证券', 'publisher_names': ['浙商证券股份有限公司']},
    'institution_17': {'name': '中信建投', 'publisher_names': ['中信建投证券股份有限公司']},
    'institution_18': {'name': '兴业证券', 'publisher_names': ['兴业证券股份有限公司']},
    'institution_19': {'name': '申万宏源', 'publisher_names': ['上海申银万国证券研究所有限公司', '申万宏源证券有限公司']},
    'institution_20': {'name': '中泰证券', 'publisher_names': ['中泰证券股份有限公司']},
    'institution_21': {'name': '国信证券', 'publisher_names': ['国信证券股份有限公司'], 'eastmoney_org': '80000007'},
    'institution_22': {'name': '国联民生证券', 'publisher_names': ['国联民生证券股份有限公司']},
    'institution_23': {'name': '中信证券', 'publisher_names': ['中信证券股份有限公司']},
    'institution_24': {'name': '国金证券', 'publisher_names': ['国金证券股份有限公司'], 'eastmoney_org': '10000082'},
    'institution_25': {'name': '广发证券', 'publisher_names': ['广发证券股份有限公司']},
}
MAX_BYTES = 4 * 1024 * 1024
EM_BASE = 'https://data.eastmoney.com'


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class EastmoneyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.capture = None
        self.depth = 0
        self.hidden = 0
        self.parts = {'body': [], 'title': [], 'publisher_header': []}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.capture:
            if tag not in {'br', 'img', 'hr', 'input', 'meta', 'link'}:
                self.depth += 1
            if tag in {'script', 'style'}:
                self.hidden += 1
            if tag in {'p', 'br', 'li', 'div'}:
                self.parts[self.capture].append('\n')
        elif attrs.get('id') == 'ctx-content':
            self.capture, self.depth = 'body', 1
        elif attrs.get('id') == 'zw-title':
            self.capture, self.depth = 'title', 1
        elif 'c-infos' in attrs.get('class', '').split():
            self.capture, self.depth = 'publisher_header', 1

    def handle_endtag(self, tag):
        if not self.capture:
            return
        if tag in {'br', 'img', 'hr', 'input', 'meta', 'link'}:
            return
        if tag in {'script', 'style'}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {'p', 'li', 'div'}:
            self.parts[self.capture].append('\n')
        self.depth -= 1
        if self.depth <= 0:
            self.capture = None

    def handle_data(self, data):
        if self.capture and not self.hidden:
            self.parts[self.capture].append(data)

    def text(self, key):
        lines = [re.sub(r'[\t \u3000\xa0]+', ' ', line).strip() for line in ''.join(self.parts[key]).splitlines()]
        return '\n'.join(line for line in lines if line)


def parse_eastmoney_detail(text, expected_name):
    parser = EastmoneyText()
    parser.feed(text)
    title, body = parser.text('title'), parser.text('body')
    header = parser.text('publisher_header')
    # The author must be in the article header, not in related-report navigation.
    if expected_name not in header.splitlines():
        raise ValueError('公开详情的作者机构与列表机构不匹配')
    if not title or len(body) < 80 or body == title or '登录后查看' in body:
        raise ValueError('没有可读取的公开研究正文或摘要')
    return title[:400], body[:18000]


class Fetcher:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last = 0
        self.sequence = 0

    def get(self, url, post=None):
        allowed = {'data.eastmoney.com', 'reportapi.eastmoney.com', 'research.citics.com'}
        if urlsplit(url).hostname not in allowed:
            raise ValueError('Unexpected public research host')
        delay = .3 - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.last = time.monotonic()
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0)',
                   'Accept': 'application/json,text/html', 'Referer': EM_BASE + '/report/macresearch.jshtml'}
        payload = None
        if post is not None:
            payload = json.dumps(post).encode('utf-8')
            headers['Content-Type'] = 'application/json'
            headers['Referer'] = 'https://research.citics.com/home'
        request = Request(url, data=payload, headers=headers)
        with urlopen(request, timeout=15) as response:
            if urlsplit(response.url).hostname not in allowed:
                raise ValueError('Unexpected cross-host redirect')
            raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Response exceeds 4 MiB')
            decoded = raw.decode('utf-8')
            final_url = response.url
        self.sequence += 1
        path = self.directory / ('broker-a-public-' + str(self.sequence) + '.json')
        collected = now_iso()
        path.write_text(json.dumps({'request_url': url, 'response_url': final_url,
            'request_method': 'POST' if post is not None else 'GET', 'request_body': post,
            'collected_at': collected, 'sha256': hashlib.sha256(raw).hexdigest(),
            'raw_response': decoded}, ensure_ascii=False), encoding='utf-8')
        return decoded, path.resolve(), collected


def parse_citic_index(text, raw_path, collected_at, limit=5):
    payload = json.loads(text)
    if payload.get('code') != 0 or not isinstance(payload.get('data'), dict):
        raise ValueError('中信官网未返回公开首页数据')
    found = {}
    def walk(value):
        if isinstance(value, dict):
            title, body = value.get('psTitle'), value.get('psCoreView')
            identifier = value.get('id')
            if identifier and isinstance(title, str) and isinstance(body, str) and len(body.strip()) >= 80:
                existing = found.get(str(identifier))
                if existing is None or len(body) > len(existing.get('psCoreView', '')):
                    found[str(identifier)] = value
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(payload['data'])
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    cutoff = (today - timedelta(days=30)).isoformat()
    rows = []
    for identifier, item in sorted(found.items(), key=lambda pair: str(pair[1].get('rptDate', '')), reverse=True):
        date = str(item.get('rptDate') or '')[:10]
        # The public homepage features some old strategy documents; never pass
        # these off as current news or replace their dates with collection time.
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date) or date < cutoff:
            continue
        rows.append({'candidate_id': 'institution_23:citic:' + identifier,
            'source_id': 'institution_23', 'source_name': '中信证券', 'external_id': 'citic:' + identifier,
            'title': item['psTitle'].strip()[:400], 'body': item['psCoreView'].strip()[:18000],
            'source_url': 'https://research.citics.com/detail?' + urlencode({'rid': identifier}),
            'source_listing_url': 'https://research.citics.com/home',
            'published_at': '', 'published_date': date, 'published_at_precision': 'date',
            'collected_at': collected_at, 'raw_path': str(raw_path),
            'content_scope': 'public_research_summary', 'collection_platform': '中信证券研究官网公开首页',
            'publisher_name': '中信证券', 'authors': [x.get('userName', '') for x in item.get('authorList', []) if isinstance(x, dict)],
            'detail_requires_login': True,
            'content_note': '仅采集无需登录首页已公开的核心观点摘要；原报告详情需要登录，未请求受限正文。'})
        if len(rows) >= limit:
            break
    return rows


def crawl_citic_home(fetcher, limit=5):
    url = 'https://research.citics.com/default/api/index'
    status = {'source_id': 'institution_23', 'source_name': '中信证券', 'status': 'failed', 'count': 0,
              'entry_urls': ['https://research.citics.com/home', url], 'errors': [],
              'collection_platform': '中信证券研究官网公开首页',
              'notes': ['仅公开核心观点摘要，详情及完整研报需要登录；未访问受限全文。']}
    try:
        text, path, collected = fetcher.get(url, post={})
        rows = parse_citic_index(text, path, collected, limit)
        status['count'] = len(rows)
        status['status'] = 'ok' if rows else 'empty'
        dates = [item['published_date'] for item in rows if item.get('published_date')]
        status['coverage'] = {'earliest_publication_date': min(dates) if dates else '',
                              'latest_publication_date': max(dates) if dates else '', 'recent_30_days': len(dates)}
        return rows, status
    except (OSError, ValueError) as error:
        status['errors'].append(type(error).__name__ + ': ' + str(error)[:250])
        return [], status


def crawl_eastmoney(source_id, spec, fetcher, limit):
    today = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))).date()
    params = {'qType': 3, 'orgCode': spec['eastmoney_org'], 'pageSize': 30, 'pageNo': 1,
              'beginTime': (today - timedelta(days=30)).isoformat(), 'endTime': today.isoformat()}
    url = 'https://reportapi.eastmoney.com/report/jg?' + urlencode(params)
    status = {'source_id': source_id, 'source_name': spec['name'], 'status': 'failed', 'count': 0,
              'entry_urls': [url], 'errors': [], 'collection_platform': '东方财富研报中心'}
    candidates = []
    try:
        text, discovery_path, _ = fetcher.get(url)
        payload = json.loads(text)
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise ValueError('研报列表未返回数据数组')
        for row in rows:
            if len(candidates) >= limit:
                break
            if (row.get('orgCode') != spec['eastmoney_org']
                    or row.get('orgSName') != spec['name']
                    or row.get('orgName') not in spec['publisher_names']):
                continue
            try:
                encoded = row.get('encodeUrl')
                if not encoded:
                    raise ValueError('缺少公开详情链接')
                source_url = EM_BASE + '/report/zw_macresearch.jshtml?' + urlencode({'encodeUrl': encoded})
                detail, path, collected = fetcher.get(source_url)
                title, body = parse_eastmoney_detail(detail, spec['name'])
                identifier = str(row.get('id') or '')
                if not identifier:
                    raise ValueError('缺少稳定报告ID')
                date = str(row.get('publishDate') or '')[:10]
                if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', date):
                    date = ''
                candidates.append({'candidate_id': source_id + ':eastmoney:' + identifier,
                    'source_id': source_id, 'source_name': spec['name'], 'external_id': 'eastmoney:' + identifier,
                    'source_url': source_url, 'title': title, 'body': body,
                    'published_at': '', 'published_date': date, 'published_at_precision': 'date' if date else 'unknown',
                    'collected_at': collected, 'raw_path': str(path), 'discovery_raw_path': str(discovery_path),
                    'content_scope': 'public_research_summary', 'collection_platform': '东方财富研报中心',
                    'publisher_name': row['orgName'], 'authors': row.get('researcher', ''),
                    'source_listing_url': url})
            except (OSError, ValueError) as error:
                status['errors'].append(str(error)[:250])
        status['count'] = len(candidates)
        status['status'] = 'ok' if candidates and not status['errors'] else 'partial' if candidates else 'empty'
        dates = [item['published_date'] for item in candidates if item.get('published_date')]
        status['coverage'] = {'earliest_publication_date': min(dates) if dates else '',
                              'latest_publication_date': max(dates) if dates else '', 'recent_30_days': len(dates)}
    except (OSError, ValueError) as error:
        status['errors'].append(type(error).__name__ + ': ' + str(error)[:250])
    return candidates, status


def crawl_domestic_brokers_a(output_dir, source_ids=None, limit_per_source=5):
    selected = list(source_ids) if source_ids is not None else list(SOURCES)
    if any(source not in SOURCES for source in selected):
        raise ValueError('未知A组券商来源')
    if not isinstance(limit_per_source, int) or not 1 <= limit_per_source <= 20:
        raise ValueError('每源上限应为1–20')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-broker-a-' + uuid.uuid4().hex[:6]
    fetcher = Fetcher(output / 'raw' / run_id)
    candidates, statuses = [], []
    fallback = {}
    for source_id in selected:
        spec = SOURCES[source_id]
        if source_id == 'institution_23':
            rows, status = crawl_citic_home(fetcher, limit_per_source)
            if rows:
                candidates.extend(rows)
                statuses.append(status)
                continue
        if spec.get('eastmoney_org'):
            rows, status = crawl_eastmoney(source_id, spec, fetcher, limit_per_source)
            if rows:
                candidates.extend(rows)
                statuses.append(status)
                continue
        fallback[source_id] = spec
    if fallback:
        report = crawl_sina_brokers(output, fallback, limit_per_source=limit_per_source,
                                   filename='broker-a-sina-candidates.jsonl')
        candidates += [json.loads(line) for line in Path(report['candidates_path']).read_text(encoding='utf-8').split('\n') if line]
        statuses += report['sources']
    seen = set()
    candidates = [item for item in candidates if not (item['candidate_id'] in seen or seen.add(item['candidate_id']))]
    path = output / 'broker-a-candidates.jsonl'
    path.write_text(''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in candidates), encoding='utf-8')
    result = {'run_id': run_id, 'completed_at': now_iso(), 'candidate_count': len(candidates),
              'candidates_path': str(path), 'sources': statuses}
    report_path = output / (run_id + '-report.json')
    result['report_path'] = str(report_path)
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    return result

