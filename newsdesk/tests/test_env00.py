"""env00 capability enforcement, UTC cutoff, transcript audit and sequencing."""
import copy
import fcntl
import io
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest
from unittest import mock

from newsdesk.batch_audit import audit_run
from newsdesk.batch_dependencies import dependency_ready
from newsdesk.batch_evaluation import PreparedManager, prepare_experiment, execute
from newsdesk.env01_search import LocalIndicatorSearch, serve
from newsdesk.environment_policy import ENV00_PROTOCOL
from newsdesk.evaluation import (AIHubPricePredictor, RETRIEVAL_TIME_POLICY, digest,
    read_json, retrieval_metadata, write_json, validate_run_retrieval_policy)
from newsdesk.tests.test_evaluation import forecast, FakeBenchmark, FakeNews, question
from newsdesk.tests.test_retrieval_cutoff_evaluation import new_context
from time_gate_agent.local import LocalTimeGate


class TechnicalOnlyTests(unittest.TestCase):
    def context(self, root):
        context = new_context(root, RETRIEVAL_TIME_POLICY)
        context.update(environment_id="env00")
        context.pop("news_snapshot_path")
        context.pop("news_snapshot_sha256")
        return context

    def scripted_api(self, predictor, requests):
        def reply(payload, protocol="chat"):
            requests.append(copy.deepcopy(payload))
            names = [tool.get('name') or tool['function']['name'] for tool in payload['tools']]
            self.assertEqual(names, ['search_indicators'])
            n = len(requests)
            if n <= 2:
                args = {'indicator':'price','start_time':'2026-01-01T00:00:00Z',
                        'end_time': '2026-10-30T22:00:00Z' if n == 1 else '2026-10-29T21:30:00Z'}
                if protocol == 'responses':
                    return {'id':'response-'+str(n),'output':[{'type':'function_call','name':'search_indicators',
                            'call_id':'tool-'+str(n),'arguments':json.dumps(args)}]},'trace'
                return {'choices':[{'message':{'role':'assistant','content':None,'tool_calls':[{
                    'id':'tool-'+str(n),'type':'function','function':{'name':'search_indicators','arguments':json.dumps(args)}}]}}]},'trace'
            answer = json.dumps(dict(forecast(),reason='Technical evidence',public_analysis={
                'analysis_document':'Public technical analysis with evidence and uncertainty. ' * 30}))
            if protocol == 'responses':
                return {'id':'final','output':[{'type':'message','content':[{'type':'output_text','text':answer}]}]},'trace'
            return {'choices':[{'message':{'role':'assistant','content':answer}}]},'trace'
        return mock.patch.object(predictor, '_request', side_effect=reply)

    def test_chat_and_responses_only_offer_indicators_and_reject_future_queries(self):
        for protocol in ('chat','responses'):
            with self.subTest(protocol=protocol), tempfile.TemporaryDirectory() as directory:
                root=Path(directory);context=self.context(root);requests=[]
                rows=read_json(context['indicator_snapshot_path'])
                rows.append({'date':'2026-10-30','price':99999,'available_at':'2026-10-30T21:00:00Z'})
                write_json(Path(context['indicator_snapshot_path']),rows);context['indicator_snapshot_sha256']=digest(rows)
                predictor=AIHubPricePredictor('deepseek-v4-pro',api_key='test',api_protocol=protocol)
                with self.scripted_api(predictor,requests), mock.patch('newsdesk.evaluation.LocalNewsSearch',side_effect=AssertionError('News must never be opened')):
                    predictor.predict({'question':context['case'],'environment_id':'env00'},root/'model',context)
                trace=read_json(root/'model/conversation.json')
                self.assertEqual(trace['protocol'],ENV00_PROTOCOL)
                self.assertEqual(trace['search_call_count'],0)
                self.assertFalse((root/'model/search').exists())
                returns=[e['result'] for e in trace['conversation'] if e['type']=='tool_result']
                self.assertEqual(returns[0]['status'],'rejected')
                self.assertTrue(returns[0].get('error') or returns[0].get('query_validation'))
                self.assertEqual(returns[1]['cutoff_utc'],'2026-10-29T21:30:00Z')
                self.assertNotIn('99999',json.dumps(returns))
                self.assertEqual(trace['indicator_call_count'],2)
                self.assertTrue(trace['public_analysis']['analysis_document'])
                self.assertFalse(trace['validation_errors'])

    def test_unadvertised_news_and_web_calls_are_rejected(self):
        for protocol in ('chat','responses'):
            for tool in ('search_news','web_search'):
                with self.subTest(protocol=protocol,tool=tool),tempfile.TemporaryDirectory() as directory:
                    root=Path(directory);context=self.context(root)
                    predictor=AIHubPricePredictor('deepseek-v4-pro',api_key='test',api_protocol=protocol)
                    data=({'output':[{'type':'function_call','name':tool,'arguments':'{"keywords":"oil"}','call_id':'bad'}]}
                          if protocol=='responses' else {'choices':[{'message':{'tool_calls':[{'id':'bad','function':{'name':tool,'arguments':'{"keywords":"oil"}'}}]}}]})
                    with mock.patch.object(predictor,'_request',return_value=(data,'trace')),mock.patch('newsdesk.evaluation.LocalNewsSearch') as news:
                        with self.assertRaisesRegex(RuntimeError,'未授权的 env00'):
                            predictor.predict({},root/'model',context)
                        news.assert_not_called()

    def test_mcp_registers_only_indicator_and_denies_direct_news_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=self.context(root)
            indicator=LocalIndicatorSearch(context['case'],read_json(context['indicator_snapshot_path']),root/'audit')
            messages=[{'jsonrpc':'2.0','id':1,'method':'tools/list'},
                      {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'search_news','arguments':{'keywords':'oil'}}}]
            output=io.StringIO();serve(None,indicator,io.StringIO(''.join(json.dumps(m)+'\n' for m in messages)),output)
            replies=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([x['name'] for x in replies[0]['result']['tools']],['search_indicators'])
            self.assertTrue(replies[1]['result']['isError'])
            self.assertEqual(json.loads(replies[1]['result']['content'][0]['text'])['error']['code'],'unauthorized_tool')

    def test_env00_stdio_startup_does_not_require_news_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=self.context(root);write_json(root/'context.json',context)
            process=subprocess.run([sys.executable,'-m','newsdesk.env01_search','--context',str(root/'context.json'),
                '--audit-dir',str(root/'search')],input=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/list'})+'\n',
                capture_output=True,text=True,timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual([t['name'] for t in json.loads(process.stdout)['result']['tools']],['search_indicators'])
            self.assertFalse((root/'search').exists())

    def test_successful_case_preserves_full_document_receipts_and_passes_scoring_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);context=self.context(root);case=context['case']
            data={'manifest':{'dataset_id':'fixture','mode':'backtest','series_type':'dominant_continuous'},
                  'questions':[case],'labels':{case['case_id']:forecast()}}
            store=mock.Mock();store.load_dataset.return_value=data
            news=mock.Mock();news.all.side_effect=AssertionError('env00 must not load news')
            manager=PreparedManager(root/'evaluation',store,news)
            run=manager.start(strategy_id='aihub-deepseek-v4-pro',environment_id='env00',experiment_id='Exp00')
            path=manager.root/'runs'/run['run_id']
            technical=[{'case_id':case['case_id'],'rows':read_json(context['indicator_snapshot_path'])}]
            write_json(path/'technical-snapshot.json',technical);run['technical_snapshot_sha256']=digest(technical)
            write_json(path/'run.json',run)
            predictor=AIHubPricePredictor('deepseek-v4-pro',api_key='test',api_protocol='chat');requests=[]
            with self.scripted_api(predictor,requests):
                result=manager._case(path,case,forecast(),LocalTimeGate([]),predictor)
            self.assertEqual(result['status'],'success',result.get('error'))
            manager._update(path,[case],finish=True)
            run=read_json(path/'run.json')
            audit=audit_run(manager.root,run['run_id'],run)
            self.assertTrue(audit['complete'])
            trace_path=path/result['trace_file'];trace=read_json(trace_path)
            event=next(event for event in trace['conversation'] if event['type']=='tool_result')
            event['result']['unexpected']='tampered transcript'
            write_json(trace_path,trace)
            with self.assertRaisesRegex(AssertionError,'transcript differs'):
                audit_run(manager.root,run['run_id'],run)
            self.assertEqual(audit['verified'],1)
            self.assertEqual(result['absolute_errors'],{'high':0,'low':0,'return_pct':0})
            saved=read_json(path/'cases'/case['case_id']/result['attempt']/'input.json')
            self.assertNotIn('search_tool',saved)
            self.assertEqual(saved['environment_id'],'env00')
            bad=dict(run,environment_id='env01')
            with self.assertRaisesRegex(ValueError,'环境协议'):
                validate_run_retrieval_policy(bad,data)


