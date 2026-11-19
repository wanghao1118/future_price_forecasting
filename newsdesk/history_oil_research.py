"""Shared public Sina oil-industry discovery for the configured brokers.

The publisher's own industry taxonomy is a discovery channel, never a relevance
decision. Every emitted, explicitly attributed public summary still requires the
ordinary Agent. This supplement neither classifies news nor writes the archive.

Industry names are declared in Sina's public dictionary:
https://finance.sina.com.cn/stock/reaserchyjbg/report/report_industry_sw2.js
Empty numbered pages can occur inside a valid archive (750100 page 2 is a real
example). They do not end history. A complete boundary needs a whole dated page
older than the requested start, after all preceding numbered pages were read.
"""

import fcntl
import hashlib
import json
import re
import threading
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError

from newsdesk.history_brokers import (Fetcher, SOURCES, atomic_candidates,
                                      atomic_json, now_iso)
from newsdesk.domestic_brokers_b import list_url, parse_detail, parse_list


INDUSTRIES = {'sw2_750100': '油气开采Ⅱ', 'sw2_750200': '油服工程',
              'sw2_750300': '炼化及贸易'}
TZ = timezone(timedelta(hours=8))
_LOCK_GUARD = threading.Lock()
_LOCKS = {}


class PublicAccessBlocked(ValueError):
    pass


