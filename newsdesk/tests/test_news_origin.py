import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from newsdesk.backfill_news_origin import run
from newsdesk.ingest import archive_candidates, prepare_candidate, validate_decision
from newsdesk.news_origin import explicit_origin, validate_origin
from newsdesk.server import NewsStore
from newsdesk.tests.test_ingest import candidate, decision


class NewsOriginTests(unittest.TestCase):
    def test_issuer_is_not_distributor_or_company_being_covered(self):
        row = {'title':'【高盛：维持中海油买入评级】', 'body':'财联社消息，高盛发布报告，维持中海油买入评级。', 'source_name':'财联社'}
        result = explicit_origin(row)
        self.assertEqual(result['origin_name'], '高盛')
        validate_origin(result['origin_name'], result['origin_evidence'], row)
        self.assertNotIn('财联社', result['origin_name'])
        self.assertNotIn('中海油', result['origin_name'])

    def test_mentions_expectations_and_mixed_digest_do_not_become_issuers(self):
        for text in ['美联储预计将在下周降息', '高盛遭监管调查', '分析师担心高盛和摩根士丹利的风险敞口',
                     '高盛：需求下降；摩根士丹利：供应增加']:
            self.assertEqual(explicit_origin({'title':text,'body':text,'source_name':'财联社'})['origin_name'], '')
        for title, body in [('景顺：日本政策释放信心','景顺的分析师表示看好前景。日本央行宣布加息。'),
                            ('金十整理：市场消息汇总','美联储宣布加息，影响市场。'),
                            ('原油市场综合分析','市场关注供需，经济前景仍不明朗。美国能源信息署发布了新的库存数据。')]:
            self.assertEqual(explicit_origin({'title':title,'body':body})['origin_name'], '')

    def test_technical_data_publisher_and_institution_byline(self):
        row = {'title':'美国原油库存增加', 'body':'据美国能源信息署发布的报告，美国原油库存增加。'}
        self.assertEqual(explicit_origin(row)['origin_name'], '美国能源信息署（EIA）')
        row = {'title':'Monthly outlook', 'source_name':'野村',
               'ingestion':{'source_id':'institution_46','publisher_name':'野村'}}
        result = explicit_origin(row)
        self.assertEqual(result['origin_name'],'野村')
        validate_origin(result['origin_name'],result['origin_evidence'],row)

    def test_agent_attribution_requires_verbatim_support(self):
        row = candidate(); answer = decision(row)
        answer['attributes'].update(origin_name='高盛',origin_evidence='高盛发布报告')
        with self.assertRaisesRegex(ValueError,'逐字'):
            validate_decision(answer,row)
        answer['attributes'].update(origin_name='高盛',origin_evidence='')
        with self.assertRaisesRegex(ValueError,'同时'):
            validate_decision(answer,row)

    def test_archive_roundtrip_and_manual_correction_survives_later_collection(self):
        row = prepare_candidate(dict(candidate(),title='高盛：港口关闭影响原油交付',
            body='高盛发布报告：港口关闭导致原油油轮交付延迟，部分炼厂调整采购。'))
        answer = decision(row);answer['attributes'].update(origin_name='高盛',origin_evidence='高盛发布报告',origin_status='identified',origin_reason='原文明确称高盛发布了这份报告')
        review = {'reviewer':'test','reviewed_at':'2026-11-04T01:00:00Z','decision':answer}
        with tempfile.TemporaryDirectory() as temporary:
            database=Path(temporary)/'test.sqlite3'
            archive_candidates([row],[review],database,'test')
            store=NewsStore(database,seed_demo=False);saved=store.all()[0]
            self.assertEqual(saved['origin_name'],'高盛')
            self.assertEqual(saved['origin_authority']['level'], 'P0')
            self.assertEqual(saved['source_name'],row['source_name'])
            store.save({'origin_name':'人工核对机构'},saved['id'])
            review['reviewed_at']='2026-11-04T02:00:00Z'
            archive_candidates([row],[review],database,'test2')
            saved=store.get(saved['id'])
            self.assertEqual((saved['origin_name'],saved['origin_method'],saved['origin_evidence']),('人工核对机构','manual',''))

    def test_migration_changes_only_origin_fields_and_is_repeatable(self):
        row = candidate();row['title']='高盛：原油需求上升'
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);database=root/'test.sqlite3'
            review={'reviewer':'test','reviewed_at':'2026-11-04T01:00:00Z','decision':decision(row)}
            archive_candidates([row],[review],database,'test')
            store=NewsStore(database,seed_demo=False);before=store.all()[0]
            # Simulate a legacy row that has not yet had semantic attribution.
            before['origin_method']=''
            with store.connect() as connection:
                connection.execute('UPDATE news SET payload=? WHERE id=?',(json.dumps(before),before['id']))
            result=run(database,root/'migration',True);after=store.all()[0]
            self.assertEqual(result['attributed'],1)
            self.assertEqual(after['origin_name'],'高盛')
            from newsdesk.news_origin import FIELDS
            changed_fields = (*FIELDS, 'origin_authority')
            self.assertEqual(after['origin_authority']['level'], 'P0')
            self.assertEqual({k:v for k,v in before.items() if k not in changed_fields}, {k:v for k,v in after.items() if k not in changed_fields})
            with sqlite3.connect(Path(result['backup']).as_uri()+'?mode=ro',uri=True) as backup:
                self.assertEqual(json.loads(backup.execute('SELECT payload FROM news').fetchone()[0]),before)
            self.assertEqual(run(database,root/'repeat')['updated'],0)

    def test_legacy_heuristic_does_not_override_completed_semantic_uncertainty(self):
        row = candidate(); row['title']='高盛：原油需求上升'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);database=root/'test.sqlite3'
            review={'reviewer':'test','reviewed_at':'2026-11-04T01:00:00Z','decision':decision(row)}
            archive_candidates([row],[review],database,'test')
            result=run(database,root/'migration',True)
            self.assertEqual(result['attributed'],0)
            self.assertEqual(NewsStore(database,seed_demo=False).all()[0]['origin_method'],'agent')


if __name__ == '__main__':
    unittest.main()
