import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from newsdesk.review_news_origins import FIELDS, apply_results, fingerprint, run, validate
from newsdesk.server import NewsStore


class FullOriginReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.database=self.root/'news.sqlite3';self.output=self.root/'review'
        self.store=NewsStore(self.database,seed_demo=False)
        self.record=self.store.save({'title':'高盛报告谈原油需求','body':'高盛发布报告，讨论原油需求。',
            'category':'demand','source_name':'财联社','published_at':'2026-11-01T01:00:00+00:00'})

    def tearDown(self):self.temp.cleanup()

    def decision(self,record,status='identified'):
        return {'id':record['id'],'origin_name':'高盛' if status=='identified' else '',
                'origin_evidence':'高盛发布报告' if status=='identified' else '',
                'origin_status':status,'origin_reason':'原文明确披露由高盛发布该研究报告' if status=='identified' else '可见原文没有足够信息确定单一机构'}

    def test_resume_uses_completed_checks_and_only_changes_attribution_fields(self):
        with patch('newsdesk.review_news_origins.call_agent',side_effect=lambda records,*args:[self.decision(r) for r in records]) as agent,contextlib.redirect_stdout(io.StringIO()):
            result=run(self.database,self.output,workers=2,batch_size=1)
            self.assertEqual((result['status'],result['completed']),('complete',1))
            saved=self.store.get(self.record['id'])
            changed_fields = (*FIELDS, 'origin_authority')
            self.assertEqual(saved['origin_authority']['level'], 'P0')
            self.assertEqual({k:v for k,v in saved.items() if k not in changed_fields},{k:v for k,v in self.record.items() if k not in changed_fields})
            self.assertEqual(saved['origin_content_hash'],fingerprint(saved))
            run(self.database,self.output,workers=2)
            self.assertEqual(agent.call_count,1)

    def test_input_change_is_rechecked_while_manual_attribution_is_preserved(self):
        with patch('newsdesk.review_news_origins.call_agent',side_effect=lambda records,*args:[self.decision(r) for r in records]) as agent,contextlib.redirect_stdout(io.StringIO()):
            run(self.database,self.output)
            self.store.save({'body':'高盛发布报告，补充原油需求分析。'},self.record['id'])
            run(self.database,self.output)
            self.assertEqual(agent.call_count,2)
            self.store.save({'origin_name':'人工核对机构'},self.record['id'])
            result=run(self.database,self.output)
            self.assertEqual(agent.call_count,2)
            self.assertEqual(result['status_counts'],{'manual_preserved':1})

    def test_concurrent_content_edit_is_not_overwritten(self):
        result=validate(self.decision(self.record),self.record)
        updated=self.store.save({'body':'人工补充的重要新信息'},self.record['id'])
        outcome,conflicts=apply_results(self.database,[self.record],{self.record['id']:result})
        self.assertEqual(conflicts,[self.record['id']])
        self.assertEqual(self.store.get(self.record['id']),updated)

    def test_unresolved_is_a_completed_evidenced_decision_not_unprocessed(self):
        with patch('newsdesk.review_news_origins.call_agent',side_effect=lambda records,*args:[self.decision(r,'unresolved') for r in records]),contextlib.redirect_stdout(io.StringIO()):
            result=run(self.database,self.output)
        self.assertEqual((result['remaining'],result['status_counts']),(0,{'unresolved':1}))
        self.assertTrue(self.store.get(self.record['id'])['origin_reason'])

    def test_fabricated_evidence_and_inconsistent_status_are_rejected(self):
        for extra in [{'origin_evidence':'原文不存在的声明'}, {'origin_status':'unresolved'}, {'origin_reason':''}]:
            with self.assertRaises(ValueError):validate(dict(self.decision(self.record),**extra),self.record)

    def test_focused_recheck_overrides_prior_conclusion_once_and_remains_resumable(self):
        with patch('newsdesk.review_news_origins.call_agent',side_effect=lambda records,*args:[self.decision(r,'unresolved') for r in records]),contextlib.redirect_stdout(io.StringIO()):
            run(self.database,self.output)
        # Guarantee the old completion precedes the new pass even in a fast test.
        with self.store.connect() as connection:
            row=self.store.get(self.record['id']);row['origin_reviewed_at']='2026-01-01T00:00:00+00:00'
            connection.execute('UPDATE news SET payload=? WHERE id=?',(json.dumps(row),row['id']))
        with patch('newsdesk.review_news_origins.call_agent',side_effect=lambda records,*args:[self.decision(r) for r in records]) as agent,contextlib.redirect_stdout(io.StringIO()):
            other=self.root/'second-pass'
            result=run(self.database,other,target_ids=[self.record['id']],recheck=True)
            self.assertEqual(result['status_counts'],{'identified':1})
            run(self.database,other)
            self.assertEqual(agent.call_count,1)


if __name__=='__main__':unittest.main()
