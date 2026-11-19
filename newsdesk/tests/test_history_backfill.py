"""No network, real archive, or model calls are used by these runner tests."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch
from types import SimpleNamespace
import threading

from newsdesk.history_backfill import Backfill, enabled_sources, publication_day, call_adapter, progress_signature, review_channel, main
from newsdesk.server import NewsStore


def item(identifier="1", day="2026-01-05"):
    return {"candidate_id": "a:" + identifier, "source_id": "a", "source_name": "Fixture",
            "external_id": identifier,
            "source_url": "https://example.test/" + identifier, "title": "Public oil news",
            "body": "Original public content for " + identifier,
            "published_date": day, "collected_at": "2026-10-30T01:00:00+00:00"}


class BackfillTests(unittest.TestCase):
    def test_cls_public_topics_continue_beyond_unavailable_flash_history(self):
        class Fetcher:
            def __init__(self,*args): pass
            def get(self,*args): raise AssertionError('offline fixture')
        primary=SimpleNamespace(HistoryFetcher=Fetcher,run_source=Mock(return_value={'status':'blocked','terminal':True}))
        topics=SimpleNamespace(HistoryFetcher=Fetcher,run_source=Mock(return_value={'status':'incomplete','terminal':False,'pages':10}))
        with patch('newsdesk.history_backfill.importlib.import_module',side_effect=[primary,topics]):
            result=call_adapter('cls',{'group':'media'},'2026-01-01','2026-10-30',self.root,threading.Event())
        self.assertFalse(result['terminal'])
        self.assertEqual(result['channels']['public_topics']['pages'],10)
        topics.run_source.assert_called_once()

    def test_original_broker_channels_are_collected_in_addition_to_sina(self):
        class Fetcher:
            def __init__(self,*args): pass
            def get(self,*args): raise AssertionError('offline fixture')
        sina=SimpleNamespace(Fetcher=Fetcher,run_source=Mock(return_value={'status':'complete'}))
        original=SimpleNamespace(Fetcher=Fetcher,run_original_channel=Mock(return_value={
            'status':'partial','coverage':{'complete':False,'terminal':True}}))
        with patch('newsdesk.history_backfill.importlib.import_module',side_effect=[sina,original]):
            result=call_adapter('institution_23',{'group':'brokers'},'2026-01-01','2026-10-30',self.root,threading.Event())
        self.assertEqual(result['status'],'incomplete')
        self.assertTrue(result['terminal'])
        self.assertFalse(original.run_original_channel.call_args.kwargs['metadata_only'])

    def test_priority_calendar_adapters_receive_all_target_months(self):
        class Fetcher:
            def __init__(self, *args): pass
            def get(self, *args): raise AssertionError('offline fixture')
        months = [f'2026-{month:02d}' for month in range(1, 10)]
        for source_id, group in [('ths', 'media'), ('institution_12', 'brokers')]:
            with self.subTest(source_id=source_id):
                module = SimpleNamespace(HistoryFetcher=Fetcher, Fetcher=Fetcher,
                    run_source=Mock(return_value={'status': 'incomplete', 'pages': 1}))
                with patch('newsdesk.history_backfill.importlib.import_module', return_value=module):
                    call_adapter(source_id, {'group': group, 'priority_months': months},
                        '2026-01-01', '2026-10-30', self.root, threading.Event())
                kwargs = module.run_source.call_args_list[0].kwargs
                self.assertEqual(kwargs['priority_months'], months)
                self.assertIsInstance(kwargs['fetcher'], Fetcher)
                if group == 'brokers':
                    self.assertGreaterEqual(kwargs['max_days'], 9)

    def test_priority_cls_uses_monthly_adapter_and_keeps_incomplete_history_running(self):
        from newsdesk import history_priority_topics
        from newsdesk.history_media_extra import HistoryFetcher
        self.assertIs(history_priority_topics.HistoryFetcher, HistoryFetcher)
        class MediaFetcher:
            def __init__(self, *args): pass
            def get(self, *args): raise AssertionError('offline media fixture')
        class MonthlyFetcher:
            def __init__(self, *args): pass
            def get(self, *args): raise AssertionError('offline monthly fixture')
        primary = SimpleNamespace(HistoryFetcher=MediaFetcher, run_source=Mock(return_value={
            'status': 'blocked', 'terminal': True, 'candidate_count': 0}))
        monthly = SimpleNamespace(HistoryFetcher=MonthlyFetcher,
            run_monthly_topics=Mock(return_value={'status': 'incomplete', 'terminal': False,
                'candidate_count': 0, 'pages': 18}))
        macro = SimpleNamespace(HistoryFetcher=MonthlyFetcher,
            run_source=Mock(return_value={'status':'incomplete','terminal':False,'candidate_count':0,'pages':18}))
        months = [f'2026-{month:02d}' for month in range(1, 10)]
        with patch('newsdesk.history_backfill.importlib.import_module', side_effect=[primary, monthly, macro]) as imported:
            result = call_adapter('cls', {'group': 'media', 'priority_months': months},
                '2026-01-01', '2026-10-30', self.root, threading.Event())
        self.assertEqual([call.args[0] for call in imported.call_args_list],
            ['newsdesk.history_media', 'newsdesk.history_priority_topics','newsdesk.history_cls_macro'])
        self.assertIsInstance(primary.run_source.call_args.kwargs['fetcher'], MediaFetcher)
        kwargs = monthly.run_monthly_topics.call_args.kwargs
        self.assertEqual(kwargs['priority_months'], months)
        self.assertIsInstance(kwargs['fetcher'], MonthlyFetcher)
        self.assertGreaterEqual(kwargs['max_pages'], 18)
        self.assertEqual(result['channels']['priority_monthly_topics']['pages'], 18)
        self.assertEqual(result['channels']['priority_macro_topics']['pages'],18)
        self.assertEqual(macro.run_source.call_args.kwargs['priority_months'],months)
        self.assertEqual(result['status'], 'incomplete')
        self.assertFalse(result['terminal'])

    def test_priority_ths_adds_commodity_history_without_losing_index_channel(self):
        class Fetcher:
            def __init__(self,*args): pass
            def get(self,*args): raise AssertionError('offline fixture')
        primary = SimpleNamespace(HistoryFetcher=Fetcher,run_source=Mock(return_value={
            'status':'blocked','terminal':True,'candidate_count':0}))
        commodity = SimpleNamespace(Fetcher=Fetcher,run_source=Mock(return_value={
            'status':'incomplete','terminal':False,'candidate_count':0,'pages':2}))
        months = ['2026-01','2026-09']
        with patch('newsdesk.history_backfill.importlib.import_module',side_effect=[primary,commodity]) as imported:
            result = call_adapter('ths',{'group':'media','priority_months':months},
                '2026-01-01','2026-10-30',self.root,threading.Event())
        self.assertEqual([call.args[0] for call in imported.call_args_list],
            ['newsdesk.history_media','newsdesk.history_commodity_ths'])
        self.assertEqual(commodity.run_source.call_args.kwargs['priority_months'],months)
        self.assertEqual(result['channels']['commodity_futures_archive']['pages'],2)
        self.assertIn('primary',result['channels'])
        self.assertFalse(result['terminal'])

    def test_broker_industry_supplement_keeps_the_same_sina_identity_and_precedes_general_reports(self):
        class Fetcher:
            def __init__(self,*args): pass
            def get(self,*args): raise AssertionError('offline fixture')
        primary_path,industry_path = self.root/'general.jsonl',self.root/'oil.jsonl'
        row = dict(item('report'),source_id='institution_12',candidate_id='institution_12:sina:report',
                   external_id='sina:report')
        primary_path.write_text(json.dumps(row)+'\n')
        industry_path.write_text(json.dumps(dict(row,discovery_industry_code='sw2_750100',
            discovery_research_channel='sina_public_oil_industries'))+'\n')
        primary = SimpleNamespace(Fetcher=Fetcher,run_source=Mock(return_value={
            'status':'incomplete','candidates_path':str(primary_path)}))
        industry = SimpleNamespace(Fetcher=Fetcher,run_source=Mock(return_value={
            'status':'incomplete','candidates_path':str(industry_path)}))
        with patch('newsdesk.history_backfill.importlib.import_module',side_effect=[primary,industry]):
            report = call_adapter('institution_12',{'group':'brokers','priority_months':['2026-01']},
                '2026-01-01','2026-10-30',self.root,threading.Event())
        result = json.loads(Path(report['candidates_path']).read_text().strip())
        self.assertEqual(result['candidate_id'],'institution_12:sina:report')
        self.assertEqual(result['discovery_industry_code'],'sw2_750100')
        self.assertEqual(review_channel(result),'oil_topics')
        self.assertEqual(review_channel(dict(result,discovery_industry_code='unverified')),'other_public_sources')
        self.assertEqual(report['candidate_count'],1)
        self.assertIn('primary',report['channels'])

    def test_failed_collector_advance_preserves_previously_captured_candidates(self):
        runner = self.runner(self.adapter([item()],'incomplete',pages=1))
        runner._collect('a')
        saved_path = runner.state['sources']['a']['report']['candidates_path']
        runner.adapter = Mock(side_effect=ValueError('fixture: failed later history page'))
        runner._collect('a')
        report = runner.state['sources']['a']['report']
        self.assertEqual(report['status'],'failed')
        self.assertEqual(report['candidates_path'],saved_path)
        self.assertEqual(report['candidate_count'],1)
        self.assertEqual(runner.state['sources']['a']['date_distribution']['candidate_counts_by_month']['2026-01'],1)
        rows,keys = runner._pending()
        self.assertEqual([row['candidate_id'] for row in rows],['a:1'])

    def test_nested_channel_work_is_comparable_after_checkpoint_reload(self):
        one={'candidate_count':0,'channels':{'original':{'channels':[{'channel':'macro','pages':1}]}}}
        two={'candidate_count':0,'channels':{'original':{'channels':[{'channel':'macro','pages':2}]}}}
        self.assertNotEqual(progress_signature(one),progress_signature(two))
        self.assertEqual(tuple(json.loads(json.dumps(progress_signature(one)))),progress_signature(one))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.specs = {"a": {"name": "Fixture", "group": "media"}}
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def adapter(self, rows, status="complete", **extra):
        def collect(identifier, spec, start, end, output, stop):
            self.calls.append(identifier)
            path = Path(output) / (identifier + ".jsonl")
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return {"status": status, "candidate_count": len(rows), "candidates_path": str(path), **extra}
        return collect

    def runner(self, adapter, processor=None, **kwargs):
        return Backfill("2026-01-01", "2026-01-31", self.root, self.root / "fixture.sqlite3", self.specs,
            adapter=adapter, processor=processor or self.processor, poll_seconds=.001, retry_delay=0, **kwargs)

    @staticmethod
    def processor(path, database, cache, stop):
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        return {"status": "complete", "reviewed_count": len(rows), "written": len(rows),
                "created": len(rows), "updated": 0, "reactivated": 0, "unchanged": 0, "exact_duplicates": 0}

    def existing(self, relevant=True):
        from newsdesk.tests.publication_fixture import verified_metadata
        store = NewsStore(self.root / "fixture.sqlite3", seed_demo=False)
        record = store.save({"title": "Existing complete report", "category": "supply", "published_at":"2026-10-28T01:00:00+00:00",
            "body": "Full public article text retained with later human research edits.",
            "summary": "Manual summary must survive history backfill.",
            "source_url": "https://example.test/original",
            "ingestion": {**verified_metadata(), "source_id": "a", "external_id": "1", "canonical_url": "https://example.test/original"},
            "agent_review": {"reviewer": "Fixture reviewer", "reviewed_at": "2026-10-28T00:00:00+00:00",
                "relevance": relevant, "relevance_reason": "Fixture public report review",
                "category_reasons": {"supply": "Fixture supply evidence"} if relevant else {},
                "evidence": ["Full public article text"] if relevant else [], "confidence": "高"}})
        return store, record

    def test_existing_full_article_is_not_replaced_by_shorter_history_same_identity(self):
        store, before = self.existing()
        historical = dict(item(), source_url="https://example.test/migrated", body="Short historical public lead.")
        processor = Mock(side_effect=AssertionError("existing record must not be reviewed or archived"))
        state = self.runner(self.adapter([historical]), processor).run()
        self.assertEqual(store.get(before["id"]), before)
        self.assertEqual(state["counts"]["created"], 0)
        self.assertEqual(state["counts"]["updated"], 0)
        self.assertEqual(state["existing_records"], 1)
        self.assertEqual(set(json.loads((self.root / "review-ledger.json").read_text()).values()), {"existing_record"})
        processor.assert_not_called()

    def test_existing_canonical_url_alias_is_protected_and_unseen_record_is_reviewed(self):
        store, before = self.existing()
        alias = dict(item("alias"), source_id="another", source_url="https://example.test/original?utm_source=history",
                     body="Short lead from another archive listing.")
        processor = Mock(side_effect=self.processor)
        state = self.runner(self.adapter([alias, item("unseen")]), processor).run()
        self.assertEqual(store.get(before["id"]), before)
        self.assertEqual(state["existing_records"], 1)
        self.assertEqual(state["counts"]["created"], 1)
        self.assertEqual(processor.call_count, 1)
        queued = [json.loads(line) for line in processor.call_args[0][0].read_text().splitlines()]
        self.assertEqual([row["candidate_id"] for row in queued], ["a:unseen"])

    def test_previously_excluded_article_can_receive_new_semantic_review(self):
        self.existing(relevant=False)
        processor = Mock(side_effect=self.processor)
        state = self.runner(self.adapter([item()]), processor).run()
        self.assertEqual(processor.call_count, 1)
        self.assertEqual(state["existing_records"], 0)
        self.assertNotIn("existing_record", json.loads((self.root / "review-ledger.json").read_text()).values())

    def test_newly_attempted_failed_dates_are_coverage_progress(self):
        calls = []
        def collect(identifier, spec, start, end, output, stop):
            calls.append(identifier)
            return {"status": "complete" if len(calls) == 5 else "incomplete", "candidate_count": 0,
                    "completed_days": 0, "attempted_days": len(calls)}
        state = self.runner(collect).run()
        self.assertEqual(len(calls), 5)
        self.assertEqual(state["status"], "complete")

    def test_zero_candidates_is_complete_without_model_invocation(self):
        processor = Mock(side_effect=AssertionError("must not call model for empty history"))
        state = self.runner(self.adapter([]), processor).run()
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["counts"]["created"], 0)
        processor.assert_not_called()

    def test_work_cap_resumes_until_complete_and_reviews_each_hash_once(self):
        calls = []
        def collect(identifier, spec, start, end, output, stop):
            calls.append(identifier)
            rows = [item(str(number)) for number in range(len(calls))]
            path = Path(output) / "candidates.jsonl"
            path.write_text("".join(json.dumps(row) + "\n" for row in rows))
            return {"status": "complete" if len(calls) == 3 else "incomplete",
                    "candidate_count": len(rows), "pages": len(calls), "candidates_path": str(path)}
        runner = self.runner(collect)
        state = runner.run()
        self.assertEqual(len(calls), 3)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["counts"]["created"], 3)
        self.assertEqual(state["counts"]["reviewed_count"], 3)
        resumed = self.runner(collect, Mock(side_effect=AssertionError("already reviewed"))).run()
        self.assertEqual(resumed["counts"]["created"], 3)
        self.assertEqual(len(calls), 3)

    def test_no_progress_is_explicit_incomplete_not_false_year_complete(self):
        state = self.runner(self.adapter([], "incomplete")).run()
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["sources"]["a"]["status"], "incomplete")
        self.assertTrue(state["sources"]["a"]["terminal"])

    def test_blocked_source_remains_blocked_while_other_source_can_complete(self):
        self.specs["b"] = {"name": "Second", "group": "media"}
        def collect(identifier, spec, start, end, output, stop):
            return {"status": "blocked" if identifier == "a" else "complete", "candidate_count": 0}
        state = self.runner(collect).run()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["sources"]["a"]["status"], "blocked")
        self.assertEqual(state["sources"]["b"]["status"], "complete")

    def test_unknown_invalid_and_outside_window_are_audited_not_archived(self):
        missing = item("unknown"); missing.pop("published_date")
        invalid = item("invalid"); invalid["body"] = ""
        processor = Mock(side_effect=AssertionError("none are archiveable year candidates"))
        state = self.runner(self.adapter([missing, invalid, item("old", "2025-12-31")]), processor).run()
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["skipped_dates"], 2)
        self.assertEqual(state["invalid_candidates"], 1)
        self.assertTrue((self.root / "excluded-dates.jsonl").exists())
        self.assertTrue((self.root / "invalid-candidates.jsonl").exists())
        processor.assert_not_called()

    def test_pending_scan_quarantines_placeholder_bodies_with_unchanged_raw_provenance(self):
        raw_path = self.root/'original-listing.html'
        raw_path.write_text('<li>... …！？</li>')
        invalid = [dict(item(str(index)), body=body, raw_path=str(raw_path))
                   for index, body in enumerate(['...', '…', '！？—'])]
        valid = dict(item('valid'), body='原油\u2028上涨')
        processor = Mock(side_effect=AssertionError('pending scan must not call the model'))
        runner = self.runner(self.adapter(invalid + [valid]), processor)
        runner._collect('a')
        rows, _ = runner._pending()
        self.assertEqual([row['candidate_id'] for row in rows], ['a:valid'])
        self.assertEqual(rows[0]['body'], valid['body'])
        audit_path = self.root/'invalid-candidates.jsonl'
        audit = [json.loads(line) for line in audit_path.read_text().split('\n') if line]
        self.assertEqual([json.loads(entry['raw_line']) for entry in audit], invalid)
        self.assertTrue(all('body' in entry['error'] for entry in audit))
        self.assertEqual(raw_path.read_text(), '<li>... …！？</li>')
        runner._pending()
        self.assertEqual(audit_path.read_text().count('\n'), 3)
        processor.assert_not_called()

    def test_review_failure_is_preserved_and_can_be_explicitly_retried(self):
        processor = Mock(side_effect=RuntimeError("fixture agent unavailable"))
        collect = self.adapter([item()])
        state = self.runner(collect, processor).run()
        self.assertEqual(processor.call_count, 3)
        self.assertEqual(state["status"], "partial")
        self.assertEqual(state["review_failed_candidates"], 1)
        recovered = self.runner(collect, retry_reviews=True).run()
        self.assertEqual(recovered["status"], "complete")
        self.assertEqual(recovered["counts"]["created"], 1)

    def test_temporary_cli_unavailability_keeps_queue_until_recovery(self):
        calls = []
        def temporarily_unavailable(path, database, cache, stop):
            calls.append(path)
            if len(calls) <= 2:
                raise RuntimeError('codex agent 调用失败（退出码 1），请检查 CLI 登录及额度')
            return self.processor(path, database, cache, stop)
        result = self.runner(self.adapter([item()]), temporarily_unavailable,
                             service_backoff_base=.001, service_backoff_max=.004).run()
        self.assertEqual(len(calls),3)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(result['counts']['created'],1)
        self.assertEqual(result['review_failed_candidates'],0)
        self.assertEqual(result['review_service_retries'],2)
        self.assertEqual(result['review_service_status'],'ready')

    def test_one_invalid_review_does_not_discard_other_history_candidates(self):
        rows=[item(str(index)) for index in range(1,9)]
        def one_bad(path,database,cache,stop):
            values=[json.loads(line) for line in path.read_text().splitlines()]
            if any(row['external_id']=='7' for row in values):
                raise RuntimeError('fixture: evidence validation failed for one candidate')
            return self.processor(path,database,cache,stop)
        result=self.runner(self.adapter(rows),one_bad).run()
        self.assertEqual(result['status'],'partial')
        self.assertEqual(result['counts']['created'],7)
        self.assertEqual(result['review_failed_candidates'],1)
        ledger=json.loads((self.root/'review-ledger.json').read_text())
        self.assertEqual(sum(value=='reviewed' for value in ledger.values()),7)
        self.assertEqual(result['review_chunk_limit'],60)

    def test_calendar_frontier_advances_even_when_early_dates_have_no_candidates(self):
        passes = []
        def calendar(identifier, spec, start, end, output, stop):
            day = len(passes)+1
            passes.append(day)
            return {'status':'complete' if day==5 else 'incomplete',
                    'candidate_count':0,'frontier_date':f'2026-01-{day+1:02d}',
                    'processed_dates':[f'2026-01-{n:02d}' for n in passes]}
        result = self.runner(calendar).run()
        self.assertEqual(passes,[1,2,3,4,5])
        self.assertEqual(result['sources']['a']['status'],'complete')

    def test_constant_retry_claim_has_only_bounded_extra_grace(self):
        result = self.runner(self.adapter([], 'incomplete', retry_pending=True,
                                         retry_attempts_remaining=2)).run()
        self.assertEqual(len(self.calls),4)
        self.assertTrue(result['sources']['a']['terminal'])
        self.assertEqual(result['status'],'partial')

    def test_review_thread_exception_cannot_turn_into_false_complete(self):
        runner = self.runner(self.adapter([]))
        runner._pending = Mock(side_effect=OSError("fixture disk read failed"))
        state = runner.run()
        self.assertEqual(state["status"], "failed")
        self.assertIn("fixture disk read failed", state["last_error"])

    def test_completed_chunk_recovers_counts_after_manifest_commit_was_lost(self):
        collect = self.adapter([item("1"), item("2")])
        first = self.runner(collect).run()
        self.assertEqual(first["counts"]["created"], 2)
        saved = json.loads((self.root / "manifest.json").read_text())
        saved["completed_chunks"] = []
        saved["counts"] = {key: 0 for key in saved["counts"]}
        (self.root / "manifest.json").write_text(json.dumps(saved))
        (self.root / "review-ledger.json").write_text("{}")
        resumed = self.runner(collect, Mock(side_effect=AssertionError("completed chunk must be recovered"))).run()
        self.assertEqual(resumed["status"], "complete")
        self.assertEqual(resumed["counts"]["created"], 2)

    def test_record_change_is_existing_update_not_a_new_count(self):
        def existing(path, database, cache, stop):
            return {"status": "complete", "reviewed_count": 1, "written": 1,
                    "created": 0, "updated": 1, "reactivated": 0}
        state = self.runner(self.adapter([item()]), existing).run()
        self.assertEqual(state["counts"]["created"], 0)
        self.assertEqual(state["counts"]["updated"], 1)

    def test_requested_window_and_enabled_sources_are_not_silently_changed(self):
        self.runner(self.adapter([])).run()
        with self.assertRaises(ValueError):
            Backfill("2026-02-01", "2026-02-28", self.root, self.root / "fixture.sqlite3", self.specs)
        grouped = enabled_sources({"sources": [
            {"id": "a", "name": "Media", "enabled": True, "adapter": "news_sources"},
            {"id": "b", "name": "Unknown", "enabled": True, "adapter": None},
            {"id": "c", "name": "Disabled", "enabled": False, "adapter": "news_sources"}]})
        self.assertEqual(set(grouped), {"a", "b"})
        self.assertEqual(grouped["a"]["group"], "media")
        self.assertIsNone(grouped["b"]["group"])

    def test_precise_dates_use_beijing_year_boundary_and_date_only_is_preserved(self):
        self.assertEqual(publication_day({"published_at": "2025-12-31T18:00:00+00:00"}), "2026-01-01")
        self.assertEqual(publication_day({"published_date": "2026-01-05"}), "2026-01-05")

    def priority_runner(self, rows_by_source, *, months=("2026-01", "2026-02"), target=1027,
                        adapter=None):
        specs = {identifier: {"name": "Fixture " + identifier, "group": "media"}
                 for identifier in rows_by_source}
        runner = Backfill("2026-01-01", "2026-10-30", self.root,
            self.root / "fixture.sqlite3", specs, adapter=adapter or self.adapter([]),
            processor=self.processor, poll_seconds=.001, retry_delay=0,
            priority_months=months, monthly_target=target, review_workers=3)
        for identifier, rows in rows_by_source.items():
            path = self.root / ("priority-" + identifier + ".jsonl")
            path.write_text("".join(json.dumps(dict(row, source_id=identifier,
                candidate_id=identifier + ":" + row["external_id"])) + "\n" for row in rows))
            runner.state["sources"][identifier]["report"] = {"candidates_path": str(path)}
        return runner

    def test_oil_topic_and_commodity_candidates_get_weighted_review_without_excluding_other_sources(self):
        topics = [dict(item('topic-'+str(n)),discovery_topic_id='1583') for n in range(10)]
        commodity = [dict(item('commodity-'+str(n)),source_listing_url=
            'https://goodsfu.10jqka.com.cn/qhkx_list/20260105/') for n in range(10)]
        general = [item('general-'+str(n)) for n in range(10)]
        runner = self.priority_runner({'cls':topics,'ths':commodity,'broker':general},months=['2026-01'])
        rows,keys = runner._pending(limit=6)
        self.assertEqual([row['source_id'] for row in rows],['cls','cls','cls','ths','ths','broker'])
        self.assertEqual(len(set(keys)),6)
        self.assertEqual(runner.state['review_queue_counts_by_month']['2026-01'],30)
        self.assertEqual(runner.state['review_queue_channel_counts'],{
            'oil_topics':10,'commodity_archive':10,'other_public_sources':10})

    def test_reference_month_raises_the_target_and_survives_resume(self):
        runner = Backfill('2026-01-01','2026-10-30',self.root,self.root/'fixture.sqlite3',self.specs,
            priority_months=['2026-01'],monthly_target=1100,reference_month='2026-10')
        records = [{'id':str(index),'published_at':'2026-10-05T09:00:00+08:00'} for index in range(1106)]
        runner._update_archive_distribution(records)
        self.assertEqual(runner.monthly_target,1200)
        self.assertEqual(runner.state['reference_month_count'],1106)
        self.assertEqual(runner.state['coverage_overview']['months']['2026-01']['shortfall'],1200)
        resumed = Backfill('2026-01-01','2026-10-30',self.root,self.root/'fixture.sqlite3',self.specs)
        self.assertEqual(resumed.monthly_target,1200)
        self.assertEqual(resumed.reference_month,'2026-10')
        resumed._update_archive_distribution([])
        self.assertEqual(resumed.monthly_target,1200)

    def test_reference_month_must_be_inside_the_requested_window(self):
        with self.assertRaises(ValueError):
            Backfill('2026-01-01','2026-01-31',self.root,self.root/'fixture.sqlite3',self.specs,
                priority_months=['2026-01'],monthly_target=1100,reference_month='2026-10')

    def test_one_off_review_concurrency_can_increase_to_twenty_four(self):
        runner = self.runner(self.adapter([]),review_workers=24)
        self.assertEqual(runner.state['review_workers'],24)
        self.assertEqual(runner.state['review_chunk_size'],720)
        self.assertEqual(runner.state['review_chunk_limit'],720)
        with self.assertRaises(ValueError):
            self.runner(self.adapter([]),review_workers=25)

    def test_cli_accepts_twenty_four_review_workers_and_rejects_twenty_five(self):
        argv = ['history_backfill', '--output', str(self.root), '--review-workers', '24']
        with patch('sys.argv', argv), patch('newsdesk.history_backfill.Backfill') as factory, patch('newsdesk.history_backfill.signal.signal'), contextlib.redirect_stdout(io.StringIO()):
            factory.return_value.run.return_value = {'status': 'complete', 'counts': {}}
            factory.return_value.path = self.root / 'manifest.json'
            main()
        self.assertEqual(factory.call_args.kwargs['review_workers'], 24)
        with patch('sys.argv', argv[:-1] + ['25']), patch('newsdesk.history_backfill.Backfill') as factory, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        factory.assert_not_called()

    def archive_for_month(self, month, count):
        store = NewsStore(self.root / "fixture.sqlite3", seed_demo=False)
        for index in range(count):
            store.save({"title": "Existing archived oil report " + str(index),
                "category": "supply", "body": "Original public oil production news.",
                "published_at": month + "-05T09:00:00+08:00",
                "source_url": "https://archive.test/" + month + "/" + str(index)})

    def test_priority_reviews_scan_later_sources_before_october_prefix(self):
        runner = self.priority_runner({
            "a": [item("oct-" + str(i), "2026-10-05") for i in range(8)],
            "b": [item("jan", "2026-01-05"), item("feb", "2026-02-05")]})
        rows, keys = runner._pending(limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["source_id"] for row in rows}, {"b"})
        self.assertEqual({row["published_date"][:7] for row in rows}, {"2026-01", "2026-02"})
        self.assertEqual(len(set(keys)), 2)

    def test_priority_reviews_fill_month_with_fewer_archived_records_first(self):
        self.archive_for_month("2026-01", 4)
        runner = self.priority_runner({"a": [
            item("jan-" + str(i), "2026-01-05") for i in range(3)] + [
            item("feb-" + str(i), "2026-02-05") for i in range(3)]})
        rows, _ = runner._pending(limit=2)
        self.assertEqual([row["published_date"][:7] for row in rows], ["2026-02", "2026-02"])
        self.assertEqual(runner.state["archive_distribution"]["by_month"]["2026-01"], 4)

    def test_priority_reviews_share_same_month_between_sources(self):
        runner = self.priority_runner({
            "a": [item("many-" + str(i), "2026-01-05") for i in range(20)],
            "b": [item("second", "2026-01-05")],
            "c": [item("third", "2026-01-05")]}, months=("2026-01",))
        rows, _ = runner._pending(limit=3)
        self.assertEqual({row["source_id"] for row in rows}, {"a", "b", "c"})

    def test_october_queue_waits_for_priority_collection_then_is_released(self):
        runner = self.priority_runner({"a": [item("oct", "2026-10-05")]})
        self.assertEqual(runner._pending(limit=2), ([], []))
        runner.collected.set()
        rows, _ = runner._pending(limit=2)
        self.assertEqual([row["candidate_id"] for row in rows], ["a:oct"])

    def test_priority_scope_and_target_survive_resume_without_cli_overrides(self):
        runner = self.priority_runner({"a": []}, months=("2026-02", "2026-09"), target=1027)
        runner._save()
        resumed = Backfill("2026-01-01", "2026-10-30", self.root,
            self.root / "fixture.sqlite3", {"a": {"name": "Fixture a", "group": "media"}},
            adapter=self.adapter([]), processor=self.processor)
        self.assertEqual(resumed.state["priority_months"], ["2026-02", "2026-09"])
        self.assertEqual(resumed.state["monthly_target"], 1027)
        self.assertEqual(resumed.state["review_workers"], 3)

    def test_priority_queue_resume_skips_reviewed_slice_of_large_source(self):
        runner = self.priority_runner({"a": [
            item("jan-" + str(i), "2026-01-05") for i in range(5)]}, months=("2026-01",))
        first_rows, first_keys = runner._pending(limit=2)
        runner.ledger.update({key: "reviewed" for key in first_keys})
        runner.ledger_path.write_text(json.dumps(runner.ledger))
        runner._save()
        resumed = Backfill("2026-01-01", "2026-10-30", self.root,
            self.root / "fixture.sqlite3", {"a": {"name": "Fixture a", "group": "media"}},
            adapter=self.adapter([]), processor=self.processor)
        next_rows, next_keys = resumed._pending(limit=2)
        self.assertEqual(len(next_rows), 2)
        self.assertFalse(set(first_keys) & set(next_keys))
        self.assertEqual([row["external_id"] for row in first_rows], ["jan-0", "jan-1"])
        self.assertEqual([row["external_id"] for row in next_rows], ["jan-2", "jan-3"])

    def test_configured_review_workers_reach_actual_ingest_subprocess(self):
        runner = Backfill("2026-01-01", "2026-10-30", self.root,
            self.root / "fixture.sqlite3", self.specs,
            review_workers=24, priority_months=("2026-01",), monthly_target=1027)
        candidate_file = self.root / "candidates.jsonl"
        candidate_file.write_text(json.dumps(item()) + "\n")
        (self.root / "pipeline.json").write_text(json.dumps({"status": "complete", "created": 1}))
        child = Mock(returncode=0)
        child.poll.return_value = 0
        with patch("newsdesk.history_backfill.subprocess.Popen", return_value=child) as popen:
            result = runner.processor(candidate_file, runner.database, self.root / "cache", runner.stop)
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--workers") + 1], "24")
        self.assertEqual(command[command.index("--batch-size") + 1], "30")
        self.assertEqual(result["created"], 1)
        self.assertEqual(runner.state["review_chunk_size"], 720)

    def test_public_history_exhaustion_with_monthly_shortfall_is_explicit_partial(self):
        runner = self.priority_runner({"a": []}, months=("2026-01",), target=1027)
        state = runner.run()
        self.assertEqual(state["status"], "partial")
        self.assertIn("2026-01", state["target_unmet_months"])
        month = state["coverage_overview"]["months"]["2026-01"]
        self.assertEqual(month["target"], 1027)
        self.assertEqual(month["shortfall"], 1027)
        self.assertTrue(state["sources"]["a"]["terminal"])

    def test_meeting_monthly_target_does_not_hide_incomplete_source_coverage(self):
        self.archive_for_month("2026-01", 2)
        runner = self.priority_runner({"a": []}, months=("2026-01",), target=2,
            adapter=self.adapter([], "blocked"))
        state = runner.run()
        self.assertEqual(state["status"], "partial")
        self.assertFalse(state["target_unmet_months"])
        self.assertEqual(state["coverage_overview"]["months"]["2026-01"]["shortfall"], 0)
        self.assertEqual(state["sources"]["a"]["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
