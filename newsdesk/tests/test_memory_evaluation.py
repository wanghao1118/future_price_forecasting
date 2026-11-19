"""Memory experiments stay isolated, closed-book, immutable and resumable."""
from collections import Counter
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from newsdesk.evaluation import AIHUB_MODELS
from newsdesk.memory_evaluation import (MemoryEvaluationManager, closed_book_audit,
    file_lock, read_json, write_json, metric, SCORING_POLICY)
from newsdesk.memory_leakage import digest


def dataset():
    questions=[]
    for month in range(1,11):
        for position in range(1,11):
            questions.append({'case_id':'memory-2026-{:02d}-{:02d}'.format(month,position),
                              'month':'2026-{:02d}'.format(month),
                              'question':'本期新公布的指标{:02d}-{:02d}为____。'.format(month,position),
                              'answer':'10000','answer_aliases':['一万'],
                              'source':{'news_id':'news-{}-{}'.format(month,position)}})
    return {'dataset_id':'frozen-test-memory-v2','version':'memory-leakage-cloze-v2',
            'total_questions':100,'questions':questions}


def audit(passed=True):
    return {'passed':passed,'tools_enabled':False,'web_search_enabled':False,
            'network_retrieval_enabled':False,'tool_calls':[],
            'search_call_count':0,'indicator_call_count':0,'tool_call_count':0}


class Harness:
    def __init__(self):
        self.predictions=Counter();self.judgments=Counter();self.order=[]
        self.prediction_failures=set();self.judge_failures=0;self.bad_tools=set()
        self.active=Counter();self.maximum=Counter();self.lock=threading.Lock()

    def predictor(self, model, generation_config=None):
        owner=self
        class Predictor:
            def predict(self,question,work):
                with owner.lock:
                    owner.order.append(model);owner.predictions[(model,question)]+=1
                    owner.active[model]+=1;owner.maximum[model]=max(owner.maximum[model],owner.active[model])
                try:
                    time.sleep(.001)
                    (work/'raw-output.txt').write_text('10000',encoding='utf-8')
                    if (model,question) in owner.prediction_failures and owner.predictions[(model,question)]==1:
                        raise RuntimeError('temporary prediction failure')
                    return {'answer':'10000','raw_output':'10000','prompt_sha256':'p','response_sha256':'r',
                            'tool_audit':audit((model,question) not in owner.bad_tools),
                            'generation_config':generation_config}
                finally:
                    with owner.lock:owner.active[model]-=1
        return Predictor()

    def evaluator(self):
        owner=self
        class Evaluator:
            def evaluate(self,items,work):
                self.work=work
                write_json(work/'evaluator-request.json',items)
                with owner.lock:
                    for item in items:owner.judgments[item['case_id']]+=1
                    if owner.judge_failures:
                        owner.judge_failures-=1
                        raise RuntimeError('temporary evaluator failure')
                return [{'case_id':item['case_id'],'correct':not item['case_id'].endswith('-10'),
                         'reason':'匹配参考答案' if not item['case_id'].endswith('-10') else '拒答或错误答案'}
                        for item in items]
        return Evaluator()


class MemoryEvaluationTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'memory-evaluations'
        self.harness=Harness()
        self.manager=MemoryEvaluationManager(self.root,dataset_loader=dataset,
            predictor_factory=self.harness.predictor,evaluator_factory=self.harness.evaluator)

    def tearDown(self):self.temp.cleanup()

    def prepare(self,models=None):return self.manager.prepare(models=models or [AIHUB_MODELS[0]])

    def test_prepare_freezes_all_models_and_public_inputs_without_references(self):
        batch=self.manager.prepare()
        self.assertEqual(set(batch['models']),set(AIHUB_MODELS));self.assertEqual(len(batch['models']),17)
        directory=self.manager._batch_dir('Exp00-extend')
        public=read_json(directory/'questions.json')
        self.assertEqual(len(public['questions']),100)
        self.assertTrue(all(set(row)=={'case_id','question'} for row in public['questions']))
        self.assertNotIn('answer',json.dumps(public));self.assertNotIn('source',json.dumps(public))
        self.assertIn('answer',read_json(directory/'reference-dataset.json')['questions'][0])
        self.assertTrue((directory/'source/newsdesk/memory_evaluation.py').exists())
        self.assertEqual(batch,self.manager.prepare())
        self.assertEqual(batch['generation_profiles']['gpt-6-luna']['max_rounds'],1)
        self.assertFalse((self.root.parent/'evaluations').exists())
        self.assertTrue(all(run.startswith('memory-') for run in batch['models'].values()))

    def test_snapshots_and_frozen_code_are_verified_before_calls(self):
        batch=self.prepare();directory=self.manager._batch_dir('Exp00-extend')
        original=(directory/'questions.json').read_bytes()
        write_json(directory/'questions.json',{'tampered':True})
        with self.assertRaisesRegex(ValueError,'快照校验失败'):self.manager.run()
        self.assertFalse(self.harness.order)
        (directory/'questions.json').write_bytes(original)
        source=directory/'source/newsdesk/memory_evaluation.py'
        source.write_text(source.read_text()+'\n# tampered\n')
        with self.assertRaisesRegex(ValueError,'源码校验失败'):self.manager.run()

    def test_fair_scheduling_all_cases_judged_and_complete_monthly_metrics(self):
        models=[AIHUB_MODELS[0],AIHUB_MODELS[1]];batch=self.prepare(models)
        summary=self.manager.run(workers=4,per_model=2,judge_workers=2)
        self.assertTrue(summary['complete']);self.assertEqual(summary['graded'],200)
        self.assertEqual(set(self.harness.order[:4]),set(models))
        self.assertTrue(all(x<=2 for x in self.harness.maximum.values()))
        self.assertEqual(sum(self.harness.judgments.values()),200) # exact matches still go through the evaluator
        for run_id in batch['models'].values():
            run=self.manager.get_run(run_id)
            self.assertEqual(run['status'],'completed');self.assertFalse(run['provisional'])
            self.assertEqual(run['metrics']['overall']['accuracy'],.9)
            self.assertEqual(run['metrics']['overall']['completion'],1)
            self.assertEqual(len(run['metrics']['months']),10)
            self.assertTrue(all(x['graded']==10 and x['correct']==9 for x in run['metrics']['months']))
            self.assertEqual(len(run['cases']),100)
        first=next(iter(batch['models'].values()));case=self.manager.get_run(first)['cases'][0]
        trace=self.manager.get_case_trace(first,case['case_id'])
        self.assertEqual(trace['prediction']['answer'],'10000')
        self.assertTrue(any(x['stage']=='evaluator' for x in trace['artifacts']))
        self.assertTrue(any(x['stage']=='prediction' for x in trace['artifacts']))
        self.assertEqual(len(self.manager.catalog()['runs']),2)
        calls=dict(self.harness.predictions);judges=dict(self.harness.judgments)
        self.manager.run(retry_failed=True)
        self.assertEqual(calls,self.harness.predictions);self.assertEqual(judges,self.harness.judgments)

    def test_failed_judgment_reuses_prediction_and_noncorrect_counts_zero(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model])
        self.harness.judge_failures=1
        first_question=dataset()['questions'][0]['question']
        self.harness.prediction_failures.add((model,first_question))
        summary=self.manager.run(workers=4,per_model=2,judge_workers=1)
        run_id=batch['models'][model];run=self.manager.get_run(run_id)
        self.assertTrue(run['complete']);self.assertFalse(run['provisional'])
        self.assertEqual(run['failed'],21);self.assertEqual(run['graded'],79)
        self.assertEqual(run['pending'],0)
        self.assertEqual(run['accuracy'],run['correct']/100)
        self.assertEqual(run['resolved'],100);self.assertEqual(run['completed'],100)
        self.assertEqual(run['wrong'],100-run['correct'])
        self.assertEqual(run['scoring_policy'],SCORING_POLICY)
        self.assertTrue(all(x['outcome']=='incorrect' and x['correct'] is None
                            for x in run['cases'] if x['status'] in {'prediction_failed','judge_failed'}))
        preserved={x['case_id']:hashlib.sha256((self.root/'runs'/run_id/'cases'/x['case_id']/'prediction.json').read_bytes()).hexdigest()
                   for x in run['cases'] if x['model_answer'] is not None}
        self.manager.run(workers=4,per_model=2,judge_workers=1,retry_failed=True)
        final=self.manager.get_run(run_id)
        self.assertEqual(final['graded'],100);self.assertEqual(final['failed'],0)
        self.assertEqual(final['accuracy'],.9);self.assertEqual(sum(self.harness.predictions.values()),101)
        self.assertEqual(self.harness.predictions[(model,first_question)],2)
        for case_id,fingerprint in preserved.items():
            self.assertEqual(hashlib.sha256((self.root/'runs'/run_id/'cases'/case_id/'prediction.json').read_bytes()).hexdigest(),fingerprint)

    def test_tool_violations_are_ungraded_and_never_sent_to_judge(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model])
        row=dataset()['questions'][0];self.harness.bad_tools.add((model,row['question']))
        self.manager.run(workers=4,per_model=2,judge_workers=1)
        run=self.manager.get_run(batch['models'][model])
        self.assertEqual(run['graded'],99);self.assertEqual(run['failed'],1)
        self.assertEqual(run['cases'][0]['status'],'tool_violation')
        self.assertEqual(run['cases'][0]['outcome'],'incorrect')
        self.assertIsNone(run['cases'][0]['correct'])
        self.assertEqual(run['accuracy'],run['correct']/100)
        self.assertTrue(run['complete'])
        self.assertNotIn(row['case_id'],self.harness.judgments)
        self.assertFalse(closed_book_audit({'tool_audit':{'passed':True}}))

    def test_fixed_denominator_unknown_error_and_pending(self):
        rows=[{'status':'graded','correct':True}, {'status':'graded','correct':False,'model_answer':'UNKNOWN'},
              {'status':'prediction_failed','correct':None}, {'status':'pending','correct':None}]
        score=metric(rows)
        self.assertEqual(score['accuracy'],.25);self.assertEqual(score['correct'],1)
        self.assertEqual(score['wrong'],2);self.assertEqual(score['incorrect'],2)
        self.assertEqual(score['resolved'],3);self.assertEqual(score['graded'],2)
        self.assertEqual(score['pending'],1);self.assertEqual(score['completion'],.75)
        self.assertFalse(score['complete'])
        self.assertEqual(metric([{'status':'pending'}])['accuracy'],0)
        self.assertEqual(metric([{'status':'prediction_failed'}])['accuracy'],0)
        self.assertTrue(metric([{'status':'prediction_failed'}])['complete'])

    def test_all_prediction_errors_are_zero_over_100_without_fake_verdicts(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model])
        self.harness.prediction_failures={(model,x['question']) for x in dataset()['questions']}
        summary=self.manager.run(workers=4,per_model=2,judge_workers=1)
        run=self.manager.get_run(batch['models'][model])
        self.assertEqual(run['accuracy'],0);self.assertEqual(run['wrong'],100)
        self.assertEqual(run['graded'],0);self.assertEqual(run['resolved'],100)
        self.assertEqual(run['failed'],100);self.assertEqual(run['pending'],0)
        self.assertTrue(run['complete']);self.assertEqual(run['status'],'completed')
        self.assertTrue(all(x['outcome']=='incorrect' and x['correct'] is None for x in run['cases']))
        self.assertFalse(self.harness.judgments)
        self.assertEqual(summary['accuracy'],0);self.assertEqual(summary['graded'],0)
        self.assertEqual(summary['resolved'],100);self.assertEqual(summary['completed'],100)
        self.assertEqual(summary['pending'],0);self.assertEqual(summary['wrong'],100)

    def test_profile_revision_is_locked_archived_and_keeps_successes_and_code(self):
        models=['gpt-6-astra','claude-fable-5-1'];batch=self.prepare(models)
        directory=self.manager._batch_dir('Exp00-extend')
        old_profiles=read_json(directory/'profiles.json');old_batch=copy.deepcopy(batch)
        run_id=batch['models'][models[0]];row=dataset()['questions'][0]
        locked=self.root/'runs'/run_id/'cases'/row['case_id']/'prediction.json'
        write_json(locked,{'answer':'10000','tool_audit':audit()});locked_bytes=locked.read_bytes()
        overrides={'gpt-6-astra':{'api_protocol':'chat','provider':'azure'},
                   'claude-fable-5-1':{'api_protocol':'chat','provider':'anthropic'}}
        with file_lock(self.root/'locks/Exp00-extend.lock'):
            with self.assertRaises(RuntimeError):self.manager.revise_profiles('Exp00-extend',overrides,'route fix')
        with self.assertRaises(ValueError):
            self.manager.revise_profiles('Exp00-extend',{'gpt-6-astra':{'api_key':'secret'}},'invalid')
        with self.assertRaises(ValueError):
            self.manager.revise_profiles('Exp00-extend',{'gpt-6-astra':{'max_output_tokens':1}},'invalid')
        revised=self.manager.revise_profiles('Exp00-extend',overrides,'confirmed provider routing')
        self.manager._verify(revised)
        self.assertEqual(locked_bytes,locked.read_bytes())
        self.assertEqual(revised['code_sha256'],old_batch['code_sha256'])
        self.assertEqual(revised['dataset_snapshot_sha256'],old_batch['dataset_snapshot_sha256'])
        self.assertEqual(len(revised['profile_revisions']),1)
        entry=revised['profile_revisions'][0];archive=read_json(self.root/entry['archive'])
        self.assertEqual(digest(archive),entry['archive_sha256'])
        self.assertEqual(archive['previous_profiles'],old_profiles)
        self.assertEqual(archive['previous_batch'],old_batch)
        for model,run_id in revised['models'].items():
            record=read_json(self.root/'runs'/run_id/'run.json')
            self.assertEqual(record['generation_config']['max_output_tokens'],old_profiles[model]['max_output_tokens'])
            self.assertEqual(record['generation_config']['provider'],overrides[model]['provider'])
            self.assertEqual(record['generation_config']['api_protocol'],'chat')
            self.assertEqual(record['profiles_sha256'],revised['profiles_sha256'])
            self.assertEqual(record['config_history'][0]['previous_generation_config'],old_profiles[model])
        self.assertEqual(self.manager.revise_profiles('Exp00-extend',overrides,'same routes'),revised)

    def test_hy4_revision_only_allows_reasoning_none_without_budget_or_route_changes(self):
        batch=self.prepare(['hy4-preview']);directory=self.manager._batch_dir('Exp00-extend')
        before=read_json(directory/'profiles.json')['hy4-preview']
        for fields in ({'reasoning_effort':'low'}, {'reasoning_effort':'none','max_output_tokens':16000},
                       {'reasoning_effort':'none','api_protocol':'chat'}, {'provider':'taiji'}):
            with self.assertRaises(ValueError):
                self.manager.revise_profiles('Exp00-extend',{'hy4-preview':fields},'invalid expanded settings')
        with file_lock(self.root/'locks/Exp00-extend.lock'):
            with self.assertRaises(RuntimeError):
                self.manager.revise_profiles('Exp00-extend',{'hy4-preview':{'reasoning_effort':'none'}},'HY4 smoke evidence')
        revised=self.manager.revise_profiles('Exp00-extend',{'hy4-preview':{'reasoning_effort':'none'}},'HY4 smoke evidence')
        after=read_json(directory/'profiles.json')['hy4-preview']
        self.assertEqual(after,{**before,'reasoning_effort':'none'})
        self.assertEqual(after['max_output_tokens'],before['max_output_tokens'])
        self.assertEqual(after.get('api_protocol'),before.get('api_protocol'))
        self.assertEqual(after.get('provider'),before.get('provider'))
        self.assertEqual(revised['code_sha256'],batch['code_sha256'])
        record=read_json(self.root/'runs'/batch['models']['hy4-preview']/'run.json')
        self.assertEqual(record['config_history'][0]['previous_generation_config'],before)
        archive=read_json(self.root/revised['profile_revisions'][0]['archive'])
        self.assertEqual(archive['previous_profiles']['hy4-preview'],before)
        self.manager._verify(revised)

    def test_batch_lock_and_identifiers(self):
        batch=self.prepare()
        with file_lock(self.root/'locks/Exp00-extend.lock'):
            with self.assertRaisesRegex(RuntimeError,'另一进程'):self.manager.run()
        for run_id in ('../outside','price-run','memory-../../outside'):
            with self.assertRaises((ValueError,KeyError)):self.manager.get_run(run_id)
        run_id=next(iter(batch['models'].values()))
        with self.assertRaises(ValueError):self.manager.get_case_trace(run_id,'../outside')
        with self.assertRaises(KeyError):self.manager.get_case_trace(run_id,'memory-missing')

    def test_stale_prediction_state_resumes_judging_without_model_call(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model]);run_id=batch['models'][model]
        case=dataset()['questions'][0];path=self.root/'runs'/run_id/'cases'/case['case_id']
        write_json(path/'prediction.json',{'answer':'10000','tool_audit':audit()})
        write_json(path/'state.json',{'status':'predicting','attempts':{'prediction':1,'judge':0}})
        self.manager.run(workers=4,per_model=2,judge_workers=1)
        self.assertEqual(sum(self.harness.predictions.values()),99)
        self.assertEqual(self.manager.get_run(run_id)['graded'],100)

    def test_committed_prediction_helper_recovered_after_coordinator_crash(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model]);run_id=batch['models'][model]
        question=dataset()['questions'][0];path=self.root/'runs'/run_id/'cases'/question['case_id']
        work=path/'attempts/prediction-001';prompt={'instruction':'test','question':question['question']}
        write_json(work/'prompt.json',prompt);write_json(work/'generation-config.json',{})
        (work/'raw-output.txt').write_text('10000',encoding='utf-8')
        write_json(work/'gate-receipt.json',{**audit(),'prompt_sha256':digest(prompt),
                   'response_sha256':hashlib.sha256(b'10000').hexdigest()})
        write_json(path/'state.json',{'status':'predicting','attempts':{'prediction':1,'judge':0}})
        self.manager.run(workers=4,per_model=2,judge_workers=1)
        self.assertEqual(sum(self.harness.predictions.values()),99)
        self.assertIn('recovered_from',read_json(path/'prediction.json'))
        self.assertEqual(self.manager.get_run(run_id)['graded'],100)

    def test_committed_judge_helper_recovered_without_duplicate_judging(self):
        model=AIHUB_MODELS[0];batch=self.prepare([model]);run_id=batch['models'][model]
        question=dataset()['questions'][0];path=self.root/'runs'/run_id/'cases'/question['case_id']
        write_json(path/'prediction.json',{'answer':'10000','tool_audit':audit()})
        work=self.root/'runs'/run_id/'judges/completed-before-crash'
        material={'instruction':'judge','items':[{key:question[key] for key in ('case_id','question','answer','answer_aliases')}]}
        material['items'][0]['model_answer']='10000'
        verdicts=[{'case_id':question['case_id'],'correct':True,'reason':'matched'}]
        write_json(work/'evaluator-request.json',material);write_json(work/'verdicts.json',verdicts)
        write_json(work/'gate-receipt.json',{'passed':True,'independent':True,'evaluator':'codex_cli',
                   'prompt_sha256':digest(material),'verdicts_sha256':digest(verdicts)})
        write_json(path/'judge-artifacts.json',[str(work.relative_to(self.root))])
        write_json(path/'state.json',{'status':'judging','attempts':{'prediction':1,'judge':1}})
        self.manager.run(workers=4,per_model=2,judge_workers=1)
        self.assertNotIn(question['case_id'],self.harness.judgments)
        self.assertEqual(sum(self.harness.predictions.values()),99)
        self.assertTrue(read_json(path/'verdict.json')['recovered'])
        self.assertEqual(self.manager.get_run(run_id)['graded'],100)


if __name__=='__main__':unittest.main()
