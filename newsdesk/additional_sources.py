"""Public AASTOCKS news and Caixin leads; no subscriber text or login bypass."""
import argparse
import concurrent.futures
import json
import re
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

SOURCES = {
    'aastocks': {'name':'阿斯达克研报','host':'www.aastocks.com','feeds':['https://www.aastocks.com/sc/stocks/news/aafn', 'https://www.aastocks.com/sc/stocks/news/aafn/research-report']},
    'caixin': {'name':'财新','host':'www.caixin.com','feeds':['https://www.caixin.com/energy/']},
}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def fetch(url, host):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname != host:
        raise ValueError('Unexpected source host')
    with urlopen(Request(url, headers={'User-Agent':'Mozilla/5.0','Accept':'text/html'}), timeout=12) as response:
        if urlsplit(response.url).hostname != host:
            raise ValueError('Unexpected redirect host')
        raw = response.read(3 * 1024 * 1024 + 1)
        if len(raw) > 3 * 1024 * 1024:
            raise ValueError('Response exceeds size limit')
        return raw


class Page(HTMLParser):
    VOID_TAGS = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self, *, ignore_related=False):
        super().__init__(convert_charrefs=True)
        self.ignore_related = ignore_related
        self.related_depth = 0
        self.meta = {}
        self.links = {}
        self.current = None
        self.content = []
        self.content_depth = 0
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if self.related_depth:
            if tag not in self.VOID_TAGS:
                self.related_depth += 1
            return
        if self.ignore_related and 'newsRelatedHeadline' in a.get('class','').split():
            if tag not in self.VOID_TAGS:
                self.related_depth = 1
            return
        if tag == 'meta':
            key = a.get('property') or a.get('name')
            if key:
                self.meta[key] = a.get('content','')
        if tag == 'a' and a.get('href'):
            self.current = a['href']
            self.links.setdefault(self.current,'')
        if tag in {'script','style'}:
            self.ignored += 1
        if a.get('id') == 'spanContent':
            self.content_depth = 1
        elif self.content_depth and tag == 'div':
            self.content_depth += 1
        if self.content_depth and tag in {'p','br','div'}:
            self.content.append('\n')

    def handle_endtag(self, tag):
        if self.related_depth:
            if tag not in self.VOID_TAGS:
                self.related_depth -= 1
            return
        if tag == 'a':
            self.current = None
        if tag in {'script','style'}:
            self.ignored = max(0,self.ignored - 1)
        if self.content_depth and tag == 'div':
            self.content_depth -= 1
        if self.content_depth and tag in {'p','br'}:
            self.content.append('\n')

    def handle_data(self, text):
        if self.related_depth:
            return
        if self.current and not self.ignored:
            self.links[self.current] += text
        if self.content_depth and not self.ignored:
            self.content.append(text)


def parse_links(raw, source_id, base):
    page = Page()
    page.feed(raw.decode('utf-8',errors='replace'))
    found = []
    for href, text in page.links.items():
        url = urljoin(base,href)
        parsed = urlsplit(url)
        if parsed.hostname != SOURCES[source_id]['host'] or not text.strip():
            continue
        if source_id == 'aastocks' and re.search(r'/aafn-con/(?:NOW\.\d+|IC\d+)/[^/]+/(?:AAFN|INFOCAST)$',parsed.path):
            found.append(url)
        elif source_id == 'caixin' and re.fullmatch(r'/\d{4}-\d{2}-\d{2}/\d+\.html',parsed.path):
            found.append(url)
    return list(dict.fromkeys(found))


