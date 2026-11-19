import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from newsdesk.history_overseas import SOURCES, article_url, atomic_jsonl, listing_links, publication_day, run_source, sitemap_links


def sitemap(urls):
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(
        '<url><loc>' + url + '</loc><lastmod>2099-10-30</lastmod></url>' for url in urls) + '</urlset>').encode()


def article(published=None, *, overseas=False):
    stamp = '<script type="application/ld+json">' + json.dumps({'@type': 'Article', 'datePublished': published}) + '</script>' if published else ''
    body = 'Oil supply and refinery activity affect demand, inventories, inflation and global growth. ' * 3
    if overseas:
        return ('<html><h1>Public research</h1>' + stamp + '<div class="wysiwyg"><p>' + body + '</p></div></html>').encode()
    return ('<html><meta property="og:title" content="Public research"><h1>Public research</h1>' + stamp + '<p>' + body + '</p></html>').encode()


class FakeFetcher:
    def __init__(self, directory, responses):
        self.directory = Path(directory)
        self.responses = responses
        self.calls = []

    def read_robots(self):
        return 'checked'

    def get(self, url):
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        path = self.directory / ('raw-' + str(len(self.calls)) + '.html')
        path.write_bytes(response)
        return response, path, '2026-10-30T00:00:00+00:00', url


