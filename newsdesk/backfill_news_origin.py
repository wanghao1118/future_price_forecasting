"""Initialize original issuer fields from explicit historical attribution."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from newsdesk.news_origin import FIELDS, explicit_origin
from newsdesk.source_authority import assess as assess_authority
from newsdesk.server import DEFAULT_DATABASE


def run(database, output, apply=False):
    database, output = Path(database).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    connection = sqlite3.connect(str(database), timeout=30)
    connection.execute('BEGIN IMMEDIATE')
    try:
        if apply:
            reader = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)
            backup = sqlite3.connect(str(output / 'before.sqlite3'))
            reader.backup(backup)
            backup.execute('PRAGMA journal_mode=DELETE')
            backup.close(); reader.close()
        changes, counts = [], Counter(total=0, attributed=0, unspecified=0,
                                     visible=0, visible_attributed=0, visible_unspecified=0)
        for identifier, payload in connection.execute('SELECT id,payload FROM news ORDER BY id').fetchall():
            record = json.loads(payload)
            if not record.get('origin_name') and record.get('origin_method') not in {'manual', 'agent'}:
                result = explicit_origin(record)
            else:
                result = {field: record.get(field, '') for field in FIELDS}
            for field in FIELDS:
                record[field] = result[field]
            counts['total'] += 1
            counts['attributed' if record['origin_name'] else 'unspecified'] += 1
            if record.get('agent_review', {}).get('relevance') is not False and not record.get('is_demo'):
                counts['visible'] += 1
                counts['visible_attributed' if record['origin_name'] else 'visible_unspecified'] += 1
            if any(json.loads(payload).get(field) != result[field] for field in FIELDS):
                record['origin_authority'] = assess_authority(record)
                changes.append({'id':identifier, 'origin':result, 'record':record})
        (output / 'attribution.jsonl').write_text(''.join(json.dumps({'id':row['id'], **row['origin']}, ensure_ascii=False)+'\n' for row in changes))
        if apply:
            connection.executemany('UPDATE news SET payload=? WHERE id=?',
                [(json.dumps(row['record'], ensure_ascii=False), row['id']) for row in changes])
            connection.commit()
        else:
            connection.rollback()
        report = {'applied':apply, 'checked_at':datetime.now(timezone.utc).isoformat(),
                  **dict(counts), 'updated':len(changes),
                  'backup':str(output / 'before.sqlite3') if apply else None}
        (output / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        return report
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
