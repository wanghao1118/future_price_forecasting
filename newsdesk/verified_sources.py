"""Live publishers admitted by the November source audit.

Discovery always starts at current indexes/accounts, never at the audit sample.
Raw responses are retained and publication_time independently reparses each item.
Only public HTTP is used; browser logins are not silently exported or assumed.
"""
import hashlib
import gzip
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen, build_opener, HTTPCookieProcessor
from http.cookiejar import CookieJar
from urllib.error import HTTPError

from newsdesk.public_company_sources import Page, Node, atomic_json, atomic_jsonl

SOURCES = json.loads(Path(__file__).with_name('verified_source_catalog.json').read_text())
XUEQIU_ACCOUNTS = {67:('2371289267','管清友'),72:('3609410221','阿基米德Biotech'),92:('4164170693','乔令投研')}
UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36'


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


class AccessError(ValueError):
    def __init__(self, message, status='access_blocked'):
        super().__init__(message)
        self.status = status


def decode_response(raw, declared=None):
    # Some Sina responses still declare GBK although the response bytes are UTF-8.
    # Strict UTF-8 validation prevents corrupting Chinese bylines before attribution.
    try:
        return raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        encoding = declared or ('gb18030' if re.search(br'charset=["\s]*gb', raw[:12000], re.I) else 'utf-8')
        return raw.decode(encoding, errors='replace')


class Fetcher:
    def __init__(self, output, source):
        self.output, self.source, self.pages = Path(output) / 'verified-raw', source, 0
        self.output.mkdir(parents=True, exist_ok=True)
        # A fresh anonymous session per source/run. No browser cookies or user credentials.
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get(self, url):
        request = Request(url, headers={'User-Agent': UA, 'Accept': 'text/html,application/json', 'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'})
        with self.opener.open(request, timeout=18) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if raw.startswith(b'\x1f\x8b'): raw = gzip.decompress(raw)
            if len(raw) > 8 * 1024 * 1024:
                raise ValueError('响应过大')
            text = decode_response(raw, response.headers.get_content_charset())
            final = response.url
        path = self.output / (self.source + '-' + hashlib.sha256(url.encode()).hexdigest()[:16] + '.json')
        atomic_json(path, {'request_url': url, 'response_url': final, 'collected_at': now(), 'raw_response': text})
        self.pages += 1
        if 'passport.weibo' in final or 'visitor.passport' in text[:4000] or '登录 - 雪球' in text:
            raise AccessError('后台请求跳转登录页；浏览器登录态未提供给采集器', 'requires_login')
        if any(marker in text for marker in ('__waf_verification_challenge','CF_APP_WAF','_waf_bd8ce2ce37')):
            raise AccessError('后台请求被访问验证/反爬拦截；浏览器可读不代表普通 HTTP 可读')
        return text, str(path.resolve()), final


def text_of(html):
    return Page(str(html).encode()).root.text().strip()


def nuxt_values(values):
    """Read Nuxt's JSON reference table without executing publisher JavaScript."""
    if not isinstance(values, list):
        return values
    cache, active = {}, set()
    def ref(index):
        if not isinstance(index, int) or isinstance(index, bool): return index
        if index < 0 or index >= len(values): return None
        if index in cache: return cache[index]
        if index in active: return None
        active.add(index)
        item = values[index]
        if isinstance(item, dict): result = {key: ref(value) for key, value in item.items()}
        elif isinstance(item, list):
            result = ref(item[1]) if len(item) == 2 and item[0] in ('Reactive', 'ShallowReactive', 'Ref', 'ShallowRef') else [ref(v) for v in item]
        else: result = item
        active.remove(index); cache[index] = result
        return result
    return ref(0)


def objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values(): yield from objects(child)
    elif isinstance(value, list):
        for child in value: yield from objects(child)


def document(text):
    page = Page(text.encode()).root
    meta, payloads = {}, []
    for node in page.walk():
        if node.tag == 'meta':
            a = node.attrs; meta[a.get('property') or a.get('name') or a.get('itemprop')] = a.get('content', '')
        if node.tag != 'script': continue
        raw = ''.join(c for c in node.children if isinstance(c, str)).strip()
        try:
            data = json.loads(raw)
            payloads.append(nuxt_values(data) if node.attrs.get('id') == '__NUXT_DATA__' else data)
        except (ValueError, RecursionError):
            # JSON after a known state assignment, not arbitrary JavaScript.
            match = re.search(r'(?:window\.)?(?:initialState|__INITIAL_STATE__)\s*=\s*', raw)
            if match:
                try: payloads.append(json.JSONDecoder().raw_decode(raw[match.end():])[0])
                except ValueError: pass
    return page, meta, [node for payload in payloads for node in objects(payload)]


