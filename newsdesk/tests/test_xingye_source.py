import json
import unittest

from newsdesk.xingye_source import parse_article, parse_f10_reports, parse_profile


def page(props):
    return '<script id="__NEXT_DATA__" type="application/json">' + json.dumps({'props': {'pageProps': props}}, ensure_ascii=False) + '</script>'


class XingyeTests(unittest.TestCase):
    def test_f10_report_author_date_and_identity_are_independent_of_shared_url(self):
        def block(publisher, title):
            return '<dl><h1 class="cont_title">' + title + '</h1><h2 class="sub_cont_title">发布时间：2026-10-28 来源：' + publisher + '</h2><div class="sub_cont">' + ('公开报告正文研究数据。' * 15) + '</div></dl>'
        first = block('兴业证券', '第一篇研究')
        second = block('兴业证券', '第二篇研究')
        wrong = block('兴业银行', '银行研究')
        rows = parse_f10_reports(first + second + first + wrong, '300750')
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]['external_id'], rows[1]['external_id'])
        self.assertTrue(rows[0]['url_is_collection'])
        self.assertEqual(rows[0]['published_at'], '')
        self.assertEqual(rows[0]['published_date'], '2026-10-28')
        self.assertEqual(rows[0]['collection_platform'], '同花顺公开F10研报')

    def article(self):
        return {'id': 123, 'title': '研究公开文章', 'content': '<p>' + ('机构公开研究正文。' * 20) + '</p>',
                'createTimestamp': 1789599988, 'purchaseInfo': None, 'remainWordNumber': 0,
                'column': {'id': 624647, 'nick': '兴证策略张启尧', 'brief': '兴业证券研究所策略团队', 'columnAuthorStatus': 'authenticated'}}

    def test_exact_authenticated_research_team_and_public_timestamp(self):
        result = parse_article(page({'articleInfo': self.article()}))
        self.assertEqual(result['publisher_name'], '兴业证券研究所策略团队')
        self.assertEqual(result['published_at'], '2026-09-16T23:06:28+00:00')
        self.assertEqual(result['content_scope'], 'public_research_summary')

    def test_non_research_owner_or_paywall_is_rejected(self):
        article = self.article()
        article['column']['brief'] = '兴业银行研究团队'
        with self.assertRaises(ValueError):
            parse_article(page({'articleInfo': article}))
        article = self.article()
        article['remainWordNumber'] = 20
        with self.assertRaises(ValueError):
            parse_article(page({'articleInfo': article}))

    def test_profile_links_must_belong_to_verified_team(self):
        profile = {'userId': 624647, 'userBrief': '兴业证券研究所策略团队',
                   'myHomePageItemVOS': [{'id': 123, 'userId': 624647}, {'id': 456, 'userId': 999}]}
        rows = parse_profile(page({'articles': profile}) + '<a href="/p/123">研究</a><a href="/p/456">其他</a>')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_url'], 'https://m.gelonghui.com/p/123')
        profile['userBrief'] = '私人投资随笔'
        with self.assertRaises(ValueError):
            parse_profile(page({'articles': profile}))


if __name__ == '__main__':
    unittest.main()
