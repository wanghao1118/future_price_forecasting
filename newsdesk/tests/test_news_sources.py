import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.news_sources import candidate, canonical_url, crawl_sources, parse_response, parse_cls_page, timestamp, ThsArticleText


class NewsSourceTests(unittest.TestCase):
    def test_cls_missing_title_uses_real_excerpt_and_stable_id(self):
        item = candidate('cls', {'id': 123, 'content': '原油库存下降。随后报告发布。', 'ctime': 1793180000}, '/tmp/raw.json', '2026-10-28T10:00:00+00:00')
        self.assertEqual(item['candidate_id'], 'cls:123')
        self.assertEqual(item['title'], '原油库存下降')
        self.assertEqual(item['body'], '原油库存下降。随后报告发布。')
        self.assertEqual(item['title_origin'], 'excerpt')
        self.assertEqual(item['source_url'], 'https://www.cls.cn/detail/123')
        self.assertEqual(item['content_scope'], 'public_flash')

    def test_ths_parse_does_not_execute_assignment(self):
        raw = 'var thsRss = {pubDate:"2026/10/28", item:[{"seq":1,"title":"公告","content":"正文"}]};'
        self.assertEqual(parse_response('ths', raw)[0]['seq'], 1)
        with self.assertRaises(ValueError):
            parse_response('ths', 'alert("not data")')

    def test_source_error_is_not_an_empty_success(self):
        with self.assertRaises(ValueError):
            parse_response('jiemian', '{"code":"403","message":"login"}')
        with self.assertRaises(ValueError):
            parse_response('cls', '{"errno":1,"data":{"roll_data":[]}}')

    def test_ths_article_extraction_stops_before_recommendations(self):
        parser = ThsArticleText()
        parser.feed('<h1>页头</h1><div class="news-content-parsed"><p>油价<strong>新闻</strong><br>正文</p></div><p>推荐广告</p>')
        body = ''.join(parser.parts)
        self.assertIn('油价新闻', body)
        self.assertIn('正文', body)
        self.assertNotIn('推荐广告', body)
        self.assertNotIn('页头', body)

    def test_jin10_public_flash_and_access_scope(self):
        row = {'id':'20261028182945166800','time':'2026-10-28 18:29:45','extras':None,
               'data':{'title':'','content':'【原油消息】公开内容','vip_level':0}}
        item = candidate('jin10', row, '/tmp/raw.json', '2026-10-28T10:00:00+00:00')
        self.assertEqual(item['published_at'], '2026-10-28T18:29:45+08:00')
        self.assertEqual(item['source_url'], 'https://flash.jin10.com/detail/20261028182945166800')
        row['data']['vip_level'] = 1
        self.assertIsNone(candidate('jin10', row, '/tmp/raw.json', 'now'))
        self.assertEqual(parse_response('jin10', '{"status":200,"data":[]}'), [])

    def test_public_cls_embedded_json_is_parsed_as_data(self):
        payload = {'props': {'pageProps': {'data': {'articles': [{'article_id': 99}]}}}}
        raw = '<script id="__NEXT_DATA__" type="application/json">' + json.dumps(payload) + '</script>'
        self.assertEqual(parse_cls_page(raw), [{'article_id': 99}])
        with self.assertRaises(ValueError):
            parse_cls_page('<script>window.location="untrusted"</script>')

    def test_native_publication_time_and_canonical_url(self):
        self.assertEqual(timestamp('2026/10/28 16:45', local=True), '2026-10-28T16:45:00+08:00')
        self.assertEqual(timestamp('bad'), '')
        self.assertEqual(canonical_url('/article/detail/123.html#share', 'https://www.stcn.com/'), 'https://www.stcn.com/article/detail/123.html')
        self.assertEqual(canonical_url('javascript:void(0)', 'https://www.stcn.com/'), '')
        self.assertEqual(canonical_url('', 'https://www.stcn.com/'), '')

    def test_crawl_keeps_non_oil_stories_and_records_partial_source_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            def fetch(self, source, url):
                raw = json.dumps({'errno':0, 'data':{'roll_data':[
                    {'id':1,'title':'某芯片公司公告','content':'与油无关也应进入agent待判队列','ctime':1793180000},
                    {'id':1,'title':'重复项','content':'去重','ctime':1793180000},
                ]}}, ensure_ascii=False)
                path = Path(temp) / 'raw.json'
                path.write_text(raw)
                return raw, path, '2026-10-28T10:00:00+00:00'
            with patch('newsdesk.news_sources.Fetcher.get', fetch):
                report = crawl_sources(temp, ['cls'], 1)
            self.assertEqual(report['candidate_count'], 1)
            record = json.loads(Path(report['candidates_path']).read_text())
            self.assertEqual(record['candidate_id'], 'cls:1')
            self.assertIn('芯片', record['title'])
            self.assertEqual(report['sources'][0]['status'], 'ok')
            with patch('newsdesk.news_sources.Fetcher.get', side_effect=TimeoutError('source timed out')):
                failed = crawl_sources(temp, ['cls'], 1)
            self.assertEqual(failed['sources'][0]['status'], 'error')
            self.assertEqual(failed['candidate_count'], 0)


if __name__ == '__main__':
    unittest.main()
