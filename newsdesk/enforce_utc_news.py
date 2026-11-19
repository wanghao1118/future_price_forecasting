"""One-off removal of unverifiable publications and UTC normalization.

Backs up the live SQLite database including WAL before the atomic migration.
Source evidence strings remain unchanged; they are provenance, not UTC clocks.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from newsdesk.publication_time import PublicationTimeError, require_verified_publication, utc_timestamp
from newsdesk.server import DATE_FIELDS, DEFAULT_DATABASE


def visible(record):
    return not record.get('is_demo') and record.get('agent_review', {}).get('relevance') is not False


def run(database, output, apply=False):
    database, output = Path(database).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(str(database), timeout=30)
    backup_path = output / 'before.sqlite3'
    # A reserved lock excludes competing writers throughout backup and commit.
    connection.execute('BEGIN IMMEDIATE')
    try:
        if apply:
            reader = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            backup = sqlite3.connect(str(backup_path))
            reader.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
            backup.close(); reader.close()
        rows = connection.execute('SELECT id,payload,updated_at FROM news ORDER BY id').fetchall()
        deleted, updated = [], []
        reasons, sources = Counter(), Counter()
        before_visible = kept_visible = 0
        for identifier, payload, updated_at in rows:
            original = json.loads(payload)
            before_visible += visible(original)
            try:
                # Demo fixtures have no source evidence and do not belong in a
                # real-news-only archive under the new admission requirement.
                require_verified_publication(original)
            except PublicationTimeError as error:
                reason = 'demo_without_source_evidence' if original.get('is_demo') else original.get('ingestion', {}).get('timezone_status') or 'unverified_time'
                reasons[reason] += 1
                sources[original.get('source_name', '')] += 1
                deleted.append({'id': identifier, 'reason': reason, 'error': str(error), 'visible': visible(original), 'record': original})
                continue
            new = deepcopy(original)
            for field in DATE_FIELDS + ['created_at', 'updated_at']:
                value = new.get(field)
                if not value: continue
                if field in {'occurred_at', 'effective_at'} and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
                    continue
                new[field] = utc_timestamp(value)
            for section, field in [('ingestion','timezone_checked_at'), ('agent_review','reviewed_at')]:
                if new.get(section, {}).get(field):
                    new[section][field] = utc_timestamp(new[section][field])
            kept_visible += visible(new)
            if new != original:
                updated.append({'id': identifier, 'record': new, 'old_payload': payload})
        # Persist the deletion audit before any mutation. A failed write leaves
        # the transaction unchanged, and a complete backup is already present.
        (output / 'deleted.jsonl').write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in deleted))
        if apply:
            connection.executemany('DELETE FROM news WHERE id=?', [(row['id'],) for row in deleted])
            connection.executemany('UPDATE news SET payload=?,updated_at=? WHERE id=?',
                [(json.dumps(row['record'], ensure_ascii=False), row['record']['updated_at'], row['id']) for row in updated])
            connection.execute('INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)',
                ('publication_time_policy', 'verified-clock-utc-v1'))
            connection.commit()
        else:
            connection.rollback()
        summary = {'applied': apply, 'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'before_total': len(rows), 'before_visible': before_visible,
                   'deleted_total': len(deleted), 'deleted_visible': sum(row['visible'] for row in deleted),
                   'deleted_by_reason': dict(reasons), 'deleted_by_source': dict(sources),
                   'retained_total': len(rows) - len(deleted), 'retained_visible': kept_visible,
                   'normalized_records': len(updated), 'storage_timezone': 'UTC+0',
                   'backup': str(backup_path) if apply else None}
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        return summary
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=DEFAULT_DATABASE)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.output, args.apply), ensure_ascii=False, indent=2))