@contextmanager
def _locked(path):
    """Coordinate shared discovery in both collector threads and processes."""
    path = Path(path)
    with _LOCK_GUARD:
        lock = _LOCKS.setdefault(str(path.resolve()), threading.RLock())
    with lock, path.open('a') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def parse_industry_list(text, industry, requested_page):
    """Verify the real publisher-selected industry and exact numbered page."""
    if industry not in INDUSTRIES:
        raise ValueError('未知新浪公开行业分类')
    kind = re.search(r"\bvar\s+t1\s*=\s*['\"]([^'\"]+)['\"]", text)
    selected = re.search(r"jQuery\(['\"]#industry['\"]\)\.val\(['\"]([^'\"]+)['\"]\)", text)
    nonce = re.search(r"<span\b[^>]*class=['\"][^'\"]*\bpagebox_num_nonce\b[^'\"]*['\"][^>]*>\s*(\d+)\s*</span>", text)
    if (not kind or kind[1] != '3' or not selected or selected[1] != industry
            or not nonce or int(nonce[1]) != requested_page):
        raise ValueError('公开列表的行业、检索类型或当前页与请求不一致')
    rows = parse_list(text)
    report_blocks = [block for block in re.findall(r'<tr\b[^>]*>(.*?)</tr>',text,re.S|re.I)
                     if re.search(r'vReport_Show',block,re.I)]
    if len(report_blocks) != len(rows):
        raise ValueError('行业列表中有报告缺少可核验的机构、编号或日期字段')
    if not rows and '没有找到相关内容' not in text:
        raise ValueError('行业列表没有数据或可核验的空结果标识')
    identifiers = [row['external_id'] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError('行业公开列表内出现重复报告编号')
    days = []
    for row in rows:
        day = date.fromisoformat(row.get('published_date') or '')
        if day.isoformat() != row['published_date']:
            raise ValueError('行业列表缺少可核验的原始发布日期')
        days.append(day.isoformat())
    if days != sorted(days, reverse=True):
        raise ValueError('行业日期列表未保持倒序，不能推定历史边界')
    links = [int(value) for value in re.findall(r"set_page_num\(['\"](\d+)['\"]\)", text)]
    # On the final page, every link can be smaller than its current nonce.
    total_pages = max([requested_page] + links)
    return rows, total_pages


def _load(path, default):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


def _read_candidates(path):
    if not path.exists():
        return {}
    return {row['candidate_id']: row for row in (
        json.loads(line) for line in path.read_text(encoding='utf-8').split('\n') if line.strip())}


def _audit(output, value):
    with (output/'errors.jsonl').open('a', encoding='utf-8') as handle:
        handle.write(json.dumps({'time':now_iso(), **value}, ensure_ascii=False)+'\n')


def _get(fetcher, url, output):
    marker = output/'access-limit.json'
    if marker.exists():
        raise PublicAccessBlocked('此渠道已记录公开访问限制，未继续请求其他日期或机构')
    try:
        return fetcher.get(url)
    except HTTPError as error:
        if error.code in {401,403}:
            atomic_json(marker, {'url':url,'http_status':error.code,'recorded_at':now_iso(),
                                 'note':'正常匿名请求受限；未绕过或使用凭据'})
        raise


def _publisher_map():
    values = {}
    for identifier, spec in SOURCES.items():
        for name in spec['publisher_names']:
            if name in values and values[name] != identifier:
                raise ValueError('配置机构的公开署名存在歧义')
            values[name] = identifier
    return values


def _discover(first, last, output, fetcher, max_pages):
    path = output/'industry-index.json'
    signature = hashlib.sha256(json.dumps(_publisher_map(),ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    scope = {'requested_start':str(first),'requested_end':str(last),'publisher_signature':signature}
    with _locked(output/'industry-index.lock'):
        state = _load(path, {'version':1, **scope,'started_at':now_iso(),'pages':0,
            'next_industry':None,'discoveries':{},'unmatched_publishers':{},'page_evidence':[],
            'empty_pages':[],
            'industries':{key:{'status':'pending','next_page':1,'pages':0,'last_day':None,
                'fingerprints':[],'attempts':{},'errors':{}} for key in INDUSTRIES}})
        if any(state.get(key) != value for key,value in scope.items()):
            raise ValueError('行业索引断点属于不同日期范围或机构配置，请另用目录')
        state.setdefault('empty_pages',[])
        pages_this_call = 0
        while pages_this_call < max_pages and not (output/'access-limit.json').exists():
            active = [key for key,value in state['industries'].items()
                      if value['status'] not in {'before_start_date','blocked','public_end_before_start'}]
            if not active:
                break
            selected = state.get('next_industry')
            industry = selected if selected in active else active[0]
            current = state['industries'][industry]
            current.setdefault('row_fingerprints',[])
            page = current['next_page']
            url = list_url(t1='3',industry=industry,p=page)
            pages_this_call += 1
            try:
                text,raw_path,collected = _get(fetcher,url,output)
                rows,total_pages = parse_industry_list(text,industry,page)
                fingerprint = hashlib.sha256(text.encode()).hexdigest()
                if fingerprint in current['fingerprints']:
                    raise ValueError('不同编号的行业历史页返回重复内容')
                if rows and current['last_day'] and rows[0]['published_date'] > current['last_day']:
                    raise ValueError('跨页发布日期未向历史推进')
                row_fingerprint = hashlib.sha256(json.dumps(rows,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                if rows and row_fingerprint in current['row_fingerprints']:
                    raise ValueError('不同编号页重复返回整页相同报告，未跳过不确定历史区间')
                current['fingerprints'].append(fingerprint)
                if rows:
                    current['row_fingerprints'].append(row_fingerprint)
                current['errors'].pop(url,None)
                current.update(status='incomplete',next_page=page+1,pages=current['pages']+1,
                               total_pages=total_pages)
                state['pages'] += 1
                for row in rows:
                    day = row['published_date']
                    if not str(first) <= day <= str(last):
                        continue
                    source_id = _publisher_map().get(row['publisher_name'])
                    if not source_id:
                        state['unmatched_publishers'][row['publisher_name']] = state['unmatched_publishers'].get(row['publisher_name'],0)+1
                        continue
                    identifier = source_id+':sina:'+row['external_id']
                    old = state['discoveries'].get(identifier)
                    if old:
                        if industry not in old['discovery_industries']:
                            old['discovery_industries'].append(industry)
                        continue
                    state['discoveries'][identifier] = dict(row,candidate_id=identifier,source_id=source_id,
                        discovery_industries=[industry],discovery_raw_path=str(Path(raw_path).resolve()),
                        source_listing_url=url,discovered_at=collected)
                state['page_evidence'].append({'industry':industry,'page':page,'url':url,
                    'raw_path':str(Path(raw_path).resolve()),'rows':len(rows),
                    'first_date':rows[0]['published_date'] if rows else None,
                    'last_date':rows[-1]['published_date'] if rows else None})
                if not rows:
                    state['empty_pages'].append({'industry':industry,'page':page,'url':url,
                        'raw_path':str(Path(raw_path).resolve()),
                        'reason':'官方编号列表明确空页，后续页仍可能有历史记录；保留索引覆盖缺口并继续'})
                if rows:
                    current['last_day'] = rows[-1]['published_date']
                    if all(row['published_date'] < str(first) for row in rows):
                        current['status'] = 'before_start_date'
                if page >= total_pages and current['status'] != 'before_start_date':
                    current.update(status='public_end_before_start',
                        reason='公开编号索引已结束，但未观察到越过开始日期，保留历史覆盖缺口')
            except (OSError,ValueError,UnicodeError) as error:
                attempts = current['attempts'].get(url,0)+1
                current['attempts'][url] = attempts
                detail = {'url':url,'industry':industry,'page':page,'attempt':attempts,
                          'error':type(error).__name__+': '+str(error)[:300]}
                current['errors'][url] = detail
                _audit(output,dict(phase='industry_list',**detail))
                if hasattr(fetcher,'invalidate') and not (output/'access-limit.json').exists():
                    fetcher.invalidate(url)
                if attempts >= 3 or (output/'access-limit.json').exists():
                    current['status'] = 'blocked'
            remaining = [key for key,value in state['industries'].items()
                         if value['status'] not in {'before_start_date','blocked','public_end_before_start'}]
            order = list(INDUSTRIES)
            subsequent = order[order.index(industry)+1:]+order[:order.index(industry)+1]
            state['next_industry'] = next((key for key in subsequent if key in remaining),None)
            state['updated_at'] = now_iso()
            atomic_json(path,state)
        if not path.exists():
            atomic_json(path,state)
    return state,pages_this_call


def run_source(source_id,start_date,end_date,output_dir,*,priority_months=None,
               max_pages=6,max_articles=20,resume=True,fetcher=None):
    """Advance a broker's public oil-industry summaries; work caps only yield.

    Every configured source shares one dated industry index and raw URL cache
    inside ``output_dir``. Keep this directory separate from date-list history.
    Candidate identity is identical to the existing Sina adapter for deduping.
    """
    if source_id not in SOURCES:
        raise ValueError('未知配置券商来源')
    first,last = date.fromisoformat(str(start_date)),date.fromisoformat(str(end_date))
    if first > last or last > datetime.now(TZ).date():
        raise ValueError('历史日期范围无效或包含未来日期')
    if any(type(value) is not int or value < 1 for value in (max_pages,max_articles)):
        raise ValueError('max_pages/max_articles 必须为正整数')
    if isinstance(priority_months,str):
        raise ValueError('优先月份必须为 YYYY-MM 列表')
    available_months = {(first+timedelta(days=n)).isoformat()[:7] for n in range((last-first).days+1)}
    priority = list(dict.fromkeys(priority_months or sorted(available_months)))
    if any(month not in available_months for month in priority):
        raise ValueError('优先月份必须属于请求范围')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    destination = output/(source_id+'-candidates.jsonl')
    checkpoint = output/(source_id+'-checkpoint.json')
    if not resume and any(path.exists() for path in (checkpoint,destination,output/'industry-index.json')):
        raise ValueError('不能在已有行业历史目录重建断点')
    fetcher = fetcher or Fetcher(output)
    spec = SOURCES[source_id]
    with _locked(output/(source_id+'.lock')):
        index,pages_this_call = _discover(first,last,output,fetcher,max_pages)
        state = _load(checkpoint,{'source_id':source_id,'requested_start':str(first),
            'requested_end':str(last),'processed_article_count':0,'errors':{},'attempts':{}})
        if state.get('source_id') != source_id or state.get('requested_start') != str(first) or state.get('requested_end') != str(last):
            raise ValueError('机构行业断点属于不同日期范围')
        candidates = _read_candidates(destination)
        discoveries = {key:row for key,row in index['discoveries'].items() if row['source_id']==source_id}
        processed = 0
        while processed < max_articles and not (output/'access-limit.json').exists():
            eligible = [row for key,row in discoveries.items() if key not in candidates and state['attempts'].get(key,0)<3]
            if not eligible:
                break
            counts = Counter(row['published_date'][:7] for row in candidates.values())
            row = min(eligible,key=lambda item:(item['published_date'][:7] not in priority,
                item['candidate_id'] in state['attempts'],counts[item['published_date'][:7]],
                item['published_date'][:7],item['published_date'],item['candidate_id']))
            identifier = row['candidate_id']
            processed += 1
            state['processed_article_count'] += 1
            state['attempts'][identifier] = state['attempts'].get(identifier,0)+1
            try:
                text,raw_path,collected = _get(fetcher,row['source_url'],output)
                parsed = parse_detail(text,spec['publisher_names'])
                if parsed.get('published_date') and parsed['published_date'] != row['published_date']:
                    raise ValueError('原始详情和行业列表的发布日期不一致')
                if parsed['publisher_name'] != row['publisher_name']:
                    raise ValueError('原始详情与行业列表的机构署名不一致')
                parsed.update(published_date=parsed.get('published_date') or row['published_date'],
                              published_at='',published_at_precision='date')
                candidates[identifier] = dict(parsed,candidate_id=identifier,source_id=source_id,
                    source_name=spec['name'],external_id='sina:'+row['external_id'],source_url=row['source_url'],
                    raw_path=str(Path(raw_path).resolve()),collected_at=collected,
                    discovery_raw_path=row['discovery_raw_path'],source_listing_url=row['source_listing_url'],
                    discovery_industries=row['discovery_industries'],discovery_industry_code=row['discovery_industries'][0],
                    discovery_research_channel='sina_public_oil_industries',
                    collection_platform='新浪财经研报·油气行业公开分类',content_scope='public_research_summary',
                    discovery='新浪官方油气行业分类发现；主题相关性仍须 Agent 逐条判断')
                state['errors'].pop(identifier,None)
            except (OSError,ValueError,UnicodeError) as error:
                detail = {'candidate_id':identifier,'url':row['source_url'],'published_date':row['published_date'],
                    'attempt':state['attempts'][identifier],'error':type(error).__name__+': '+str(error)[:300]}
                state['errors'][identifier] = detail
                _audit(output,dict(phase='public_summary',source_id=source_id,**detail))
                if hasattr(fetcher,'invalidate') and not (output/'access-limit.json').exists():
                    fetcher.invalidate(row['source_url'])
            atomic_candidates(destination,candidates)
            atomic_json(checkpoint,state)
        atomic_candidates(destination,candidates)
        atomic_json(checkpoint,state)
    access = _load(output/'access-limit.json',None)
    index_terminal = bool(access) or all(value['status'] in {
        'before_start_date','blocked','public_end_before_start'} for value in index['industries'].values())
    index_complete = all(value['status']=='before_start_date' for value in index['industries'].values())
    pending = [key for key in discoveries if key not in candidates and state['attempts'].get(key,0)<3]
    errors = [detail for value in index['industries'].values() for detail in value['errors'].values()]+list(state['errors'].values())
    complete = index_complete and not pending and not errors and not access and not index['empty_pages']
    terminal = bool(access) or (index_terminal and not pending)
    counts = Counter(row['published_date'][:7] for row in candidates.values())
    retries = [3-state['attempts'].get(key,0) for key in pending if key in state['errors']]
    retries.extend(3-value['attempts'].get(url,0) for value in index['industries'].values()
                   if value['status']!='blocked' for url in value['errors'])
    report = {'source_id':source_id,'source_name':spec['name'],'adapter':'sina_public_oil_industries',
        'status':'complete' if complete else 'blocked' if access and not candidates else 'incomplete',
        'terminal':terminal,'candidate_count':len(candidates),'discovery_count':len(discoveries),
        'pages':index['pages'],'pages_this_call':pages_this_call,'processed_article_count':state['processed_article_count'],
        'processed_articles_this_call':processed,'pending_articles':len(pending),'priority_months':priority,
        'monthly_candidate_counts':{month:counts.get(month,0) for month in sorted(available_months)},
        'candidates_path':str(destination),'checkpoint_path':str(checkpoint),
        'shared_index_path':str(output/'industry-index.json'),'errors':errors,'access_limit':access,
        'retry_pending':bool(retries),'retry_attempts_remaining':max(retries,default=0),'updated_at':now_iso(),
        'coverage':{'requested_start':str(first),'requested_end':str(last),'complete':complete,'terminal':terminal,
            'industries':index['industries'],'pending_articles':len(pending),
            'empty_page_gaps':index['empty_pages'],
            'scope':'新浪三个已声明油气行业公开编号列表、配置机构的明确署名研究摘要；不代表机构全部或私有研究；行业仅用于发现，仍须 Agent 判断相关性'}}
    atomic_json(output/(source_id+'-report.json'),report)
    return report
