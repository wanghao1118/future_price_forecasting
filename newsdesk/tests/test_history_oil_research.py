"""Public-industry history contracts, with no network/model/archive writes."""

import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError

from newsdesk.domestic_brokers_b import list_url
from newsdesk.history_oil_research import INDUSTRIES, SOURCES, parse_industry_list, run_source


A, B = 'institution_11', 'institution_12'
AN, BN = SOURCES[A]['publisher_names'][0], SOURCES[B]['publisher_names'][0]


def report_url(identifier):
    return 'https://stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/'+identifier+'/index.phtml'


def listing(industry,page,rows=(),last_page=2):
    html = "<script>var t1 = '3';jQuery('#industry').val('"+industry+"');</script>"
    html += "<span class='pagebox_num_nonce'>"+str(page)+"</span>"
    # Real last pages contain only links smaller than their current nonce.
    html += '<a onclick="set_page_num(\''+str(last_page)+'\')">末页</a><table>'
    for identifier,day,publisher in rows:
        html += '<tr><td><a href="'+report_url(identifier)+'" title="真实公开行业研究 '+identifier+'">研究</a></td>'
        html += '<td class="fname05"><span>'+publisher+'</span></td><td>'+day+'</td></tr>'
    html += '</table>'
    return html if rows else html+'没有找到相关内容'


def detail(identifier,day,publisher=AN,body=None):
    body = body if body is not None else '真实公开研究摘要：原油供应、油气产量、炼化开工与燃料需求发生变化。'*6
    return '<h1>真实公开行业研究 '+identifier+'</h1>机构：<a>'+publisher+'</a>日期：'+day+'<div class="blk_container"><p>'+body+'</p></div>'


class FakeFetcher:
    def __init__(self,output,responses):
        self.output,self.responses,self.calls,self.invalidated = output,responses,[],[]
    def get(self,url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value,Exception):
            raise value
        path = self.output/('raw-'+str(len(self.calls))+'.json')
        path.write_text(json.dumps({'request_url':url,'raw_response':value},ensure_ascii=False))
        return value,path,'2026-10-30T10:00:00+00:00'
    def invalidate(self,url):
        self.invalidated.append(url)


class OilResearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.out = self.root/'history'
    def tearDown(self):
        self.temp.cleanup()
    def responses(self,rows):
        result = {}
        for industry in INDUSTRIES:
            first_rows = rows if industry=='sw2_750100' else [('900','2025-12-01',AN)]
            result[list_url(t1='3',industry=industry,p=1)] = listing(industry,1,first_rows)
            result[list_url(t1='3',industry=industry,p=2)] = listing(industry,2,[('901','2025-11-01',AN)])
        return result
    def run_source(self,fetcher,source=A,**kwargs):
        return run_source(source,'2026-01-01','2026-09-30',self.out,fetcher=fetcher,**kwargs)

    def test_selected_industry_page_nonce_and_type_are_required(self):
        raw = listing('sw2_750100',1,[('1','2026-01-05',AN)])
        for invalid in [raw.replace("var t1 = '3'","var t1 = '6'"),
                        raw.replace("val('sw2_750100')","val('sw2_750200')"),
                        raw.replace("pagebox_num_nonce'>1","pagebox_num_nonce'>2")]:
            with self.assertRaises(ValueError):
                parse_industry_list(invalid,'sw2_750100',1)

    def test_final_page_nonce_is_included_in_total_page_count(self):
        rows,total = parse_industry_list(listing('sw2_750100',160,[('1','2004-01-01',AN)],159),'sw2_750100',160)
        self.assertEqual(total,160)
        self.assertEqual(len(rows),1)

    def test_invalid_dates_order_duplicates_and_unmarked_empty_are_rejected(self):
        cases = [listing('sw2_750100',1,[('1','2026-01-05',AN),('1','2026-01-05',AN)]),
                 listing('sw2_750100',1,[('1','2026-01-01',AN),('2','2026-02-01',AN)]),
                 listing('sw2_750100',1,[('1','2026-02-30',AN)]),
                 listing('sw2_750100',1).replace('没有找到相关内容','challenge')]
        for raw in cases:
            with self.assertRaises(ValueError):
                parse_industry_list(raw,'sw2_750100',1)

    def test_report_row_without_verified_author_is_not_silently_lost(self):
        raw = listing('sw2_750100',1,[('1','2026-01-05',AN)]).replace('class="fname05"','class="unknown"')
        with self.assertRaises(ValueError):
            parse_industry_list(raw,'sw2_750100',1)

    def test_repeated_whole_page_records_are_a_gap_not_false_progress(self):
        rows = [('1','2026-01-05',AN)]
        data = self.responses(rows)
        data[list_url(t1='3',industry='sw2_750100',p=2)] = listing('sw2_750100',2,rows,3)
        data[report_url('1')] = detail('1','2026-01-05')
        f = FakeFetcher(self.root,data)
        result = self.run_source(f,max_pages=6)
        self.assertTrue(result['terminal'])
        self.assertFalse(result['coverage']['complete'])
        self.assertTrue(any('整页相同报告' in item['error'] for item in result['errors']))
        self.assertEqual(result['candidate_count'],1)

    def test_empty_numbered_page_does_not_hide_later_history(self):
        data = self.responses([('1','2026-01-05',AN)])
        data[list_url(t1='3',industry='sw2_750100',p=1)] = listing('sw2_750100',1,[('1','2026-01-05',AN)],3)
        data[list_url(t1='3',industry='sw2_750100',p=2)] = listing('sw2_750100',2,(),3)
        data[list_url(t1='3',industry='sw2_750100',p=3)] = listing('sw2_750100',3,[('2','2025-12-31',AN)],3)
        data[report_url('1')] = detail('1','2026-01-05')
        fetcher = FakeFetcher(self.root,data)
        first = self.run_source(fetcher,max_pages=3)
        self.assertFalse(first['terminal'])
        second = self.run_source(fetcher,max_pages=3)
        self.assertIn(list_url(t1='3',industry='sw2_750100',p=3),fetcher.calls)
        self.assertEqual(second['candidate_count'],1)
        self.assertTrue(second['terminal'])
        self.assertFalse(second['coverage']['complete'])
        self.assertEqual(second['coverage']['empty_page_gaps'][0]['page'],2)
        self.assertEqual(fetcher.calls.count(report_url('1')),1)

    def test_mixed_year_page_does_not_end_before_whole_older_page(self):
        data = self.responses([('1','2026-01-05',AN),('2','2025-12-25',AN)])
        data[report_url('1')] = detail('1','2026-01-05')
        f = FakeFetcher(self.root,data)
        first = self.run_source(f,max_pages=3)
        self.assertFalse(first['terminal'])
        second = self.run_source(f,max_pages=1)
        self.assertEqual(second['status'],'complete')
        self.assertTrue(second['terminal'])
        self.assertNotIn(report_url('2'),f.calls)

    def test_shared_index_dedupes_requests_and_checks_broker_identity_twice(self):
        data = self.responses([('1','2026-01-05',AN),('2','2026-01-05',BN)])
        data[report_url('1')] = detail('1','2026-01-05',AN)
        data[report_url('2')] = detail('2','2026-01-05',BN)
        f = FakeFetcher(self.root,data)
        first = self.run_source(f,max_pages=4)
        second = self.run_source(f,source=B,max_pages=4)
        self.assertEqual(first['status'],'complete')
        self.assertEqual(second['status'],'complete')
        self.assertEqual(sum('vReport_List' in url for url in f.calls),4)
        a = json.loads(Path(first['candidates_path']).read_text().strip())
        b = json.loads(Path(second['candidates_path']).read_text().strip())
        self.assertEqual(a['candidate_id'],A+':sina:1')
        self.assertEqual(a['external_id'],'sina:1')
        self.assertEqual(b['source_id'],B)
        self.assertEqual(a['published_at'],'')
        self.assertEqual(a['published_date'],'2026-01-05')
        self.assertEqual(a['published_at_precision'],'date')
        self.assertEqual(a['discovery_industry_code'],'sw2_750100')
        self.assertEqual(a['content_scope'],'public_research_summary')
        self.assertTrue(Path(a['raw_path']).is_file())

    def test_one_bad_detail_does_not_block_other_report_or_become_title_only(self):
        data = self.responses([('1','2026-01-05',AN),('2','2026-01-05',AN)])
        data[report_url('1')] = detail('1','2026-01-05',BN)
        data[report_url('2')] = detail('2','2026-01-05',AN)
        f = FakeFetcher(self.root,data)
        first = self.run_source(f,max_pages=4,max_articles=2)
        self.assertEqual(first['candidate_count'],1)
        self.assertEqual(f.calls[-2:],[report_url('1'),report_url('2')])
        self.assertFalse(first['terminal'])
        second = self.run_source(f,max_pages=1,max_articles=2)
        self.assertTrue(second['terminal'])
        self.assertFalse(second['coverage']['complete'])
        rows = [json.loads(line) for line in Path(second['candidates_path']).read_text().splitlines()]
        self.assertEqual([row['candidate_id'] for row in rows],[A+':sina:2'])
        self.assertEqual(f.calls.count(report_url('1')),3)

    def test_date_conflict_is_quarantined_without_changing_original_date(self):
        data = self.responses([('1','2026-01-05',AN)])
        data[report_url('1')] = detail('1','2026-01-06')
        f = FakeFetcher(self.root,data)
        result = self.run_source(f,max_pages=4,max_articles=3)
        self.assertEqual(result['candidate_count'],0)
        self.assertTrue(result['terminal'])
        self.assertIn('发布日期不一致',result['errors'][0]['error'])

    def test_403_stops_every_later_industry_and_broker_request(self):
        url = list_url(t1='3',industry='sw2_750100',p=1)
        f = FakeFetcher(self.root,{url:HTTPError(url,403,'forbidden',{},None)})
        first = self.run_source(f)
        self.assertEqual(first['status'],'blocked')
        self.assertTrue(first['terminal'])
        second = self.run_source(f,source=B)
        self.assertEqual(second['status'],'blocked')
        self.assertEqual(f.calls,[url])

    def test_priority_months_share_body_budget_and_resume(self):
        data = self.responses([('2','2026-09-05',AN),('1','2026-01-05',AN)])
        data[report_url('1')] = detail('1','2026-01-05')
        data[report_url('2')] = detail('2','2026-09-05')
        f = FakeFetcher(self.root,data)
        first = self.run_source(f,max_pages=4,max_articles=1,priority_months=['2026-01','2026-09'])
        self.assertEqual(first['monthly_candidate_counts']['2026-01'],1)
        self.assertEqual(first['monthly_candidate_counts']['2026-09'],0)
        self.assertFalse(first['terminal'])
        second = self.run_source(f,max_pages=1,max_articles=1,priority_months=['2026-01','2026-09'])
        self.assertEqual(second['monthly_candidate_counts']['2026-09'],1)
        self.assertEqual(second['candidate_count'],2)
        self.assertTrue(second['coverage']['complete'])

    def test_unknown_publisher_is_never_reassigned_to_similar_name(self):
        data = self.responses([('1','2026-01-05','东吴证券另一家公司')])
        f = FakeFetcher(self.root,data)
        result = self.run_source(f,max_pages=4)
        self.assertEqual(result['candidate_count'],0)
        self.assertNotIn(report_url('1'),f.calls)
        index = json.loads((self.out/'industry-index.json').read_text())
        self.assertEqual(index['unmatched_publishers']['东吴证券另一家公司'],1)

    def test_scope_and_work_limits_do_not_silently_reset(self):
        f = FakeFetcher(self.root,self.responses([('1','2026-01-05',AN)]))
        f.responses[report_url('1')] = detail('1','2026-01-05')
        result = self.run_source(f,max_pages=1,max_articles=1)
        self.assertFalse(result['terminal'])
        self.assertEqual(result['pages_this_call'],1)
        with self.assertRaises(ValueError):
            run_source(A,'2026-02-01','2026-09-30',self.out,fetcher=f)
        with self.assertRaises(ValueError):
            self.run_source(f,resume=False)
        with self.assertRaises(ValueError):
            self.run_source(f,max_articles=0)


if __name__ == '__main__':
    unittest.main()
