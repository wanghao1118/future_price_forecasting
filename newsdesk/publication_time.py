"""Source-backed publication times. Never infer a zone from a publisher's country."""

from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

AUDIT_VERSION = 'source-timezone-v1-20261102'
AUDIT_FIELDS = ('source_published_at', 'source_timezone', 'timezone_status', 'timezone_evidence',
                'timezone_checked_at', 'timezone_audit_version', 'original_published_at',
                'publication_raw_path', 'publication_raw_sha256')
VERIFIED = {'verified_epoch', 'verified_offset', 'verified_site_timezone'}
PRECISE = {'minute', 'second', 'timestamp'}


class PublicationTimeError(ValueError):
    """A source was collected, but its publication time is not admissible."""


def utc_timestamp(value):
    """Require a clock and an explicit, known offset; never attach a zone."""
    if (not isinstance(value, str) or not re.fullmatch(
            r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})', value)
            or value.endswith('-00:00')):
        raise PublicationTimeError('新闻时间必须包含具体时分及明确时区')
    point = iso(value)
    if point is None or point.tzinfo is None:
        raise PublicationTimeError('新闻时间或时区无效')
    return point.astimezone(timezone.utc).isoformat()


def require_verified_publication(record):
    meta = record.get('ingestion') or record
    if (meta.get('timezone_status') not in VERIFIED
            or (meta.get('published_at_precision') or meta.get('publication_precision')) not in PRECISE
            or not meta.get('source_published_at') or not meta.get('source_timezone')
            or not meta.get('timezone_evidence') or not meta.get('publication_raw_sha256')):
        raise PublicationTimeError('首发时间未通过验证：必须有具体时分、可信时区和原始来源证据')
    value = utc_timestamp(record.get('published_at'))
    raw = meta['source_published_at']
    try:
        if meta['timezone_status'] == 'verified_epoch':
            milliseconds = meta.get('source_id') in {'stcn', 'authority_67', 'authority_72', 'authority_76', 'authority_92', 'authority_93'}
            if meta.get('source_id') == 'authority_80':
                milliseconds = (urlsplit(record.get('source_url','')).hostname or '') in {'36kr.com','www.36kr.com'}
            point = datetime.fromtimestamp(float(raw) / (1000 if milliseconds else 1), timezone.utc)
        else:
            point = iso(raw)
            if meta['timezone_status'] == 'verified_site_timezone' and point and point.tzinfo is None:
                point = point.replace(tzinfo=ZoneInfo(meta['source_timezone']))
        if point is None or point.tzinfo is None or raw.endswith('-00:00') or point != iso(value):
            raise PublicationTimeError('首发时间与原始来源证据不一致')
    except (ValueError, TypeError, OverflowError, KeyError) as error:
        raise PublicationTimeError('首发时间与原始来源证据不一致') from error
    return value


