import copy,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import warehouse_orders as wo,normal_exports as ne,store_defaults as sd,parameter_schema as schema


def row(sku,wh,qty=2,new=False,**extra):
    return dict(E=sku,D=sku,AU=wh,_source='资料匹配',_meta={'blocking':[]},AS=qty,BV=qty*3,V=3,BD=1,J=0 if new else 30,K=0,L=0,N=0,**extra)

class WarehouseTests(unittest.TestCase):
    def test_partition_preserves_order_excludes_local_disabled_and_zero_new(self):
        rows=[row('s1','山东仓'),row('h1','河北仓'),row('s2','山东仓'),row('zero','河北仓',0,True),row('local','山东仓'),row('off','河北仓',S='禁用')];rows[4]['_source']='地采商品'
        before=copy.deepcopy(rows);p=wo.partitions({'rows':rows},None)
        self.assertEqual(list(p),['河北仓','山东仓']);self.assertEqual([d['E'] for d in p['山东仓']['中心仓动销补货清单']],['s1','s2']);self.assertEqual([d['E'] for d in p['河北仓']['往期新品']],['zero']);self.assertEqual(rows,before)
    def test_unknown_blocks_entire_store(self):
        with self.assertRaises(ValueError):wo.partitions({'rows':[row('x','未知')]},None)
    def test_no_empty_warehouse_file(self):
        self.assertEqual(list(wo.partitions({'rows':[row('n','山东仓',0,True)]},None)),['山东仓'])
        self.assertEqual(wo.partitions({'rows':[]},None),{})
    def test_filename_and_child_number(self):
        o={'day':'2026-10-30','store':'001','payload':{'result':{'store_name':'测试店'}}}
        self.assertEqual(ne.filename(o,'河北仓'),'2026-10-30测试店（精简版·河北仓）.xlsx');self.assertEqual(wo.child_number('BH-x-ZC','山东仓'),'BH-x-ZC-SD')
    def test_defaults_effective_config_and_budget_optin(self):
        c=schema.standard();p={'id':'standard','version':1,'config':copy.deepcopy(c)};c['warehouses']['山东仓']=[9,6]
        ui={'primary':'山东仓','secondary':'河北仓','mode':2,'amount':'3000','selected':False}
        v=sd.record(p,c,ui);self.assertEqual(v['preset']['config'],c);self.assertEqual(v['ui']['mode'],0);self.assertTrue(v['ui']['selected']);self.assertTrue(sd.matches(v,p,c,ui))
        v=sd.record(p,c,ui,v,include_budget=True);self.assertEqual(v['ui']['amount'],'3000');ui['amount']='5000';v2=sd.record(p,c,ui,v);self.assertEqual(v2['ui']['amount'],'3000')
        ui['primary'],ui['secondary']='河北仓','山东仓';v3=sd.record(p,c,ui,v2);self.assertEqual(v3['preset']['config']['warehouses'],c['warehouses'])
    def test_invalid_route_not_saved(self):
        c=schema.standard()
        with self.assertRaises(ValueError):sd.record({'config':c},c,{'primary':'山东仓','secondary':'山东仓'})
    def test_warehouse_feedback_status(self):
        e={s:d for s,d in [('a',row('a','山东仓')),('b',row('b','河北仓'))]};r=wo.feedback_status(e,{'a':0});self.assertEqual(r['山东仓']['status'],'已确认');self.assertEqual(r['河北仓']['status'],'尚无回传')
if __name__=='__main__':unittest.main()
