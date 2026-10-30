"""Portable stdlib-only core boundary tests. No Excel/user settings required."""
import sys,unittest,copy,zlib,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import model,allocation,one_box_exception as box,storage_chunks,work_repository as wr

def row(**kw):
    d=dict(A='测试',B='T',D='商品',E='SKU',H=1.71,I=14.,J=38.,K=0.,L=24.,N=None,O=24.,V=6.8,S='启用',AB=.2,AC=.4,AD=.3,AG=8.,AH=4.,AI=15.,AJ=5.)
    d.update(kw);return d
META={'match':'商品编码精确匹配','blocking':[],'export_status':'正常'}

class ModelTests(unittest.TestCase):
    def test_screenshot(self):
        d=model.calculate(row());self.assertEqual((d['BJ'],d['BK'],d['AR'],d['BP'],d['AS']),(24,0,0,0,0));self.assertEqual(d['_inbound_cap']['floor_before'],24)
    def test_both_zero_holding_and_cartons(self):
        for step in (1,2,6,24,40):
            for sales in ((.07,1,1),(1.71,14,38),(5,35,150)):
                d=model.calculate(row(O=step,L=0,H=sales[0],I=sales[1],J=sales[2]));self.assertGreater(d['AS'],0);self.assertEqual(d['AS']%step,0);self.assertNotIn('_inbound_cap',d)
    def test_disabled_cap_and_recompute_clear_audit(self):
        d=model.calculate(row());n=model.calculate(d,model.Rules(cap_days=0));self.assertGreater(n['AS'],0);self.assertNotIn('_inbound_cap',n)
    def test_stockout_conflict_only_discards_N(self):
        a=model.calculate(row(N=31,L=0));b=model.calculate(row(N=0,L=0));self.assertEqual(a['N'],31);self.assertEqual(a['BA'],0)
        for k in ['W','Z','AA','AN','AM','AS']:self.assertEqual(a[k],b[k])
    def test_zero_and_extreme_restore(self):
        for n in (4,30,45):
            d=model.calculate(row(H=0,I=0,J=0,K=0,L=0,N=n,O=6));self.assertEqual(d['AS'],6);self.assertNotIn('_inbound_cap',d)
    def test_missing_data_not_zero(self):
        d=model.calculate(row(J=None));self.assertIsInstance(d['AS'],str)
    def test_box_wrapper_does_not_readd(self):
        d=model.calculate(row());d.update(BI=1,AN=10,AM=1,AH=5)
        out,a=box.apply_one_box_exception(d,META);self.assertEqual(out['AS'],0);self.assertFalse(a['applied']);self.assertIn('已有在途',a['reason'])
        old={k:v for k,v in d.items() if k!='_inbound_cap'};out,a=box.apply_one_box_exception(old,META);self.assertFalse(a['applied']);self.assertTrue(a['inbound_cap_blocked'])
        old['L']=0;out,a=box.apply_one_box_exception(old,META);self.assertTrue(a['applied']);self.assertEqual(out['AS'],24)
        out,a=box.apply_one_box_exception(old,META,enabled=False);self.assertEqual(out['AS'],0)
    def test_small_inbound_cap_agrees_with_post_validation(self):
        import input_adapter
        d=model.calculate(row(H=.1,I=1,J=1,L=.5,O=24));d['_meta']={'export_row':2}
        self.assertIn('_inbound_cap',d);self.assertEqual(d['AS'],0)
        self.assertEqual(input_adapter.business_checks([d],model.Rules())['failures'],[])
        d['AS']=24;self.assertTrue(input_adapter.business_checks([d],model.Rules())['failures'])
    def test_fractional_multiples_generated_by_model(self):
        for step in (.1,.3,1.,6.,24.):
            for stock in (0.,1.,10.):
                d=model.calculate(row(O=step,K=stock,L=0));self.assertAlmostEqual(d['AS']/step,round(d['AS']/step),places=8)

class BudgetTests(unittest.TestCase):
    def test_priority_exact_budget_and_stable_tie(self):
        base=dict(_candidate_qty=6,_candidate_amount=12,V=2,BD=2,K=0,L=0,H=1,I=7,J=30,AN=1,AM=1,AH=5)
        rows=[dict(base,E='b',_must_sell=False),dict(base,E='must',_must_sell=True),dict(base,E='a',_must_sell=False)]
        before=copy.deepcopy(rows);receipt=allocation.apply(rows,16)
        self.assertEqual([d['_allocated_qty'] for d in rows],[0.,6.,2.]);self.assertEqual(receipt['spent'],16)
        for a,b in zip(before,rows):
            for k in a:self.assertEqual(a[k],b[k])
    def test_zero_budget_and_invalid_step_fail_closed(self):
        d=dict(E='x',_candidate_qty=1,_candidate_amount=1,V=1,BD=1,K=0,L=0,H=1,I=7,J=30,AN=1,AM=1,AH=5)
        allocation.apply([d],0);self.assertEqual(d['_allocated_qty'],0);self.assertTrue(d['_holding_gap'])
        for q in (1.000001,.999999):
            with self.assertRaises(ValueError):allocation.apply([dict(d,_candidate_qty=q)],10)

class IntegrityTests(unittest.TestCase):
    def test_legacy_checksum_runtime_not_assert(self):
        blob=zlib.compress(wr.dumps({'hello':1}).encode())
        with self.assertRaisesRegex(ValueError,'校验'):storage_chunks.load(blob,expected='0'*64)

if __name__=='__main__':unittest.main(verbosity=2)