def match_state(node, url):
    tail = urlsplit(url).path.rstrip('/').split('/')[-1].replace('.html', '')
    ids = ('id','itemId','post_guid','content_id','newsId','article_id','ID','T_ID','id_str','ARTICLE_ID')
    return any(str(node.get(key, '')) == tail for key in ids)


def parse_article(source, url, text):
    spec = SOURCES[source]; row = spec['row']
    if row in XUEQIU_ACCOUNTS and urlsplit(url).hostname == 'www.xueqiu.com':
        item = json.loads(text)
        uid, name = XUEQIU_ACCOUNTS[row]
        if (urlsplit(url).path != '/'+uid+'/'+str(item.get('id','')) or str(item.get('user_id')) != uid
                or item.get('user',{}).get('screen_name') != name):
            raise ValueError('雪球文章 ID 或作者账号不匹配')
        if item.get('truncated') or not item.get('text'):
            raise ValueError('雪球只返回摘要，未取得完整正文')
        body = text_of(item['text'])
        return {'title':item.get('title') or body[:80], 'body':name+'\n\n'+body,
                'stamp':str(item.get('created_at') or ''), 'zone':'epoch_ms',
                'field':'雪球正文接口按文章 ID / 作者 ID 匹配 created_at（Unix 毫秒）'}
    if row in (66,68,76):
        try: payload = json.loads(text)
        except ValueError: payload = {}
        identity = urlsplit(url).path.rstrip('/').split('/')[-1]
        if row == 76:
            from urllib.parse import parse_qs
            match = re.fullmatch(r'/f-d-(\d+)\.html',urlsplit(url).path)
            identity = match[1] if match else parse_qs(urlsplit(url).query).get('aid',[''])[0]
            entries = payload.get('attachment',{}).get('flashes',[])
            item = next((n for n in entries if str(n.get('aid'))==identity),{})
            raw = str(item.get('writeTime') or '')
            return {'title':item.get('title',''), 'body':text_of(item.get('content','')), 'stamp':raw,'zone':'epoch_ms' if re.fullmatch(r'\d{13}',raw) else '', 'field':'按 aid 匹配官方接口 writeTime（Unix 毫秒）'}
        item = payload.get('data',{})
        if str(item.get('id','')) == identity:
            expected = '134881' if row == 66 else '100001415753'
            if str(item.get('author',{}).get('id','')) != expected: raise ValueError('文章作者与目标账号不一致')
            return {'title':item.get('title',''), 'body':text_of(item.get('content','')), 'stamp':str(item.get('display_time') or ''),'zone':'epoch','field':'按文章 ID 和作者 ID 匹配华尔街见闻 display_time（Unix 秒）'}
    page, meta, nodes = document(text)
    if row == 93 and urlsplit(url).hostname == 'www.laohu8.com':
        raw = next((n.text() for n in page.walk() if n.attrs.get('id') == '__APP_DATA__'), '{}')
        item = json.loads(raw).get('data', {})
        author = item.get('author', {})
        if (str(item.get('id')) != urlsplit(url).path.rstrip('/').split('/')[-1]
                or str(author.get('authorIdStr')) != '3557188805155421' or author.get('name') != spec['name']):
            raise ValueError('文章 ID 或作者账号不匹配')
        body = next((n.text().strip() for n in page.walk() if n.has_class('post-article')), '')
        return {'title':meta.get('og:title',''), 'body':spec['name']+'\n\n'+body if body else '',
                'stamp':str(item.get('gmtCreate') or ''), 'zone':'epoch_ms',
                'field':'老虎社区当前文章 ID / 作者 ID 匹配 gmtCreate（Unix 毫秒，转载发布时间）'}
    title = meta.get('og:title', '')
    if not title:
        title = next((n.text().strip() for n in page.walk() if n.tag == 'h1'), '')
    if not title:
        title = next((n.text().strip() for n in page.walk() if n.tag == 'title'), '')
    body = ''; stamp = ''; field = ''; zone = ''
    classes = {26:['article__content','insight-content','entry-content'],30:['entry-content'],39:['Artical_Content'],41:['multi-text'],45:['leftcontent__XcRTe'],49:['entry-content'],50:['article-body'],51:['content'],53:['detail-article'],56:['article'],58:['article'],59:['article'],64:['articleContent'],69:['g-art-content'],73:['news-content'],74:['article-content'],75:['rich_media_content'],77:['div_content'],78:['mp-editor'],79:['_content_mode'],80:['articleDetailContent'],81:['_content_mode'],83:['entry-content'],85:['articleContent'],87:['_content_mode'],91:['mp-editor'],94:['article_content'],95:['_content_mode']}
    ids = {26:'article',41:'multi-text',53:'news-detail-main-article',64:'articleContent',77:'div_content',78:'mp-editor',85:'articleContent',91:'mp-editor'}
    if row == 30: classes[row] += ['tl-single-page-content']
    if row == 75: classes[row] += ['yema_edit_box']
    if row == 86: classes[row] = ['article__content']
    if row == 60 and urlsplit(url).hostname == 'm.pedaily.cn': classes[row] = ['news-content']
    if row == 80 and urlsplit(url).hostname == 'www.cyzone.cn': classes[row] = ['g-art-content']
    if row == 82: classes[row] = ['articleContent']; ids[row] = 'articleContent'
    if row in (66,68): classes[row] = ['article-content','content','rich-text']
    found = [n.text().strip() for n in page.walk() if any(n.has_class(c) for c in classes.get(row, [])) or (row in ids and n.attrs.get('id') == ids[row]) or (row in (56,58,59) and n.attrs.get('id') == 'artibody')]
    if found: body = max(found, key=len)
    if row == 86:
        if not any(n.tag == 'a' and '/member/3350257.html' in n.attrs.get('href','') for n in page.walk()) or '投资作业本' not in body[:500]:
            raise ValueError('文章署名与目标账号不一致')
    if row in (64,78,82,85,91):
        expected = {64:'吴晓波频道',78:'深潜atom',82:'达摩财经',85:'汽车K线',91:'尺度商业'}[row]
        # The account suffix scopes discovery; byline prevents recycled IDs or redirects.
        byline = ' '.join(n.text() for n in page.walk() if n.attrs.get('id') == 'user-info' or n.has_class('author-name') or n.has_class('author') or n.has_class('author-info'))
        if expected not in byline and expected not in meta.get('author',''):
            raise ValueError('文章署名与目标账号不一致')
        # Retain the verified page byline in the agent's evidence, so account
        # attribution is supported by captured text rather than a registry label.
        if body and expected not in body[:500]: body = expected + '\n\n' + body
    if row in (60,80) and urlsplit(url).hostname in ('m.pedaily.cn','www.cyzone.cn'):
        author_link = '/author/579' if row == 60 else '/author/1545715'
        if not any(n.tag == 'a' and urlsplit(n.attrs.get('href','')).path.rstrip('/') == author_link and spec['name'] in n.text() for n in page.walk()):
            raise ValueError('转载作者主页与目标来源不一致')
        if body and spec['name'] not in body[:500]: body = spec['name'] + '\n\n' + body
    for node in nodes:
        kind = node.get('@type', [])
        if isinstance(kind, str): kind = [kind]
        if set(kind) & {'Article','NewsArticle','TechArticle','BlogPosting','WebPage'}:
            # Reject recommendations with a different URL or title.
            canonical = node.get('url') or node.get('mainEntityOfPage')
            if isinstance(canonical, dict): canonical = canonical.get('@id')
            if isinstance(canonical, str) and urlsplit(canonical).path.rstrip('/') != urlsplit(url).path.rstrip('/'): continue
            if node.get('headline') and title and node['headline'] not in title and title not in node['headline']: continue
            if node.get('datePublished'):
                stamp, field = str(node['datePublished']), 'JSON-LD datePublished'
            if not body and node.get('articleBody'): body = text_of(node['articleBody'])
        if not match_state(node, url): continue
        for key in ('published_at','time_published','T_RELEASE_TM_ORIGINAL','publishTime','pubTimestamp'):
            if node.get(key): stamp, field = str(node[key]), '当前文章 ' + key
        for key in ('content','main','widgetContent','article_content'):
            if isinstance(node.get(key), str) and len(text_of(node[key])) > len(body): body = text_of(node[key])
        title = title or node.get('title', '')
    if row in (56,58,59):
        stamp, field = meta.get('bytedance:published_time', ''), 'bytedance:published_time（转载发布）'
        # Attribution must appear in source/byline, not merely a recommendation.
        byline = ' '.join(n.text() for n in page.walk() if n.has_class('source') or n.has_class('date-source'))
        if spec['name'] not in byline and not re.search(r'(?:来源[：:]|文章来源[：:])\s*' + re.escape(spec['name']), body[:700]):
            raise ValueError('转载署名与目标账号不一致')
    elif row == 41:
        m = re.search(r"['\"]actime['\"]\s*,\s*['\"]([^'\"]+)", text)
        if m: stamp, field = m[1], 'actime（发布）'
    elif not stamp:
        for key in ('article:published_time','datePublished','pubdate','publishdate','publishDate'):
            if meta.get(key): stamp, field = meta[key], 'meta ' + key; break
    if row in (69,74,79,81,87,95) and not stamp:
        # These sites' decoded article objects sometimes use an internal GUID.
        for node in nodes:
            nt = node.get('title') or node.get('T_TITLE') or node.get('C_TITLE') or ''
            if not nt or not title or not (nt in title or title in nt): continue
            for key in ('published_at','time_published','T_RELEASE_TM_ORIGINAL'):
                if node.get(key): stamp, field = str(node[key]), '当前文章标题匹配 ' + key
    if spec['self_media'] and not stamp:
        # Restrict visible date extraction to the article's own byline/header.
        header = ' '.join(n.text() for n in page.walk() if any(n.has_class(c) for c in ('article-info','article_info','article-time','time','date','art-time','source','info','article-meta','news-time','tpt-tip-jing')) or row == 77 and n.tag == 'time')
        times = re.findall(r'(?<!\d)(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)', header)
        if len(set(times)) == 1:
            date, clock = times[0]; stamp = date.replace('/','-') + ' ' + clock; field = '文章署名区发布时间'
    if re.fullmatch(r'\d{10}(?:\.\d+)?', stamp): zone = 'epoch'
    elif re.fullmatch(r'\d{13}', stamp): zone = 'epoch_ms'
    elif re.search(r'\d{2}:\d{2}', stamp):
        stamp = re.sub(r'([+-]\d{2})$', r'\1:00', stamp)
        zone = 'offset' if re.search(r'(Z|[+-]\d{2}:\d{2})$', stamp) else ('Asia/Shanghai' if spec['self_media'] else 'unknown')
    else: zone = 'date' if stamp else ''
    evidence = field
    if zone == 'Asia/Shanghai': evidence += '；用户2026-11-05确认该自媒体账号采用中国标准时间（UTC+8），非源站显式时区声明'
    if row in (69,73,74,75,77,79,81,87,94,95):
        expected = '秦朔朋友圈' if row == 69 else spec['name']
        identities = [meta.get('author','')]
        identities += [n.text() for n in page.walk() if any(n.has_class(c) for c in ('author','author-name','name','tpt-tip','source','article-author')) and len(n.text()) < 600]
        for node in nodes:
            if match_state(node,url):
                identities += [str(node.get(k,'')) for k in ('author','author_name','authors','authorCompany')]
        if not any(expected in identity for identity in identities) and not re.search(r'来源[：:|｜]\s*' + re.escape(expected), body[:500]):
            raise ValueError('文章署名与目标来源不一致')
    return {'title': unescape(title).strip(), 'body': body.strip(), 'stamp': stamp, 'zone': zone, 'field': evidence}


