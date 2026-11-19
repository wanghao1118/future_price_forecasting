"""Public THS commodity flash archive, independent of its stock-index feed.

The page's own date-picker links to ``qhkx_list/YYYYMMDD/``. A public listing
excerpt is real source text, never claimed to be a complete article. Candidates
from this broad commodity channel still require the ordinary semantic Agent.
"""

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from newsdesk.history_media import HistoryFetcher, TZ, _read_jsonl, enrich_ths, now, publication_day
from newsdesk.ingest import has_text_content, write_json
from newsdesk.news_sources import text_only


CHANNEL = "https://goodsfu.10jqka.com.cn/qhkx_list/"
USER_AGENT = "Mozilla/5.0 (compatible; OilNewsResearch/1.0)"


class Fetcher(HistoryFetcher):
    """Ordinary public requests, honoring declared robots rules per hostname."""

    def __init__(self, output_dir):
        super().__init__(output_dir)
        self.robots = {}

    def get(self, source_id, url):
        host = urlsplit(url).hostname or ""
        if source_id != 'ths' or not host.endswith('.10jqka.com.cn'):
            raise ValueError('商品归档链接不属于同花顺公开信源')
        if host not in self.robots:
            parser = RobotFileParser()
            try:
                raw, _, _ = super().get(source_id, 'https://' + host + '/robots.txt')
                parser.parse(raw.splitlines())
            except HTTPError as error:
                if error.code in {404, 410}:
                    parser.parse([])
                else:
                    raise
            self.robots[host] = parser
        if not self.robots[host].can_fetch(USER_AGENT, url):
            raise PermissionError('同花顺 robots 不允许抓取此公开路径')
        return super().get(source_id, url)


def parse_listing(raw, url, raw_path, collected_at, expected_day):
    title = re.search(r'<title[^>]*>(.*?)</title>', raw, re.S | re.I)
    if (not title or '期货快讯' not in text_only(title.group(1))
            or 'view_otherday' not in raw or 'qhkx_list/' not in raw):
        raise ValueError('未返回同花顺商品期货快讯日期栏目')
    rows = []
    for block in re.findall(r'<li\b[^>]*>(.*?)</li>', raw, re.S | re.I):
        if not re.search(r'class=["\'][^"\']*arc-title', block):
            continue
        link = re.search(r'<a\b([^>]+)>(.*?)</a>', block, re.S | re.I)
        href = re.search(r'href=["\']([^"\']+)', link.group(1)) if link else None
        if not href:
            continue
        article_url = urljoin(url, href.group(1)).replace('http://', 'https://', 1)
        parsed = urlsplit(article_url)
        identity = re.fullmatch(r'/(\d{8})/c(\d+)\.shtml', parsed.path)
        if not (parsed.hostname or '').endswith('.10jqka.com.cn') or not identity:
            continue
        excerpt = re.search(r'<a\b[^>]*class=["\'][^"\']*arc-cont[^"\']*["\'][^>]*>(.*?)</a>', block, re.S | re.I)
        clock = re.search(r'(\d{2})月(\d{2})日\s+(\d{2}:\d{2})', block)
        if not clock:
            continue
        stamp = identity.group(1)[:4] + '-' + clock.group(1) + '-' + clock.group(2) + ' ' + clock.group(3)
        published = datetime.strptime(stamp, '%Y-%m-%d %H:%M').replace(tzinfo=TZ)
        if published.date().isoformat() != expected_day:
            raise ValueError('商品日期归档返回了其他日期内容，不能当作请求日覆盖')
        external = identity.group(2)
        rows.append({'candidate_id': 'ths:' + external, 'source_id': 'ths', 'source_name': '同花顺',
            'external_id': external, 'source_url': article_url, 'title': text_only(link.group(2)),
            'body': text_only(excerpt.group(1)) if excerpt else '', 'published_at': published.isoformat(),
            'publication_precision': 'minute', 'collected_at': collected_at,
            'raw_path': str(Path(raw_path).resolve()), 'discovery_raw_path': str(Path(raw_path).resolve()),
            'source_listing_url': url, 'content_scope': 'public_article_excerpt', 'title_origin': 'source',
            'collection_platform': '同花顺商品期货快讯公开日期归档'})
    next_url = None
    for attributes in re.findall(r'<a\b([^>]+)>', raw, re.I):
        if re.search(r'class=["\'][^"\']*\bnext\b[^"\']*["\']', attributes, re.I):
            following = re.search(r'href=["\']([^"\']+)', attributes, re.I)
            if following:
                next_url = urljoin(url, following.group(1)).replace('http://', 'https://', 1)
                break
    if next_url:
        parts = urlsplit(next_url)
        expected_path = '/qhkx_list/' + expected_day.replace('-', '') + '/'
        if parts.hostname != 'goodsfu.10jqka.com.cn' or not parts.path.startswith(expected_path):
            raise ValueError('商品日期分页跳出了同一天的公开栏目')
    if not rows:
        raise ValueError('商品日期栏目没有可核验的新闻，不将页面异常当作空新闻日')
    return rows, next_url


