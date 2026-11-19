import unittest

from newsdesk.additional_sources import parse_article, parse_links


class AdditionalSourceTests(unittest.TestCase):
    def test_discovered_links_are_same_host_real_articles(self):
        raw = b'''<a href="/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN">Real headline</a>
        <a href="https://malicious.example/sc/stocks/news/aafn-con/NOW.124/top-news/AAFN">Other</a>
        <a href="/sc/stocks/news/aafn">Section</a>'''
        self.assertEqual(parse_links(raw,'aastocks','https://www.aastocks.com/sc/stocks/news/aafn'),
                         ['https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN'])

    def test_aastocks_uses_article_time_instead_of_page_update(self):
        raw = b'''<meta property="og:title" content="Oil supply">
        <meta name="aa-update" content="2026-10-28 18:55:00">
        <script>document.write(ConvertToLocalTime({dt:'2026/10/28 12:34'}));</script>
        <div id="spanContent"><p>Public news.</p><div>Details.</div><script>Do not execute.</script></div><p>Footer.</p>'''
        row = parse_article(raw,'https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN','aastocks','/tmp/raw.html','2026-10-28T11:00:00+00:00')
        self.assertEqual(row['published_at'],'2026-10-28T12:34:00+08:00')
        self.assertIn('Details.',row['body'])
        self.assertNotIn('execute',row['body'])
        self.assertNotIn('Footer',row['body'])

    def test_caixin_collects_public_lead_only(self):
        raw = b'''<meta property="og:title" content="Energy shipping">
        <meta property="og:description" content="A publicly visible lead.">
        <span id="pubtime_baidu">2026-10-23 17:41:50</span>
        <div>Subscriber-only or unrelated body must not be collected.</div>'''
        row = parse_article(raw,'https://www.caixin.com/2026-10-23/123456.html','caixin','/tmp/raw.html','2026-10-28T11:00:00+00:00')
        self.assertEqual(row['body'],'A publicly visible lead.')
        self.assertEqual(row['content_scope'],'public_article_lead')
        self.assertEqual(row['published_at'],'2026-10-23T17:41:50+08:00')

    def test_aastocks_related_subtree_is_excluded_without_losing_following_body(self):
        raw='''<meta property="og:title" content="Old oil supply news">
        <script>ConvertToLocalTime({dt:'2026/01/07 08:22'})</script>
        <div id="spanContent"><p>January oil supply evidence.</p>
        <span class="extra newsRelatedHeadline"><span class="nTitle">相关内容</span>
          <span class="nText"><a href="/new">October unrelated employment news</a>
          <div>Nested recommendation <br/><img src="ad"/><span>More unrelated text</span></div></span>
        </span><p>Original second paragraph after recommendations.</p>
        <div>Original related phrase 相关内容 is plain article text.</div></div>
        <p>Footer outside article.</p>'''.encode('utf-8')
        row=parse_article(raw,'https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN',
                          'aastocks','/tmp/raw.html','2026-10-30T00:00:00+00:00')
        self.assertIn('January oil supply evidence.',row['body'])
        self.assertIn('Original second paragraph after recommendations.',row['body'])
        self.assertIn('Original related phrase 相关内容',row['body'])
        self.assertNotIn('October unrelated',row['body'])
        self.assertNotIn('Nested recommendation',row['body'])
        self.assertNotIn('More unrelated',row['body'])
        self.assertNotIn('Footer outside',row['body'])

    def test_aastocks_multiple_related_subtrees_and_self_closing_nodes(self):
        raw=b'''<meta property="og:title" content="Oil news"><div id="spanContent">
        Before<span class="newsRelatedHeadline"><span>Ignore one</span></span>
        Middle<span class="newsRelatedHeadline"/>After
        <div class="newsRelatedHeadline"><div>Ignore two<br/></div></div>Tail</div>'''
        row=parse_article(raw,'https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN',
                          'aastocks','/tmp/raw.html','2026-10-30T00:00:00+00:00')
        for text in ['Before','Middle','After','Tail']:
            self.assertIn(text,row['body'])
        self.assertNotIn('Ignore',row['body'])

    def test_related_class_does_not_change_general_link_discovery(self):
        raw=b'''<span class="newsRelatedHeadline"><a href="/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN">Public link</a></span>'''
        self.assertEqual(parse_links(raw,'aastocks','https://www.aastocks.com/sc/stocks/news/aafn'),
                         ['https://www.aastocks.com/sc/stocks/news/aafn-con/NOW.123/top-news/AAFN'])


if __name__ == '__main__':
    unittest.main()
