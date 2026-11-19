import tempfile
from pathlib import Path
import unittest

from newsdesk.evaluation import write_json
from newsdesk.experiment_catalog import experiment_catalog


class ExperimentCatalogTests(unittest.TestCase):
    def test_v2_full_backtest_uses_frozen_question_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            counts={"WTI":41,"Brent":42,"SC":41}
            questions=[{"family":f} for f,count in counts.items() for _ in range(count)]
            write_json(root / "runs/v2/dataset.json",{"manifest":{"profile":"v2"},"questions":questions})
            run={"run_id":"v2","model":"m","experiment_id":"Exp02","mode":"backtest",
                 "environment_id":"env02","total":124,"status":"queued"}
            card=experiment_catalog(root,[run])[0]
            self.assertTrue(card["full_backtest"])
            self.assertEqual(card["questions_per_model"],124)
            self.assertEqual(card["family_question_counts"],counts)
            self.assertEqual(card["benchmark_profile"],"v2")
            self.assertFalse(card["ready_for_leaderboard"])

    def test_publication_requires_every_model_success_and_complete_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = [{"run_id":model,"model":model,"experiment_id":"Exp01","mode":"backtest",
                     "protocol":"env01-v5","status":"completed","total":90,"succeeded":90,
                     "failed":0,"started_at":"2026-11-02"} for model in ("a","b")]
            write_json(root / "batches/Exp01.json", {"models":{"a":"a","b":"b"},"expected_total":180})
            self.assertFalse(experiment_catalog(root,runs)[0]["ready_for_leaderboard"])
            state={"status":"completed","validation":[{"run_id":"a","complete":True,"verified":90,"total":90}]}
            write_json(root / "batches/Exp01-execution.json",state)
            self.assertFalse(experiment_catalog(root,runs)[0]["ready_for_leaderboard"])
            state["validation"].append({"run_id":"b","complete":True,"verified":90,"total":90})
            write_json(root / "batches/Exp01-execution.json",state)
            self.assertTrue(experiment_catalog(root,runs)[0]["ready_for_leaderboard"])
            runs[1].update(succeeded=89,failed=1,status="partial")
            self.assertFalse(experiment_catalog(root,runs)[0]["ready_for_leaderboard"])

    def test_only_explicit_experiments_get_cards(self):
        with tempfile.TemporaryDirectory() as directory:
            rows=[{"run_id":str(i),"model":"m","mode":"backtest","status":"completed","total":1,
                   "succeeded":1,"protocol":protocol,"started_at":"2026-11-0"+str(i)}
                  for i,protocol in enumerate(("env01-v3","env01-v4","env01-v5"),1)]
            self.assertEqual(experiment_catalog(Path(directory),rows),[])
            rows.append(dict(rows[-1],run_id="new",experiment_id="Exp01"))
            cards=experiment_catalog(Path(directory),rows)
            self.assertEqual(len(cards),1)
            self.assertEqual({c["name"] for c in cards},{"Exp01"})
            self.assertFalse(any(c["ready_for_leaderboard"] for c in cards))
            self.assertEqual(len(rows),4,'Unnamed runs remain available in model history')

    def test_exp00_appears_after_exp01_and_records_dependency_and_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[{"run_id":name,"experiment_id":name,"model":"m","mode":"backtest","total":90,
                   "status":"waiting_dependency" if name=="Exp00" else "running",
                   "environment_id":"env00" if name=="Exp00" else "env01","started_at":day}
                  for name,day in (("Exp01","2026-11-02"),("Exp00","2026-11-03"))]
            write_json(root/'batches/Exp00.json',{"phase":"waiting_dependency","depends_on_experiment":"Exp01"})
            cards=experiment_catalog(root,rows)
            self.assertEqual([c['id'] for c in cards],['Exp01','Exp00'])
            self.assertEqual(cards[1]['status'],'waiting_dependency')
            self.assertEqual(cards[1]['news_policy'],'disabled')
            self.assertEqual(cards[1]['depends_on_experiment'],'Exp01')


if __name__ == "__main__":
    unittest.main()
