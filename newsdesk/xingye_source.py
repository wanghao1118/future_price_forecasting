"""Explicitly attributed public Xingye research-column articles on Gelonghui."""
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from newsdesk.domestic_brokers_b import plain

SOURCES = {'institution_18': {'name': '兴业证券'}}
PROFILE = 'https://m.gelonghui.com/column/624647'
F10_PAGES = {'300750': 'https://basic.10jqka.com.cn/48/300750/worth.html'}
MAX_BYTES = 8 * 1024 * 1024


def next_data(text):
    match = re.search(r'<script\b[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', text, re.S)
    if not match:
        raise ValueError('缺少公开页面结构化数据')
    return json.loads(match.group(1))['props']['pageProps']


def parse_profile(text):
    profile = next_data(text)['articles']
    if profile.get('userId') != 624647 or '兴业证券研究所策略团队' not in profile.get('userBrief', ''):
        raise ValueError('专栏不是已确认的兴业证券研究所策略团队')
    links = {re.search(r'/p/(\d+)', href).group(1): urljoin(PROFILE, href)
             for href in re.findall(r'href=["\'](/p/\d+)["\']', text)}
    return [dict(row, source_url=links[str(row['id'])]) for row in profile.get('myHomePageItemVOS', [])
            if row.get('userId') == 624647 and str(row.get('id')) in links]


def parse_article(text):
    article = next_data(text)['articleInfo']
    column = article.get('column') or {}
    if (column.get('id') != 624647 or column.get('columnAuthorStatus') != 'authenticated'
            or '兴业证券研究所策略团队' not in column.get('brief', '')):
        raise ValueError('文章不属于平台认证的兴业证券研究团队专栏')
    if article.get('purchaseInfo') or article.get('remainWordNumber'):
        raise ValueError('文章有付费/未公开内容，跳过')
    body = plain(article.get('content'))
    if len(body) < 80 or not article.get('title'):
        raise ValueError('没有足够的公开研报文字')
    stamp = article.get('createTimestamp')
    published = datetime.fromtimestamp(float(stamp), timezone.utc).isoformat(timespec='seconds') if stamp else ''
    return {'external_id': 'gelonghui:' + str(article['id']), 'title': article['title'][:400], 'body': body[:18000],
            'published_at': published, 'published_at_precision': 'timestamp' if published else 'unknown',
            'publisher_name': '兴业证券研究所策略团队', 'author_name': column.get('nick', ''),
            'collection_platform': '格隆汇认证专栏', 'content_scope': 'public_research_summary'}


