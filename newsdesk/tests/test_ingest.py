import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from newsdesk.ingest import CodexReviewer, REVIEW_MODEL, archive_candidates, canonical_url, collect, main, prepare_candidate, process, read_candidates, review_candidates, save_invalid_candidates, validate_decision
from newsdesk.server import NewsStore


def candidate():
    return prepare_candidate({
        "candidate_id": "fixture:001", "source_id": "fixture", "source_name": "测试公开来源",
        "external_id": "001", "source_url": "https://example.org/news/001?utm_source=test#top",
        "title": "油轮交付延迟", "body": "测试文本：港口关闭导致原油油轮交付延迟，部分炼厂调整采购。",
        "published_at": "2026-10-28T09:00:00+08:00", "collected_at": "2026-10-28T02:00:00+00:00",
        "raw_path": str(Path(__file__).parent / 'fixtures/publication-time.html'), "content_scope": "test fixture"})


def decision(item):
    return {"candidate_id": item["candidate_id"], "relevant": True,
        "primary_category": "logistics", "categories": ["logistics", "refining"],
        "reason": "原油运输及炼厂采购受到影响", "confidence": "高",
        "category_reasons": [{"category": "logistics", "reason": "港口关闭延迟交付"},
                             {"category": "refining", "reason": "炼厂调整采购"}],
        "evidence": ["港口关闭导致原油油轮交付延迟"], "summary": "港口关闭影响原油交付。",
        "attributes": {"origin_name":"", "origin_evidence":"", "origin_status":"unresolved", "origin_reason":"原文没有披露发布机构", "region": "", "rationale": "未给出规模及预期，方向不确定。", "actual_value": "",
            "expected_value": "", "previous_value": "", "unit": "", "scale": "", "duration": "",
            "occurred_at": "", "effective_at": "", "source_type": "事实公告", "stage": "实施",
            "horizon": "1–5个交易日", "direction": "不确定", "assets": ["Brent"],
            "channels": ["运输交付", "炼厂加工"], "entities": []}}


class FakeReviewer:
    """Deterministic test double; production never calls this class."""
    def __init__(self):
        self.calls = 0

    def review(self, candidates):
        self.calls += 1
        return [decision(item) for item in candidates]


