"""Connectivity means readable content with an admissible publication time.

Probes use production collectors and the publication gate, but never call the
review agent or import probe articles into the archive. Results also refresh on
scheduled collection. Atomic per-source files keep workers independent.
"""
import json
import threading
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from newsdesk.public_company_sources import atomic_json

REGISTRY = Path(__file__).with_name('source_registry.json')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def registry():
    return json.loads(REGISTRY.read_text())['sources']


def update_results(directory, manifest, candidates, source_ids, origin='scheduled'):
    directory = Path(directory); directory.mkdir(parents=True, exist_ok=True)
    records = {s:[] for s in source_ids}
    def walk(value):
        if isinstance(value,dict):
            if value.get('source_id') in records and 'status' in value: records[value['source_id']].append(value)
            for child in value.values(): walk(child)
        elif isinstance(value,list):
            for child in value: walk(child)
    walk(manifest)
    accepted = Counter(c['source_id'] for c in candidates)
    for source, entries in records.items():
        errors = [e.get('error') or e.get('message') for e in entries if e.get('error') or e.get('message')]
        explicit = [e.get('health_status') for e in entries if e.get('health_status')]
        count = accepted[source]
        skipped = manifest.get('time_skipped_by_source',{}).get(source,0)
        status = ('available' if count else explicit[-1] if explicit else
                  'time_unverified' if skipped else
                  'network_error' if not entries or all(e.get('status') in ('failed','error','requires_access','no_public') for e in entries) else 'no_qualified_items')
        sample = next((c for c in candidates if c['source_id']==source), {})
        checked_at = manifest.get('collected_at') or now()
        result = {'source_id':source,'status':status,'checked_at':checked_at, 'origin':origin,
                  'candidate_count':count, 'message':'正文与发布时间验证通过' if count else '；'.join(dict.fromkeys(errors))[:700] or '本轮未取得时间合格的正文；不等同于没有原油相关新闻',
                  'sample_url':sample.get('source_url',''),'sample_published_at':sample.get('published_at',''),
                  'last_success_at':'','run_dir':str(manifest.get('run_dir',''))}
        if not count and skipped:
            result['message'] = f'已取得 {skipped} 条内容，但缺少具体发布时间或可信时区；本轮样本未通过入库时间验证'
        path = directory / (source + '.json')
        if path.exists():
            try: result['last_success_at'] = json.loads(path.read_text()).get('last_success_at','')
            except (OSError,ValueError): pass
        if count: result['last_success_at'] = result['checked_at']
        atomic_json(path,result)


class SourceMaintenance:
    def __init__(self, data_dir, collector=None):
        self.data_dir = Path(data_dir); self.directory = self.data_dir / 'source-health'
        self._lock = threading.Lock(); self._job = {'running':False,'completed':0,'total':0}
        self.job_path = self.directory / 'check-job.json'
        try:
            self._job.update(json.loads(self.job_path.read_text()))
            if self._job['running']:
                self._job.update(running=False,error='上轮手动检测被服务重启中断，可以重新检测')
        except (OSError,ValueError): pass
        self.collector = collector

    def snapshot(self):
        sources = []
        for entry in registry():
            if not entry.get('enabled'): continue
            result = {'status':'unchecked','message':'尚未进行后台连通性检测','checked_at':'','candidate_count':0}
            try: result.update(json.loads((self.directory/(entry['id']+'.json')).read_text()))
            except (OSError,ValueError): pass
            sources.append({'source_id':entry['id'],'name':entry['name'],'url':entry.get('official_url',''),
                            'adapter':entry.get('adapter',''),'authority_row':entry.get('authority_row'), **result})
        with self._lock: job = dict(self._job)
        return {'sources':sources,'counts':dict(Counter(s['status'] for s in sources)), 'total':len(sources),'job':job}

    def trigger(self, source_ids=None):
        enabled = {e['id'] for e in registry() if e.get('enabled')}
        ids = sorted(enabled if source_ids is None else set(source_ids))
        if not ids or any(s not in enabled for s in ids): raise ValueError('请选择已启用的信源')
        with self._lock:
            if self._job['running']: return False
            self._job = {'running':True,'completed':0,'total':len(ids),'started_at':now(),'id':uuid.uuid4().hex[:12]}
            atomic_json(self.job_path,self._job)
        threading.Thread(target=self._run,args=(ids,),daemon=True,name='source-connectivity').start()
        return True

    def _run(self, ids):
        from newsdesk.ingest import collect, read_candidates
        collector = self.collector or collect
        run = self.data_dir / 'source-checks' / self._job['id']
        def probe(source):
            output = run / source
            try:
                manifest = collector(output, source_ids=[source],limit=1,research_limit=1,institution_limit=1)
                candidates = read_candidates(output/'candidates.jsonl')
            except Exception as error:
                manifest = {'source_id':source,'status':'failed','error':str(error)[:500]}; candidates=[]
            update_results(self.directory,manifest,candidates,[source],origin='manual')
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                for future in as_completed([pool.submit(probe,source) for source in ids]):
                    try: future.result()
                    except Exception as error:
                        with self._lock: self._job['error'] = str(error)[:300]
                    finally:
                        with self._lock: self._job['completed'] += 1
        finally:
            with self._lock:
                self._job.update(running=False,finished_at=now())
                atomic_json(self.job_path,self._job)
