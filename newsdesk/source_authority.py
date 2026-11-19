"""Source-level tiers from the user's workbook, separate from agent confidence."""
from collections import Counter
from functools import lru_cache
import json
from pathlib import Path
import re
import unicodedata

REGISTRY_PATH = Path(__file__).with_name('source_authority_registry.json')


def normalize(name):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', name or '')).casefold()


@lru_cache(maxsize=1)
def registry():
    value = json.loads(REGISTRY_PATH.read_text(encoding='utf-8'))
    index = {}
    for entry in value['entries']:
        if entry['level'] not in {'P0', 'P1', 'P2'}:
            raise ValueError('Unknown workbook tier')
        for name in [entry['name'], *entry['aliases']]:
            key = normalize(name)
            if key in index and index[key]['name'] != entry['name']:
                raise ValueError('Conflicting authority alias: ' + name)
            index[key] = entry
    return value, index


def match_source(name, record=None):
    value, index = registry()
    entry = index.get(normalize(name))
    if entry and entry['name'] == '金融时报' and normalize(name) == normalize('金融时报'):
        # The workbook's Chinese designated-disclosure newspaper is not the UK FT.
        text = ' '.join((record or {}).get(k, '') for k in ['origin_evidence', 'title', 'body'])
        if re.search(r'英国[《\s]*金融时报|Financial\s+Times', text, re.I):
            return None
        if not re.search(r'中国[《\s]*金融时报', text):
            return None
    return entry


def issuer_parts(record):
    name = (record.get('origin_name') or '').strip()
    return [part.strip() for part in re.split(r'[、；;]', name) if part.strip()]


def assess(record):
    value, _ = registry()
    result = {'level': '', 'status': 'unresolved', 'matched_source': '', 'reference': '',
              'basis': '', 'notes': '', 'version': value['version']}
    parts = issuer_parts(record)
    if not parts:
        result['notes'] = '尚无可匹配的原始发布机构'
        return result
    matches = [match_source(part, record) for part in parts]
    if any(entry is None for entry in matches):
        result['status'] = 'partial' if any(matches) else 'unlisted'
        if len(parts) > 1:
            result['notes'] = '；'.join(part + '：' + (entry['level'] if entry else '未匹配榜单')
                                        for part, entry in zip(parts, matches))
        elif normalize(parts[0]) == normalize('金融时报'):
            result['status'] = 'ambiguous'
            result['notes'] = '榜单列的是中国《金融时报》；本条未确认是该报，英国 Financial Times 不沿用其档位'
        else:
            result['notes'] = '原始发布机构未列入本次榜单'
        return result
    unique = {entry['name']: entry for entry in matches}
    result['matched_source'] = '、'.join(unique)
    result['reference'] = '；'.join(f"{entry['sheet']}!{entry['level_cell']}" for entry in unique.values())
    result['basis'] = '；'.join(entry['name'] + '：' + entry['basis'] for entry in unique.values())
    result['notes'] = '；'.join(entry['notes'] for entry in unique.values() if entry['notes'])
    levels = {entry['level'] for entry in unique.values()}
    if len(levels) == 1:
        result.update(level=levels.pop(), status='matched')
    else:
        result.update(status='mixed', notes='；'.join(entry['name'] + '：' + entry['level'] for entry in unique.values()))
    return result


def enrich(record):
    return dict(record, origin_authority=assess(record))


def coverage(records, source_registry, last_update=None):
    """Count actual issuer/feed coverage separately from configured collection."""
    value, _ = registry()
    visible = [r for r in records if not r.get('is_demo') and r.get('agent_review', {}).get('relevance') is not False]
    origins, feeds, source_ids = Counter(), Counter(), Counter()
    for record in visible:
        names = {entry['name'] for part in issuer_parts(record) if (entry := match_source(part, record))}
        origins.update(names)
        entry = match_source(record.get('source_name', ''))
        if entry:
            feeds[entry['name']] += 1
        source_ids[record.get('ingestion', {}).get('source_id', '')] += 1
    configured = {}
    for source in source_registry['sources']:
        entry = match_source(source['name'])
        if entry:
            configured.setdefault(entry['name'], []).append(source)
    latest = last_update or {}
    entries = []
    for original in value['entries']:
        entry = dict(original)
        sources = configured.get(entry['name'], [])
        enabled = any(s.get('enabled') for s in sources)
        news_count = origins[entry['name']]
        feed_count = feeds[entry['name']]
        status = '已有新闻' if news_count or feed_count else '已接入，暂无有效入库' if enabled else '未覆盖'
        entry.update(coverage_status=status, issuer_news_count=news_count, feed_news_count=feed_count,
                     collection_enabled=enabled,
                     configured_sources=[{'id': s['id'], 'name': s['name'], 'enabled': s.get('enabled', False),
                                          'access_status': s.get('access_status', ''), 'note': s.get('note', ''),
                                          'last_time_skipped': latest.get('time_skipped_by_source', {}).get(s['id'], 0),
                                          'last_collection_status': 'failed' if s['id'] in latest.get('failed_sources', [])
                                          else 'partial' if s['id'] in latest.get('partial_sources', [])
                                          else 'completed' if s.get('enabled') and latest else 'not_enabled'} for s in sources])
        entries.append(entry)
    return {'workbook': Path(value['source_file']).name, 'version': value['version'],
            'source_sheet': value['source_sheet'], 'source_range': value['source_range'],
            'source_count': len(entries), 'visible_news_count': len(visible),
            'counts': dict(Counter(e['coverage_status'] for e in entries)),
            'collection_enabled_count': sum(e['collection_enabled'] for e in entries),
            'issuer_covered_count': sum(bool(e['issuer_news_count']) for e in entries),
            'feed_covered_count': sum(bool(e['feed_news_count']) for e in entries),
            'rated_news_count': sum(bool(assess(r)['level']) for r in visible),
            'level_counts': dict(Counter(assess(r)['level'] for r in visible if assess(r)['level'])),
            'last_update_at': latest.get('completed_at', ''), 'entries': entries}