def parse_article(raw, url, source_id, raw_path, collected_at):
    text = raw.decode('utf-8',errors='replace')
    page = Page(ignore_related=source_id == 'aastocks')
    page.feed(text)
    title = page.meta.get('og:title','').strip()
    published = ''
    if source_id == 'aastocks':
        # This is the article's displayed time argument, not aa-update/server time.
        match = re.search(r"ConvertToLocalTime\(\{dt:['\"](\d{4}/\d{2}/\d{2} \d{2}:\d{2})['\"]",text)
        if match:
            published = datetime.strptime(match.group(1),'%Y/%m/%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Hong_Kong')).isoformat()
        body = '\n'.join(line.strip() for line in ''.join(page.content).splitlines() if line.strip())
        scope = 'public_article'
        external = re.search(r'/aafn-con/([^/]+)/',url).group(1)
    else:
        # The publicly served lead is intentionally the entire collected scope.
        # Do not inspect scripts or call subscriber-content endpoints.
        match = re.search(r'id=["\']pubtime_baidu["\'][^>]*>(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})<',text)
        if match:
            published = datetime.strptime(match.group(1),'%Y-%m-%d %H:%M:%S').replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat()
        body = page.meta.get('og:description','').strip()
        scope = 'public_article_lead'
        external = urlsplit(url).path.rsplit('/',1)[-1].split('.')[0]
    if not title or not body:
        raise ValueError('No public title or requested public text scope found')
    return {'candidate_id':source_id+':'+external,'source_id':source_id,'source_name':SOURCES[source_id]['name'],'external_id':external,
            'source_url':url,'title':title,'body':body[:18000],'published_at':published,'collected_at':collected_at,
            'raw_path':str(Path(raw_path).resolve()),'content_scope':scope,'title_origin':'source'}


def crawl_additional_sources(output_dir, source_ids=None, limit_per_source=20):
    output = Path(output_dir).resolve()
    output.mkdir(parents=True,exist_ok=True)
    selected = list(SOURCES) if source_ids is None else list(source_ids)
    if any(sid not in SOURCES for sid in selected):
        raise ValueError('Unknown additional source')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')+'-extra-'+uuid.uuid4().hex[:6]
    raw_dir = output/'raw'/run_id
    raw_dir.mkdir(parents=True,exist_ok=True)
    report = {'run_id':run_id,'started_at':now(),'sources':[],'candidates_path':str(output/'extra-candidates.jsonl')}
    candidates = []
    limit = max(1,min(50,int(limit_per_source)))
    for source_id in selected:
        spec = SOURCES[source_id]
        state = {'source_id':source_id,'source_name':spec['name'],'status':'failed','count':0,'errors':[]}
        urls = []
        for number,url in enumerate(spec['feeds']):
            try:
                raw = fetch(url,spec['host'])
                (raw_dir/(source_id+'-listing-'+str(number)+'.html')).write_bytes(raw)
                discovered = parse_links(raw,source_id,url)
                # Each feed contributes topics as published, without oil filtering.
                urls.extend(discovered[:max(1,limit//len(spec['feeds']))])
            except (OSError,ValueError) as error:
                state['errors'].append({'url':url,'error':type(error).__name__+': '+str(error)[:150]})
        unique = {}
        for url in urls:
            identity = re.search(r'/aafn-con/([^/]+)/', url).group(1) if source_id == 'aastocks' else url
            unique.setdefault(identity, url)
        urls = list(unique.values())[:limit]
        def get_article(pair):
            index,url = pair
            try:
                time.sleep(.2)
                raw = fetch(url,spec['host'])
                collected = now()
                path = raw_dir/(source_id+'-article-'+str(index)+'.html')
                path.write_bytes(raw)
                return parse_article(raw,url,source_id,path,collected),None
            except (OSError,ValueError) as error:
                return None,{'url':url,'error':type(error).__name__+': '+str(error)[:150]}
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for row,error in pool.map(get_article,enumerate(urls)):
                if row:
                    candidates.append(row)
                    state['count'] += 1
                if error:
                    state['errors'].append(error)
        state['status'] = 'ok' if state['count'] and not state['errors'] else 'partial' if state['count'] else 'failed'
        report['sources'].append(state)
    with open(report['candidates_path'],'w',encoding='utf-8') as handle:
        for row in candidates:
            handle.write(json.dumps(row,ensure_ascii=False)+'\n')
    report.update(completed_at=now(),candidate_count=len(candidates))
    report['report_path'] = str(output/('additional-run-'+run_id+'.json'))
    Path(report['report_path']).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


if __name__ == '__main__':
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--output-dir',default=str(Path(__file__).resolve().parent/'data'/'ingestion'))
    cli.add_argument('--limit',type=int,default=20)
    args = cli.parse_args()
    print(json.dumps(crawl_additional_sources(args.output_dir,limit_per_source=args.limit),ensure_ascii=False,indent=2))