class IngestionTests(unittest.TestCase):
    def test_default_and_empty_collection_never_reenable_removed_feeds(self):
        for enabled, requested, expected in [(['aastocks'], None, ['aastocks']), ([], None, []), (['aastocks'], [], [])]:
            with self.subTest(enabled=enabled,requested=requested), tempfile.TemporaryDirectory() as directory, contextlib.ExitStack() as stack:
                root = Path(directory)
                (root/'source_registry.json').write_text(json.dumps({'sources':[
                    {'id':sid,'enabled':sid in enabled} for sid in ['aastocks','caixin','goldman_sachs','cls','ths','jin10']]}))
                stack.enter_context(patch('newsdesk.ingest.ROOT',root))
                stack.enter_context(patch('newsdesk.ingest.institution_adapters',return_value=[]))
                blocked = [stack.enter_context(patch(target)) for target in [
                    'newsdesk.news_sources.crawl_sources','newsdesk.news_sources.crawl_oil_supplement',
                    'newsdesk.news_sources.crawl_ths_futures','newsdesk.research_sources.crawl_research_sources']]
                def additional(output,source_ids,limit_per_source):
                    (Path(output)/'extra-candidates.jsonl').write_text('')
                    return {'sources':[]}
                extra = stack.enter_context(patch('newsdesk.additional_sources.crawl_additional_sources',side_effect=additional))
                report = collect(root/'run',source_ids=requested)
                for crawler in blocked: crawler.assert_not_called()
                if expected: self.assertEqual(extra.call_args.kwargs['source_ids'],expected)
                else: extra.assert_not_called()
                self.assertEqual(report['candidate_count'],0)

    def test_body_requires_unicode_letters_or_numbers_not_placeholder_punctuation(self):
        for body in ['...', '…', '……', '！？—﹏', '\u2028…\u2029\u0085', ' \t\n ']:
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, 'body'):
                prepare_candidate(dict(candidate(), body=body))
        for body in ['油', 'a', '1', '١', '原油\u2028上涨', '…油价…', '1.2%']:
            with self.subTest(body=body):
                self.assertEqual(prepare_candidate(dict(candidate(), body=body))['body'], body)

    def test_cli_accepts_twenty_four_review_workers_and_rejects_twenty_five(self):
        argv = ['ingest', 'review', '--candidates', '/tmp/fixture-candidates.jsonl', '--workers', '24']
        with patch('sys.argv', argv), patch('newsdesk.ingest.process', return_value={}) as review, patch('newsdesk.ingest.progress'):
            main()
        self.assertEqual(review.call_args.args[-2:], (30, 24))
        with patch('sys.argv', argv[:-1] + ['25']), patch('newsdesk.ingest.process') as review, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        review.assert_not_called()

    def test_backfilled_date_without_clock_cannot_reenter_archive_from_review_cache(self):
        undated = prepare_candidate(dict(candidate(), published_at='', raw_path='', publication_raw_path=''))
        dated = prepare_candidate(dict(undated, published_date='2026-01-05'))
        self.assertNotEqual(undated['content_hash'], dated['content_hash'])
        with tempfile.TemporaryDirectory() as directory:
            reviewer = FakeReviewer()
            first_review = review_candidates([undated], Path(directory)/'cache', reviewer=reviewer)
            dated_review = review_candidates([dated], Path(directory)/'cache', reviewer=reviewer)
            self.assertEqual(reviewer.calls,2)
            database = Path(directory)/'news.sqlite3'
            archive_candidates([undated], first_review, database, 'undated')
            report = archive_candidates([dated], dated_review, database, 'dated')
            self.assertEqual(report['created'],0)
            self.assertEqual(report['updated'],0)
            self.assertEqual(report['archive_time_skipped_count'],1)
            rows = NewsStore(database,seed_demo=False).all()
            self.assertEqual(len(rows),0)

    def test_invalid_rows_are_quarantined_without_weakening_strict_reader(self):
        row = candidate()
        bad = dict(row, candidate_id='fixture:empty', body='')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'candidates.jsonl'
            path.write_text('\n'.join([json.dumps(bad), '{broken json', '[]', json.dumps(row), json.dumps(row)]))
            with self.assertRaisesRegex(ValueError, 'body'):
                read_candidates(path)
            invalid = []
            valid = read_candidates(path, invalid)
            self.assertEqual([item['candidate_id'] for item in valid], [row['candidate_id']])
            self.assertEqual(len(invalid), 4)
            self.assertEqual(invalid[0]['candidate']['body'], '')
            self.assertEqual(invalid[0]['source_id'], 'fixture')
            self.assertEqual(invalid[0]['line'], 1)
            self.assertIn('body', invalid[0]['error'])
            self.assertEqual(invalid[1]['raw_line'], '{broken json')
            report = save_invalid_candidates(directory, invalid)
            self.assertEqual(report['invalid_count'], 4)
            again = save_invalid_candidates(directory, invalid)
            self.assertEqual(again, report)
            saved = [json.loads(line) for line in Path(report['invalid_candidates_path']).read_text().splitlines()]
            self.assertEqual(len(saved), 4)

    def test_bad_main_and_supplement_rows_do_not_block_later_sources_or_review(self):
        def row(identifier, source):
            return dict(candidate(), candidate_id=source + ':' + identifier, external_id=identifier,
                        source_id=source, source_url='https://example.org/news/' + identifier)
        def write_rows(output, filename, rows):
            (Path(output) / filename).write_text(''.join(json.dumps(item) + '\n' for item in rows))
        def main_feed(output, **kwargs):
            write_rows(output, 'candidates.jsonl', [dict(row('empty-main','cls'),body=''), row('main','cls')])
            return {'sources':[{'source_id':'cls','status':'ok','count':2}]}
        def supplement(output):
            write_rows(output, 'supplement-candidates.jsonl', [dict(row('empty-supplement','cls'),body=''),row('supplement','cls')])
            return {'sources':[{'source_id':'cls','status':'ok','count':2}]}
        calls = []
        def broker(output, **kwargs):
            calls.append('broker')
            write_rows(output, 'broker-fixture.jsonl', [row('broker','broker-fixture')])
            return {'sources':[{'source_id':'broker-fixture','status':'ok','count':1}]}
        adapters = [{'name':'broker-fixture','sources':{'broker-fixture':{}},'crawl':broker,'candidate_file':'broker-fixture.jsonl'}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('newsdesk.ingest.institution_adapters',return_value=adapters), patch('newsdesk.news_sources.crawl_sources',side_effect=main_feed), patch('newsdesk.news_sources.crawl_oil_supplement',side_effect=supplement):
                collection = collect(root, source_ids=['cls','broker-fixture'])
            self.assertEqual(calls, ['broker'])
            self.assertEqual(collection['candidate_count'], 3)
            self.assertEqual(collection['invalid_count'], 2)
            self.assertEqual(collection['invalid_by_source'], {'cls':2})
            with patch('newsdesk.ingest.CodexReviewer',return_value=FakeReviewer()):
                report = process(root / 'candidates.jsonl',output=root / 'output',database=root / 'news.sqlite3')
            self.assertEqual(report['status'],'complete')
            self.assertEqual(report['invalid_count'],2)
            self.assertEqual(report['candidate_count'],3)
            self.assertEqual(report['created'],3)
            self.assertEqual(report['updated'],0)
            self.assertEqual(len(Path(report['invalid_candidates_path']).read_text().splitlines()),2)

    def test_empty_update_completes_with_zero_new_records_without_constructing_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'candidates.jsonl'
            path.write_text('')
            with patch('newsdesk.ingest.CodexReviewer') as reviewer:
                report = process(path,output=root / 'output',database=root / 'news.sqlite3')
            reviewer.assert_not_called()
            self.assertEqual(report['status'],'complete')
            self.assertEqual(report['created'],0)
            self.assertEqual(report['updated'],0)
            self.assertEqual(report['reactivated'],0)
            self.assertEqual(report['written'],0)
            self.assertEqual(report['relevant_count'],0)
            self.assertEqual(report['invalid_count'],0)

    def test_institution_only_collection_preserves_public_distributor_and_authorship(self):
        import json
        calls = []
        def crawl(output, source_ids, limit_per_source):
            calls.append((source_ids, limit_per_source))
            row = dict(candidate(), source_id='broker-fixture', source_name='测试券商',
                       collection_platform='测试公开研报平台', publisher_name='测试券商股份有限公司',
                       content_scope='public_research_summary')
            (Path(output) / 'broker-fixture.jsonl').write_text(json.dumps(row, ensure_ascii=False) + '\n')
            return {'sources': [{'source_id': 'broker-fixture', 'status': 'ok', 'count': 1}]}
        adapters = [{'name':'broker-fixture', 'sources':{'broker-fixture':{'name':'测试券商'}},
                     'crawl':crawl, 'candidate_file':'broker-fixture.jsonl'}]
        with tempfile.TemporaryDirectory() as directory:
            with patch('newsdesk.ingest.institution_adapters', return_value=adapters):
                report = collect(directory, source_ids=['broker-fixture'], institution_limit=3)
            self.assertEqual(calls, [(['broker-fixture'], 3)])
            self.assertEqual(report['candidate_count'], 1)
            row = json.loads((Path(directory) / 'candidates.jsonl').read_text())
            review = {'reviewer':'test agent','reviewed_at':'2026-10-28T02:10:00+00:00','decision':decision(row)}
            database = Path(directory) / 'news.sqlite3'
            archive_candidates([row],[review],database,'fixture')
            saved = NewsStore(database, seed_demo=False).all()[0]
            self.assertEqual(saved['ingestion']['collection_platform'],'测试公开研报平台')
            self.assertEqual(saved['ingestion']['publisher_name'],'测试券商股份有限公司')
            self.assertEqual(saved['ingestion']['content_scope'],'public_research_summary')

    def test_cli_explicitly_selects_requested_model_and_structured_read_only_review(self):
        import json
        item = candidate()
        calls = []
        def run(command, **kwargs):
            calls.append((command, kwargs))
            path = Path(command[command.index('--output-last-message') + 1])
            path.write_text(json.dumps({'decisions': [decision(item)]}, ensure_ascii=False))
            return SimpleNamespace(returncode=0)
        with patch('newsdesk.ingest.subprocess.run', side_effect=run):
            reviewed = CodexReviewer(executable='codex').review([item])
        command, kwargs = calls[0]
        self.assertEqual(command[command.index('--model') + 1], 'gpt-6.1-sol')
        self.assertEqual(REVIEW_MODEL, 'gpt-6.1-sol')
        self.assertEqual(command[command.index('--sandbox') + 1], 'read-only')
        self.assertIn('--output-schema', command)
        self.assertIn(item['candidate_id'], kwargs['input'])
        self.assertEqual(reviewed[0]['candidate_id'], item['candidate_id'])

    def test_canonical_identity_drops_tracking_preserves_business_query(self):
        self.assertEqual(canonical_url("https://example.org/a?id=7&utm_source=x#view"), "https://example.org/a?id=7")
        with self.assertRaises(ValueError):
            canonical_url("javascript:alert(1)")

    def test_no_evidence_or_invented_evidence_is_rejected(self):
        item = candidate()
        result = decision(item)
        for evidence in ([], ["原油库存下降500万桶"]):
            result["evidence"] = evidence
            with self.assertRaises(ValueError):
                validate_decision(result, item)

    def test_each_secondary_category_requires_reason(self):
        item = candidate()
        result = decision(item)
        result["category_reasons"] = result["category_reasons"][:1]
        with self.assertRaises(ValueError):
            validate_decision(result, item)

    def test_invalid_agent_dates_and_entities_are_never_cached(self):
        item = candidate()
        for key, value in (("occurred_at", "下周"), ("entities", [123])):
            result = decision(item)
            result["attributes"][key] = value
            with self.assertRaises(ValueError):
                validate_decision(result, item)

    def test_rejected_candidate_never_reaches_archive(self):
        item = candidate()
        result = decision(item)
        result.update(relevant=False, primary_category="", categories=[], category_reasons=[], evidence=[], summary="", attributes=None)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            report = archive_candidates([item], [{"decision": result}], database, "test")
            self.assertEqual(report["written"], 0)
            self.assertEqual(report["created"], 0)
            self.assertEqual(NewsStore(database, seed_demo=False).all(), [])

    def test_stricter_review_excludes_previous_record_without_deleting_it(self):
        item = candidate()
        review = {"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00", "decision": decision(item)}
        rejected = copy.deepcopy(review)
        rejected['reviewed_at'] = '2026-10-28T03:10:00+00:00'
        rejected['decision'].update(relevant=False, primary_category='', categories=[], category_reasons=[], evidence=[], summary='', attributes=None)
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'news.sqlite3'
            archive_candidates([item], [review], database, 'first')
            report = archive_candidates([item], [rejected], database, 'review')
            rows = NewsStore(database, seed_demo=False).all()
            self.assertEqual(report['excluded_previous'], 1)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]['agent_review']['relevance'])
            self.assertEqual(rows[0]['body'], item['body'])
            self.assertEqual(rows[0]['ingestion']['content_hash'], item['content_hash'])
            restored = archive_candidates([item], [review], database, 'restore')
            self.assertEqual(restored['created'],0)
            self.assertEqual(restored['updated'],0)
            self.assertEqual(restored['reactivated'],1)
            self.assertTrue(NewsStore(database, seed_demo=False).all()[0]['agent_review']['relevance'])

    def test_multicategory_archive_provenance_and_idempotency(self):
        item = candidate()
        review = {"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00", "decision": decision(item)}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            first = archive_candidates([item], [review], database, "first")
            store = NewsStore(database, seed_demo=False)
            saved = store.all()[0]
            self.assertEqual(first["written"], 1)
            self.assertEqual(first["created"], 1)
            self.assertEqual(first["updated"], 0)
            self.assertEqual(saved["categories"], ["logistics", "refining"])
            self.assertEqual(saved["published_at"], item["published_at"])
            self.assertEqual(saved["verification"], "待核验")
            self.assertEqual(saved["ingestion"]["content_hash"], item["content_hash"])
            self.assertEqual(saved["agent_review"]["evidence"], review["decision"]["evidence"])
            second = archive_candidates([item], [review], database, "second")
            self.assertEqual(second["written"], 0)
            self.assertEqual(second["created"], 0)
            self.assertEqual(second["updated"], 0)
            self.assertEqual(second["unchanged"], 1)
            self.assertEqual(store.all()[0]["updated_at"], saved["updated_at"])

    def test_review_cache_reused_only_for_unchanged_source_content(self):
        reviewer = FakeReviewer()
        with tempfile.TemporaryDirectory() as directory:
            item = candidate()
            review_candidates([item], directory, reviewer)
            review_candidates([item], directory, reviewer)
            self.assertEqual(reviewer.calls, 1)
            changed = dict(item, body=item["body"] + "后续更新。")
            changed = prepare_candidate(changed)
            review_candidates([changed], directory, reviewer)
            self.assertEqual(reviewer.calls, 2)

    def test_cache_from_different_model_is_not_reused(self):
        import json
        reviewer = FakeReviewer()
        with tempfile.TemporaryDirectory() as directory:
            item = candidate()
            review_candidates([item], directory, reviewer)
            path = next((Path(directory) / 'review-cache').glob('*.json'))
            cached = json.loads(path.read_text())
            cached['model'] = 'another-model'
            path.write_text(json.dumps(cached))
            review_candidates([item], directory, reviewer)
            self.assertEqual(reviewer.calls, 2)

    def test_same_article_update_keeps_stable_identity(self):
        item = candidate()
        review = {"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00", "decision": decision(item)}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            archive_candidates([item], [review], database, "first")
            old = NewsStore(database, seed_demo=False).all()[0]
            changed = prepare_candidate(dict(item, body=item["body"] + "次日恢复。"))
            newer = copy.deepcopy(review)
            newer["decision"]["summary"] = "交付延迟后恢复。"
            changed_report = archive_candidates([changed], [newer], database, "second")
            self.assertEqual(changed_report['created'],0)
            self.assertEqual(changed_report['updated'],1)
            self.assertEqual(changed_report['reactivated'],0)
            rows = NewsStore(database, seed_demo=False).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], old["id"])
            self.assertEqual(rows[0]["created_at"], old["created_at"])
            self.assertEqual(rows[0]["summary"], newer["decision"]["summary"])

    def test_different_candidate_ids_for_same_url_are_one_archive_record(self):
        item = candidate()
        other = prepare_candidate(dict(item, candidate_id="fixture:002", body=item["body"] + "更多公开细节。"))
        reviews = [{"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00", "decision": decision(row)} for row in [item, other]]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            report = archive_candidates([item, other], reviews, database, "test")
            self.assertEqual(report["written"], 1)
            self.assertEqual(report["created"], 1)
            self.assertEqual(report["exact_duplicates"], 1)
            self.assertEqual(NewsStore(database, seed_demo=False).all()[0]["body"], other["body"])

    def test_publisher_article_id_survives_section_url_changes(self):
        item = candidate()
        review = {"reviewer": "test agent", "reviewed_at": "2026-10-28T02:10:00+00:00", "decision": decision(item)}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "news.sqlite3"
            archive_candidates([item], [review], database, "first")
            old = NewsStore(database, seed_demo=False).all()[0]
            moved = prepare_candidate(dict(item, source_url="https://example.org/research/001"))
            archive_candidates([moved], [review], database, "second")
            rows = NewsStore(database, seed_demo=False).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["id"], old["id"])
            self.assertEqual(rows[0]["source_url"], moved["source_url"])

    def test_moved_article_and_other_source_old_url_link_share_one_archive_update(self):
        item = candidate()
        review = {"reviewer":"test agent","reviewed_at":"2026-10-28T02:10:00+00:00","decision":decision(item)}
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'news.sqlite3'
            archive_candidates([item],[review],database,'first')
            identifier = NewsStore(database,seed_demo=False).all()[0]['id']
            moved = prepare_candidate(dict(item,source_url='https://example.org/research/001',
                                           body=item['body']+'迁移后公开页面补充了后续交付恢复的更多信息。'))
            linked = prepare_candidate(dict(item,candidate_id='other:002',source_id='other',external_id='002'))
            reviews = [dict(review,decision=decision(row)) for row in [linked,moved]]
            result = archive_candidates([linked,moved],reviews,database,'second')
            rows = NewsStore(database,seed_demo=False).all()
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['id'],identifier)
            self.assertEqual(rows[0]['source_url'],moved['source_url'])
            self.assertEqual(rows[0]['body'],moved['body'])
            self.assertEqual(result['written'],1)
            self.assertEqual(result['created'],0)
            self.assertEqual(result['updated'],1)
            self.assertEqual(result['exact_duplicates'],1)

    def test_collection_page_preserves_distinct_reports_and_history(self):
        item = prepare_candidate(dict(candidate(), url_is_collection=True, external_id='report-1'))
        other = prepare_candidate(dict(item, candidate_id='fixture:002', external_id='report-2',
                                       title='另一份油轮研究',body=item['body']+'另一份报告记录不同进展。'))
        reviews = [{'reviewer':'test agent','reviewed_at':'2026-10-28T02:10:00+00:00','decision':decision(row)} for row in [item,other]]
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'news.sqlite3'
            first = archive_candidates([item,other],reviews,database,'first')
            self.assertEqual(first['written'],2)
            rows = NewsStore(database,seed_demo=False).all()
            self.assertEqual(len(rows),2)
            self.assertEqual(len({row['id'] for row in rows}),2)
            self.assertTrue(all(row['ingestion']['source_page_kind']=='collection' for row in rows))
            second = archive_candidates([item,other],reviews,database,'repeat')
            self.assertEqual(second['written'],0)
            self.assertEqual(second['unchanged'],2)


if __name__ == "__main__":
    unittest.main()
