import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from newsdesk import public_company_sources as company

CN = 'https://www.zjky.cn'
EN = 'https://www.zijinmining.com'
NEWS = CN + '/news/news_list.jsp'
MEDIA = CN + '/news/media_focus.jsp'


def listing(rows, next_url=''):
    cards = ''.join('<li><a href="' + url + '"><div class="tit">Article</div>'
                    + '<div class="date">' + day + '</div></a></li>' for url, day in rows)
    # The live publisher has duplicate class attributes: the label must work too.
    more = '<a class="gobutton next" class="num" href="' + next_url + '">下一页</a>' if next_url else ''
    return ('<html><section class="news_list"><ul>' + cards + '</ul>' + more + '</section></html>').encode()


def article(title='Public article', day='2026/03/01', extra=''):
    return ('<html><nav>navigation must be excluded</nav>' + extra + '<div class="news_detail01">'
            + '<div class="title">' + title + '</div><span class="_tool time"><i>icon</i>' + day + '</span>'
            + '<div class="myart"><p>' + title + ' full original public text.</p><p>Second paragraph.</p></div>'
            + '</div><footer>footer must be excluded</footer></html>').encode()


class FakeFetcher:
    def __init__(self, directory, responses):
        self.directory, self.responses, self.calls = Path(directory), responses, []
        self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, url):
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        path = self.directory / ('response-' + str(len(self.calls)) + '.html')
        path.write_bytes(value)
        return value, path, '2026-10-30T00:00:00+00:00', url


