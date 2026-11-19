"""Closed-book host capability and independent-judge contracts; no live calls."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from newsdesk.evaluation import AIHUB_MODELS
from newsdesk.memory_model import (ClosedBookPredictor, CodexMemoryEvaluator,
    CLOSED_BOOK_INSTRUCTION, CLOSED_BOOK_PROTOCOL, JUDGE_INSTRUCTION)
from time_gate_agent.closed_book import ClosedBookGate, ClosedBookViolation


def question():
    return '2026年1月公布的该期数据同比增长____。'


def chat_response(text='2.7%'):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': text,
            'reasoning_content': 'PRIVATE HIDDEN REASONING'}}]}


def responses_response(text='2.7%'):
    return {'status': 'completed', 'output': [
        {'type': 'reasoning', 'encrypted_content': 'PRIVATE HIDDEN REASONING'},
        {'type': 'message', 'content': [{'type': 'output_text', 'text': text}]}]}


class PredictorTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {'AIHUB_API_KEY': 'fixture-secret',
                                              'AIHUB_BASE_URL': 'http://localhost/v1'})
        self.env.start()
        self.config = mock.patch('newsdesk.evaluation.load_private_aihub_config')
        self.config.start()

    def tearDown(self):
        self.config.stop(); self.env.stop()

    def test_all_seventeen_models_have_question_only_payload_and_saved_receipt(self):
        self.assertEqual(len(AIHUB_MODELS), 17)
        for model in AIHUB_MODELS:
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                p = ClosedBookPredictor(model)
                protocol = p.generation_config['api_protocol']
                response = responses_response() if protocol == 'responses' else chat_response()
                def reply(payload, protocol='chat'):
                    messages = payload['input' if protocol == 'responses' else 'messages']
                    self.assertEqual(messages, [{'role':'system','content':CLOSED_BOOK_INSTRUCTION},
                                                {'role':'user','content':question()}])
                    self.assertEqual(payload['tools'], [])
                    self.assertEqual(payload['tool_choice'], 'none')
                    self.assertNotIn('previous_response_id', payload)
                    return response, 'fixture-trace'
                with mock.patch.object(p.transport, '_request', side_effect=reply) as request, \
                        mock.patch.object(p.transport, 'predict', side_effect=AssertionError('Price/search entry forbidden')):
                    value = p.predict(question(), Path(directory))
                self.assertEqual(request.call_count, 1)
                self.assertEqual(value['answer'], '2.7%')
                self.assertTrue(value['tool_audit']['passed'])
                self.assertEqual(value['tool_audit']['tool_calls'], [])
                self.assertEqual(value['tool_audit']['protocol'], CLOSED_BOOK_PROTOCOL)
                saved = ''.join(path.read_text() for path in Path(directory).iterdir())
                self.assertNotIn('fixture-secret', saved)
                self.assertNotIn('PRIVATE HIDDEN REASONING', saved)
                if model == 'gpt-6-luna':
                    self.assertEqual(protocol, 'chat')
                    self.assertEqual(p.generation_config['provider'], 'azure')
                    self.assertEqual(p.generation_config['reasoning_effort'], 'none')
                if model == 'claude-haiku-5-5':
                    self.assertEqual(p.generation_config['provider'], 'anthropic')

    def test_question_object_context_and_unknown_generation_fields_are_rejected(self):
        p = ClosedBookPredictor('gpt-6-luna')
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(p.transport, '_request') as request:
            with self.assertRaises(ValueError):
                p.predict({'question':question(),'answer':'secret'},Path(directory))
            request.assert_not_called()
        with self.assertRaises(ValueError):
            ClosedBookPredictor('gpt-6-luna', {'tools':[{'type':'web_search'}]})

    def test_actual_outgoing_payload_cannot_be_augmented_with_retrieval_or_labels(self):
        base = {'model':'m','messages':[{'role':'system','content':CLOSED_BOOK_INSTRUCTION},
               {'role':'user','content':question()}],'tools':[],'tool_choice':'none','stream':False,'max_tokens':100}
        for mutate in (lambda p:p.update(previous_response_id='history'),
                       lambda p:p.update(tools=[{'type':'web_search'}]),
                       lambda p:p.update(tool_choice='auto'),
                       lambda p:p['messages'].append({'role':'user','content':'answer=secret'}),
                       lambda p:p.update(source_url='http://news')):
            payload=copy.deepcopy(base);mutate(payload)
            with self.assertRaises(ClosedBookViolation):
                ClosedBookGate(question()).validate_payload(payload,'chat','m')

    def test_chat_responses_and_provider_tool_calls_fail_closed(self):
        cases = [('gpt-6-luna',{'choices':[{'message':{'content':'2.7%','tool_calls':[{'function':{'name':'web_search'}}]}}]}),
                 ('gpt-6.1-sol',{'output':[{'type':'web_search_call','status':'completed'}]}),
                 ('claude-haiku-5-5',{'choices':[{'message':{'content':[{'type':'tool_use','name':'search_news'}]}}]})]
        for model,response in cases:
            with self.subTest(model=model),tempfile.TemporaryDirectory() as directory:
                p=ClosedBookPredictor(model)
                with mock.patch.object(p.transport,'_request',return_value=(response,None)):
                    with self.assertRaises(ClosedBookViolation):
                        p.predict(question(),Path(directory))
                receipt=json.loads((Path(directory)/'gate-receipt.json').read_text())
                self.assertFalse(receipt['passed'])
                self.assertTrue(receipt['denied_attempts'])

    def test_empty_or_truncated_outputs_are_not_scored_as_valid_predictions(self):
        for response in (chat_response(''),dict(choices=[dict(chat_response()['choices'][0],finish_reason='length')])):
            with tempfile.TemporaryDirectory() as directory:
                p=ClosedBookPredictor('gpt-6-luna')
                with mock.patch.object(p.transport,'_request',return_value=(response,None)):
                    with self.assertRaises(RuntimeError):p.predict(question(),Path(directory))
                self.assertFalse(json.loads((Path(directory)/'gate-receipt.json').read_text())['passed'])


class EvaluatorTests(unittest.TestCase):
    def items(self):
        return [{'case_id':'a','question':question(),'answer':'2.7%','answer_aliases':['百分之2.7'],'model_answer':'2.7%'},
                {'case_id':'b','question':question(),'answer':'2.7%','answer_aliases':[],'model_answer':'2.7% or 3%'}]

    def process(self, verdicts, tool=False):
        def spawn(command, **kwargs):
            self.command=command
            class Process:
                returncode=0
                def communicate(process, prompt=None, timeout=None):
                    self.prompt=prompt
                    Path(command[command.index('--output-last-message')+1]).write_text(json.dumps({'verdicts':verdicts}))
                    events=([{'type':'item.completed','item':{'type':'command_execution','command':'cat secret'}}] if tool else [])
                    return '\n'.join(json.dumps(e) for e in events+[{'type':'turn.completed'}]),''
            return Process()
        return spawn

    def test_every_answer_including_exact_match_is_really_sent_to_cli_and_audited(self):
        verdicts=[{'case_id':'a','correct':True,'reason':'Equivalent value.'},
                  {'case_id':'b','correct':False,'reason':'Multiple incompatible guesses.'}]
        with tempfile.TemporaryDirectory() as directory, mock.patch('newsdesk.memory_model.subprocess.Popen',
                side_effect=self.process(verdicts)) as spawn:
            out=CodexMemoryEvaluator(executable='fixture-codex').evaluate(self.items(),Path(directory))
            spawn.assert_called_once()
            self.assertEqual(out,verdicts)
            self.assertIn('2.7% or 3%',self.prompt)
            self.assertIn(JUDGE_INSTRUCTION,self.prompt)
            for flag in ('--ignore-user-config','--ignore-rules','--ephemeral','--sandbox'):
                self.assertIn(flag,self.command)
            self.assertIn('read-only',self.command)
            runtime=json.loads((Path(directory)/'runtime-config.json').read_text())
            for key in ('features.shell_tool','features.unified_exec','features.apps','features.plugins','features.multi_agent'):
                self.assertFalse(runtime['settings'][key])
            self.assertEqual(runtime['settings']['web_search'],'disabled')
            self.assertEqual(runtime['settings']['mcp_servers'],{})
            receipt=json.loads((Path(directory)/'gate-receipt.json').read_text())
            self.assertTrue(receipt['passed'])
            self.assertTrue(receipt['independent'])
            self.assertEqual(receipt['evaluated_cases'],2)
            for name in ('stdout.jsonl','stderr.txt','evaluator-request.json','schema.json','response.json','verdicts.json'):
                self.assertTrue((Path(directory)/name).is_file())

    def test_duplicate_ids_nonboolean_and_extra_verdicts_are_rejected(self):
        bads=[ [{'case_id':'a','correct':True,'reason':'x'}]*2,
               [{'case_id':'a','correct':1,'reason':'x'},{'case_id':'b','correct':False,'reason':'x'}],
               [{'case_id':'a','correct':True,'reason':'x'}] ]
        for verdicts in bads:
            with self.subTest(verdicts=verdicts),tempfile.TemporaryDirectory() as directory, \
                    mock.patch('newsdesk.memory_model.subprocess.Popen',side_effect=self.process(verdicts)):
                with self.assertRaises(ValueError):
                    CodexMemoryEvaluator(executable='fixture-codex').evaluate(self.items(),Path(directory))
                self.assertFalse(json.loads((Path(directory)/'gate-receipt.json').read_text())['passed'])

    def test_cli_tool_event_is_audit_failure_instead_of_a_wrong_answer(self):
        verdicts=[{'case_id':x['case_id'],'correct':True,'reason':'x'} for x in self.items()]
        with tempfile.TemporaryDirectory() as directory,mock.patch('newsdesk.memory_model.subprocess.Popen',
                side_effect=self.process(verdicts,tool=True)):
            with self.assertRaises(ClosedBookViolation):
                CodexMemoryEvaluator(executable='fixture-codex').evaluate(self.items(),Path(directory))
            self.assertFalse(json.loads((Path(directory)/'gate-receipt.json').read_text())['passed'])

    def test_disabled_code_host_diagnostic_is_recorded_without_enabling_tools(self):
        gate=ClosedBookGate(question())
        event={'type':'item.completed','item':{'type':'error','message':
            'Code Mode is unavailable because code-mode host is disabled. '
            'Code mode will fail closed; enable `features.code_mode_host` and install '
            '`codex-code-mode-host`.'}}
        receipt=gate.validate_cli_events([event,{'type':'turn.completed'}])
        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['tool_call_count'],0)
        self.assertEqual(receipt['cli_diagnostics'][0]['kind'],'disabled_capability_warning')
        event['item']['message']='A real provider or runtime failure'
        with self.assertRaises(RuntimeError):gate.validate_cli_events([event])
        self.assertFalse(gate.receipt()['passed'])

    def test_input_does_not_accept_source_context_or_duplicate_case_ids(self):
        for items in ([dict(self.items()[0],source='news')],[self.items()[0],self.items()[0]]):
            with tempfile.TemporaryDirectory() as directory,mock.patch('newsdesk.memory_model.subprocess.Popen') as spawn:
                with self.assertRaises(ValueError):
                    CodexMemoryEvaluator(executable='fixture-codex').evaluate(items,Path(directory))
                spawn.assert_not_called()


if __name__=='__main__':unittest.main()