def publication_stamp(source, url, text):
    data = parse_article(source, url, text)
    return data['stamp'], data['zone'], ('second' if re.search(r':\d{2}:\d{2}',data['stamp']) or data['zone'].startswith('epoch') else 'minute' if data['zone'] in ('offset','Asia/Shanghai','unknown') else 'date'), data['field']


def discover(source, url, text):
    row = SOURCES[source]['row']; page, meta, nodes = document(text)
    patterns = {26:r'/insights/[^/]+/[^/]+/?$',30:r'/en/economics/[^/]+/[^/]+/?$',39:r'/a/\d{6}/\d{2}/WS\w+\.html$',41:r'/news/\d+\.html$',45:r'/newsDetail_forward_\d+$',49:r'/resource-center/press-releases/[^/]+/?$',50:r'/(?:en/)?insights/[^/]+/?$',51:r'/presscenter/news/\d+-\d+\.html$',53:r'/news/\d+$',56:r'/.*doc-\w+\.shtml$',58:r'/.*doc-\w+\.shtml$',59:r'/.*doc-\w+\.shtml$',64:r'/a/\d+_121956368$',69:r'/article/\d+\.html$',73:r'/news/\d+/?$',74:r'/sq/w/\w+$',75:r'/Index/view/id/\d+\.html$',77:r'/analyst/detail/[^/]+/[^/]+\.html$',78:r'/a/\d+_100150$',79:r'/\d+\.html$',80:r'/p/\d+$',81:r'/\d+\.html$',83:r'/archives/\d+$',85:r'/a/\d+_115871$',87:r'/\d+\.html$',91:r'/a/\d+_157944$',94:r'/IG\d+\.shtml$',95:r'/\d+\.html$'}
    pattern = patterns.get(row)
    if row in (66,68): pattern = r'/(?:member|premium)/articles/\d+$'
    if row == 86: pattern = r'/article/\d+\.html$'
    if row == 60 and urlsplit(url).hostname == 'm.pedaily.cn': pattern = r'/news/\d+$'
    if row == 80 and urlsplit(url).hostname == 'www.cyzone.cn': pattern = r'/article/\d+\.html$'
    if row == 82: pattern = r'/a/\d+_100011510$'
    if row == 93 and urlsplit(url).hostname == 'www.laohu8.com': pattern = r'/post/\d+$'
    if not pattern: return []
    results = []
    for node in page.walk():
        if node.tag != 'a' or not node.attrs.get('href'): continue
        target = urljoin(url, unescape(node.attrs['href'])); parts = urlsplit(target)
        if parts.scheme not in ('http','https') or parts.username: continue
        origin = urlsplit(url).hostname or ''
        domain = '.'.join(origin.split('.')[-3:] if origin.endswith('.com.cn') else origin.split('.')[-2:])
        if parts.hostname != domain and not (parts.hostname or '').endswith('.' + domain): continue
        if re.fullmatch(pattern, parts.path):
            if row == 26 and '/insights/category/' in parts.path: continue
            target = urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
            if target not in results: results.append(target)
    return results


