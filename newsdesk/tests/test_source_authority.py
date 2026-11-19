import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from newsdesk.backfill_source_authority import run
from newsdesk.source_authority import assess, coverage, registry
from newsdesk.server import NewsStore


class SourceAuthorityTests(unittest.TestCase):
    def test_workbook_rows_and_tiers(self):
        value, _ = registry()
        self.assertEqual(len(value['entries']), 95)
        self.assertEqual({r['level'] for r in value['entries']}, {'P0', 'P1', 'P2'})
        self.assertEqual(assess({'origin_name': '高盛'})['reference'], '工作表1!D24')

    def test_original_institution_not_aggregator_or_body_mention(self):
        value = assess({'origin_name': '高盛', 'source_name': '财联社'})
        self.assertEqual((value['level'], value['matched_source']), ('P0', '高盛'))
        self.assertEqual(assess({'source_name': '财联社', 'body': '高盛表示'})['level'], '')
        self.assertEqual(assess({'origin_name': 'EIA', 'source_name': '财联社'})['level'], '')

    def test_aliases_do_not_leak_to_similarly_named_entities_or_accounts(self):
        self.assertEqual(assess({'origin_name': '美国银行'})['matched_source'], '美银')
        self.assertEqual(assess({'origin_name': '巴克莱'})['level'], 'P1')
        self.assertEqual(assess({'origin_name': '中金'})['level'], 'P0')
        for name in ['中金财富期货', '摩根士丹利三菱日联证券', '中信期货', '方正证券']:
            self.assertEqual(assess({'origin_name': name})['level'], '')
        self.assertEqual(assess({'origin_name': '王涵论宏观'})['matched_source'], '王涵论宏观')

    def test_chinese_financial_times_is_not_uk_financial_times(self):
        for row in [{'origin_name': '英国《金融时报》'}, {'origin_name': '金融时报'},
                    {'origin_name': '金融时报', 'origin_evidence': '据英国《金融时报》独家报道'}]:
            self.assertEqual(assess(row)['level'], '')
        self.assertEqual(assess({'origin_name': '中国金融时报'})['level'], 'P0')

    def test_multiple_issuers_are_not_silently_assigned_first_tier(self):
        self.assertEqual(assess({'origin_name': '高盛、摩根大通'})['level'], 'P0')
        self.assertEqual(assess({'origin_name': '高盛、巴克莱'})['status'], 'mixed')
        self.assertEqual(assess({'origin_name': '花旗、YOUGOV'})['status'], 'partial')
        self.assertEqual(assess({'origin_name': '花旗、YOUGOV'})['level'], '')

    def test_coverage_separates_configured_sources_issuers_and_feeds(self):
        rows = [{'origin_name': '高盛', 'source_name': '财联社'},
                {'origin_name': '金融时报', 'source_name': '同花顺', 'body': '英国《金融时报》'},
                {'origin_name': '兴业证券', 'source_name': '兴业证券', 'ingestion': {'collection_platform': '格隆汇'}},
                {'origin_name': 'Barclays', 'agent_review': {'relevance': False}}]
        config = {'sources': [{'id': 'a', 'name': '财新', 'enabled': True}]}
        report = coverage(rows, config)
        by_name = {r['name']: r for r in report['entries']}
        self.assertEqual(by_name['高盛']['issuer_news_count'], 1)
        self.assertEqual(by_name['财联社']['feed_news_count'], 1)
        self.assertEqual(by_name['财联社']['issuer_news_count'], 0)
        self.assertEqual(by_name['财新']['coverage_status'], '已接入，暂无有效入库')
        for name in ['金融时报', 'Barclays', '格隆汇', '王涵论宏观']:
            self.assertEqual(by_name[name]['coverage_status'], '未覆盖')

    def test_migration_only_changes_authority_and_can_be_repeated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); database = root / 'archive.sqlite3'
            row = {'id': 'fixture', 'title': '新闻', 'origin_name': '高盛',
                   'published_at': '2026-01-01T00:01:02+00:00', 'updated_at': '2026-01-02T00:00:00Z'}
            with sqlite3.connect(database) as connection:
                connection.execute('CREATE TABLE news(id TEXT PRIMARY KEY,payload TEXT,updated_at TEXT)')
                connection.execute('INSERT INTO news VALUES(?,?,?)', ('fixture', json.dumps(row), row['updated_at']))
            report = run(database, root / 'audit')
            with sqlite3.connect(database) as connection:
                payload, clock = connection.execute('SELECT payload,updated_at FROM news').fetchone()
            after = json.loads(payload)
            self.assertEqual(after.pop('origin_authority')['level'], 'P0')
            self.assertEqual(after, row)
            self.assertEqual(clock, row['updated_at'])
            self.assertEqual(report['updated'], 1)
            self.assertEqual(run(database, root / 'audit')['updated'], 0)

    def test_new_records_and_manual_issuer_changes_recompute_tier(self):
        with tempfile.TemporaryDirectory() as directory:
            store = NewsStore(Path(directory) / 'test.sqlite3', seed_demo=False)
            row = store.save({'title': '原油需求', 'category': 'demand', 'origin_name': '高盛',
                              'published_at': '2026-01-01T00:00:00Z', 'confidence': '低',
                              'origin_authority': {'level': 'P2'}})
            self.assertEqual(row['origin_authority']['level'], 'P0')
            self.assertEqual(row['confidence'], '低')
            row = store.save({'origin_name': 'EIA'}, row['id'])
            self.assertEqual(row['origin_authority']['level'], '')
            self.assertEqual(store.get(row['id'])['confidence'], '低')


if __name__ == '__main__':
    unittest.main()