def run_source(source_id, start_date, end_date, output_dir, *, priority_months=None,
               max_pages=10, max_articles=90, resume=True, fetcher=None, fetch_articles=False):
    """Fair monthly turns through real commodity date pages; all caps yield."""
    if source_id != 'ths':
        raise ValueError('商品快讯归档仅支持同花顺')
    first, last = date.fromisoformat(str(start_date)), date.fromisoformat(str(end_date))
    if first > last or last > datetime.now(TZ).date():
        raise ValueError('历史日期范围无效或包含未来日期')
    if type(max_pages) is not int or max_pages < 1 or type(max_articles) is not int or max_articles < 1:
        raise ValueError('max_pages/max_articles 必须为正整数')
    dates = [(first + timedelta(days=n)).isoformat() for n in range((last-first).days+1)]
    all_months = list(dict.fromkeys(day[:7] for day in dates))
    if isinstance(priority_months, str):
        raise ValueError('优先月份必须为 YYYY-MM 列表')
    priority = list(dict.fromkeys(priority_months or all_months))
    if any(month not in all_months for month in priority):
        raise ValueError('优先月份必须属于请求日期范围')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    checkpoint, destination = output/'checkpoint.json', output/'candidates.jsonl'
    scope = {'source_id': source_id, 'requested_start': str(first), 'requested_end': str(last)}
    if checkpoint.exists() and resume:
        state = json.loads(checkpoint.read_text(encoding='utf-8'))
        if any(state.get(k) != v for k,v in scope.items()):
            raise ValueError('商品归档断点属于不同日期范围')
    else:
        if destination.exists() and destination.stat().st_size:
            raise ValueError('不能在非空商品归档目录重建断点')
        state = {'version': 1, **scope, 'started_at': now(), 'days': {}, 'pages': 0,
                 'processed_article_count': 0, 'invalid_count': 0, 'errors': [], 'next_month': None}
        destination.touch(exist_ok=True)
    state['priority_months'] = priority
    fetcher = fetcher or Fetcher(output)
    existing = _read_jsonl(destination)
    seen = {row['candidate_id'] for row in existing}
    counts = Counter((publication_day(row).isoformat()[:7] if publication_day(row) else 'unknown') for row in existing)
    invalid_path = output/'invalid-candidates.jsonl'
    content_gap_dates = {str(publication_day(entry['candidate'])) for entry in _read_jsonl(invalid_path)
                         if publication_day(entry['candidate']) and not has_text_content(entry['candidate'].get('body'))}
    pages_this_call = processed_this_call = new_candidates = 0

    def save():
        state['updated_at'] = now()
        write_json(checkpoint, state)

    def active_days(month):
        return [day for day in dates if day[:7] == month and state['days'].get(day,{}).get('status') not in {'complete','gap'}]

    def months_in_order():
        active_priority = [month for month in priority if active_days(month)]
        values = active_priority or [month for month in all_months if active_days(month)]
        if state.get('next_month') in values:
            index = values.index(state['next_month'])
            values = values[index:] + values[:index]
        return values

    while processed_this_call < max_articles and not state.get('access_limit'):
        months = months_in_order()
        if not months:
            break
        progressed = False
        for index, month in enumerate(months):
            if processed_this_call >= max_articles:
                break
            remaining = active_days(month)
            # Drain an actual source page before loading another day in that
            # month, while other months receive their own independent turns.
            pending_days = [day for day in remaining if state['days'].get(day,{}).get('pending')]
            if pending_days:
                day = pending_days[0]
            else:
                day = min(remaining, key=lambda value:(value in state['days'], value))
            current = state['days'].setdefault(day, {'status':'pending','pending':[], 'next_url':None, 'attempts':{}})
            if not current['pending']:
                if pages_this_call >= max_pages:
                    continue
                url = current.get('next_url') or CHANNEL + day.replace('-','') + '/'
                pages_this_call += 1
                try:
                    raw, path, collected = fetcher.get('ths',url)
                    rows, following = parse_listing(raw,url,path,collected,day)
                    current.update(status='queued',pending=rows,next_url=following)
                    current.pop('last_error',None)
                    state['pages'] += 1
                    save()
                except Exception as error:
                    attempt = current['attempts'].get(url,0)+1
                    current['attempts'][url] = attempt
                    current['last_error'] = {'url':url,'error':type(error).__name__+': '+str(error)[:300]}
                    denied = isinstance(error,PermissionError) or isinstance(error,HTTPError) and error.code in {401,403}
                    missing = isinstance(error,HTTPError) and error.code in {404,410}
                    if attempt >= 3 or denied or missing:
                        current['status'] = 'gap'
                        state['errors'].append({'date':day,**current['last_error']})
                    if denied:
                        state['access_limit'] = {'date':day,**current['last_error'],
                            'reason':'官网明确限制或 robots 拒绝，停止该渠道后续请求，未更换身份或其他路径绕过'}
                    save()
                    progressed = True
                    if denied:
                        break
                    continue
            if current['pending']:
                row = current['pending'][0]
                if row['candidate_id'] not in seen:
                    if fetch_articles:
                        try:
                            row = enrich_ths(row,fetcher)
                        except Exception as error:
                            row['content_fetch_error'] = type(error).__name__+': '+str(error)[:220]
                    actual_day = publication_day(row)
                    if actual_day and first <= actual_day <= last and row.get('title','').strip() and has_text_content(row.get('body')):
                        with destination.open('a',encoding='utf-8') as target:
                            target.write(json.dumps(row,ensure_ascii=False)+'\n')
                        seen.add(row['candidate_id'])
                        counts[actual_day.isoformat()[:7]] += 1
                        new_candidates += 1
                    else:
                        with invalid_path.open('a',encoding='utf-8') as target:
                            target.write(json.dumps({'reason':'缺少真实日期、标题或有效公开正文，未用标题代替正文','candidate':row},ensure_ascii=False)+'\n')
                        state['invalid_count'] += 1
                        if actual_day and not has_text_content(row.get('body')):
                            content_gap_dates.add(str(actual_day))
                current['pending'].pop(0)
                if not current['pending']:
                    current['status'] = 'paginating' if current['next_url'] else 'complete'
                processed_this_call += 1
                state['processed_article_count'] += 1
                progressed = True
                state['next_month'] = months[(index+1)%len(months)]
            save()
        if not progressed:
            break
    save()
    completed = [day for day in dates if state['days'].get(day,{}).get('status') == 'complete']
    remaining = [day for day in dates if state['days'].get(day,{}).get('status') not in {'complete','gap'}]
    gaps = [day for day in dates if state['days'].get(day,{}).get('status') == 'gap']
    complete = not remaining and not gaps and not state['invalid_count']
    retries = []
    for detail in state['days'].values():
        if detail.get('last_error') and detail.get('status') != 'gap':
            retries.append(max(0,3-detail.get('attempts',{}).get(detail['last_error']['url'],3)))
    access_limited = bool(state.get('access_limit'))
    report = {'source_id':'ths','source_name':'同花顺','adapter':'ths_commodity_date_archive',
        'status':'blocked' if access_limited else 'complete' if complete else 'incomplete',
        'terminal':access_limited or not remaining,
        'candidate_count':len(seen),'new_candidates':new_candidates,'pages':state['pages'],
        'pages_this_call':pages_this_call,'processed_article_count':state['processed_article_count'],
        'processed_articles_this_call':processed_this_call,'completed_days':len(completed),
        'attempted_days':len(state['days']),'processed_dates':sorted(completed+gaps),
        'frontier_date':min(remaining,default=None),'priority_months':priority,
        'monthly_candidate_counts':{month:counts.get(month,0) for month in all_months},
        'invalid_count':state['invalid_count'],'errors':state['errors'],
        'retry_pending':bool(retries) and not access_limited,
        'retry_attempts_remaining':0 if access_limited else max(retries,default=0),
        'access_limit':state.get('access_limit'),
        'candidates_path':str(destination),'checkpoint_path':str(checkpoint),'updated_at':now(),
        'coverage':{'requested_start':str(first),'requested_end':str(last),'complete':complete,
            'terminal':access_limited or not remaining,'gap_dates':sorted(set(gaps)|content_gap_dates),
            'content_gap_dates':sorted(content_gap_dates),
            'unprocessed_days':len(remaining),'access_limited':access_limited,
            'scope':'同花顺商品期货快讯 qhkx_list 公开日期归档；未冒充全站，公开摘要不冒充全文，仍须 Agent 逐条判定'}}
    write_json(output/'report.json',report)
    return report
