"""Collector scheduling tests use events, never network calls or model work."""

import tempfile
import threading
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

from newsdesk.history_backfill import Backfill


class FastLaneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self, sources, adapter, priority=True):
        return Backfill('2026-01-01', '2026-01-31', self.root,
            self.root/'news.sqlite3', sources, adapter=adapter,
            processor=Mock(side_effect=AssertionError('there are no candidates')),
            poll_seconds=.001, priority_months=['2026-01'] if priority else [])

    def test_fast_media_advances_before_broker_barrier_with_three_calls_maximum(self):
        sources = {'cls': {'name': 'CLS', 'group': 'media'},
                   'broker-a': {'name': 'A', 'group': 'brokers'},
                   'broker-b': {'name': 'B', 'group': 'brokers'}}
        lock = threading.Lock()
        calls, active = Counter(), Counter()
        broker_started = {key: threading.Event() for key in ('broker-a','broker-b')}
        fast_second, release_fast, release_brokers, fast_lane_done = [threading.Event() for _ in range(4)]
        observed = {'maximum': 0, 'same_source_overlap': False}

        def adapter(identifier, spec, start, end, output, stop):
            with lock:
                calls[identifier] += 1
                active[identifier] += 1
                observed['same_source_overlap'] |= active[identifier] > 1
                observed['maximum'] = max(observed['maximum'],sum(active.values()))
                attempt = calls[identifier]
            try:
                if identifier == 'cls':
                    for event in broker_started.values():
                        if not event.wait(3):
                            raise AssertionError('ordinary lane did not start both broker workers')
                    if attempt == 2:
                        fast_second.set()
                        if not release_fast.wait(3):
                            raise AssertionError('test did not release fast lane')
                    return {'status':'complete' if attempt == 2 else 'incomplete',
                            'candidate_count':0,'pages':attempt}
                broker_started[identifier].set()
                if not release_brokers.wait(3):
                    raise AssertionError('test did not release broker lane')
                return {'status':'complete','candidate_count':0}
            finally:
                with lock:
                    active[identifier] -= 1

        runner = self.runner(sources,adapter)
        original = runner._collect_sources
        def observe_lane(identifiers, workers):
            original(identifiers,workers)
            if identifiers == ['cls']:
                fast_lane_done.set()
        runner._collect_sources = observe_lane
        results, failures = [], []
        def run():
            try:
                results.append(runner.run())
            except BaseException as error:
                failures.append(error)
        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(fast_second.wait(3),'CLS must run twice before brokers finish their first call')
            self.assertFalse(runner.collected.is_set())
            release_fast.set()
            self.assertTrue(fast_lane_done.wait(3))
            self.assertFalse(runner.collected.is_set(),'one completed lane cannot release October review')
        finally:
            release_fast.set(); release_brokers.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures,[])
        self.assertEqual(results[0]['status'],'complete')
        self.assertTrue(runner.collected.is_set())
        self.assertEqual(calls,{'cls':2,'broker-a':1,'broker-b':1})
        self.assertEqual(observed['maximum'],3)
        self.assertFalse(observed['same_source_overlap'])

    def test_no_priority_keeps_one_three_worker_round_robin(self):
        sources = {'cls':{'name':'CLS','group':'media'},'b':{'name':'B','group':'brokers'}}
        runner = self.runner(sources,Mock(),priority=False)
        runner._collect_sources = Mock()
        runner._collect_all()
        runner._collect_sources.assert_called_once_with(['cls','b'],3)

    def test_no_fast_media_keeps_one_three_worker_round_robin(self):
        sources = {'a':{'name':'A','group':'brokers'}}
        runner = self.runner(sources,Mock())
        runner._collect_sources = Mock()
        runner._collect_all()
        runner._collect_sources.assert_called_once_with(['a'],3)

    def test_already_terminal_fast_lane_ends_and_ordinary_lane_continues(self):
        sources = {'ths':{'name':'THS','group':'media'},'b':{'name':'B','group':'brokers'}}
        adapter = Mock(return_value={'status':'complete','candidate_count':0})
        runner = self.runner(sources,adapter)
        runner.state['sources']['ths'].update(status='complete',terminal=True)
        result = runner.run()
        self.assertEqual(result['status'],'complete')
        self.assertEqual([call.args[0] for call in adapter.call_args_list],['b'])
        self.assertTrue(runner.collected.is_set())

    def test_fatal_fast_lane_error_stops_other_lane_and_propagates(self):
        sources = {'cls':{'name':'CLS','group':'media'},'b':{'name':'B','group':'brokers'}}
        runner = self.runner(sources,Mock())
        ordinary_started = threading.Event()
        def collect(identifiers,workers):
            if identifiers == ['cls']:
                if not ordinary_started.wait(3):
                    raise AssertionError('ordinary lane not started')
                raise OSError('fixture checkpoint disk failure')
            ordinary_started.set()
            if not runner.stop.wait(3):
                raise AssertionError('fatal fast lane must stop ordinary work')
        runner._collect_sources = collect
        with self.assertRaisesRegex(OSError,'checkpoint disk failure'):
            runner._collect_all()
        self.assertTrue(runner.stop.is_set())


if __name__ == '__main__':
    unittest.main()
