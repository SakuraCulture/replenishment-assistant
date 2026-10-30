import copy,unittest,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import allocation

def sku(code,qty=1,step=1,price=5,must=False):
    return dict(E=code,_candidate_qty=qty,_candidate_amount=qty*price,BD=step,V=price,_must_sell=must,H=1,I=7,J=30,K=0,L=0,AN=1,AM=1,AH=5)

class OverrunTests(unittest.TestCase):
    def test_exact_five_percent_one_only_and_stable_order(self):
        rows=[sku('target',10,1,10,True),sku('B'),sku('A')];before=copy.deepcopy(rows)
        strict=copy.deepcopy(rows);allocation.apply(strict,100,allow_overrun=False)
        r=allocation.apply(rows,100)
        self.assertEqual([d['_allocated_qty'] for d in strict],[10,0,0]);self.assertEqual([d['_allocated_qty'] for d in rows],[10,0,1])
        self.assertEqual(r['spent'],105);self.assertEqual(r['remaining'],-5);self.assertEqual(r['overrun']['sku'],'A')
        for a,b in zip(before,rows):
            for k,v in a.items():self.assertEqual(v,b[k])
    def test_above_five_percent_rejected(self):
        rows=[sku('target',10,1,10,True),sku('extra',price=5.01)]
        r=allocation.apply(rows,100);self.assertEqual(r['spent'],100);self.assertIsNone(r['overrun']['sku'])
    def test_zero_never_buys(self):
        rows=[sku('x')];r=allocation.apply(rows,0);self.assertEqual(r['spent'],0);self.assertEqual(rows[0]['_allocated_qty'],0)
    def test_whole_carton_not_one_piece(self):
        rows=[sku('target',10,1,10,True),sku('carton',qty=6,step=6,price=.8)]
        r=allocation.apply(rows,100);self.assertEqual(rows[1]['_allocated_qty'],6);self.assertEqual(r['spent'],104.8)
    def test_no_candidate_increase_or_free_budget(self):
        rows=[sku('full',qty=10,price=10,must=True),sku('excluded',qty=0)]
        r=allocation.apply(rows,100);self.assertEqual(r['spent'],100);self.assertIsNone(r['overrun']['sku'])
    def test_positive_remaining_can_finish_a_unit(self):
        rows=[sku('target',qty=9,price=10,must=True),sku('finish',price=14)]
        r=allocation.apply(rows,100);self.assertEqual(r['spent'],104);self.assertEqual(r['overrun']['amount'],4)
    def test_reallocation_clears_audit(self):
        rows=[sku('target',qty=10,price=10,must=True),sku('finish')];allocation.apply(rows,100)
        allocation.apply(rows,200);self.assertNotIn('_budget_overrun',rows[1]);self.assertEqual(rows[1]['_allocated_qty'],1)
    def test_pending_price_does_not_get_extra(self):
        rows=[sku('target',qty=10,price=10,must=True),sku('unpriced')];rows[1]['V']=None
        r=allocation.apply(rows,100);self.assertEqual(r['spent'],100);self.assertEqual(rows[1]['_allocated_qty'],0)
if __name__=='__main__':unittest.main()