def collect_one(source, output, limit=5):
    from newsdesk.publication_time import normalize_candidate, require_verified_publication, PublicationTimeError
    spec = SOURCES[source]; fetch = Fetcher(output, source)
    report = {'source_id':source, 'name':spec['name'], 'status':'failed', 'checked_at':now(), 'candidates':0, 'discovered_count':0, 'body_count':0, 'time_skipped':0, 'errors':[]}
    candidates, links = [], []
    try:
        feeds = {}
        for entry in spec['entry_urls']:
            text, path, final = fetch.get(entry)
            if spec['row'] in XUEQIU_ACCOUNTS and urlsplit(entry).hostname == 'www.xueqiu.com':
                uid, name = XUEQIU_ACCOUNTS[spec['row']]
                text, path, final = fetch.get('https://www.xueqiu.com/v4/statuses/user_timeline.json?user_id='+uid+'&page=1')
                for item in json.loads(text).get('statuses',[]):
                    if str(item.get('user_id')) == uid:
                        links.append('https://www.xueqiu.com/'+uid+'/'+str(item['id']))
            elif spec['row'] == 89:
                payload = json.loads(text)
                if payload.get('code') == -403 and payload.get('message') == 'Need login':
                    raise AccessError('萝卜投研内容接口明确返回 Need login；现有浏览器登录尚未接入后台采集，无需重复登录', 'requires_login')
            elif spec['row'] == 76:
                for item in json.loads(text).get('attachment',{}).get('flashes',[]):
                    url = 'https://www.arfgc.com/f-d-' + str(item['aid']) + '.html'
                    links.append(url); feeds[url] = (text,path,url)
            elif spec['row'] in (56,58,59):
                payload = json.loads(text)
                data = payload.get('data',{})
                for item in (data if isinstance(data,list) else data.get('list',[])):
                    url = item.get('url') or item.get('link','')
                    parts = urlsplit(url)
                    if parts.hostname == 'finance.sina.com.cn' and not parts.username and re.fullmatch(r'/.*doc-\w+\.shtml',parts.path):
                        links.append(urlunsplit(('https',parts.netloc,parts.path,'','')))
            else: links += discover(source, final, text)
        links = list(dict.fromkeys(links)); report['discovered_count'] = len(links)
        for url in links[:max(10,limit*3)]:
            try:
                request_url = 'https://api-one-wscn.awtmt.com/apiv1/content/articles/' + urlsplit(url).path.split('/')[-1] + '?extract=0' if spec['row'] in (66,68) else url
                if spec['row'] in XUEQIU_ACCOUNTS and urlsplit(url).hostname == 'www.xueqiu.com':
                    request_url = 'https://www.xueqiu.com/statuses/show.json?id='+urlsplit(url).path.split('/')[-1]
                text, path, final = feeds[url] if url in feeds else fetch.get(request_url)
                if spec['row'] in (66,68) or spec['row'] in XUEQIU_ACCOUNTS: final = url
                data = parse_article(source, final, text)
                if len(data['body']) < 100 or not data['title']:
                    raise ValueError('未取得可用分析正文')
                report['body_count'] += 1
                record = {'source_id':source,'source_name':spec['name'],'publisher_name':spec['name'], 'collection_platform':urlsplit(final).hostname, 'source_url':url,'source_listing_url':spec['entry_urls'][0], 'external_id':urlsplit(url).path.rstrip('/').split('/')[-1].replace('.html',''), 'title':data['title'],'body':data['body'], 'collected_at':now(),'raw_path':path,'content_scope':'public_article', 'published_at':'', 'language':'zh' if spec['self_media'] or spec['row'] in (39,41,45,53) else 'en'}
                if spec['row'] == 76:
                    record['external_id'] = re.fullmatch(r'/f-d-(\d+)\.html',urlsplit(url).path)[1]
                record = normalize_candidate(record)
                require_verified_publication(record)
                candidates.append(record)
                if len(candidates) >= limit: break
            except PublicationTimeError as error:
                report['time_skipped'] += 1
                report['errors'].append({'url':url,'error':str(error)})
            except (OSError, ValueError) as error:
                report['errors'].append({'url':url,'error':str(error)[:250]})
        report['status'] = 'ok' if candidates else 'failed'
        report['health_status'] = 'available' if candidates else 'time_unverified' if report['body_count'] else 'content_unavailable'
        if not links:
            report['health_status'] = 'discovery_failed'
            report['error'] = '当前目录未返回可解析的文章链接；不能据此判断需要登录'
        elif not candidates: report['error'] = (report['errors'][0]['error'] if report['errors'] else '未取得时间合格的正文')
    except (OSError, ValueError) as error:
        report['error'] = str(error)[:250]
        report['health_status'] = (error.status if isinstance(error, AccessError) else
                                   'requires_login' if isinstance(error, HTTPError) and error.code == 401 else
                                   'access_blocked' if isinstance(error, HTTPError) and error.code in (403,405,412,432,429,567) else 'network_error')
        if report['health_status'] == 'access_blocked' and isinstance(error, HTTPError):
            report['error'] = '公开入口返回 HTTP {}；属于访问限制，尚无需要账号登录的证据'.format(error.code)
    report.update(candidates=len(candidates), pages=fetch.pages, checked_at=now())
    if candidates: report.update(sample_url=candidates[0]['source_url'], sample_published_at=candidates[0]['published_at'])
    return candidates, report


def crawl_verified_sources(output, source_ids=None, limit_per_source=5):
    output = Path(output); output.mkdir(parents=True,exist_ok=True)
    ids = list(source_ids or SOURCES)
    if any(source not in SOURCES for source in ids): raise ValueError('未知来源')
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda source: collect_one(source, output, limit_per_source), ids))
    candidates = [c for group, report in results for c in group]
    report = {'adapter':'verified_sources','sources':[r for group,r in results], 'candidate_count':len(candidates),'collected_at':now()}
    atomic_jsonl(output / 'verified-candidates.jsonl', candidates)
    atomic_json(output / 'verified-collection.json',report)
    return report