class SequencingTests(unittest.TestCase):
    def test_dependency_requires_terminal_audit_and_all_locks_released(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);batch=root/'batches/Exp01.json'
            write_json(batch,{'models':{'m':'r'}})
            state={'status':'running','validation':[{'run_id':'r'}],'finished_at':'now'}
            run={'status':'completed','completed':90,'total':90}
            write_json(root/'runs/r/run.json',run)
            for status in ('running','audit_failed'):
                state['status']=status;write_json(root/'batches/Exp01-execution.json',state)
                self.assertFalse(dependency_ready(root,'Exp01'))
            state['status']='partial';write_json(root/'batches/Exp01-execution.json',state)
            self.assertTrue(dependency_ready(root,'Exp01'))
            for path in (batch.with_suffix('.watch.lock'),root/'runs/r/execution.lock'):
                with path.open('a') as lock:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    self.assertFalse(dependency_ready(root,'Exp01'))
            write_json(root/'batches/Exp00.json',{'depends_on_experiment':'Exp01'})
            run['status']='running';write_json(root/'runs/r/run.json',run)
            with mock.patch('newsdesk.batch_evaluation.AIHubPricePredictor') as predictor:
                with self.assertRaisesRegex(ValueError,'前置实验'):
                    execute(root,root/'batches/Exp00.json')
                predictor.assert_not_called()

    def test_prepared_exp00_reuses_frozen_inputs_and_parameters_without_launching(self):
        benchmark=FakeBenchmark();benchmark.questions=[question(f'{f}-{i}',f) for f in ('WTI','Brent','SC') for i in range(30)]
        benchmark.labels={q['case_id']:forecast() for q in benchmark.questions}
        models=['deepseek-v4-pro','gpt-6.1-sol']
        with tempfile.TemporaryDirectory() as directory,mock.patch('newsdesk.batch_evaluation.BenchmarkStore',return_value=benchmark),mock.patch('newsdesk.batch_evaluation.AIHubPricePredictor'):
            root=Path(directory)
            parent=prepare_experiment(root,'Exp01',models,news_store=FakeNews())
            parent_bytes=parent.read_bytes();b1=read_json(parent)
            child=prepare_experiment(root,'Exp00',models,environment_id='env00',reference_batch=parent)
            b0=read_json(child)
            self.assertEqual(parent.read_bytes(),parent_bytes)
            self.assertEqual(b0['phase'],'waiting_dependency')
            self.assertEqual(b0['technical_snapshot_sha256'],b1['technical_snapshot_sha256'])
            self.assertEqual(b0['dataset_snapshot_sha256'],b1['dataset_snapshot_sha256'])
            for model,rid in b0['models'].items():
                row=read_json(root/'runs'/rid/'run.json')
                original=read_json(root/'runs'/b1['models'][model]/'run.json')
                self.assertEqual(row['generation_config'],original['generation_config'])
                self.assertEqual(row['status'],'waiting_dependency')
                self.assertEqual(read_json(root/'runs'/rid/'news-snapshot.json'),[])
                self.assertEqual(row['protocol'],ENV00_PROTOCOL)
                self.assertFalse((root/'runs'/rid/'cases').exists())


if __name__=='__main__':unittest.main()
