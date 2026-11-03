import copy,sys,tempfile,unittest,sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import admission as a
import engine,parameter_schema as ps,batch_service as bs,result_semantics as sem
import work_repository as wr,normal_orders as no
from test_rules import row
from test_preissue import row as execution_row,result as execution_result,edit
import preissue,new_products

class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);self.work=wr.Repository(self.root);self.repo=no.Repository(self.work)
    def tearDown(self):self.tmp.cleanup()
    def context(self,record=None):
        value=record or {};return {'version':a.VERSION,'store':'T','day':'2026-11-03','revision':a.digest(value),'record':value}
    def qualified(self,expires=None):return {'qualification':{'state':'已核验','reviewer':'合成核验人','verified_on':'2026-11-01','expires':expires}}
    def test_multitag_raw_preserved_and_unknown(self):
        raw=' 必卖清单,二类医疗器械；冬季商品\n二类医疗器械，活动品 '
        v=a.check(self.context(),'sku',{'product_tags':raw,'product_tags_known':True})
        self.assertEqual(v['tags'],['必卖清单','二类医疗器械','冬季商品','活动品']);self.assertEqual(v['tags_raw'],raw)
        self.assertEqual(v['reasons'],['二类医疗资质待核验'])
    def test_states_expiry_and_priority(self):
        meta={'product_tags':'二类医疗','product_tags_known':True}
        for state in ('待核验','无资质'):
            self.assertEqual(a.check(self.context({'qualification':{'state':state}}),'x',meta)['reasons'],['二类医疗资质'+state])
        self.assertFalse(a.check(self.context(self.qualified('2026-11-03')),'x',meta)['reasons'])
        self.assertIn('已过期',a.check(self.context(self.qualified('2026-11-02')),'x',meta)['reasons'][0])
    def test_refusal_boundary_and_release(self):
        record={'refusals':{'x':{'reason':'本店拒收','expires':'2026-11-03','active':True}}};meta={'product_tags_known':True,'product_tags':''}
        self.assertTrue(a.check(self.context(record),'x',meta)['reasons'])
        c=self.context(record);c['day']='2026-11-04';self.assertFalse(a.check(c,'x',meta)['reasons'])
        record['refusals']['x']['expires']=None;self.assertTrue(a.check(self.context(record),'x',meta)['reasons'])
        record['refusals']['x']['active']=False;self.assertFalse(a.check(self.context(record),'x',meta)['reasons'])
    def test_blank_and_absent_tag_are_distinct(self):
        self.assertFalse(a.check(self.context(),'x',{'product_tags':None,'product_tags_known':True})['reasons'])
        self.assertTrue(a.check(self.context(),'x',{})['reasons'])
    def test_crosswarehouse_medical_requirement(self):
        from test_jst import catalog
        hb=catalog();sd=catalog('山东仓');hb['items']['0001']['商品标签']='二类医疗器械';sd['items']['0001']['商品标签']=''
        meta=a.catalog_meta({'河北仓':hb,'山东仓':sd},'山东仓','0001')
        self.assertTrue(a.check(self.context(),'0001',meta)['requires_medical'])
        self.assertEqual(meta['product_tags'],'');self.assertEqual(meta['medical_sources'][0]['warehouse'],'河北仓')
    def test_atomic_save_and_revision_rejection(self):
        c=a.snapshot(self.work.path,'T');v=self.qualified();a.save(self.work.path,{'T':v},{'T':{}})
        with self.assertRaises(wr.Conflict):a.save(self.work.path,{'T':{}},{'T':{}})
        self.assertEqual(a.snapshot(self.work.path,'T')['record'],v);self.assertEqual(c['record'],{})
    def calculation(self,context=None):
        d=row(B='T',E='x',L=0);d.update(_source='资料匹配',AU='河北仓',U='快销',AT='未提供（不作为数量拦截）')
        m={'match':'商品编码精确匹配','blocking':[],'notes':[],'category':'快销','product_tags':'二类医疗,必卖清单','product_tags_known':True,'export_row':2,'export_status':'正常'}
        loaded={'normalized':[(d,m)],'config':engine.config(),'stores':{'T':'合成门店'},'hashes':{}}
        settings={'warehouses':ps.standard()['warehouses'],'exception':True,'budget':None}
        if context:settings['admission']=context
        return bs.calculate_store(loaded,'T',settings,None)
    def test_model_unchanged_execution_excluded_and_all_reasons(self):
        old=self.calculation();new=self.calculation(self.context());x,y=old['rows'][0],new['rows'][0]
        self.assertGreater(x['AS'],0);self.assertEqual(y['AS'],x['AS']);self.assertEqual(y['BV'],x['BV']);self.assertEqual(sem.order_quantity(y),0)
        self.assertEqual(y['_candidate_qty'],0)
        for key,value in x.items():
            if key not in ('AT','_meta','_candidate_qty','_candidate_amount'):self.assertEqual(value,y[key],key)
        y['S']='禁用';y['_meta']['export_status']='停采'
        self.assertTrue(sem.exclusion_reason(y).startswith('二类医疗'));self.assertIn('禁用',sem.exclusion_reason(y));self.assertIn('停采',sem.exclusion_reason(y))
    def test_manual_new_and_floor_cannot_bypass(self):
        d=execution_row();d['_meta']['admission']=a.check(self.context(),'a',{'product_tags':'二类医疗','product_tags_known':True})
        r=execution_result([d]);r['rows'][0]['_candidate_qty']=0
        with self.assertRaises(ValueError):preissue.preview(r,{'a':edit(48)},a.today())
        d.update(H=0,I=0,J=0,K=0,L=0,N=0);r=execution_result([d])
        with self.assertRaises(ValueError):new_products.apply(r,{'a':24})
    def test_sign_stale_refusal_and_historical_read(self):
        a.save(self.work.path,{'T':self.qualified()},{'T':{}});c=a.snapshot(self.work.path,'T');r=self.calculation(c)
        p={'schema':1,'day':wr.today(),'model_version':no.MODEL_VERSION,'input_dirty':False,'results':{'T':r},'states':{'T':{'state':'已完成','name':'合成'}}}
        order=self.repo.confirm(p,['T'],None,{'T':None})[0]
        updated=self.qualified();updated['refusals']={'x':{'reason':'不要','active':True}}
        a.save(self.work.path,{'T':updated},{'T':self.qualified()})
        old=self.repo.get(wr.today(),'T',order['revision']);self.assertEqual(old['payload']['result']['rows'][0]['AS'],r['rows'][0]['AS'])
        with self.assertRaisesRegex(ValueError,'准入|拒收'):self.repo.confirm(p,['T'],self.work.get_job(wr.today())['revision'],{'T':order['revision']})
    def test_jst_no_stock_reserved_for_blocked(self):
        from test_jst import catalog
        import jst_allocation as alloc
        balances={};rows=alloc.allocate([{'sku':'0001','base_quantity':'24','_admission_error':'门店拒收'}],catalog()['items'],balances)
        self.assertEqual(rows[0]['quantity'],'0');self.assertEqual(balances,{})
    def test_backup_and_metadata_are_recorded_side_effects(self):
        a.save(self.work.path,{'T':self.qualified()},{'T':{}})
        with closing(sqlite3.connect(self.work.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM admission_events').fetchone()[0],1)

    def test_checked_jst_preview_rejects_changed_admission_and_catalog(self):
        from test_jst import Service,catalog
        import jst_orders as j,warehouse_catalog as wc
        f=Service();f.setUp()
        try:
            wc.Repository(f.path.parent).save(catalog())
            p=j.prepare_checked(f.path,[f.h]);self.assertEqual(p['payload']['plans'][0]['rows'][0]['quantity'],'40')
            a.save(f.path,{'T':{'refusals':{'0001':{'reason':'本店不收','active':True}}}},{'T':{}})
            result=j.commit(f.path,p);self.assertFalse(result['generated']);self.assertTrue(result['errors']);self.assertEqual(f.balances(),100)
            p=j.prepare_checked(f.path,[f.h]);self.assertEqual(p['payload']['plans'][0]['rows'][0]['quantity'],'0')
            result=j.commit(f.path,p,accept_issues=True);self.assertEqual(f.balances(),100)
            a.save(f.path,{'T':{}},{'T':a.snapshot(f.path,'T')['record']})
            p=j.prepare_checked(f.path,[f.h]);changed=catalog();changed['items']['0001']['商品标签']='二类医疗';changed['revision']=wc.revision(changed['items']);wc.Repository(f.path.parent).save(changed)
            self.assertTrue(j.commit(f.path,p)['errors'])
        finally:f.tearDown()

    def test_import_blank_expiry_preserves_source_and_other_refusals(self):
        from openpyxl import Workbook
        from admission_ui import read_import
        p=self.root/'拒收.xlsx';b=Workbook();b.active.append(['拒收原因','商品编码','门店编码','到期日']);b.active.append(['不要','0001','T',None]);b.save(p);b.close()
        value=read_import(p,'store_refusals')['rows'][0]['value']
        self.assertIsNone(value['expires']);self.assertEqual(value['source']['row'],2);self.assertEqual(value['source']['original']['商品编码'],'0001')

if __name__=='__main__':unittest.main()