class HistoryOverseasTests(unittest.TestCase):
    def test_candidate_snapshot_stays_old_until_complete_file_is_atomically_replaced(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'candidates.jsonl'
            previous = '{"previous": true}\n'
            path.write_text(previous)
            rows = [{'title': '第一条'}, {'title': '第二条'}]
            replace = os.replace

            def observe_before_replace(source, destination):
                self.assertEqual(path.read_text(), previous)
                self.assertEqual([json.loads(line) for line in Path(source).read_text().splitlines()], rows)
                replace(source, destination)

            with patch('newsdesk.history_overseas.os.replace', side_effect=observe_before_replace) as swapped:
                atomic_jsonl(path, rows)
            swapped.assert_called_once()
            self.assertEqual([json.loads(line) for line in path.read_text().splitlines()], rows)

    def test_failed_snapshot_serialization_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'unknown-dates.jsonl'
            previous = '{"previous": true}\n'
            path.write_text(previous)
            with self.assertRaises(ValueError):
                atomic_jsonl(path, [{'ok': 1}, {'invalid_number': float('nan')}])
            self.assertEqual(path.read_text(), previous)
            self.assertEqual(list(Path(temporary).iterdir()), [path])

    def test_article_cap_resumes_and_unknown_date_is_not_inferred_from_lastmod(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = ['https://www.goldmansachs.com/insights/articles/' + name for name in ['current', 'old', 'unknown']]
            fake = FakeFetcher(root, {SOURCES['goldman_sachs']['entry']: sitemap(urls),
                urls[0]: article('2026-02-01'), urls[1]: article('2025-10-01'), urls[2]: article()})
            first = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', max_articles=1, fetcher=fake)
            self.assertEqual(first['status'], 'incomplete')
            self.assertEqual(first['processed_article_count'], 1)
            second = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', max_articles=1, fetcher=fake)
            self.assertEqual(second['processed_article_count'], 2)
            final = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', max_articles=1, fetcher=fake)
            self.assertEqual(final['processed_article_count'], 3)
            self.assertEqual(final['candidate_count'], 1)
            self.assertEqual(final['unknown_date_count'], 1)
            self.assertEqual(final['outside_window_count'], 1)
            self.assertEqual(final['status'], 'incomplete')
            self.assertTrue(final['public_index_exhausted'])
            self.assertFalse(final['complete'])
            self.assertEqual(len(fake.calls), 4)
            row = json.loads(Path(final['candidates_path']).read_text().strip())
            self.assertEqual(row['published_at'], '')
            self.assertEqual(row['published_date'], '2026-02-01')
            self.assertNotIn('2099', row['published_date'])

    def test_full_public_index_completes_only_after_every_article_is_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = ['https://www.goldmansachs.com/insights/articles/' + name for name in ['march', 'october']]
            fake = FakeFetcher(root, {SOURCES['goldman_sachs']['entry']: sitemap(urls),
                urls[0]: article('2026-03-01T00:00:00-05:00'), urls[1]: article('2026-10-15')})
            report = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['candidate_count'], 2)
            self.assertEqual(report['coverage']['requested_start'], '2026-01-01')
            self.assertTrue(report['coverage']['public_index_exhausted'])
            self.assertIn('公开研究栏目索引', report['coverage']['scope'])

    def test_pagination_discovery_resumes_instead_of_declaring_sample_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            index, child = SOURCES['goldman_sachs']['entry'], 'https://www.goldmansachs.com/sitemap-1.xml'
            url = 'https://www.goldmansachs.com/insights/articles/history'
            raw_index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><sitemap><loc>' + child + '</loc></sitemap></sitemapindex>').encode()
            fake = FakeFetcher(root, {index: raw_index, child: sitemap([url]), url: article('2026-01-05')})
            first = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', max_discovery_pages=1, fetcher=fake)
            self.assertEqual(first['status'], 'incomplete')
            self.assertEqual(first['pending_discovery_pages'], 1)
            final = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', max_discovery_pages=1, fetcher=fake)
            self.assertEqual(final['status'], 'complete')
            self.assertEqual(final['candidate_count'], 1)
            self.assertEqual(fake.calls.count(index), 1)

    def test_nomura_follows_published_pagination_links_and_filters_article_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = 'institution_46'
            index = SOURCES[source]['entry']
            page2 = 'https://www.nomuraconnects.com/Articles/p2/'
            old = 'https://www.nomuraconnects.com/focused-thinking-posts/old/'
            current = 'https://www.nomuraconnects.com/focused-thinking-posts/current/'
            fake = FakeFetcher(root, {index: ('<a href="' + page2 + '">Next</a><a href="' + current + '">Article</a>').encode(),
                page2: ('<a href="' + index + '">Previous</a><a href="' + old + '">Article</a>').encode(),
                old: article('2025-10-30', overseas=True), current: article('2026-07-01', overseas=True)})
            report = run_source(source, '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(report['status'], 'complete')
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['outside_window_count'], 1)
            self.assertEqual(report['discovery_pages_total'], 2)

    def test_bad_article_does_not_discard_other_public_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            urls = ['https://www.goldmansachs.com/insights/articles/' + name for name in ['bad', 'good']]
            fake = FakeFetcher(root, {SOURCES['goldman_sachs']['entry']: sitemap(urls),
                urls[0]: b'<html><nav>Only a menu</nav></html>', urls[1]: article('2026-04-01')})
            report = run_source('goldman_sachs', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(report['status'], 'incomplete')
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['failed_article_count'], 1)

    def test_private_sources_stay_explicitly_blocked_and_do_not_fetch_client_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            fake = FakeFetcher(temporary, {})
            report = run_source('institution_43', '2026-01-01', '2026-10-30', temporary, fetcher=fake)
            self.assertEqual(report['status'], 'blocked')
            self.assertFalse(report['complete'])
            self.assertEqual(fake.calls, [])

    def test_window_uses_beijing_day_for_precise_timestamps_without_fabricating_time(self):
        self.assertEqual(publication_day({'published_at': '2025-12-31T20:00:00-05:00'}), '2026-01-01')
        self.assertEqual(publication_day({'published_date': '2026-01-01'}), '2026-01-01')
        self.assertFalse(article_url('goldman_sachs', 'https://other.example/insights/articles/test'))
        self.assertFalse(article_url('goldman_sachs', 'https://www.goldmansachs.com/insights/top-of-mind/private'))

    def test_login_or_changed_index_is_not_mislabeled_as_empty_complete_history(self):
        with self.assertRaises(ValueError):
            sitemap_links(b'<html><body>Please log in</body></html>', 'goldman_sachs')
        with self.assertRaises(ValueError):
            listing_links(b'<html><h1>Service unavailable</h1></html>', 'institution_46', SOURCES['institution_46']['entry'])


if __name__ == '__main__':
    unittest.main()
