"""Audit and repair publication metadata with backup and optimistic concurrency."""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from newsdesk.publication_time import AUDIT_FIELDS, VERIFIED, audit_publication, iso
from newsdesk.server import validate_ingestion


def run(database, output, evidence_dir=None, apply=False):
    database, output = Path(database).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    read = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
    # The SQLite backup API includes committed WAL changes.
    if apply:
        backup = sqlite3.connect(str(output / 'before.sqlite3'))
        read.backup(backup); backup.close()
    rows = read.execute('SELECT id,payload,updated_at FROM news ORDER BY id').fetchall()
    read.close()
    overrides = {}
    if evidence_dir:
        root = Path(evidence_dir).resolve()
        for p in (root / 'live').glob('caixin-*.html'):
            overrides[p.stem[len('caixin-'):]] = p
    counts, by_source, plans = Counter(), defaultdict(Counter), []
    for identifier, payload, updated_at in rows:
        record = json.loads(payload)
        if record.get('is_demo'): continue
        candidate = dict(record)
        if identifier in overrides:
            candidate['publication_raw_path'] = str(overrides[identifier])
        result = audit_publication(candidate)
        changed = json.loads(payload)
        # Preserve an equivalent stored representation; only real instant
        # changes and removed artificial instants count as time corrections.
        old_point, new_point = iso(record.get('published_at')), iso(result['published_at'])
        if old_point and new_point and old_point.tzinfo and new_point.tzinfo and old_point == new_point:
            result['published_at'] = record['published_at']
        changed['published_at'] = result['published_at']
        meta = dict(record.get('ingestion') or {})
        for key in ('published_date', 'publication_precision', *AUDIT_FIELDS):
            meta[key] = result[key]
        old_meta = record.get('ingestion') or {}
        if all(old_meta.get(k, '') == meta.get(k, '') for k in meta if k != 'timezone_checked_at'):
            meta['timezone_checked_at'] = old_meta.get('timezone_checked_at', meta['timezone_checked_at'])
        changed['ingestion'] = validate_ingestion(meta)
        if record.get('agent_review', {}).get('relevance') is not False:
            counts[result['timezone_status']] += 1
            by_source[record['source_name']][result['timezone_status']] += 1
        if changed != record:
            changed['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            plans.append({'id': identifier, 'old_payload': payload, 'old_updated_at': updated_at,
                          'new_record': changed, 'time_changed': record['published_at'] != changed['published_at']})
    (output / 'changes.json').write_text(json.dumps(plans, ensure_ascii=False))
    committed, conflicts = [], []
    if apply:
        connection = sqlite3.connect(str(database), timeout=30)
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            for p in plans:
                new = p['new_record']
                cursor = connection.execute('UPDATE news SET payload=?,updated_at=? WHERE id=? AND payload=? AND updated_at=?',
                    (json.dumps(new, ensure_ascii=False), new['updated_at'], p['id'], p['old_payload'], p['old_updated_at']))
                (committed if cursor.rowcount == 1 else conflicts).append(p['id'])
        connection.close()
    report = {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'), 'apply': apply,
              'database': str(database), 'checked_non_demo': sum(not json.loads(r[1]).get('is_demo') for r in rows),
              'visible_news': sum(counts.values()), 'verified_clock': sum(counts[k] for k in VERIFIED),
              'status_counts': dict(counts), 'by_source': {k:dict(v) for k,v in sorted(by_source.items())},
              'metadata_changes': len(plans), 'published_at_changes': sum(p['time_changed'] for p in plans),
              'committed': len(committed), 'conflicts': conflicts,
              'backup': str(output / 'before.sqlite3') if apply else None}
    (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, default=Path(__file__).parent / 'data/news.sqlite3')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--evidence-dir', type=Path)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.output, args.evidence_dir, args.apply), ensure_ascii=False, indent=2))
