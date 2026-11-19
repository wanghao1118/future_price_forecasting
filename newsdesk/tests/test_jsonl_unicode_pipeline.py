"""Unicode separators inside JSON strings are data, never JSONL boundaries."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from newsdesk.history_backfill import Backfill
from newsdesk.history_media import _read_jsonl
from newsdesk.history_merge import merge_channel_reports
from newsdesk.ingest import prepare_candidate, read_candidates, review_candidates, save_invalid_candidates


SPECIAL = '供应\u2028需求\u2029储备\u0085运输'


def candidate(identifier):
    return {'candidate_id':'cls:'+identifier,'source_id':'cls','source_name':'Fixture',
        'external_id':identifier,'title':'公开原油新闻 '+SPECIAL,
        'body':'真实原始正文 '+SPECIAL+'；逐字保留。',
        'source_url':'https://www.cls.cn/detail/'+identifier,
        'published_date':'2026-01-05','collected_at':'2026-10-30T10:00:00+00:00',
        'raw_path':str(Path(__file__).parent / 'fixtures/publication-time.html')}


def encoded_rows(rows):
    return '\n'.join(json.dumps(row,ensure_ascii=False) for row in rows)+'\n'


class UnicodePipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()

    def test_media_reader_preserves_three_unicode_separators_and_appends_only_missing_lf(self):
        rows = [candidate('1'),candidate('2')]
        path = self.root/'candidates.jsonl'
        text = encoded_rows(rows)
        self.assertGreater(len(text.splitlines()),2,'fixture must expose splitlines corruption')
        path.write_text(text[:-1])
        self.assertEqual(_read_jsonl(path),rows)
        self.assertEqual(path.read_text(),text)
        self.assertFalse(path.with_suffix('.jsonl.partial-tail.txt').exists())

    def test_interrupted_tail_recovery_keeps_valid_unicode_record_unchanged(self):
        good = encoded_rows([candidate('1')])
        tail = '{"body":"partial'+SPECIAL
        path = self.root/'candidates.jsonl'
        path.write_text(good+tail)
        self.assertEqual(_read_jsonl(path),[candidate('1')])
        self.assertEqual(path.read_text(),good)
        self.assertEqual(path.with_suffix('.jsonl.partial-tail.txt').read_text(),tail+'\n')

    def test_genuine_corrupt_lf_terminated_or_interior_records_are_not_repaired(self):
        for suffix in ['{"body":\n','{invalid}\n'+encoded_rows([candidate('2')])]:
            with self.subTest(suffix=suffix):
                path = self.root/'candidates.jsonl'
                original = encoded_rows([candidate('1')])+suffix
                path.write_text(original)
                with self.assertRaises(ValueError):
                    _read_jsonl(path)
                self.assertEqual(path.read_text(),original)

    def test_merge_then_agent_input_keeps_two_physical_records_without_quarantine(self):
        rows = [candidate('1'),candidate('2')]
        path = self.root/'input.jsonl'
        path.write_text(encoded_rows(rows))
        merged = merge_channel_reports('cls','2026-01-01','2026-01-31',self.root/'merged',{
            'public_topics':{'source_id':'cls','status':'complete','candidate_count':2,'candidates_path':str(path)}})
        self.assertEqual(merged['candidate_count'],2)
        self.assertEqual(merged['errors'],[])
        self.assertTrue(merged['coverage']['complete'])
        invalid = []
        prepared = read_candidates(merged['candidates_path'],invalid_records=invalid)
        self.assertEqual(invalid,[])
        self.assertEqual([row['body'] for row in prepared],[row['body'] for row in rows])
        seen = []
        class Reviewer:
            def review(self,batch):
                seen.extend(batch)
                return [{'candidate_id':row['candidate_id'],'relevant':False,'primary_category':'',
                    'categories':[],'reason':'Fixture excludes this item','confidence':'高',
                    'category_reasons':[],'evidence':[],'summary':'','attributes':None} for row in batch]
        result = review_candidates(prepared,self.root/'review',reviewer=Reviewer(),workers=1)
        self.assertEqual(len(result),2)
        self.assertEqual([row['body'] for row in seen],[row['body'] for row in rows])

    def test_invalid_record_journal_round_trip_uses_only_lf(self):
        first = {'candidate_file':'fixture','source_id':'cls','line':1,'error':'invalid','raw_line':SPECIAL}
        second = dict(first,line=2)
        save_invalid_candidates(self.root,[first])
        report = save_invalid_candidates(self.root,[first,second])
        self.assertEqual(report['invalid_count'],2)
        saved = [json.loads(line) for line in Path(report['invalid_candidates_path']).read_text().split('\n') if line.strip()]
        self.assertEqual(saved,[first,second])

    def test_completed_chunk_recovery_uses_physical_records_and_never_double_counts(self):
        directory = self.root/'review/history-unicode'
        directory.mkdir(parents=True)
        (directory/'candidates.jsonl').write_text(encoded_rows([candidate('1'),candidate('2')]))
        (directory/'pipeline.json').write_text(json.dumps({'status':'complete','created':2,'reviewed_count':2}))
        specs = {'cls':{'name':'Fixture','group':'media'}}
        first = Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',specs)
        self.assertEqual(len(first.ledger),2)
        self.assertEqual(first.state['counts']['created'],2)
        resumed = Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',specs)
        self.assertEqual(resumed.ledger,first.ledger)
        self.assertEqual(resumed.state['counts']['created'],2)

    def test_completed_legacy_chunk_quarantines_invalid_rows_and_recovers_valid_rows_once(self):
        directory = self.root/'review/history-legacy'
        directory.mkdir(parents=True)
        good = candidate('good')
        unicode_row = candidate('unicode')
        placeholder = dict(candidate('placeholder'), body='...')
        candidate_path = directory/'candidates.jsonl'
        lines = [json.dumps(good,ensure_ascii=False), json.dumps(placeholder,ensure_ascii=False),
                 json.dumps(unicode_row,ensure_ascii=False), '  {"broken":  ']
        original = '\n'.join(lines)+'\n'
        candidate_path.write_text(original)
        report = {'status':'complete','created':3,'reviewed_count':4,'updated':1,'written':4}
        (directory/'pipeline.json').write_text(json.dumps(report))
        legacy_fingerprint = {key:placeholder.get(key,'') for key in ('title','body','published_at','canonical_url')}
        legacy_fingerprint['canonical_url'] = placeholder['source_url']
        legacy_fingerprint['published_date'] = placeholder['published_date']
        legacy_hash = hashlib.sha256(json.dumps(legacy_fingerprint,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        old_reviewed_key = placeholder['candidate_id']+':'+legacy_hash
        (self.root/'review-ledger.json').write_text(json.dumps({old_reviewed_key:'reviewed'}))
        specs = {'cls':{'name':'Fixture','group':'media'}}
        first = Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',specs)
        self.assertEqual(first.state['counts']['created'],3)
        self.assertEqual(first.state['counts']['reviewed_count'],4)
        self.assertEqual(first.state['counts']['updated'],1)
        self.assertEqual(first.state['completed_chunks'], ['history-legacy'])
        for row in [good,unicode_row]:
            prepared = prepare_candidate(row)
            self.assertEqual(first.ledger[prepared['candidate_id']+':'+prepared['content_hash']], 'reviewed')
        self.assertEqual(first.ledger[old_reviewed_key], 'reviewed')
        self.assertEqual(sum(value=='invalid' for value in first.ledger.values()),2)
        self.assertEqual(sum(value=='reviewed' for value in first.ledger.values()),3)
        audit_path = self.root/'invalid-candidates.jsonl'
        audit_text = audit_path.read_text()
        audit = [json.loads(line) for line in audit_text.split('\n') if line]
        self.assertEqual([entry['line'] for entry in audit], [2,4])
        self.assertEqual([entry['raw_line'] for entry in audit], [lines[1],lines[3]])
        self.assertTrue(all(entry['candidate_file']==str(candidate_path.resolve()) and entry['error'] for entry in audit))
        self.assertEqual(candidate_path.read_text(), original)
        resumed = Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',specs)
        self.assertEqual(resumed.ledger, first.ledger)
        self.assertEqual(resumed.state['counts'], first.state['counts'])
        self.assertEqual(resumed.state['completed_chunks'], ['history-legacy'])
        self.assertEqual(audit_path.read_text(), audit_text)

    def test_completed_legacy_invalid_row_does_not_create_a_reviewed_key(self):
        directory = self.root/'review/history-invalid-only'
        directory.mkdir(parents=True)
        placeholder = dict(candidate('placeholder'), body='...')
        (directory/'candidates.jsonl').write_text(encoded_rows([placeholder]))
        (directory/'pipeline.json').write_text(json.dumps({'status':'complete','reviewed_count':1}))
        runner = Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',
                          {'cls':{'name':'Fixture','group':'media'}})
        self.assertEqual(list(runner.ledger.values()), ['invalid'])
        self.assertEqual(runner.state['counts']['reviewed_count'],1)


if __name__ == '__main__':
    unittest.main()
