import json
from pathlib import Path
import tempfile
import unittest

from newsdesk.publication_time import audit_publication, normalize_candidate
from newsdesk.server import validate_ingestion
from time_gate_agent.local import LocalTimeGate


class PublicationTimeTests(unittest.TestCase):
    def audit(self, source, text, **fields):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'raw.html'
            path.write_text(text)
            return audit_publication(dict(source_id=source, external_id='123', source_url='https://example.com/20260101/c123.shtml', raw_path=str(path), **fields))

    def test_epoch_is_matched_to_article_id_not_neighbour(self):
        text = json.dumps({'data': [{'id': 999, 'ctime': 1788220800}, {'id': 123, 'ctime': 1767225600}]})
        a = self.audit('cls', text, published_at='2026-01-01T00:00:00+08:00')
        self.assertEqual(a['published_at'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(a['timezone_status'], 'verified_epoch')

    def test_missing_raw_record_is_unverified(self):
        a = self.audit('cls', '{"data":[{"id":999,"ctime":1767225600}]}', published_at='2026-01-01T08:00:00+08:00')
        self.assertEqual(a['timezone_status'], 'unverified_time')

    def test_epoch_milliseconds_survive_utc_validation(self):
        from newsdesk.publication_time import require_verified_publication
        a = self.audit('stcn', '{"data":[{"id":123,"time":1767225600123}]}', published_at='')
        self.assertEqual(a['published_at'], '2026-01-01T00:00:00.123000+00:00')
        self.assertEqual(require_verified_publication(dict(a, source_id='stcn')), a['published_at'])

    def test_aa_clock_is_hong_kong_not_browser_zone(self):
        a = self.audit('aastocks', "ConvertToLocalTime({dt:'2026/01/01 08:12'});", published_at='')
        self.assertEqual(a['published_at'], '2026-01-01T00:12:00+00:00')
        self.assertEqual(a['publication_precision'], 'minute')

    def test_morgan_midnight_in_both_dst_and_standard_time_is_date_only(self):
        for raw, expected in [('2026-01-15T05:00:00Z', '2026-01-15'), ('2026-09-17T04:00:00Z', '2026-09-17')]:
            a = self.audit('institution_45', '<meta name="content_publishedAt" content="' + raw + '">', published_at=raw)
            self.assertEqual(a['published_at'], '')
            self.assertEqual(a['published_date'], expected)
            self.assertEqual(a['timezone_status'], 'date_only')
            self.assertEqual(a['original_published_at'], raw)

    def test_goldman_naive_date_and_offset_midnight_never_become_clock(self):
        for raw in ['2026-06-18T00:00:00', '2026-06-18T00:00:00-5:00']:
            a = self.audit('goldman_sachs', '<script type="application/ld+json">' + json.dumps({'@type':'Article', 'datePublished':raw}) + '</script>', published_at='')
            self.assertEqual(a['published_at'], '')
            self.assertEqual(a['published_date'], '2026-06-18')

    def test_nomura_explicit_offset_and_naive_non_midnight_differ(self):
        for raw, status in [('2026-04-20T06:18:00+00:00','verified_offset'), ('2026-04-20T06:18:00','unverified_timezone')]:
            a = self.audit('institution_46', '<script type="application/ld+json">' + json.dumps({'@type':'Article','datePublished':raw}) + '</script>', published_at=raw)
            self.assertEqual(a['timezone_status'], status)
            if status == 'unverified_timezone': self.assertEqual(a['published_at'], '')

    def test_caixin_article_clock_corrected_without_guessing_zone(self):
        a = self.audit('caixin', '<span id="pubtime_baidu">2026-01-04 20:29:26</span>', published_at='2026-01-05T14:19:22+08:00')
        self.assertEqual(a['published_at'], '')
        self.assertEqual(a['source_published_at'], '2026-01-04 20:29:26')
        self.assertEqual(a['published_date'], '2026-01-04')
        self.assertEqual(a['timezone_status'], 'unverified_timezone')

    def test_caixin_listing_is_not_claimed_as_first_publication(self):
        a = self.audit('caixin', json.dumps({'datas':[{'nid':123,'time':'2026-01-05 14:19:22'}]}), published_at='2026-01-05T14:19:22+08:00')
        self.assertEqual(a['timezone_status'], 'unverified_time')
        self.assertEqual(a['published_at'], '')

    def test_date_only_broker_not_assigned_headquarters_timezone(self):
        a = self.audit('institution_11', '<div>2026-01-05</div>', published_at='', published_date='2026-01-05', publication_precision='date')
        self.assertEqual(a['source_timezone'], '')
        self.assertEqual(a['timezone_status'], 'date_only')

    def test_metadata_roundtrip_and_gate_rejects_uncertain_time(self):
        a = self.audit('caixin', '<span id="pubtime_baidu">2026-01-04 20:29:26</span>', published_at='2026-01-04T20:29:26+08:00')
        from newsdesk.publication_time import AUDIT_FIELDS
        metadata = validate_ingestion({'source_id':'caixin', 'published_date':a['published_date'], **{k:a[k] for k in AUDIT_FIELDS}})
        article = {'id':'a','body':'油价相关的真实新闻内容','ingestion':metadata,'published_at':a['published_at'],'assets':['WTI']}
        result = LocalTimeGate([article]).select({'case_id':'a','family':'WTI','input_date':'2026-01-05','target_date':'2026-01-06'})
        self.assertEqual(result['articles'], [])
        self.assertEqual(result['audit']['rejection_counts'], {'unverified_publication_time':1})

    def test_normalization_cannot_restore_midnight_during_next_ingest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'raw.html'
            path.write_text('<meta name="content_publishedAt" content="2026-09-17T04:00:00Z">')
            item = {'source_id':'institution_45','raw_path':str(path),'published_at':'2026-09-17T04:00:00+00:00'}
            first = normalize_candidate(item)
            second = normalize_candidate(first)
            self.assertEqual(first['published_at'], '')
            self.assertEqual(second['published_at'], '')
            self.assertEqual(second['original_published_at'], item['published_at'])
            self.assertEqual(second['published_at_precision'], 'date')


if __name__ == '__main__':
    unittest.main()
