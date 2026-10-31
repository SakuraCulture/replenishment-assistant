import copy,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import model,allocation,input_adapter,engine
from test_rules import row
from quantity_contract import packs
from source_binding import bindings,restore
from table_records import bridge_table
from conflict_choices import proposals
from parameter_view import comparison

class ReviewTests(unittest.TestCase):
    def test_generated_fractional_step_budget(self):
        for step in (.01,.03,.1,.2,.3,.6,.7,1,6,24):
            for stock in (0,1,2,5,10,25):
                d=model.calculate(row(O=step,K=stock,L=0));original=copy.deepcopy(d)
                d.update(_candidate_qty=d['AS'],_candidate_amount=d['BV'],_must_sell=False)
                r=allocation.apply([d],10000)
                self.assertEqual(d['_allocated_qty'],float(allocation.dec(step)*packs(original['AS'],step)))
                self.assertEqual(d['_budget_gap'],0);self.assertEqual(d['AS'],original['AS']);self.assertLessEqual(r['spent'],10000)
                d['_meta']={'export_row':2};self.assertFalse(input_adapter.business_checks([d],model.Rules())['failures'])
    def test_real_mismatch_not_silently_normalized(self):
        for q in (1.000001,.999999,-1,float('nan')):
            with self.assertRaises(ValueError):packs(q,1)
        with self.assertRaises(ValueError):packs(1,0)
        self.assertEqual(packs(.30000000000000004,.1),3)
    def test_normalization_does_not_survive_reallocation(self):
        d=dict(_candidate_qty=.3*77,_candidate_amount=23.1,BD=.3,V=1,H=1,I=7,J=30,K=0,L=0,AN=1,AM=1,AH=5,E='x')
        allocation.apply([d],100);self.assertIn('_quantity_normalization',d)
        d['_candidate_qty']=3;allocation.apply([d],100);self.assertNotIn('_quantity_normalization',d)
    def test_z_zero_has_explicit_reason_not_invented_forecast(self):
        d=row(H=1,I=14,J=14+1e-12,O=1,L=0)
        with self.assertRaisesRegex(ValueError,'前7天日均Z为0'):model.calculate(d)
        r=input_adapter.execute_one(d,{'blocking':[],'export_row':2},model.Rules())
        self.assertEqual(r['AS'],'计算异常');self.assertIn('诊断号',r['BM']);self.assertNotEqual(r['AS'],0)
    def test_source_map_detects_reorder_and_wrong_identity(self):
        records=[{'values':{'仓库/门店编码':'0001','商品编码':s},'path':'x','row':i+20,'sheet':'S'} for i,s in enumerate(('0002','longcode'))]
        headers=list(records[0]['values']);table=bridge_table(headers,[r['values'] for r in records],'销售')
        mapping=bindings(table,records,headers)
        self.assertIs(restore(mapping,{'B':'0001','E':'longcode'},{'export_row':3}),records[1])
        with self.assertRaises(ValueError):restore(mapping,{'B':'0001','E':'0002'},{'export_row':3})
        with self.assertRaises(ValueError):bindings((list(reversed(table[0])),table[1]),records,headers)
        with self.assertRaises(ValueError):bindings((table[0][:1],table[1]),records,headers)
    def test_bulk_proposal_never_guesses_duplicate_source(self):
        cs=[{'variants':[{'path':'A'},{'path':'B'}]},{'variants':[{'path':'A'},{'path':'A'}]},{'variants':[{'path':'A'},{'path':'B'}]}]
        self.assertEqual(proposals(cs,[None,None,1],'A',{0,1,2}),[(0,0)])
        self.assertEqual(proposals(cs,[None,None,1],'A',{2},True),[(2,0)])
        self.assertEqual(proposals(cs,[None,None,1],'A',set()),[])
    def test_effective_values_show_overrides_without_mutation(self):
        base={'parameters':{'cap_days':21,'depth':8}};current={'parameters':{'cap_days':20,'depth':8}};edited={'parameters':{'cap_days':19,'depth':8}}
        rows=comparison(base,base,current,edited)
        self.assertEqual(rows[0][1:5],[21,21,20,19]);self.assertEqual(rows[0][5],'本次编辑');self.assertIn('各仓',rows[1][-1]);self.assertEqual(current['parameters']['cap_days'],20)
    def test_single_archive_read_equals_legacy(self):
        import openpyxl
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'sample.xlsx';b=openpyxl.Workbook();s=b.active;s.append(['仓库/门店编码','商品编码','日销','周销','月销','库存单位']);s.append(['0001','00002',1,7,30,'包']);s.append([None]*6);s.append(['0001','x',0,0,0,'包']);b.save(p);b.close()
            sheet,heads=engine.sheet_name(p,'sales');old=input_adapter.read_table(p,sheet,['商品编码'])
            name,newheads,rows,info=engine.read_identified(p,'sales',['商品编码']);self.assertEqual((sheet,heads,old),(name,newheads,(rows,info)));self.assertEqual(rows[1]['_row'],4)
if __name__=='__main__':unittest.main()
