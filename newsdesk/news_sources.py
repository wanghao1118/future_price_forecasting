"""Fetch public first-party news feeds before any relevance/classification step.

No keyword acceptance rules, authentication workarounds or search snippets are
used here. Each candidate points to its original public source and raw response.
"""
import argparse
import concurrent.futures
import hashlib
import html
import json
import re
import threading
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SOURCES = {
    'jin10': {'name': '金十数据', 'page': 'https://www.jin10.com/', 'encoding': 'utf-8'},
    'cls': {'name': '财联社', 'page': 'https://www.cls.cn/telegraph', 'encoding': 'utf-8'},
    'jiemian': {'name': '界面快报', 'page': 'https://www.jiemian.com/lists/4.html', 'encoding': 'utf-8'},
    'stcn': {'name': '证券时报', 'page': 'https://www.stcn.com/article/list/kx.html', 'encoding': 'utf-8'},
    'ths': {'name': '同花顺', 'page': 'https://news.10jqka.com.cn/realtimenews.html', 'encoding': 'gbk'},
}


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class TextOnly(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.suppressed += 1
        if tag in {'p', 'br', 'div', 'li'}:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in {'script', 'style'} and self.suppressed:
            self.suppressed -= 1
        if tag in {'p', 'div', 'li'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.suppressed:
            self.parts.append(data)


def text_only(value):
    parser = TextOnly()
    parser.feed(str(value or ''))
    return re.sub(r'\n[ \t]*\n+', '\n', ''.join(parser.parts)).strip()


def timestamp(value, *, milliseconds=False, local=False):
    if value in {None, ''}:
        return ''
    try:
        if local:
            stamp = datetime.strptime(str(value), '%Y/%m/%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Shanghai'))
        else:
            stamp = datetime.fromtimestamp(float(value) / (1000 if milliseconds else 1), timezone.utc)
        return stamp.isoformat(timespec='seconds')
    except (ValueError, TypeError, OverflowError, OSError):
        return ''


def canonical_url(value, base):
    if not value:
        return ''
    parsed = urlsplit(urljoin(base, html.unescape(str(value or ''))))
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc:
        return ''
    # These first-party links are public over HTTPS, including THS's legacy HTTP URLs.
    return urlunsplit(('https', parsed.netloc.lower(), parsed.path, parsed.query, ''))


def candidate(source_id, row, raw_path, collected_at):
    spec = SOURCES[source_id]
    if source_id == 'cls':
        external_id = str(row.get('id') or '')
        title = text_only(row.get('title'))
        body = text_only(row.get('content') or row.get('brief'))
        source_url = 'https://www.cls.cn/detail/' + external_id
        published = timestamp(row.get('ctime'))
    elif source_id == 'jiemian':
        external_id = str(row.get('id') or '')
        title = text_only(row.get('title'))
        body = text_only(row.get('summary'))
        source_url = 'https://www.jiemian.com/article/' + external_id + '.html'
        published = timestamp(row.get('publishtime'))
    elif source_id == 'stcn':
        external_id = str(row.get('id') or '')
        title = text_only(row.get('title'))
        body = text_only(row.get('content'))
        source_url = canonical_url(row.get('web_url') or row.get('url'), spec['page'])
        published = timestamp(row.get('time'), milliseconds=True)
    elif source_id == 'jin10':
        data = row.get('data') or {}
        if (row.get('extras') or {}).get('ad') or data.get('vip_level', 0):
            return None
        external_id = str(row.get('id') or '')
        title = text_only(data.get('title'))
        body = text_only(data.get('content'))
        source_url = 'https://flash.jin10.com/detail/' + external_id
        try:
            published = datetime.strptime(str(row.get('time')), '%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
        except (TypeError, ValueError):
            published = ''
    else:
        external_id = str(row.get('seq') or '')
        title = text_only(row.get('title'))
        body = text_only(row.get('content'))
        source_url = canonical_url(row.get('url') or row.get('curl'), spec['page'])
        published = timestamp(row.get('pubDate'), local=True)
    if not external_id or not (title or body) or not source_url:
        return None
    title_origin = 'source' if title else 'excerpt'
    if not title:
        title = re.split(r'[。\n]', body, maxsplit=1)[0][:160]
    return {
        'candidate_id': source_id + ':' + external_id,
        'source_id': source_id, 'source_name': spec['name'],
        'external_id': external_id, 'source_url': source_url,
        'title': title[:400], 'body': body[:20000],
        'published_at': published, 'collected_at': collected_at,
        'raw_path': str(Path(raw_path).resolve()), 'content_scope': 'public_flash',
        'title_origin': title_origin,
    }


def parse_response(source_id, raw):
    if source_id == 'ths':
        # The public feed is a JavaScript assignment, but its item array is JSON.
        # Parse only that array, never execute or evaluate publisher code.
        match = re.search(r'\bitem\s*:\s*', raw)
        if not match:
            raise ValueError('同花顺公共 feed 没有有效 item 数组')
        rows, _ = json.JSONDecoder().raw_decode(raw[match.end():].lstrip())
    else:
        data = json.loads(raw)
        if source_id == 'cls':
            if data.get('errno') != 0:
                raise ValueError('财联社返回错误')
            rows = data.get('data', {}).get('roll_data')
        elif source_id == 'jiemian':
            if str(data.get('code')) != '0':
                raise ValueError('界面快报返回错误')
            rows = data.get('result', {}).get('list')
        elif source_id == 'jin10':
            if data.get('status') != 200:
                raise ValueError('金十返回错误')
            rows = data.get('data')
        else:
            if data.get('state') != 1:
                raise ValueError('证券时报返回错误')
            rows = data.get('data')
    if not isinstance(rows, list):
        raise ValueError('上游没有返回新闻数组')
    return [row for row in rows if isinstance(row, dict)]


class Fetcher:
    def __init__(self, raw_dir):
        self.raw_dir = Path(raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.last_request = {}
        self.lock = threading.Lock()
        self.sequence = {}

    def get(self, source_id, url):
        # At most one request per source per 350 ms; source workers run separately.
        wait = .35 - (time.monotonic() - self.last_request.get(source_id, 0))
        if wait > 0:
            time.sleep(wait)
        self.last_request[source_id] = time.monotonic()
        request = Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0; local research)',
            'Accept': 'application/json,text/javascript,text/html;q=0.8',
            'Referer': SOURCES[source_id]['page'],
            'X-Requested-With': 'XMLHttpRequest',
        })
        if source_id == 'jin10':
            # Public website application headers, copied from its own frontend bundle;
            # no user token, cookie or authenticated endpoint is used.
            request.add_header('x-app-id', 'bVBF4FyRTn5NJF5n')
            request.add_header('x-version', '1.0.0')
        with urlopen(request, timeout=18) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError('响应超过 4 MiB 限制')
            encoding = response.headers.get_content_charset() or SOURCES[source_id]['encoding']
            final_url = response.url
            content_type = response.headers.get('Content-Type', '')
        number = self.sequence.get(source_id, 0) + 1
        self.sequence[source_id] = number
        raw_path = self.raw_dir / (source_id + '-' + str(number) + '.json')
        collected = utc_now()
        decoded = raw.decode(encoding, errors='strict')
        # raw_response is the exact decoded upstream response, not a generated article.
        raw_path.write_text(json.dumps({
            'request_url': url, 'response_url': final_url,
            'content_type': content_type, 'encoding': encoding,
            'collected_at': collected, 'sha256': hashlib.sha256(raw).hexdigest(),
            'raw_response': decoded,
        }, ensure_ascii=False), encoding='utf-8')
        return decoded, raw_path, collected


