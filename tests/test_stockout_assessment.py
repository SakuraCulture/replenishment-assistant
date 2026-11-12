"""Mandatory assessment exclusions, old persisted facts, cards and exports."""
import sys,tempfile,threading,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from stockout_tags import assessment
from tests.test_warehouse_stockout import workbook,HEADERS
import warehouse_stockout as wh
from warehouse_stockout_worker import execute
from stockout_common import today


class AssessmentTests(unittest.TestCase):
    def test_exact_tokens_blank_missing_and_near_names(self):
        for raw,want in [('淘便考核',1),('食品； 淘便考核 \n其他',1),('非淘便考核',0),('淘便考核候选',0),('',0),(None,None)]:
            with self.subTest(raw=raw):self.assertEqual(assessment(raw),want)
    def test_warehouse_existing_facts_reinterpreted_without_rewrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=wh.Repository(root);repo.initialize()
            rows=[['a','考核缺货',0,'启用','淘便考核','','类目','S','供货'],['b','考核有货',5,'启用','其他;淘便考核','','类目','S','供货'],['c','双标',0,'禁用','淘便考核','日配','类目','S','供货'],['d','普通负库存',-1,'启用','','','类目','S','供货'],['e','近似词',3,'启用','非淘便考核','','类目','S','供货']]
            result=execute(repo,'ingest',{'path':str(workbook(root/'source.xlsx',rows)),'warehouse':'山东仓'},threading.Event(),lambda _:None,lambda *_:None)
            with repo.connect(True) as db:
                db.execute("UPDATE facts SET excluded=0,bad=1,reason='' WHERE sku IN ('a','b')")
                db.execute("UPDATE snapshots SET version='warehouse-stockout-1'")
            before=repo.path.read_bytes();options={'start':today(),'end':today()};value=wh.query(repo,options)
            self.assertEqual((value['base'],value['zero'],value['excluded'],value['pending']),(2,1,3,0));self.assertEqual(value['rate'],.5)
            detail=wh.detail(repo,{**options,'mode':'excluded'})['rows'];self.assertEqual({r['sku'] for r in detail},{'a','b','c'})
            self.assertTrue(all('淘便考核' in r['reason'] for r in detail));self.assertEqual(repo.path.read_bytes(),before)
            self.assertEqual(sum(r['base'] for r in wh.cuts(repo,options,['supplier','category'])['rows']),2)


if __name__=='__main__':unittest.main()
