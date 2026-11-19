"""Macro-topic scheduling contracts use offline public-history fixtures only."""

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from newsdesk.history_cls_macro import HistoryFetcher, MACRO_TOPIC_IDS, MACRO_TOPICS, run_source
from newsdesk.history_media_extra import HistoryFetcher as OriginalFetcher


def month_keys(first, last):
    current, end = date.fromisoformat(first).replace(day=1), date.fromisoformat(last)
    result = []
    while current <= end:
        result.append(current.strftime('%Y-%m'))
        current = (current.replace(day=28)+timedelta(days=4)).replace(day=1)
    return result


class MacroTopicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.ids = list(MACRO_TOPIC_IDS)

    def fake(self, source_id, first, last, output, priority_months=None, *,
             max_pages, max_articles, fetcher, topic_ids, complete_month=False):
        self.assertEqual(source_id, 'cls')
        self.assertEqual(len(topic_ids), 1)
        sid = topic_ids[0]
        output = Path(output)
        output.mkdir(parents=True, exist_ok=True)
        report_path = output/'report.json'
        old = json.loads(report_path.read_text()) if report_path.exists() else {}
        channels = old.get('channels', {month:{'source_id':'cls','status':'pending',
            'terminal':False,'coverage':{'complete':False}} for month in month_keys(first,last)})
        ordered = list(priority_months or month_keys(first,last))
        eligible = [month for month in ordered if not channels[month].get('terminal')]
        eligible = eligible or [month for month in channels if not channels[month].get('terminal')]
        selected = eligible[0] if eligible else next(iter(channels))
        count = max_articles if max_articles is not None else 2
        before = old.get('candidate_count',0)
        candidate_path = output/'candidates.jsonl'
        rows = [json.loads(line) for line in candidate_path.read_text().split('\n') if line.strip()] if candidate_path.exists() else []
        for number in range(before,before+count):
            rows.append({'source_id':'cls','candidate_id':'cls:'+sid+':'+str(number),
                'source_url':'https://www.cls.cn/detail/'+sid+str(number),
                'title':'公开宏观新闻','body':'真实公开正文，仍需 Agent 判断原油相关性。',
                'published_date':max(first,selected+'-01'),'discovery_topic_id':sid})
        candidate_path.write_text(''.join(json.dumps(row,ensure_ascii=False)+'\n' for row in rows))
        channels[selected].update(status='complete' if complete_month else 'incomplete',
                                  terminal=complete_month,coverage={'complete':complete_month})
        terminal = all(value.get('terminal') for value in channels.values())
        report = {'source_id':'cls','topic_ids':[sid],'status':'complete' if terminal else 'incomplete',
            'terminal':terminal,'channels':channels,'candidate_count':len(rows),'new_candidates':count,
            'pages':old.get('pages',0)+max_pages,'pages_this_call':max_pages,
            'processed_article_count':old.get('processed_article_count',0)+count,
            'processed_articles_this_call':count,'candidates_path':str(candidate_path),'errors':[],
            'coverage':{'requested_start':first,'requested_end':last,'topic_ids':[sid],'complete':terminal}}
        report_path.write_text(json.dumps(report,ensure_ascii=False))
        self.calls.append({'topic':sid,'priority':list(ordered),'month':selected,
                           'max_pages':max_pages,'max_articles':max_articles,'output':str(output)})
        return report

    def call(self, **kwargs):
        return run_source('cls','2026-01-01','2026-10-30',self.root,
                          priority_months=[f'2026-{n:02d}' for n in range(1,10)],**kwargs)

    def test_default_allowlist_and_fetcher_export_are_explicit(self):
        self.assertIs(HistoryFetcher,OriginalFetcher)
        self.assertEqual(len(MACRO_TOPIC_IDS),13)
        self.assertEqual(set(MACRO_TOPIC_IDS),set(MACRO_TOPICS))
        self.assertEqual(MACRO_TOPICS['1556'],'环球市场情报')

    def test_topic_work_budget_and_round_robin_survive_restart(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            first = self.call(max_pages=18,max_articles=180)
            first_calls = list(self.calls)
            second = self.call(max_pages=18,max_articles=180)
        self.assertEqual(first['topics_visited_this_call'],self.ids[:9])
        self.assertEqual(second['topics_visited_this_call'],self.ids[9:]+self.ids[:5])
        self.assertEqual(sum(row['max_pages'] for row in first_calls),18)
        self.assertEqual(sum(row['max_articles'] for row in first_calls),180)
        self.assertTrue(all(row['max_pages']<=2 for row in self.calls))
        self.assertEqual(first['processed_articles_this_call'],180)
        self.assertEqual(second['processed_articles_this_call'],180)
        self.assertEqual(second['candidate_count'],360)
        self.assertFalse(first['terminal'])
        self.assertFalse(first['coverage']['complete'])
        self.assertEqual(set(first['channels']),set(self.ids))

    def test_topics_start_in_different_priority_months(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            self.call(max_pages=18,max_articles=18)
        months = [f'2026-{n:02d}' for n in range(1,10)]
        self.assertEqual([row['priority'][0] for row in self.calls],months)
        for index,row in enumerate(self.calls):
            self.assertEqual(row['priority'],months[index:]+months[:index])
            self.assertEqual(Path(row['output']),(self.root/'topics'/self.ids[index]).resolve())

    def test_one_article_budget_visits_only_one_topic_then_advances(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            first = self.call(max_pages=18,max_articles=1)
            second = self.call(max_pages=18,max_articles=1)
        self.assertEqual(first['topics_visited_this_call'],[self.ids[0]])
        self.assertEqual(second['topics_visited_this_call'],[self.ids[1]])
        self.assertEqual(first['candidate_count'],1)
        self.assertFalse(first['coverage']['complete'])

    def test_nonpriority_october_waits_for_every_topic_priority_month(self):
        def complete(*args,**kwargs):
            return self.fake(*args,**kwargs,complete_month=True)
        selected = self.ids[:2]
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=complete):
            reports = [run_source('cls','2026-09-01','2026-10-30',self.root,
                priority_months=['2026-09'],topic_ids=selected,max_pages=2,max_articles=1) for _ in range(4)]
        self.assertEqual([(row['topic'],row['month']) for row in self.calls],
            [(selected[0],'2026-09'),(selected[1],'2026-09'),
             (selected[0],'2026-10'),(selected[1],'2026-10')])
        self.assertFalse(reports[1]['terminal'])
        self.assertFalse(reports[1]['coverage']['complete'])
        self.assertTrue(reports[-1]['terminal'])
        self.assertTrue(reports[-1]['coverage']['complete'])

    def test_blocked_topic_keeps_gap_but_other_topic_can_complete(self):
        def blocked_one(*args,**kwargs):
            report = self.fake(*args,**kwargs,complete_month=True)
            if kwargs['topic_ids']==[self.ids[0]]:
                report.update(status='blocked',terminal=True,errors=['公开渠道拒绝访问'])
                report['coverage']['complete'] = False
                (Path(args[3])/'report.json').write_text(json.dumps(report))
            return report
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=blocked_one):
            result = run_source('cls','2026-01-01','2026-01-31',self.root,
                topic_ids=self.ids[:2],max_pages=4,max_articles=2)
        self.assertTrue(result['terminal'])
        self.assertFalse(result['coverage']['complete'])
        self.assertEqual(result['channels'][self.ids[0]]['status'],'blocked')
        self.assertTrue(result['errors'])

    def test_same_article_from_two_topics_retains_fuller_body_once(self):
        def duplicates(*args,**kwargs):
            report = self.fake(*args,**kwargs)
            path = Path(report['candidates_path'])
            row = json.loads(path.read_text().strip())
            row['candidate_id'] = 'cls:same-article'
            row['source_url'] = 'https://www.cls.cn/detail/123'
            row['body'] = '短摘要' if kwargs['topic_ids']==[self.ids[0]] else '同一新闻的完整公开正文，保留更多原始事实。'
            path.write_text(json.dumps(row,ensure_ascii=False)+'\n')
            return report
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=duplicates):
            result = self.call(topic_ids=self.ids[:2],max_pages=4,max_articles=2)
        self.assertEqual(result['candidate_count'],1)
        row = json.loads(Path(result['candidates_path']).read_text().strip())
        self.assertEqual(row['body'],'同一新闻的完整公开正文，保留更多原始事实。')

    def test_child_report_recovers_after_parent_checkpoint_was_not_saved(self):
        first = self.ids[0]
        raw = self.root/'raw'
        raw.mkdir()
        evidence = raw/'original-response.json'
        evidence.write_text('{"public":"source evidence retained"}')
        self.fake('cls','2026-01-01','2026-01-31',self.root/'topics'/first,
            priority_months=['2026-01'],max_pages=1,max_articles=1,fetcher=None,
            topic_ids=[first],complete_month=True)
        self.calls.clear()
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            report = run_source('cls','2026-01-01','2026-01-31',self.root,
                topic_ids=self.ids[:2],max_pages=2,max_articles=1)
        self.assertEqual([row['topic'] for row in self.calls],[self.ids[1]])
        self.assertEqual(report['candidate_count'],2)
        self.assertEqual(report['channels'][first]['status'],'complete')
        self.assertEqual(evidence.read_text(),'{"public":"source evidence retained"}')

    def test_scope_or_topic_changes_cannot_reuse_checkpoint(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            self.call(topic_ids=self.ids[:2],max_pages=2,max_articles=1)
        for changed in [dict(start='2026-02-01'),dict(end='2026-09-30'),dict(ids=self.ids[1:3])]:
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                run_source('cls',changed.get('start','2026-01-01'),changed.get('end','2026-10-30'),
                    self.root,topic_ids=changed.get('ids',self.ids[:2]))

    def test_mismatched_durable_child_scope_is_rejected_before_collection(self):
        directory = self.root/'topics'/self.ids[0]
        report = self.fake('cls','2026-01-01','2026-10-30',directory,
            priority_months=['2026-01'],max_pages=1,max_articles=1,fetcher=None,
            topic_ids=[self.ids[0]])
        report['coverage']['requested_start'] = '2025-01-01'
        (directory/'report.json').write_text(json.dumps(report))
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics') as child:
            with self.assertRaises(ValueError):
                self.call(topic_ids=self.ids[:2])
        child.assert_not_called()

    def test_existing_monthly_archive_cannot_be_reused_as_a_macro_root(self):
        original = {
            'monthly-checkpoint.json':json.dumps({'source_id':'cls','requested_start':'2026-01-01',
                'requested_end':'2026-10-30','topic_ids':['1583','1101','10986','11394','1515']}),
            'report.json':json.dumps({'source_id':'cls','adapter':'cls_monthly_public_topics',
                'coverage':{'requested_start':'2026-01-01','requested_end':'2026-10-30'}}),
        }
        for name,content in original.items():
            (self.root/name).write_text(content)
        before = {name:(self.root/name).read_bytes() for name in original}
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics') as child:
            with self.assertRaises(ValueError):
                self.call(topic_ids=self.ids[:2])
        child.assert_not_called()
        self.assertEqual({name:(self.root/name).read_bytes() for name in original},before)
        self.assertFalse((self.root/'macro-checkpoint.json').exists())

    def test_saved_topic_scope_is_checked_when_the_child_report_is_missing(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            report = self.call(topic_ids=self.ids[:2],max_pages=2,max_articles=1)
        checkpoint = Path(report['checkpoint_path'])
        state = json.loads(checkpoint.read_text())
        state['topics'][self.ids[0]]['coverage']['requested_start'] = '2025-01-01'
        checkpoint.write_text(json.dumps(state))
        (self.root/'topics'/self.ids[0]/'report.json').unlink()
        before = checkpoint.read_bytes()
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics') as child:
            with self.assertRaises(ValueError):
                self.call(topic_ids=self.ids[:2],max_pages=2,max_articles=1)
        child.assert_not_called()
        self.assertEqual(checkpoint.read_bytes(),before)

    def test_unbounded_article_budget_still_obeys_finite_topic_page_turns(self):
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics',side_effect=self.fake):
            report = self.call(topic_ids=self.ids[:2],max_pages=4,max_articles=None)
        self.assertEqual([row['max_articles'] for row in self.calls],[None,None])
        self.assertEqual(sum(row['max_pages'] for row in self.calls),4)
        self.assertEqual(report['processed_articles_this_call'],4)
        self.assertFalse(report['coverage']['complete'])

    def test_real_monthly_layer_keeps_two_page_topic_calls_on_one_rotating_month(self):
        calls = []
        def leaf(source_id,first,last,output,*,max_pages,max_articles,fetcher,topic_ids):
            output = Path(output)
            output.mkdir(parents=True,exist_ok=True)
            sid = topic_ids[0]
            candidate_path = output/'candidates.jsonl'
            row = {'source_id':'cls','candidate_id':'cls:'+sid+':'+first,
                   'source_url':'https://www.cls.cn/detail/'+sid+first.replace('-',''),
                   'title':'公开宏观新闻','body':'来源公开正文，待Agent语义判断。',
                   'published_date':first,'discovery_topic_id':sid}
            candidate_path.write_text(json.dumps(row,ensure_ascii=False)+'\n')
            report = {'source_id':'cls','topic_ids':topic_ids,'status':'incomplete','terminal':False,
                'candidate_count':1,'new_candidates':1,'pages':1,'pages_this_call':1,
                'processed_article_count':1,'candidates_path':str(candidate_path),'errors':[],
                'coverage':{'requested_start':first,'requested_end':last,'complete':False,'topic_ids':topic_ids}}
            (output/'report.json').write_text(json.dumps(report))
            calls.append((sid,first[:7],max_pages))
            return report
        with patch('newsdesk.history_priority_topics.topics.run_source',side_effect=leaf):
            self.call(topic_ids=self.ids[:2],max_pages=4,max_articles=4)
            self.call(topic_ids=self.ids[:2],max_pages=4,max_articles=4)
        self.assertEqual(calls,[(self.ids[0],'2026-01',2),(self.ids[1],'2026-02',2),
                                (self.ids[0],'2026-02',2),(self.ids[1],'2026-03',2)])

    def test_invalid_arguments_fail_before_network(self):
        cases = [dict(max_pages=0),dict(max_articles=0),dict(max_articles=True),
                 dict(topic_ids=['unverified']),dict(topic_ids=[]),dict(topic_ids='3140'),
                 dict(priority_months=['2027-01']),dict(priority_months='2026-01')]
        with patch('newsdesk.history_cls_macro.monthly.run_monthly_topics') as child:
            for index,kwargs in enumerate(cases):
                with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):
                    run_source('cls','2026-01-01','2026-10-30',self.root/str(index),**kwargs)
            with self.assertRaises(ValueError):
                run_source('not-cls','2026-01-01','2026-10-30',self.root/'wrong')
        child.assert_not_called()


if __name__=='__main__':
    unittest.main()
