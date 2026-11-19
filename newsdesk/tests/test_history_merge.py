import json
import tempfile
import unittest
from pathlib import Path

from newsdesk.history_merge import merge_channel_reports


class MergeTests(unittest.TestCase):
    def test_bounded_retry_of_one_channel_is_not_hidden_by_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=merge_channel_reports('a','2026-01-01','2026-10-30',tmp,{
                'feed':{'status':'blocked','terminal':True},
                'topics':{'status':'incomplete','retry_pending':True,'retry_attempts_remaining':2}})
            self.assertTrue(result['retry_pending'])
            self.assertEqual(result['retry_attempts_remaining'],2)
            self.assertFalse(result['terminal'])

    def test_unavailable_feed_does_not_prevent_another_channel_advancing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = merge_channel_reports('a','2026-01-01','2026-10-30',tmp,{
                'feed':{'status':'blocked','terminal':True},
                'topics':{'status':'incomplete','terminal':False,'pages':10}})
            self.assertEqual(result['status'],'incomplete')
            self.assertFalse(result['terminal'])
            self.assertFalse(result['coverage']['complete'])

    def test_keeps_fuller_text_once_and_preserves_unavailable_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name,body in [('feed','brief'),('topic','full original public article')]:
                (root/(name+'.jsonl')).write_text(json.dumps({'candidate_id':'a:1','source_id':'a','body':body})+'\n')
            result=merge_channel_reports('a','2026-01-01','2026-10-30',root/'merged',{
                'feed':{'status':'blocked','terminal':True,'candidates_path':str(root/'feed.jsonl')},
                'topics':{'status':'complete','candidates_path':str(root/'topic.jsonl')}})
            rows=[json.loads(line) for line in Path(result['candidates_path']).read_text().splitlines()]
            self.assertEqual(len(rows),1)
            self.assertEqual(rows[0]['body'],'full original public article')
            self.assertTrue(result['terminal'])
            self.assertEqual(result['status'],'incomplete')

    def test_only_all_complete_channels_without_errors_are_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=merge_channel_reports('a','2026-01-01','2026-10-30',tmp,{
                'one':{'status':'complete'},'two':{'status':'complete'}})
            self.assertEqual(result['status'],'complete')
            self.assertTrue(result['terminal'])


if __name__=='__main__':
    unittest.main()