def _crawl_one(source_id, fetcher, limit):
    result = {'source_id': source_id, 'source_name': SOURCES[source_id]['name'],
              'status': 'ok', 'candidates': [], 'pages': 0, 'error': ''}
    cursor = int(time.time())
    stcn_cursor = {}
    jin10_cursor = {}
    seen = set()
    max_pages = min(30, (limit + 19) // 20 + 1)
    try:
        for page in range(1, max_pages + 1):
            if source_id == 'cls':
                url = 'https://www.cls.cn/api/cache?' + urlencode({'rn': 20, 'lastTime': cursor, 'name': 'telegraph'})
            elif source_id == 'jiemian':
                url = 'https://papi.jiemian.com/page/api/kuaixun/getlistmore?' + urlencode({'cid': '1323kb', 'start_time': cursor, 'page': page, 'tagid': '1323'})
            elif source_id == 'stcn':
                url = 'https://www.stcn.com/article/list.html?' + urlencode(dict({'type': 'kx'}, **stcn_cursor))
            elif source_id == 'jin10':
                url = 'https://flash-api.jin10.com/get_flash_list?' + urlencode(dict({'channel': '-8200'}, **jin10_cursor))
            else:
                url = 'https://stock.10jqka.com.cn/thsgd/ywjh.js'
            raw, raw_path, collected = fetcher.get(source_id, url)
            rows = parse_response(source_id, raw)
            result['pages'] += 1
            added = 0
            for row in rows:
                record = candidate(source_id, row, raw_path, collected)
                if record and record['candidate_id'] not in seen:
                    seen.add(record['candidate_id'])
                    result['candidates'].append(record)
                    added += 1
                    if len(result['candidates']) >= limit:
                        break
            if not rows or not added or len(result['candidates']) >= limit or source_id == 'ths':
                break
            if source_id == 'jin10':
                dates = [row.get('time') for row in rows if row.get('time')]
                if not dates:
                    break
                jin10_cursor = {'max_time': min(dates)}
            if source_id == 'stcn':
                metadata = json.loads(raw)
                stcn_cursor = {key: metadata[key] for key in ('page_time', 'last_time') if metadata.get(key) is not None}
                if not stcn_cursor:
                    break
            if source_id == 'cls':
                cursor = min(int(row['ctime']) for row in rows if row.get('ctime'))
            elif source_id == 'jiemian':
                cursor = min(int(row['publishtime']) for row in rows if row.get('publishtime'))
    except Exception as error:
        result['status'] = 'partial' if result['candidates'] else 'error'
        result['error'] = '{}: {}'.format(type(error).__name__, str(error)[:300])
    result['count'] = len(result['candidates'])
    if not result['count'] and result['status'] == 'ok':
        result['status'] = 'empty'
    return result


def crawl_sources(output_dir, source_ids=None, limit_per_source=100):
    """Write raw responses, candidates.jsonl and crawl_report.json; return report.

    Crawling never accepts/discards a story based on oil keywords. Agent decisions
    are a separate, auditable second phase in the ingestion workflow.
    """
    selected = list(dict.fromkeys(source_ids or SOURCES))
    if any(source_id not in SOURCES for source_id in selected):
        raise ValueError('未实现的信源适配器：' + ', '.join(source_id for source_id in selected if source_id not in SOURCES))
    if not isinstance(limit_per_source, int) or not 1 <= limit_per_source <= 500:
        raise ValueError('limit_per_source 必须为 1–500')
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    fetcher = Fetcher(destination / 'raw' / run_id)
    started_at = utc_now()
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        runs = list(pool.map(lambda source: _crawl_one(source, fetcher, limit_per_source), selected))
    candidates = []
    seen = set()
    for run in runs:
        for row in run.pop('candidates'):
            if row['candidate_id'] not in seen:
                seen.add(row['candidate_id'])
                candidates.append(row)
    path = destination / 'candidates.jsonl'
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in candidates), encoding='utf-8')
    report = {'run_id': run_id, 'started_at': started_at, 'finished_at': utc_now(),
              'sources': runs, 'candidate_count': len(candidates),
              'candidates_path': str(path), 'raw_directory': str(fetcher.raw_dir)}
    (destination / 'crawl_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def parse_cls_page(raw, detail=False):
    match = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not match:
        raise ValueError('财联社公开页面未返回可解析的正文数据')
    props = json.loads(match.group(1))['props']['pageProps']
    if detail:
        value = props.get('articleDetail')
        if not isinstance(value, dict) or not value.get('id'):
            raise ValueError('公开详情没有新闻正文')
        return value
    data = props.get('data', {})
    rows = data.get('articles')
    if not isinstance(rows, list):
        raise ValueError('公开频道没有新闻列表')
    return rows


def crawl_oil_supplement(output_dir, max_age_hours=48):
    """Discover recent stories in first-party topic feeds, then fetch public bodies.

    Topic membership is only a discovery hint. Every resulting candidate still
    requires an agent relevance decision; topic labels are not archive categories.
    Existing candidates.jsonl is deliberately left untouched.
    """
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-topics'
    fetcher = Fetcher(destination / 'raw' / run_id)
    started = utc_now()
    cutoff = time.time() - max_age_hours * 3600
    candidates = {}
    runs = []
    topics = [('1583', '原油市场动态'), ('1101', '能源行业新闻'),
              ('10986', '中东冲突'), ('11394', '能源类期货'), ('1515', '石油化工')]
    for topic_id, name in topics:
        status = {'source_id':'cls', 'topic_id':topic_id, 'topic_name':name,
                  'status':'ok', 'count':0, 'error':''}
        try:
            raw, raw_path, collected = fetcher.get('cls', 'https://www.cls.cn/subject/' + topic_id)
            for row in parse_cls_page(raw):
                if not row.get('article_id') or float(row.get('article_time') or 0) < cutoff:
                    continue
                identifier = 'cls:' + str(row['article_id'])
                if identifier in candidates:
                    continue
                detail_raw_path = raw_path
                publication = row.get('article_time')
                title = text_only(row.get('article_title'))
                body = title if row.get('article_type') == -1 else text_only(row.get('article_brief') or row.get('article_guide_text'))
                scope = 'public_flash' if row.get('article_type') == -1 else 'public_article_excerpt'
                content_error = ''
                # Long-form pages are only used when their public payload says free.
                if row.get('article_type') != -1:
                    try:
                        detail_raw, detail_raw_path, detail_collected = fetcher.get('cls', 'https://www.cls.cn/detail/' + str(row['article_id']))
                        detail = parse_cls_page(detail_raw, detail=True)
                        title = text_only(detail.get('title')) or title
                        if detail.get('isFree') is True and detail.get('content'):
                            body = text_only(detail['content'])
                            scope = 'public_article'
                        else:
                            body = text_only(detail.get('brief')) or body
                        publication = detail.get('ctime') or publication
                    except Exception as error:
                        detail_raw_path = raw_path
                        content_error = type(error).__name__
                record = candidate('cls', {'id': row['article_id'], 'title': title,
                                           'content': body or title, 'ctime': publication},
                                   detail_raw_path, json.loads(detail_raw_path.read_text(encoding='utf-8'))['collected_at'])
                record['content_scope'] = scope
                record['discovery_topic'] = name
                record['discovery_raw_path'] = str(raw_path.resolve())
                if content_error:
                    record['content_fetch_error'] = content_error
                candidates[identifier] = record
                status['count'] += 1
        except Exception as error:
            status['status'] = 'error'
            status['error'] = '{}: {}'.format(type(error).__name__, str(error)[:250])
        runs.append(status)
    path = destination / 'supplement-candidates.jsonl'
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in candidates.values()), encoding='utf-8')
    report = {'run_id': run_id, 'started_at': started, 'finished_at': utc_now(),
              'max_age_hours': max_age_hours, 'sources': runs,
              'candidate_count': len(candidates), 'candidates_path': str(path),
              'raw_directory': str(fetcher.raw_dir)}
    (destination / 'supplement-crawl_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


class ThsArticleText(TextOnly):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        classes = dict(attrs).get('class', '').split()
        if not self.depth and 'news-content-parsed' in classes:
            self.depth = 1
            return
        if self.depth:
            if tag not in {'img', 'br', 'hr', 'input', 'meta', 'link', 'source', 'wbr'}:
                self.depth += 1
            super().handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.depth:
            super().handle_endtag(tag)
            self.depth -= 1
            if not self.depth:
                self.done = True

    def handle_data(self, data):
        if self.depth:
            super().handle_data(data)


def crawl_ths_futures(output_dir, limit=20):
    """Fetch actual public article bodies linked by THS's futures homepage."""
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '-ths-futures'
    fetcher = Fetcher(destination / 'raw' / run_id)
    candidates = []
    errors = []
    raw, discovery_path, _ = fetcher.get('ths', 'https://futures.10jqka.com.cn/')
    links = re.findall(r'<div class="liTit">\s*<a[^>]+href="([^"]+)"', raw)
    for link in list(dict.fromkeys(links))[:limit]:
        url = canonical_url(link, 'https://futures.10jqka.com.cn/')
        # Only first-party URLs from the real page, never arbitrary offsite targets.
        host = urlsplit(url).hostname or ''
        identifier = re.search(r'/c(\d+)\.shtml', url)
        if not host.endswith('.10jqka.com.cn') or not identifier:
            continue
        try:
            document, raw_path, collected = fetcher.get('ths', url)
            title = re.search(r'<h1[^>]*>(.*?)</h1>', document, re.S)
            published = re.search(r'>(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})<', document)
            extractor = ThsArticleText()
            extractor.feed(document)
            body = re.sub(r'\n[ \t]*\n+', '\n', ''.join(extractor.parts)).strip()
            if not title or not body or not published:
                raise ValueError('公开详情缺少标题、正文或发布时间')
            candidates.append({
                'candidate_id': 'ths:' + identifier.group(1), 'source_id':'ths',
                'source_name':'同花顺', 'external_id':identifier.group(1),
                'source_url':url, 'title':text_only(title.group(1)), 'body':body[:20000],
                'published_at':datetime.strptime(published.group(1),'%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),
                'collected_at':collected, 'raw_path':str(raw_path.resolve()),
                'discovery_raw_path':str(discovery_path.resolve()),
                'content_scope':'public_article', 'title_origin':'source',
            })
        except Exception as error:
            errors.append({'url':url,'error':type(error).__name__ + ': ' + str(error)[:150]})
    path = destination / 'extra-ths-futures-candidates.jsonl'
    path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in candidates),encoding='utf-8')
    report = {'run_id':run_id, 'source_id':'ths', 'candidate_count':len(candidates),
              'status':'partial' if errors else 'ok', 'errors':errors,
              'candidates_path':str(path), 'raw_directory':str(fetcher.raw_dir)}
    (destination / 'extra-ths-futures-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', default=str(Path(__file__).resolve().parent / 'data' / 'ingestion'))
    parser.add_argument('--sources', nargs='+', choices=list(SOURCES))
    parser.add_argument('--limit-per-source', type=int, default=100)
    args = parser.parse_args()
    print(json.dumps(crawl_sources(args.output_dir, args.sources, args.limit_per_source), ensure_ascii=False, indent=2))