def iso(value):
    try:
        return datetime.fromisoformat(re.sub(r'([+-])(\d):(\d\d)$', r'\g<1>0\2:\3', str(value)).replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None


def day(value):
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        for pattern in ('%B %d, %Y', '%b %d, %Y'):
            try:
                return datetime.strptime(str(value).strip(), pattern).date().isoformat()
            except ValueError:
                pass
    return ''


@lru_cache(maxsize=128)
def raw_document(path):
    raw = Path(path).read_bytes()
    text = raw.decode('utf-8', errors='replace')
    if text.count('\ufffd') > 20:
        text = raw.decode('gb18030', errors='replace')
    try:
        wrapper = json.loads(text)
        if isinstance(wrapper, dict) and isinstance(wrapper.get('raw_response'), str):
            text = wrapper['raw_response']
    except ValueError:
        pass
    return text, hashlib.sha256(raw).hexdigest()


def nodes(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from nodes(child)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child)


def json_payloads(text):
    try:
        yield json.loads(text)
    except ValueError:
        pass
    for script in re.findall(r'<script\b[^>]*>(.*?)</script>', text, re.S | re.I):
        try:
            yield json.loads(script)
        except ValueError:
            pass


def metadata(text):
    from html.parser import HTMLParser
    class Meta(HTMLParser):
        def __init__(self):
            super().__init__(); self.values = {}
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == 'meta':
                self.values[a.get('name') or a.get('property')] = a.get('content', '')
    parser = Meta(); parser.feed(text)
    return parser.values


def declared_stamp(text):
    for payload in json_payloads(text):
        for node in nodes(payload):
            types = node.get('@type', [])
            if isinstance(types, str): types = [types]
            if any(t in ['Article', 'NewsArticle', 'BlogPosting'] for t in types) and node.get('datePublished'):
                return str(node['datePublished'])
    return ''


def source_stamp(source, external, url, text):
    """Return (upstream value, interpretation, precision, evidence)."""
    if source.startswith('authority_'):
        from newsdesk.verified_sources import SOURCES, publication_stamp
        if source in SOURCES:
            return publication_stamp(source, url, text)
    identifier = str(external).split(':')[-1]
    if source in {'cls', 'jiemian', 'stcn', 'institution_18'}:
        keys = {'cls': ('ctime', 'article_time'), 'jiemian': ('publishtime',),
                'stcn': ('time',), 'institution_18': ('createTimestamp',)}[source]
        for payload in json_payloads(text):
            for node in nodes(payload):
                if str(node.get('id', node.get('article_id', ''))) != identifier:
                    continue
                for key in keys:
                    value = node.get(key)
                    if value:
                        return str(value), 'epoch_ms' if source == 'stcn' else 'epoch', 'second', '原始接口字段 ' + key + '；按新闻 ID 匹配的 Unix 时间戳'
    if source == 'jin10':
        for payload in json_payloads(text):
            for node in nodes(payload):
                if str(node.get('id', '')) == identifier and node.get('time'):
                    return str(node['time']), 'Asia/Shanghai', 'second', '金十官方首页脚本将快讯 time 统一为 Asia/Shanghai；2026-11-02 核对 index.5afc042c17.js'
    if source == 'aastocks':
        match = re.search(r"ConvertToLocalTime\(\{dt:['\"](\d{4}/\d{2}/\d{2} \d{2}:\d{2})['\"]", text)
        if match:
            return match[1].replace('/', '-'), 'Asia/Hong_Kong', 'minute', 'AASTOCKS 官方 js_std 的 ConvertToLocalTime 使用 moment.tz(dt, fmt, Asia/Hong_Kong)'
    if source == 'ths':
        # Modern article metadata declares UTC; old feeds/lists use the matching
        # China clock (11 article pairs across Jan-Nov verified on 2026-11-02).
        explicit = declared_stamp(text)
        if not explicit:
            match = re.search(r'"datePublished"\s*:\s*"([^"<>]+)', text.replace('\\', ''))
            explicit = match[1] if match else ''
        if explicit and iso(explicit) and iso(explicit).tzinfo:
            return explicit, 'offset', 'second', '文章 datePublished 显式时区'
        from newsdesk.news_sources import parse_response
        try:
            for node in parse_response('ths', text):
                if str(node.get('seq')) == identifier:
                    return str(node['pubDate']).replace('/', '-'), 'Asia/Shanghai', 'minute', '公开 feed pubDate；同站 datePublished UTC 与列表时分跨月对照一致（UTC+8）'
        except (ValueError, KeyError):
            pass
        if 'view_otherday' in text:
            for block in re.findall(r'<li\b[^>]*>(.*?)</li>', text, re.S | re.I):
                if not re.search(r'/c' + re.escape(identifier) + r'\.shtml', block): continue
                m = re.search(r'(\d{2})月(\d{2})日\s+(\d{2}:\d{2})', block)
                year = re.search(r'/(20\d{2})\d{4}/c', url)
                if m and year:
                    return year[1] + '-' + m[1] + '-' + m[2] + ' ' + m[3], 'Asia/Shanghai', 'minute', '公开日期栏目文章时分；同站 1—11 月文章 UTC 元数据对照确认东八区'
        match = re.search(r'>(20\d{2}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})<', text)
        if match:
            return match[1], 'Asia/Shanghai', 'second', '文章显示时间；同站 UTC 元数据与东八区显示跨月对照一致'
    if source == 'caixin':
        match = re.search(r'id=["\']pubtime_baidu["\'][^>]*>([^<]+)', text)
        if match:
            return match[1].strip(), 'unknown', 'second', '文章 pubtime_baidu 首发时间已核对；未找到来源明确时区声明，原 UTC+8 仅为采集约定'
        for payload in json_payloads(text):
            for node in nodes(payload):
                if str(node.get('nid', '')) == identifier or node.get('link') == url:
                    if node.get('time'):
                        return str(node['time']), 'unconfirmed_field', 'second', '栏目 time 可能为更新时间；未取得文章 pubtime_baidu 核对，且时区无明确声明'
    if source in {'goldman_sachs', 'institution_45', 'institution_46', 'institution_51', 'zijin_mining'}:
        meta = metadata(text)
        raw = declared_stamp(text) or meta.get('article:published_time') or meta.get('content_publishedAt') or meta.get('publishDate') or meta.get('datePublished') or ''
        if raw:
            point = iso(raw)
            if source == 'institution_45' and point and point.tzinfo:
                ny = point.astimezone(ZoneInfo('America/New_York'))
                if ny.hour == ny.minute == ny.second == 0:
                    return raw, 'date', 'date', 'content_publishedAt 在纽约冬/夏令时均对应午夜；pagePublishDate 可能为重新发布，不能作为首次发布时分'
            if point and point.hour == point.minute == point.second == 0:
                return raw, 'date', 'date', '来源日期字段为午夜占位，缺少可确认的发布时分'
            if point and point.tzinfo:
                return raw, 'offset', 'second', '文章发布元数据显式声明偏移；按原始 datePublished/content_publishedAt 保存'
            if len(raw) <= 10 or not point:
                return raw, 'date', 'date', '来源仅声明发布日期，未提供时分和时区'
            return raw, 'unknown', 'minute', '来源元数据有时分但未声明时区'
    # Unknown/new article adapters may provide explicit Article metadata.
    # Known feeds must still match their own ID/field rules above.
    if source not in {'cls', 'jiemian', 'stcn', 'institution_18', 'jin10', 'aastocks', 'ths', 'caixin'}:
        raw = declared_stamp(text)
        point = iso(raw)
        if point and point.tzinfo and not raw.endswith('-00:00'):
            return raw, 'offset', 'second', '原文 Article.datePublished 显式声明时区'
    return '', '', '', ''


def audit_publication(record):
    """No network and no database writes. Preserve original strings for review."""
    ing = record.get('ingestion') or {}
    def get(key, default=''):
        return record.get(key) or ing.get(key) or default
    source = get('source_id')
    original = get('original_published_at') or record.get('published_at', '')
    result = {'published_at': record.get('published_at', ''), 'published_date': get('published_date'),
              'publication_precision': get('published_at_precision') or get('publication_precision'),
              'original_published_at': original, 'source_published_at': '', 'source_timezone': '',
              'timezone_status': 'unverified_time', 'timezone_evidence': '缺少可核验的原始发布时间证据',
              'timezone_checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
              'timezone_audit_version': AUDIT_VERSION, 'publication_raw_path': get('publication_raw_path') or get('raw_path'),
              'publication_raw_sha256': ''}
    raw = zone = precision = evidence = ''
    try:
        text, digest = raw_document(result['publication_raw_path'])
        result['publication_raw_sha256'] = digest
        raw, zone, precision, evidence = source_stamp(source, get('external_id'), record.get('source_url', ''), text)
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        result['timezone_evidence'] = '原始时间证据不可解析：' + type(error).__name__
    if not raw:
        if not record.get('published_at') and day(result['published_date']):
            result.update(published_at='', publication_precision='date', source_published_at=result['published_date'],
                          timezone_status='date_only', timezone_evidence='公开研报/文章仅有日期；未提供可核实的时分和时区')
        # Unverified strings are retained, explicitly flagged, and barred from
        # time-gated model input. Never manufacture a corrected UTC instant.
        return result
    result.update(source_published_at=raw, publication_precision=precision, timezone_evidence=evidence)
    if zone in {'epoch', 'epoch_ms'}:
        try:
            point = datetime.fromtimestamp(float(raw) / (1000 if zone == 'epoch_ms' else 1), timezone.utc)
        except (ValueError, OverflowError, OSError):
            result.update(published_at='', timezone_evidence='原始 Unix 时间戳无效')
            return result
        result.update(published_at=point.isoformat(), published_date='', source_timezone='UTC (Unix epoch)', timezone_status='verified_epoch')
    elif zone in {'Asia/Shanghai', 'Asia/Hong_Kong', 'offset'}:
        point = iso(raw)
        if point is None: return result
        if zone != 'offset' and point.tzinfo is None: point = point.replace(tzinfo=ZoneInfo(zone))
        if raw.endswith('-00:00'):
            result.update(published_at='', published_date=day(raw), timezone_status='unverified_timezone')
            return result
        result.update(published_at=point.astimezone(timezone.utc).isoformat(), published_date='',
                      source_timezone=zone if zone != 'offset' else point.strftime('UTC%z'),
                      timezone_status='verified_offset' if zone == 'offset' else 'verified_site_timezone')
    elif zone == 'date':
        value = day(raw)
        point = iso(raw)
        if source == 'institution_45' and point and point.tzinfo:
            value = point.astimezone(ZoneInfo('America/New_York')).date().isoformat()
        result.update(published_at='', published_date=value, publication_precision='date',
                      source_timezone='America/New_York (date only)' if source == 'institution_45' else '', timezone_status='date_only')
    else:
        result.update(published_at='', published_date=day(raw), timezone_status='unverified_timezone' if zone == 'unknown' else 'unverified_time')
    return result


def normalize_candidate(candidate):
    result = dict(candidate)
    result.update(audit_publication(candidate))
    result['published_at_precision'] = result['publication_precision']
    return result
