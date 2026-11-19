import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from newsdesk.history_commodity_ths import Fetcher, parse_listing, run_source


def listing(day, ids=('1','2'), body='真实公开快讯：原油出口量增加。 ...', next_url=None):
    compact = day.replace('-','')
    result = '<title>期货快讯_期货_同花顺财经</title><div class="view_otherday">'+day+'</div><script>qhkx_list/</script>'
    for key in ids:
        result += ('<li><span class="arc-title"><a href="https://goodsfu.10jqka.com.cn/'+compact+'/c'+key+'.shtml">真实快讯 '+key+'</a>'
                   '<span>'+day[5:7]+'月'+day[8:]+'日 12:30</span></span><a class="arc-cont news-link" href="#">'+body+'</a></li>')
    if next_url:
        result += '<a href="'+next_url+'" class="next">下一页</a>'
    return result


class FakeFetcher:
    def __init__(self, directory, responses):
        self.directory, self.responses = directory, responses
        self.calls=[]
    def get(self, source_id,url):
        self.calls.append((source_id,url))
        value=self.responses[url]
        if isinstance(value,Exception):
            raise value
        path=self.directory/(str(len(self.calls))+'.html')
        path.write_text(value)
        return value,path,'2026-10-30T09:00:00+00:00'


class CommodityHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def test_public_listing_excerpt_keeps_real_date_and_source_identity(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        rows,following=parse_listing(listing('2026-01-05'),url,self.root/'raw.html','2026-10-30T09:00:00+00:00','2026-01-05')
        self.assertIsNone(following)
        self.assertEqual(rows[0]['candidate_id'],'ths:1')
        self.assertEqual(rows[0]['published_at'],'2026-01-05T12:30:00+08:00')
        self.assertEqual(rows[0]['body'],'真实公开快讯：原油出口量增加。 ...')
        self.assertEqual(rows[0]['content_scope'],'public_article_excerpt')
    def test_other_date_and_challenge_are_not_empty_covered_days(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        for raw in [listing('2026-01-06'),'<script>challenge</script>']:
            with self.assertRaises(ValueError):
                parse_listing(raw,url,self.root/'raw','now','2026-01-05')
    def test_following_page_cannot_leave_the_public_day_scope(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        with self.assertRaises(ValueError):
            parse_listing(listing('2026-01-05',next_url='https://example.test/page2'),url,self.root/'raw','now','2026-01-05')
    def test_next_page_supports_class_before_href(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        raw=listing('2026-01-05')+'<a class="next" href="index_2.html">下一页</a>'
        _,following=parse_listing(raw,url,self.root/'raw','now','2026-01-05')
        self.assertEqual(following,url+'index_2.html')
    def test_months_interleave_and_resume_each_saved_public_page(self):
        urls={f'https://goodsfu.10jqka.com.cn/qhkx_list/{day.replace("-","")}/':listing(day,ids)
              for day,ids in [('2026-01-01',('1','2')),('2026-09-01',('3','4'))]}
        fetcher=FakeFetcher(self.root,urls)
        report=run_source('ths','2026-01-01','2026-09-01',self.root/'out',priority_months=['2026-01','2026-09'],max_articles=2,fetcher=fetcher)
        self.assertEqual(report['monthly_candidate_counts']['2026-01'],1)
        self.assertEqual(report['monthly_candidate_counts']['2026-09'],1)
        self.assertFalse(report['coverage']['complete'])
        second=run_source('ths','2026-01-01','2026-09-01',self.root/'out',priority_months=['2026-01','2026-09'],max_articles=2,fetcher=fetcher)
        self.assertEqual(len(fetcher.calls),2)
        self.assertEqual(second['candidate_count'],4)
        self.assertEqual(second['completed_days'],2)
    def test_one_day_cap_is_not_complete_until_pending_excerpt_queue_drained(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=FakeFetcher(self.root,{url:listing('2026-01-05')})
        first=run_source('ths','2026-01-05','2026-01-05',self.root/'out',max_articles=1,fetcher=fetcher)
        self.assertFalse(first['terminal']);self.assertFalse(first['coverage']['complete'])
        second=run_source('ths','2026-01-05','2026-01-05',self.root/'out',max_articles=1,fetcher=fetcher)
        self.assertEqual(second['candidate_count'],2)
        self.assertEqual(second['status'],'complete')
        self.assertTrue(second['terminal'])
        self.assertEqual(len(fetcher.calls),1)
    def test_missing_body_is_quarantined_never_replaced_with_title(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=FakeFetcher(self.root,{url:listing('2026-01-05',('1',),body='')})
        report=run_source('ths','2026-01-05','2026-01-05',self.root/'out',fetcher=fetcher)
        self.assertEqual(report['candidate_count'],0)
        self.assertEqual(report['invalid_count'],1)
        self.assertNotEqual(report['status'],'complete')

    def test_placeholder_excerpt_is_gap_retaining_raw_and_next_page_is_processed(self):
        url = 'https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        following = url + 'index_2.html'
        fetcher = FakeFetcher(self.root, {
            url: listing('2026-01-05', ('1',), body='…！？', next_url=following),
            following: listing('2026-01-05', ('2',), body='油')})
        report = run_source('ths', '2026-01-05', '2026-01-05', self.root/'out', fetcher=fetcher)
        self.assertEqual(report['candidate_count'], 1)
        self.assertEqual(report['invalid_count'], 1)
        self.assertEqual(report['status'], 'incomplete')
        self.assertTrue(report['terminal'])
        self.assertEqual(report['coverage']['content_gap_dates'], ['2026-01-05'])
        self.assertEqual(report['coverage']['gap_dates'], ['2026-01-05'])
        saved = json.loads(Path(report['candidates_path']).read_text())
        self.assertEqual(saved['external_id'], '2')
        self.assertEqual(saved['body'], '油')
        invalid = json.loads((self.root/'out/invalid-candidates.jsonl').read_text())['candidate']
        self.assertEqual(invalid['body'], '…！？')
        self.assertEqual(invalid['source_listing_url'], url)
        self.assertTrue(invalid['source_url'].endswith('/c1.shtml'))
        self.assertEqual(invalid['raw_path'], invalid['discovery_raw_path'])
        self.assertIn('…！？', Path(invalid['raw_path']).read_text())
        again = run_source('ths', '2026-01-05', '2026-01-05', self.root/'out', fetcher=fetcher)
        self.assertEqual(len(fetcher.calls), 2)
        self.assertEqual(again['coverage']['content_gap_dates'], ['2026-01-05'])
    def test_403_is_explicit_gap_and_is_not_retried_or_bypassed(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=FakeFetcher(self.root,{url:HTTPError(url,403,'Forbidden',None,None)})
        report=run_source('ths','2026-01-05','2026-01-05',self.root/'out',fetcher=fetcher)
        self.assertTrue(report['terminal']);self.assertFalse(report['coverage']['complete'])
        self.assertEqual(report['coverage']['gap_dates'],['2026-01-05'])
        self.assertEqual(len(fetcher.calls),1)
    def test_source_access_limit_stops_all_remaining_dates_and_survives_restart(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260101/'
        fetcher=FakeFetcher(self.root,{url:HTTPError(url,403,'Forbidden',None,None)})
        report=run_source('ths','2026-01-01','2026-09-30',self.root/'out',fetcher=fetcher)
        self.assertEqual(report['status'],'blocked');self.assertEqual(len(fetcher.calls),1)
        self.assertGreater(report['coverage']['unprocessed_days'],200)
        again=run_source('ths','2026-01-01','2026-09-30',self.root/'out',fetcher=fetcher)
        self.assertEqual(again['status'],'blocked');self.assertEqual(len(fetcher.calls),1)
    def test_interrupted_final_candidate_append_is_recovered_and_replayed(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=FakeFetcher(self.root,{url:listing('2026-01-05')})
        first=run_source('ths','2026-01-05','2026-01-05',self.root/'out',max_articles=1,fetcher=fetcher)
        with Path(first['candidates_path']).open('a') as destination:
            destination.write('{"interrupted":')
        again=run_source('ths','2026-01-05','2026-01-05',self.root/'out',max_articles=1,fetcher=fetcher)
        self.assertEqual(again['candidate_count'],2)
        self.assertEqual(again['status'],'complete')
    def test_transient_error_retries_bounded_three_then_records_gap(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=FakeFetcher(self.root,{url:HTTPError(url,504,'Timeout',None,None)})
        report=run_source('ths','2026-01-05','2026-01-05',self.root/'out',max_pages=3,fetcher=fetcher)
        self.assertTrue(report['terminal']);self.assertEqual(len(fetcher.calls),3)
        self.assertEqual(report['coverage']['gap_dates'],['2026-01-05'])
    def test_robots_404_allows_ordinary_public_listing(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=Fetcher(self.root)
        with patch('newsdesk.history_commodity_ths.HistoryFetcher.get',side_effect=[HTTPError(url,404,'Not found',None,None),('body',self.root/'raw','now')]) as parent:
            self.assertEqual(fetcher.get('ths',url)[0],'body')
            self.assertEqual(parent.call_count,2)
    def test_robots_explicit_disallow_prevents_listing_request(self):
        url='https://goodsfu.10jqka.com.cn/qhkx_list/20260105/'
        fetcher=Fetcher(self.root)
        with patch('newsdesk.history_commodity_ths.HistoryFetcher.get',return_value=('User-agent: *\nDisallow: /qhkx_list/',self.root/'robots','now')) as parent:
            with self.assertRaises(PermissionError):
                fetcher.get('ths',url)
            self.assertEqual(parent.call_count,1)


if __name__=='__main__':
    unittest.main()