class PublicCompanyTests(unittest.TestCase):
    def test_publisher_date_without_timezone_remains_date_only_and_body_is_scoped(self):
        row = company.parse_article(article(day='2026/03/01 09:32'), CN + '/news/news-detail-10.htm',
                                    '/tmp/original.html', '2026-10-30T00:00:00+00:00')
        self.assertEqual(row['published_date'], '2026-03-01')
        self.assertEqual(row['published_at'], '')
        self.assertEqual(row['published_at_precision'], 'date')
        self.assertIn('09:32', row['published_time_raw'])
        self.assertEqual(row['content_scope'], 'public_article')
        self.assertNotIn('navigation', row['body'])
        self.assertNotIn('footer', row['body'])
        self.assertNotIn('categories', row)

    def test_timestamp_is_kept_only_when_publisher_declares_timezone(self):
        raw = article(day='', extra='<meta property="article:published_time" content="2026-02-01T11:00:00+08:00">')
        row = company.parse_article(raw, CN + '/news/news-detail-10.htm', '/tmp/r', 'now')
        self.assertEqual(row['published_at'], '2026-02-01T11:00:00+08:00')
        self.assertEqual(row['published_at_precision'], 'timestamp')

    def test_listing_date_fallback_keeps_its_original_raw_evidence(self):
        row = company.parse_article(article(day=''), CN + '/news/news-detail-10.htm', '/tmp/r', 'now',
                                    {'published_date': '2026-01-04', 'published_time_raw': '2026/01/04',
                                     'listing_raw_path': '/tmp/listing.html'})
        self.assertEqual(row['published_date_origin'], 'source_listing')
        self.assertEqual(row['listing_raw_path'], '/tmp/listing.html')
        self.assertEqual(row['published_at'], '')

    def test_only_real_next_link_is_followed_and_no_arbitrary_page_numbers_are_enumerated(self):
        raw = listing([('/news/news-detail-10.htm', '2026/03/01')], '?nowPage=2')
        rows, next_url = company.parse_listing(raw, NEWS)
        self.assertEqual(next_url, NEWS + '?nowPage=2')
        self.assertEqual(rows[0]['published_date'], '2026-03-01')
        self.assertEqual(rows[0]['url'], CN + '/news/news-detail-10.htm')
        with self.assertRaises(ValueError):
            company.parse_listing(b'<html><div>Login or dynamic empty shell</div></html>', NEWS)

    def test_sitemap_lastmod_does_not_become_publication_date_and_legacy_alias_deduplicates(self):
        xml = ('<urlset><url><loc>' + CN + '/news/news-detail.jsp?id=10</loc><lastmod>2099-10-30</lastmod></url>'
               + '<url><loc>' + CN + '/news/news-detail-10.htm</loc></url>'
               + '<url><loc>https://unrelated.example/news/news-detail-11.htm</loc></url></urlset>').encode()
        _, rows = company.sitemap_links(xml, CN + '/sitemap.xml')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['published_date'], '')
        self.assertEqual(company.article_identity(EN + '/news/news-detail-10.htm'), company.article_identity(rows[0]['url']))

    def test_caps_are_per_call_and_pagination_resumes_through_year_boundary(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', {'cn_news': (NEWS, '紫金新闻', 'zh')}):
            root = Path(temporary)
            urls = [CN + '/news/news-detail-' + str(n) + '.htm' for n in [1, 2, 3]]
            page2, page3 = NEWS + '?nowPage=2', NEWS + '?nowPage=3'
            fake = FakeFetcher(root, {NEWS: listing([(urls[0], '2026/10/01')], '?nowPage=2'),
                page2: listing([(urls[1], '2026/01/01')], '?nowPage=3'),
                page3: listing([(urls[2], '2025/12/31')], '?nowPage=4'),
                urls[0]: article('October', '2026/10/01'), urls[1]: article('January', '2026/01/01')})
            output = root / 'source'
            first = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', output, max_pages=1, max_articles=1, fetcher=fake)
            self.assertFalse(first['terminal'])
            second = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', output, max_pages=1, max_articles=1, fetcher=fake)
            self.assertEqual(second['candidate_count'], 2)
            final = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', output, max_pages=1, max_articles=1, fetcher=fake)
            self.assertEqual(final['status'], 'complete')
            self.assertTrue(final['terminal'])
            self.assertEqual(final['pages'], 3)
            self.assertEqual(fake.calls.count(NEWS), 1)
            self.assertNotIn(NEWS + '?nowPage=4', fake.calls)
            self.assertNotIn(urls[2], fake.calls)
            self.assertEqual(len(Path(final['candidates_path']).read_text().splitlines()), 2)

    def test_columns_have_independent_round_robin_discovery_and_article_frontiers(self):
        channels = {'cn_news': (NEWS, '紫金新闻', 'zh'), 'cn_media': (MEDIA, '媒体聚焦', 'zh')}
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', channels):
            root = Path(temporary)
            first = CN + '/news/news-detail-1.htm'
            second = CN + '/news/news-detail-2.htm'
            media = CN + '/news/media-detail-3.htm'
            fake = FakeFetcher(root, {NEWS: listing([(first, '2026/10/01'), (second, '2026/10/01')]),
                MEDIA: listing([(media, '2026/05/27')]), first: article('News'), second: article('More news'), media: article('Media')})
            report = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', max_pages=2, max_articles=2, fetcher=fake)
            rows = [json.loads(line) for line in Path(report['candidates_path']).read_text().splitlines()]
            self.assertEqual([row['source_section'] for row in rows], ['紫金新闻', '媒体聚焦'])
            self.assertEqual(report['pending_article_count'], 1)
            self.assertIn(MEDIA, fake.calls)

    def test_unknown_dates_and_404_are_gaps_not_empty_success(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', {'cn_news': (NEWS, '紫金新闻', 'zh')}):
            root = Path(temporary)
            unknown, missing = CN + '/news/news-detail-1.htm', CN + '/news/news-detail-2.htm'
            fake = FakeFetcher(root, {NEWS: listing([(unknown, ''), (missing, '2026/01/01')]),
                unknown: article(day=''), missing: HTTPError(missing, 404, 'Not Found', {}, None)})
            report = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertTrue(report['terminal'])
            self.assertEqual(report['status'], 'incomplete')
            self.assertFalse(report['complete'])
            self.assertEqual(report['unknown_date_count'], 1)
            self.assertEqual(report['gap_count'], 2)

    def test_failed_listing_retries_on_later_calls_without_starving_other_columns(self):
        channels = {'cn_news': (NEWS, '紫金新闻', 'zh'), 'cn_media': (MEDIA, '媒体聚焦', 'zh')}
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', channels):
            root = Path(temporary)
            current = CN + '/news/news-detail-1.htm'
            old = CN + '/news/media-detail-2.htm'
            fake = FakeFetcher(root, {NEWS: [TimeoutError('temporarily unavailable'), listing([(current, '2026/01/01')])],
                MEDIA: listing([(old, '2025/12/31')]), current: article()})
            first = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', max_pages=2, fetcher=fake)
            self.assertFalse(first['terminal'])
            self.assertEqual(fake.calls.count(NEWS), 1)
            self.assertIn(MEDIA, fake.calls)
            final = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(final['status'], 'complete')
            self.assertEqual(final['candidate_count'], 1)

    def test_official_same_id_and_declared_translation_are_deduplicated_but_not_guessed(self):
        channels = {'cn_news': (NEWS, '紫金新闻', 'zh'), 'en_news': (EN + '/news/news_list.jsp', 'News', 'en')}
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', channels):
            root = Path(temporary)
            cn, en, independent = CN + '/news/news-detail-1.htm', EN + '/news/news-detail-2.htm', EN + '/news/news-detail-3.htm'
            fake = FakeFetcher(root, {NEWS: listing([(cn, '2026/01/01')]), channels['en_news'][0]: listing([(en, '2026/01/01'), (independent, '2026/01/01')]),
                cn: article('中文原文', extra='<link rel="alternate" hreflang="en" href="' + en + '">'),
                en: article('Declared English translation'), independent: article('Independent English article')})
            report = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(report['candidate_count'], 2)
            self.assertEqual(report['duplicate_count'], 1)
            self.assertEqual(report['processed_article_count'], 3)

    def test_transient_article_failure_remains_pending_until_a_later_success(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', {'cn_news': (NEWS, '紫金新闻', 'zh')}):
            root = Path(temporary)
            current = CN + '/news/news-detail-1.htm'
            fake = FakeFetcher(root, {NEWS: listing([(current, '2026/01/01')]),
                current: [TimeoutError('temporary TLS timeout'), article()]})
            first = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertFalse(first['terminal'])
            self.assertEqual(first['pending_article_count'], 1)
            self.assertEqual(first['gap_count'], 0)
            self.assertEqual(fake.calls.count(current), 1)
            final = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertEqual(final['status'], 'complete')
            self.assertEqual(final['candidate_count'], 1)
            self.assertEqual(final['transient_error_count'], 1)

    def test_forbidden_does_not_trigger_requests_to_more_articles_on_the_host(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', {'cn_news': (NEWS, '紫金新闻', 'zh')}):
            root = Path(temporary)
            one, two = CN + '/news/news-detail-1.htm', CN + '/news/news-detail-2.htm'
            fake = FakeFetcher(root, {NEWS: listing([(one, '2026/01/01'), (two, '2026/01/01')]),
                one: HTTPError(one, 403, 'Forbidden', {}, None)})
            report = company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'source', fetcher=fake)
            self.assertNotIn(two, fake.calls)
            self.assertTrue(report['terminal'])
            self.assertEqual(report['candidate_count'], 0)
            self.assertEqual(report['gap_count'], 2)

    def test_changed_window_rejects_resume_and_wrapper_writes_expected_candidate_filename(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(company, 'CHANNELS', {'cn_news': (NEWS, '紫金新闻', 'zh')}):
            root = Path(temporary)
            current = CN + '/news/news-detail-1.htm'
            fake = FakeFetcher(root, {NEWS: listing([(current, '2026/01/01')]), current: article()})
            company.run_source('zijin_mining', '2026-01-01', '2026-10-30', root / 'history', fetcher=fake)
            with self.assertRaises(ValueError):
                company.run_source('zijin_mining', '2026-02-01', '2026-10-30', root / 'history', fetcher=fake)
            with patch.object(company, 'Fetcher', return_value=fake):
                report = company.crawl_public_company_sources(root / 'latest', limit_per_source=1)
            self.assertEqual(Path(report['candidates_path']).name, 'public-company-candidates.jsonl')
            self.assertEqual(report['candidate_count'], 1)
            self.assertEqual(report['sources'][0]['source_id'], 'zijin_mining')

    def test_only_explicit_official_domains_and_article_paths_are_accepted(self):
        self.assertFalse(company.valid_url('https://unrelated.example/news/news-detail-1.htm'))
        self.assertFalse(company.valid_url('https://user:password@www.zjky.cn/news/news-detail-1.htm'))
        self.assertFalse(company.article_identity(CN + '/global/program-detail-1.htm'))
        self.assertEqual(company.article_identity(CN + '/news/news-detail.jsp?id=1'), 'news:1')
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                company.Fetcher(temporary).get('https://unrelated.example/')


if __name__ == '__main__':
    unittest.main()