def crawl_xingye_source(output_dir, source_ids=None, limit_per_source=5):
    selected = list(SOURCES) if source_ids is None else source_ids
    if any(source not in SOURCES for source in selected):
        raise ValueError('未知兴业来源ID')
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / 'raw' / ('xingye-' + uuid.uuid4().hex[:12])
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': []}
    status = {'source_id': 'institution_18', 'source_name': '兴业证券', 'status': 'empty', 'count': 0,
              'entry_urls': [PROFILE], 'collection_platform': '格隆汇认证专栏', 'errors': []}
    rows, number = [], 0
    def fetch(url):
        nonlocal number
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.hostname not in {'m.gelonghui.com', 'www.gelonghui.com'}:
            raise ValueError('不是已配置的公开分发平台')
        time.sleep(.3)
        with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0)'}), timeout=15) as response:
            if urlsplit(response.url).hostname not in {'m.gelonghui.com', 'www.gelonghui.com'}:
                raise ValueError('出现非预期跳转')
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('响应过大')
        text = raw.decode('utf-8')
        number += 1
        path = raw_dir / ('response-' + str(number) + '.json')
        collected = datetime.now(timezone.utc).isoformat(timespec='seconds')
        path.write_text(json.dumps({'request_url': url, 'collected_at': collected, 'encoding': 'utf-8',
                                   'sha256': hashlib.sha256(raw).hexdigest(), 'raw_response': text}, ensure_ascii=False), encoding='utf-8')
        return text, path, collected
    if selected:
        try:
            text, _, _ = fetch(PROFILE)
            articles = parse_profile(text)
            for article in articles[:min(15, max(1, int(limit_per_source)) * 2)]:
                if len(rows) >= min(10, max(1, int(limit_per_source))):
                    break
                try:
                    text, path, collected = fetch(article['source_url'])
                    parsed = parse_article(text)
                    rows.append(dict(parsed, candidate_id='institution_18:' + parsed['external_id'], source_id='institution_18',
                                     source_name='兴业证券', source_url=article['source_url'], collected_at=collected, raw_path=str(path)))
                except (OSError, ValueError, KeyError, TypeError) as error:
                    status['errors'].append({'url': article['source_url'], 'error': type(error).__name__ + ': ' + str(error)[:150]})
        except (OSError, ValueError, KeyError, TypeError) as error:
            status['errors'].append({'url': PROFILE, 'error': type(error).__name__ + ': ' + str(error)[:150]})
        status['count'] = len(rows)
        status['status'] = 'ok' if rows and not status['errors'] else 'partial' if rows else 'error' if status['errors'] else 'empty'
        dates = sorted(row['published_at'] for row in rows if row['published_at'])
        latest = datetime.fromisoformat(dates[-1]) if dates else None
        recent = sum((datetime.now(timezone.utc) - datetime.fromisoformat(row['published_at'])).total_seconds() <= 30 * 86400 for row in rows if row['published_at'])
        status['coverage'] = {'earliest_publication': dates[0] if dates else '', 'latest_publication': dates[-1] if dates else '',
                              'recent_30_days': recent, 'freshness': 'recent' if recent else 'historical_only'}
        status['notes'] = ['账号及文章均明确标为兴业证券研究所策略团队，原文只收公开文字，不含图表。',
                           '该专栏本次最新文章超过30天，属于历史研究覆盖，不能作为本日新发布新闻。' if rows and not recent else '按原始发布时间保存。']
        report['sources'].append(status)
    destination = output / 'xingye-candidates.jsonl'
    destination.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    report.update(completed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), candidate_count=len(rows),
                  candidates_path=str(destination), raw_directory=str(raw_dir))
    path = output / ('xingye-run-' + uuid.uuid4().hex[:8] + '.json')
    report['report_path'] = str(path)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def parse_f10_reports(text, stock_code):
    rows = {}
    for block in re.findall(r'<dl\b[^>]*>(.*?)</dl>', text, re.S | re.I):
        heading = re.search(r'<h1\b[^>]*class=["\']cont_title["\'][^>]*>(.*?)</h1>', block, re.S | re.I)
        meta = re.search(r'<h2\b[^>]*class=["\']sub_cont_title["\'][^>]*>(.*?)</h2>', block, re.S | re.I)
        body = re.search(r'<div\b[^>]*class=["\']sub_cont["\'][^>]*>(.*?)</div>', block, re.S | re.I)
        if not heading or not meta or not body:
            continue
        metadata = plain(meta.group(1))
        publication = re.search(r'发布时间：\s*(\d{4}-\d{2}-\d{2})', metadata)
        publisher = re.search(r'来源：\s*([^\s]+)', metadata)
        if not publication or not publisher or publisher.group(1) not in {'兴业证券', '兴业证券股份有限公司'}:
            continue
        title, content = plain(heading.group(1)), plain(body.group(1))
        if not title or len(content) < 80:
            continue
        date = publication.group(1)
        title_hash = hashlib.sha256(title.encode()).hexdigest()[:16]
        external_id = 'ths-f10:{}:{}:{}'.format(stock_code, date, title_hash)
        rows.setdefault(external_id, {'external_id': external_id, 'title': title[:400], 'body': content[:18000],
                                     'published_at': '', 'published_date': date, 'published_at_precision': 'date',
                                     'publisher_name': publisher.group(1), 'collection_platform': '同花顺公开F10研报',
                                     'content_scope': 'public_research_summary', 'url_is_collection': True})
    return list(rows.values())


