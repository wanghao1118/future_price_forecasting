import json
import tempfile
import unittest
from pathlib import Path

from newsdesk.domestic_brokers_a import SOURCES, crawl_eastmoney, parse_eastmoney_detail, parse_citic_index


BODY = '原油供应受到运输瓶颈影响，炼厂采购节奏变化，需要关注公开报告中的库存和交付指标。' * 5


class BrokerASourceTests(unittest.TestCase):
    def test_citic_only_public_substantive_recent_core_views(self):
        payload = {'code': 0, 'data': {'101': {'rptFirst': {'id': 1, 'psTitle': '公开宏观观点', 'psCoreView': BODY,
            'rptDate': '2026-10-27', 'authorList': [{'userName': '研究员'}]}},
            '102': {'rptList': [{'id': 2, 'psTitle': '仅标题', 'rptDate': '2026-10-27'},
                {'id': 3, 'psTitle': '旧报告', 'psCoreView': BODY, 'rptDate': '2024-07-01'}]}}}
        rows = parse_citic_index(json.dumps(payload, ensure_ascii=False), '/tmp/raw', '2026-10-28T10:00:00+00:00')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_name'], '中信证券')
        self.assertEqual(rows[0]['published_at'], '')
        self.assertEqual(rows[0]['published_date'], '2026-10-27')
        self.assertTrue(rows[0]['detail_requires_login'])
        self.assertIn('公开首页', rows[0]['collection_platform'])

    def test_article_header_attribution_not_related_navigation(self):
        html = '<h1 id="zw-title">宏观报告</h1><div class="c-infos"><span>东吴证券</span></div><div id="ctx-content"><img src="figure.png"/><p>' + BODY + '</p><script>恶意代码</script></div><div>导航假内容</div>'
        title, body = parse_eastmoney_detail(html, '东吴证券')
        self.assertEqual(title, '宏观报告')
        self.assertEqual(body, BODY)
        with self.assertRaises(ValueError):
            parse_eastmoney_detail(html + '<span>中信证券</span>', '中信证券')

    def test_title_only_or_login_is_not_research(self):
        html = '<h1 id="zw-title">报告</h1><div class="c-infos">东吴证券</div><div id="ctx-content">登录后查看' + BODY + '</div>'
        with self.assertRaises(ValueError):
            parse_eastmoney_detail(html, '东吴证券')

    def test_broker_identity_and_date_precision_preserved(self):
        class FakeFetcher:
            def get(self, url):
                if 'reportapi' in url:
                    return json.dumps({'data': [{'orgCode': '80000031', 'orgSName': '东吴证券',
                        'orgName': '东吴证券股份有限公司', 'id': 123, 'encodeUrl': 'abc=',
                        'publishDate': '2026-10-28 00:00:00.000'}]}, ensure_ascii=False), Path('/tmp/list'), '2026-10-28T10:00:00+00:00'
                return '<h1 id="zw-title">公开报告</h1><div class="c-infos"><span>东吴证券</span></div><div id="ctx-content">' + BODY + '</div>', Path('/tmp/detail'), '2026-10-28T10:00:00+00:00'
        rows, status = crawl_eastmoney('institution_11', SOURCES['institution_11'], FakeFetcher(), 5)
        self.assertEqual(status['count'], 1)
        self.assertEqual(rows[0]['publisher_name'], '东吴证券股份有限公司')
        self.assertEqual(rows[0]['collection_platform'], '东方财富研报中心')
        self.assertEqual(rows[0]['published_at'], '')
        self.assertEqual(rows[0]['published_date'], '2026-10-28')
        self.assertEqual(rows[0]['published_at_precision'], 'date')


if __name__ == '__main__':
    unittest.main()
