"""Precise, source-verified clocks are required before news reaches the archive."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.enforce_utc_news import run
from newsdesk.ingest import archive_candidates, prepare_candidate, process, read_candidates
from newsdesk.publication_time import PublicationTimeError, utc_timestamp
from newsdesk.server import NewsStore, ValidationError
from newsdesk.tests.test_ingest import candidate, decision


class UTCAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'test.sqlite3'

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, row):
        review = {'reviewer':'fixture', 'reviewed_at':'2026-11-02T00:00:00Z', 'decision':decision(row)}
        return archive_candidates([row], [review], self.database, 'fixture')

    def test_utc_conversion_preserves_instants_at_day_boundary_and_dst(self):
        for raw, expected in [
            ('2026-01-01T00:30:12.123+08:00','2025-12-31T16:30:12.123000+00:00'),
            ('2026-07-01T01:30:00-04:00','2026-07-01T05:30:00+00:00'),
            ('2026-12-01T01:30:00-05:00','2026-12-01T06:30:00+00:00')]:
            self.assertEqual(utc_timestamp(raw), expected)
        for raw in ['', '2026-01-01', '2026-01-01T08:30', '2026-01-01T08:30:00-00:00']:
            with self.assertRaises(PublicationTimeError): utc_timestamp(raw)

    def test_time_only_rejections_complete_with_zero_added_without_calling_agent(self):
        cases = [
            ('institution_46', '<meta name="datePublished" content="2026-01-05">'),
            ('caixin', '<span id="pubtime_baidu">2026-01-05 09:32:00</span>'),
            ('institution_46', '<meta name="datePublished" content="2026-01-05T09:32:00-00:00">'),
            ('institution_46', '<div>no publication evidence</div>')]
        rows = []
        for index, (source, raw) in enumerate(cases):
            path = self.root / f'{index}.html'; path.write_text(raw)
            rows.append(dict(candidate(), candidate_id=str(index), source_id=source,
                raw_path=str(path), publication_raw_path=str(path),
                # Deliberately retain a plausible offset and old verified metadata.
                published_at='2026-01-05T09:32:00+08:00'))
        path = self.root / 'candidates.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        with patch('newsdesk.ingest.CodexReviewer') as reviewer:
            report = process(path, self.root / 'output', self.database)
        reviewer.assert_not_called()
        self.assertEqual((report['status'], report['created'], report['invalid_count'], report['time_skipped_count']),
                         ('complete', 0, 0, 4))
        self.assertEqual(NewsStore(self.database, seed_demo=False).all(), [])

    def test_cached_review_and_forged_status_cannot_restore_uncertain_time(self):
        row = dict(candidate(), raw_path='/missing/raw', publication_raw_path='/missing/raw')
        result = self.archive(row)
        self.assertEqual((result['created'], result['archive_time_skipped_count']), (0,1))

    def test_store_rejects_unverified_insert_import_and_timestamp_edit(self):
        self.archive(candidate())
        store = NewsStore(self.database, seed_demo=False)
        original = store.all()[0]
        for stamp in ['', '2026-11-02', '2026-11-02T10:00:00']:
            with self.assertRaises(ValidationError):
                store.save({'title':'No verified clock', 'category':'supply', 'published_at':stamp})
        with self.assertRaises(ValidationError):
            store.save({'published_at':'2026-01-01T02:00:00Z'}, original['id'])
        with self.assertRaises(ValidationError):
            store.import_items([dict(original, published_at='2026-01-01T02:00:00Z')])
        bad = deepcopy(original); bad['ingestion']['timezone_status'] = 'unverified_timezone'
        bad['id'] = '00000000-0000-0000-0000-000000000001'
        with self.assertRaises(ValidationError):
            store.import_items([dict(original, summary='must roll back'), bad])
        self.assertEqual(store.get(original['id']), original)
        stored = store.save({'title':'Explicit timezone', 'category':'supply',
                             'published_at':'2026-01-01T00:30:00+08:00',
                             'collected_at':'2026-01-01T09:00:00+08:00'})
        self.assertEqual(stored['published_at'], '2025-12-31T16:30:00+00:00')
        self.assertEqual(stored['collected_at'], '2026-01-01T01:00:00+00:00')

    def test_atomic_migration_deletes_unverifiable_rows_and_backs_up_every_record(self):
        self.archive(candidate())
        store = NewsStore(self.database, seed_demo=False)
        good = store.all()[0]
        old = deepcopy(good); old['published_at'] = '2026-10-28T09:00:00+08:00'
        excluded = []
        for index, status in enumerate(['date_only','unverified_timezone','unverified_time']):
            row = deepcopy(good); row['id'] = 'bad-' + str(index)
            row['published_at'] = ''; row['ingestion']['timezone_status'] = status
            excluded.append(row)
        with store.connect() as connection:
            connection.execute('UPDATE news SET payload=? WHERE id=?', (json.dumps(old), old['id']))
            connection.executemany('INSERT INTO news VALUES(?,?,?)',
                [(row['id'],json.dumps(row),row['updated_at']) for row in excluded])
        report = run(self.database, self.root / 'migration', apply=True)
        self.assertEqual((report['deleted_total'], report['retained_total'], report['normalized_records']), (3,1,1))
        self.assertEqual(store.all()[0], good)
        with sqlite3.connect(report['backup']) as backup:
            self.assertEqual(backup.execute('SELECT count(*) FROM news').fetchone()[0],4)
        again = run(self.database, self.root / 'repeat')
        self.assertEqual((again['deleted_total'],again['normalized_records']), (0,0))


if __name__ == '__main__':
    unittest.main()
