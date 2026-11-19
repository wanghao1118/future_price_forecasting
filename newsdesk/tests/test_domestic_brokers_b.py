import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.domestic_brokers_b import parse_detail, parse_founder_detail, parse_list, crawl_sina_brokers


class SinaBrokerTests(unittest.TestCase):
    def test_list_preserves_explicit_publisher_and_stable_report_id(self):
        rows = parse_list('''<table><tr><td>1</td><td><a title="研究标题" href="//stock.finance.sina.com.cn/stock/go.php/vReport_Show/kind/search/rptid/123/index.phtml">研究...</a></td><td>行业</td><td>2026-10-28</td><td><div class="fname05"><span>华安证券股份有限公司</span></div></td></tr></table>''')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['external_id'], '123')
        self.assertEqual(rows[0]['publisher_name'], '华安证券股份有限公司')
        self.assertTrue(rows[0]['source_url'].startswith('https://stock.finance.sina.com.cn/'))

    def test_detail_requires_real_summary_and_exact_author(self):
        text = '<h1>原油供给研究</h1><div>机构：<a>华安证券股份有限公司</a>日期：2026-10-28</div><div class="blk_container"><p>' + ('原油供给和需求研究。' * 15) + '</p></div>'
        result = parse_detail(text, ['华安证券股份有限公司'])
        self.assertEqual(result['published_at'], '')
        self.assertEqual(result['published_date'], '2026-10-28')
        self.assertEqual(result['published_at_precision'], 'date')
        with self.assertRaises(ValueError):
            parse_detail(text, ['方正证券股份有限公司'])
        with self.assertRaises(ValueError):
            parse_detail('<h1>标题</h1>机构：<a>华安证券股份有限公司</a>', ['华安证券股份有限公司'])

    def test_scripts_and_navigation_are_not_summary(self):
        text = '<h1>报告</h1>机构：<a>华安证券股份有限公司</a><div class="blk_container"><script>' + ('doNotRun();' * 20) + '</script></div>'
        with self.assertRaises(ValueError):
            parse_detail(text, ['华安证券股份有限公司'])

    def test_founder_official_summary_requires_origin_statement(self):
        text = '<div class="k-infor_d-2">原油供给研究</div><div>时间：2026-10-27</div><div class="k-infor_d-3">' + ('研究摘要中的供需数据。' * 15) + '</div><div>完整观点以方正证券研究所发布的完整报告为准。</div>'
        result = parse_founder_detail(text)
        self.assertEqual(result['publisher_name'], '方正证券股份有限公司')
        self.assertEqual(result['published_date'], '2026-10-27')
        self.assertEqual(result['published_at'], '')
        with self.assertRaises(ValueError):
            parse_founder_detail(text.replace('方正证券研究所', '其他机构'))

    def test_empty_publisher_feed_is_not_marked_connected(self):
        with tempfile.TemporaryDirectory() as directory:
            def fake_get(self, url):
                return '<html><h1>机构首页</h1></html>', Path(directory) / 'raw.json', '2026-10-28T03:00:00+00:00'
            with patch('newsdesk.domestic_brokers_b.Fetcher.get', fake_get):
                result = crawl_sina_brokers(directory, {'institution_test': {'name': '测试机构', 'publisher_names': ['测试机构全名']}}, 2)
            self.assertEqual(result['candidate_count'], 0)
            self.assertEqual(result['sources'][0]['status'], 'empty')
            self.assertEqual(Path(result['candidates_path']).read_text(), '')


if __name__ == '__main__':
    unittest.main()