def crawl_xingye_f10(output_dir, limit_per_source=5):
    """A verified public F10 page; individual report identity is not its page URL."""
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    raw_dir = output / 'raw' / ('xingye-f10-' + uuid.uuid4().hex[:10])
    raw_dir.mkdir(parents=True, exist_ok=True)
    report = {'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'sources': []}
    source = {'source_id': 'institution_18', 'source_name': '兴业证券', 'status': 'empty', 'count': 0,
              'entry_urls': list(F10_PAGES.values()), 'collection_platform': '同花顺公开F10研报', 'errors': []}
    rows = []
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    for stock_code, url in F10_PAGES.items():
        try:
            time.sleep(.3)
            with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0 (compatible; OilNewsResearch/1.0)'}), timeout=15) as response:
                if urlsplit(response.url).hostname != 'basic.10jqka.com.cn':
                    raise ValueError('Unexpected F10 redirect')
                raw = response.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('Response exceeds size limit')
            text = raw.decode('gb18030')
            collected = datetime.now(timezone.utc).isoformat(timespec='seconds')
            path = raw_dir / ('ths-f10-' + stock_code + '.json')
            path.write_text(json.dumps({'request_url': url, 'collected_at': collected, 'encoding': 'gb18030',
                                       'sha256': hashlib.sha256(raw).hexdigest(), 'raw_response': text}, ensure_ascii=False), encoding='utf-8')
            for row in parse_f10_reports(text, stock_code):
                age = (today - datetime.strptime(row['published_date'], '%Y-%m-%d').date()).days
                if 0 <= age <= 30:
                    rows.append(dict(row, candidate_id='institution_18:' + row['external_id'], source_id='institution_18',
                                     source_name='兴业证券', source_url=url, raw_path=str(path), collected_at=collected))
        except (OSError, ValueError, UnicodeError) as error:
            source['errors'].append({'url': url, 'error': type(error).__name__ + ': ' + str(error)[:150]})
    rows = sorted(rows, key=lambda row: row['published_date'], reverse=True)[:max(1, min(int(limit_per_source), 10))]
    source['count'] = len(rows)
    source['status'] = 'ok' if rows and not source['errors'] else 'partial' if rows else 'error' if source['errors'] else 'empty'
    source['coverage'] = {'recent_30_days': len(rows), 'latest_publication_date': rows[0]['published_date'] if rows else '',
                          'freshness': 'recent' if rows else 'empty'}
    source['notes'] = ['F10已公开交付的研报文字，严格按每篇“来源：兴业证券”筛选。',
                       '多篇报告共用真实页面URL，url_is_collection=True，按机构+股票代码+报告日期+标题哈希识别。']
    report['sources'].append(source)
    path = output / 'xingye-f10-candidates.jsonl'
    path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
    report.update(completed_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), candidate_count=len(rows),
                  candidates_path=str(path), raw_directory=str(raw_dir))
    report_path = output / ('xingye-f10-run-' + uuid.uuid4().hex[:8] + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def crawl_xingye_sources(output_dir, source_ids=None, limit_per_source=5):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if source_ids is not None and any(source not in SOURCES for source in source_ids):
        raise ValueError('未知兴业来源ID')
    reports = []
    candidates = []
    if source_ids is None or source_ids:
        reports = [crawl_xingye_f10(output / 'xingye-f10', limit_per_source),
                   crawl_xingye_source(output / 'xingye-column', source_ids, limit_per_source)]
        for report in reports:
            candidates.extend(json.loads(line) for line in Path(report['candidates_path']).read_text().split('\n') if line)
    # Recent F10 reports are preferred; historical authenticated-column material
    # fills remaining coverage and keeps its actual original publication time.
    candidates = candidates[:max(1, min(int(limit_per_source), 10))]
    destination = output / 'xingye-candidates.jsonl'
    destination.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in candidates), encoding='utf-8')
    report = {'completed_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'candidate_count': len(candidates),
              'candidates_path': str(destination), 'sources': [], 'subreports': reports}
    if source_ids is None or source_ids:
        report['sources'].append({'source_id': 'institution_18', 'source_name': '兴业证券',
                                  'status': 'ok' if candidates and all(not r['sources'][0]['errors'] for r in reports) else 'partial' if candidates else 'empty',
                                  'count': len(candidates), 'entry_urls': [PROFILE] + list(F10_PAGES.values()),
                                  'collection_platform': '同花顺公开F10研报 / 格隆汇认证专栏',
                                  'errors': [e for r in reports for e in r['sources'][0]['errors']],
                                  'notes': ['公开F10已取得近30天明确署名报告；专栏部分仍为历史研究覆盖，逐篇保留原日期和精度。']})
    report_path = output / ('xingye-run-' + uuid.uuid4().hex[:8] + '.json')
    report['report_path'] = str(report_path)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report
