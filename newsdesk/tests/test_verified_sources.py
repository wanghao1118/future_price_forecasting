import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.verified_sources import SOURCES, collect_one, discover, nuxt_values, parse_article, decode_response, AccessError
from newsdesk.publication_time import normalize_candidate, require_verified_publication, PublicationTimeError
from newsdesk.source_maintenance import SourceMaintenance, update_results


class VerifiedSourcesTests(unittest.TestCase):
    def test_misdeclared_sina_encoding_does_not_corrupt_source_identity(self):
        text = '<span class="source">草叔消费升级研究</span>'
        self.assertEqual(decode_response(text.encode('utf-8'), 'gbk'), text)
        self.assertEqual(decode_response(text.encode('gb18030'), 'gb18030'), text)

    def test_xueqiu_requires_matching_author_and_full_body(self):
        url = 'https://www.xueqiu.com/4164170693/406167386'
        item = {'id':406167386,'user_id':4164170693,'user':{'screen_name':'乔令投研'},
                'created_at':1787379650000,'title':'研究','text':'<p>分析内容</p>','truncated':False}
        self.assertEqual(self.normalize('authority_92',url,json.dumps(item))['published_at'], '2026-08-22T06:20:50+00:00')
        for changes in ({'user_id':1},{'id':1},{'truncated':True},{'text':''},{'user':{'screen_name':'其他作者'}}):
            with self.assertRaises(ValueError): parse_article('authority_92',url,json.dumps({**item,**changes}))

    def test_cyzone_seconds_and_36kr_milliseconds_keep_distinct_units(self):
        cyzone = '<a href="/author/1545715">远川科技评论</a><script type="application/json">'+json.dumps({'content_id':850795,'published_at':1793087013})+'</script>'
        self.assertEqual(self.normalize('authority_80','https://www.cyzone.cn/article/850795.html',cyzone)['published_at'],'2026-10-27T07:43:33+00:00')
        kr = '<script type="application/json">'+json.dumps({'itemId':123,'publishTime':1793087013000})+'</script>'
        self.assertEqual(self.normalize('authority_80','https://www.36kr.com/p/123',kr)['published_at'],'2026-10-27T07:43:33+00:00')

    def test_empty_directory_is_not_evidence_of_login_requirement(self):
        with tempfile.TemporaryDirectory() as d,patch('newsdesk.verified_sources.Fetcher') as factory:
            fetch=factory.return_value;fetch.pages=1
            fetch.get.return_value=('<html></html>','unused','https://www.huxiu.com/member/3350257.html')
            candidates,report=collect_one('authority_86',d,1)
            self.assertEqual(report['health_status'],'discovery_failed')
            fetch.get.side_effect=AccessError('需要登录','requires_login')
            self.assertEqual(collect_one('authority_86',d,1)[1]['health_status'],'requires_login')
            fetch.get.side_effect=AccessError('访问验证')
            self.assertEqual(collect_one('authority_86',d,1)[1]['health_status'],'access_blocked')

    def normalize(self, sid, url, text):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'raw.json';p.write_text(json.dumps({'raw_response':text}))
            result=normalize_candidate({'source_id':sid,'source_url':url,'raw_path':str(p)})
            require_verified_publication(result)
            return result

    def test_active_catalog_has_real_collectors(self):
        registry=json.loads(Path('newsdesk/source_registry.json').read_text())['sources']
        enabled={s['id'] for s in registry if s.get('enabled') and s.get('adapter')=='verified_sources'}
        self.assertEqual(len(SOURCES),39)
        self.assertTrue(enabled.issubset(SOURCES))

    def test_user_timezone_only_for_self_media_and_no_relative_or_date_only(self):
        url='https://m.sohu.com/a/123_121956368'
        def raw(stamp): return '<meta itemprop="datePublished" content="'+stamp+'"><h1>文章</h1><div class="author-info">吴晓波频道</div><section id="articleContent">分析内容</section>'
        result=self.normalize('authority_64',url,raw('2026-11-05 10:00:59'))
        self.assertEqual(result['published_at'],'2026-11-05T02:00:59+00:00')
        self.assertIn('用户',result['timezone_evidence'])
        for value in ['2026-11-05','3小时前','11-05 10:00','2026-11-05T10:00:00-00:00']:
            with self.assertRaises(PublicationTimeError):self.normalize('authority_64',url,raw(value))
        with self.assertRaises(PublicationTimeError): self.normalize('authority_39','https://www.chinadaily.com.cn/a/123.html',raw('2026-11-05 10:00:59'))

    def test_explicit_zone_is_not_reattached(self):
        result=self.normalize('authority_64','https://m.sohu.com/a/123_121956368','<meta itemprop="datePublished" content="2026-11-05T10:00:00Z"><div class="author-info">吴晓波频道</div>')
        self.assertEqual(result['published_at'],'2026-11-05T10:00:00+00:00')

    def test_article_and_author_ids_are_verified_in_public_api(self):
        data={'data':{'id':3598484,'author':{'id':134881},'display_time':1748522511,'title':'研究','content':'公开分析正文'}}
        self.assertEqual(self.normalize('authority_66','https://wallstreetcn.com/member/articles/3598484',json.dumps(data))['published_at'],'2025-05-29T12:41:51+00:00')
        data['data']['id']=999
        with self.assertRaises(PublicationTimeError):self.normalize('authority_66','https://wallstreetcn.com/member/articles/3598484',json.dumps(data))
        data['data'].update(id=3598484,author={'id':1})
        with self.assertRaises(ValueError):parse_article('authority_66','https://wallstreetcn.com/member/articles/3598484',json.dumps(data))

    def test_millisecond_feed_matches_exact_aid_and_converts_utc(self):
        text=json.dumps({'attachment':{'flashes':[{'aid':1,'title':'a','writeTime':1793689652000,'content':'分析'},{'aid':2,'writeTime':1793680000000}]}})
        result=self.normalize('authority_76','https://www.arfgc.com/f-d-1.html',text)
        self.assertEqual(result['source_published_at'],'1793689652000')
        self.assertEqual(result['published_at'],'2026-11-03T07:07:32+00:00')
        second=self.normalize('authority_76','https://www.arfgc.com/f-d-2.html',text)
        self.assertEqual(second['source_published_at'],'1793680000000')

    def test_nuxt_reference_numbers_are_not_timestamps(self):
        self.assertEqual(nuxt_values([{'date':1,'child':2},1793689652,{'name':3},'作者']),{'date':1793689652,'child':{'name':'作者'}})

    def test_feed_items_keep_distinct_external_ids_and_real_detail_links(self):
        text=json.dumps({'attachment':{'flashes':[
            {'aid':1,'title':'第一篇','writeTime':1793689652000,'content':'分析正文'*40},
            {'aid':2,'title':'第二篇','writeTime':1793680000000,'content':'另一篇正文'*40}]}})
        with tempfile.TemporaryDirectory() as d,patch('newsdesk.verified_sources.Fetcher') as factory:
            raw=Path(d)/'raw.json';raw.write_text(json.dumps({'raw_response':text}))
            fetch=factory.return_value;fetch.pages=1
            fetch.get.return_value=(text,str(raw),SOURCES['authority_76']['entry_urls'][0])
            candidates,report=collect_one('authority_76',d,2)
            self.assertEqual(report['candidates'],2)
            self.assertEqual([c['external_id'] for c in candidates],['1','2'])
            self.assertEqual([c['source_url'] for c in candidates],['https://www.arfgc.com/f-d-1.html','https://www.arfgc.com/f-d-2.html'])

    def test_discovery_scopes_accounts_and_ignores_audit_samples(self):
        text='<a href="/a/1_100150">目标</a><a href="/a/2_123">其他</a><a href="https://evil.test/a/3_100150">外站</a>'
        self.assertEqual(discover('authority_78','https://www.sohu.com/media/100150',text),['https://www.sohu.com/a/1_100150'])
        self.assertEqual(discover('authority_78','https://www.sohu.com/media/100150',''),[])
        html='<a href="https://other.com.cn/a/202611/05/WS123.html">其他站</a><a href="/a/202611/05/WS456.html">当前站</a>'
        self.assertEqual(discover('authority_39','https://www.chinadaily.com.cn/',html),['https://www.chinadaily.com.cn/a/202611/05/WS456.html'])

    def test_http_success_without_qualified_content_is_not_available(self):
        with tempfile.TemporaryDirectory() as d:
            manifest={'collected_at':'2026-11-05T10:00:00+00:00','sources':[{'source_id':'a','status':'ok'}]}
            update_results(d,manifest,[],['a'])
            self.assertEqual(json.loads((Path(d)/'a.json').read_text())['status'],'no_qualified_items')
            update_results(d,manifest,[{'source_id':'a','source_url':'https://example.com','published_at':'2026-11-05T00:00:00Z'}],['a'])
            update_results(d,{'source_id':'a','status':'failed','health_status':'requires_access'},[],['a'])
            saved=json.loads((Path(d)/'a.json').read_text())
            self.assertEqual(saved['status'],'requires_access')
            self.assertEqual(saved['last_success_at'],'2026-11-05T10:00:00+00:00')

    def test_manual_check_rejects_unknown_ids_and_overlap(self):
        with tempfile.TemporaryDirectory() as d,patch('newsdesk.source_maintenance.registry',return_value=[{'id':'a','enabled':True}]):
            manager=SourceMaintenance(d)
            with self.assertRaises(ValueError): manager.trigger(['http://internal/'])
            manager._job['running']=True
            self.assertFalse(manager.trigger(['a']))


if __name__=='__main__':unittest.main()
