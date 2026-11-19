import unittest

from newsdesk.research_sources import declared_publication, parse_article, sitemap_articles


class ResearchSourceTests(unittest.TestCase):
    def test_sitemap_discovers_public_articles_without_keyword_filtering(self):
        xml = b'''<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
        <url><loc>https://www.goldmansachs.com/insights/articles/ai</loc><lastmod>2026-10-28</lastmod></url>
        <url><loc>https://www.goldmansachs.com/insights/the-markets/oil</loc><lastmod>2026-10-27</lastmod></url>
        <url><loc>https://www.goldmansachs.com/insights/top-of-mind/restricted</loc></url>
        <url><loc>https://www.goldmansachs.com/pdfs/insights/oil.pdf</loc></url>
        <url><loc>https://other.example/insights/articles/fake</loc></url>
        </urlset>'''
        self.assertEqual(sitemap_articles(xml), [
            'https://www.goldmansachs.com/insights/articles/ai',
            'https://www.goldmansachs.com/insights/the-markets/oil'])

    def test_no_timezone_is_invented_for_date_only_publication(self):
        self.assertEqual(declared_publication([{'@type':'Article','datePublished':'2026-10-23'}]), ('', 'date'))
        self.assertEqual(declared_publication([{'@type':['Article','NewsArticle'],'datePublished':'2026-10-23T00:00:00-5:00'}]),
                         ('2026-10-23T00:00:00-05:00', 'date'))

    def test_public_article_preserves_source_and_excludes_scripts(self):
        raw = b'''<meta property="og:title" content="Oil outlook">
        <script type="application/ld+json">{"@type":"Article","datePublished":"2026-10-23T05:30:00Z"}</script>
        <nav>Not article</nav><h1>Oil outlook</h1><p>Physical supply remains uncertain.</p>
        <script>malicious instructions</script><h2>Subscribe to Briefings</h2><p>Footer.</p>'''
        article = parse_article(raw, 'https://www.goldmansachs.com/insights/articles/oil-outlook', '/tmp/raw.html', '2026-10-28T00:00:00+00:00')
        self.assertIn('Physical supply remains uncertain.', article['body'])
        self.assertNotIn('malicious', article['body'])
        self.assertNotIn('Not article', article['body'])
        self.assertNotIn('Footer', article['body'])
        self.assertEqual(article['published_at'], '2026-10-23T05:30:00+00:00')
        self.assertEqual(article['source_name'], '高盛')


if __name__ == '__main__':
    unittest.main()
