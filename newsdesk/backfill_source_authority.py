"""Apply workbook authority tiers without altering news or publication times."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from newsdesk.source_authority import assess, coverage, registry


def run(database, output):
    database, output = Path(database), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    backup = output / 'before.sqlite3'
    connection = sqlite3.connect(str(database), timeout=30)
    try:
        if not backup.exists():
            saved = sqlite3.connect(str(backup))
            try:
                connection.backup(saved)
                saved.execute('PRAGMA journal_mode=DELETE')
            finally:
                saved.close()
        updated = 0
        records = []
        with connection:
            connection.execute('BEGIN IMMEDIATE')
            for identifier, payload in connection.execute('SELECT id,payload FROM news').fetchall():
                record = json.loads(payload)
                if record.get('is_demo'):
                    continue
                authority = assess(record)
                if record.get('origin_authority') != authority:
                    record['origin_authority'] = authority
                    connection.execute('UPDATE news SET payload=? WHERE id=?',
                                       (json.dumps(record, ensure_ascii=False), identifier))
                    updated += 1
                records.append(record)
        value, _ = registry()
        report = {'checked_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                  'version': value['version'], 'total': len(records), 'updated': updated,
                  'rated': sum(bool(r['origin_authority']['level']) for r in records),
                  'levels': dict(Counter(r['origin_authority']['level'] for r in records if r['origin_authority']['level'])),
                  'backup': str(backup.resolve())}
        (output / 'migration.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
        return report
    finally:
        connection.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=Path(__file__).with_name('data') / 'news.sqlite3')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(run(args.database, args.output), ensure_ascii=False, indent=2))
