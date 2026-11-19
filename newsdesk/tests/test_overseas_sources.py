import unittest

from newsdesk.overseas_sources import parse_article, sitemap_urls


class OverseasSourceTests(unittest.TestCase):
    def test_morgan_stanley_only_captures_primary_podcast_transcript(self):
        raw = b'''<html><meta name="content_publishedAt" content="2026-10-27T04:00:00Z">
        <h1>Oil research</h1><div class="transcript_text"><p>Main interview:
        inventories are falling and oil supply remains tight for several months.
        Oil production and refining recovery need time.</p></div>
        <div class="transcript_text"><p>UP NEXT unrelated advice should not enter the article.</p></div></html>'''
        row = parse_article('institution_45', raw, 'https://www.morganstanley.com/insights/podcasts/thoughts-on-the-market/test', '/tmp/test.html', '2026-10-28T00:00:00+00:00')
        self.assertIn('inventories are falling', row['body'])
        self.assertNotIn('UP NEXT', row['body'])
        self.assertEqual(row['published_at'], '2026-10-27T04:00:00+00:00')
        self.assertEqual(row['content_scope'], 'public_podcast_transcript')

    def test_nomura_uses_article_containers_and_original_timezone(self):
        raw = b'''<html><nav>Do not use menu</nav><h1>Central banks and oil</h1>
        <script type="application/ld+json">{"@type":"NewsArticle","datePublished":"2026-10-07T03:25:00+00:00"}</script>
        <div class="wysiwyg"><p>Sticky inflation and renewed oil prices are changing policy forecasts.
        The original report discusses how supply risks affect global inflation and growth.</p></div>
        <script>secret script is not content</script></html>'''
        row = parse_article('institution_46', raw, 'https://www.nomuraconnects.com/focused-thinking-posts/test/', '/tmp/test.html', '2026-10-28T00:00:00+00:00')
        self.assertNotIn('menu', row['body'])
        self.assertNotIn('secret script', row['body'])
        self.assertEqual(row['published_at'], '2026-10-07T03:25:00+00:00')

    def test_jpmorgan_date_only_is_not_given_a_fabricated_timezone(self):
        raw = b'''<html><meta name="publishDate" content="October 19, 2026"><h1>Bond markets</h1>
        <article class="cmp-article-side-panel-container">Header teaser only</article>
        <div class="cmp-text"><p>Higher energy prices and government bond supply
        have changed expectations for inflation and global interest rates. This is public research text.</p></div></html>'''
        row = parse_article('institution_51', raw, 'https://www.jpmorgan.com/insights/global-research/test', '/tmp/test.html', '2026-10-28T00:00:00+00:00')
        self.assertEqual(row['published_at'], '')
        self.assertEqual(row['published_at_precision'], 'date')
        self.assertEqual(row['publication_date_text'], 'October 19, 2026')
        self.assertEqual(row['published_date'], '2026-10-19')
        self.assertEqual(row['publisher_name'], '摩根大通')
        self.assertEqual(row['collection_platform'], '机构官网')

    def test_sitemap_only_discovers_configured_publisher_research_urls(self):
        raw = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://www.jpmorgan.com/insights/global-research/commodities/oil-prices</loc><lastmod>2099-10-28</lastmod></url>
        <url><loc>https://evil.example/insights/global-research/oil-prices</loc><lastmod>2099-10-28</lastmod></url>
        <url><loc>https://www.jpmorgan.com/login</loc><lastmod>2099-10-28</lastmod></url></urlset>'''
        self.assertEqual(sitemap_urls(raw, 'institution_51'), ['https://www.jpmorgan.com/insights/global-research/commodities/oil-prices'])

    def test_navigation_only_is_not_a_successful_article(self):
        with self.assertRaises(ValueError):
            parse_article('institution_46', b'<html><h1>Navigation</h1><nav>Markets</nav></html>',
                          'https://www.nomuraconnects.com/focused-thinking-posts/test/', '/tmp/test.html', '2026-10-28T00:00:00+00:00')


if __name__ == '__main__':
    unittest.main()
