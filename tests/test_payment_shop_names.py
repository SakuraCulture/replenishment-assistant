"""Isolated names configuration and real payment prepare/commit contracts."""
import copy,json,sqlite3,sys,threading,unittest,time
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
import openpyxl
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests')]
import test_payment_matching as fixtures
import payment_shop_names as names,payment_shop_names_io as xlsx,payment_matching as pm,order_lifecycle as lc,work_repository as wr
from runtime_support import Cancelled

class ShopNamesTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.PaymentMatchingTests();self.f.setUp();self.path=self.f.path;self.h=self.f.target
    def tearDown(self):self.f.tearDown()
    def data(self):return names.load(self.path)
    def change(self,value='聚水潭独立店名',head=None):
        h=head or self.h;return names.save(self.path,names.prepare(self.data(),[{'store':h['store'],'warehouse':h['warehouse'],'value':value}]))
    def plan(self,doc):return pm.prepare(self.path,[doc],None,pm.build(self.path),None)
    def analyze(self,doc):return pm.analyze(self.path,doc,pm.build(self.path))
    def dump_other(self):
        with closing(sqlite3.connect(self.path)) as db:
            return {t:list(db.execute('SELECT * FROM "'+t+'" ORDER BY rowid')) for (t,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'") if t!='lifecycle_mappings'}
    def test_only_names_written_and_two_warehouses_have_independent_cas(self):
        before=self.dump_other();data=self.data();wh=self.h['warehouse'];other=next(w for w in names.WAREHOUSES if w!=wh)
        a=names.prepare(data,[{'store':self.h['store'],'warehouse':wh,'value':'A'}]);b=names.prepare(data,[{'store':self.h['store'],'warehouse':other,'value':'B'}])
        names.save(self.path,a);names.save(self.path,b)
        self.assertEqual(before,self.dump_other());self.assertEqual(len(self.data()['records']),2)
        with self.assertRaises(wr.Conflict):names.save(self.path,a)
        lc.mappings(self.path,'payment','普通字段映射',{'headers':['SKU'],'mapping':{},'defaults':{}})
        self.assertEqual(list(lc.mappings(self.path,'payment')),['普通字段映射'])
        with self.assertRaisesRegex(ValueError,'独立付款资料'):lc.mappings(self.path,'payment',names.key(self.h['store'],wh),{})
    def test_migrated_orders_without_defaults_still_show_application_store_names(self):
        data=self.data();self.assertEqual(data['names'][self.h['store']],self.h['name'])
    def test_blank_keep_clear_noop_and_cancel_or_failure_rolls_back(self):
        self.change();data=self.data();before=self.path.read_bytes()
        self.assertEqual(names.save(self.path,names.prepare(data,[{'store':self.h['store'],'warehouse':self.h['warehouse'],'value':''}]))['changed'],0)
        self.assertEqual(self.path.read_bytes(),before)
        p=names.prepare(data,[{'store':self.h['store'],'warehouse':self.h['warehouse'],'value':''}],clear=True)
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):names.save(self.path,p,cancel=stop)
        with self.assertRaisesRegex(RuntimeError,'injected'):
            names.save(self.path,p,progress=lambda _:(_ for _ in ()).throw(RuntimeError('injected')))
        self.assertEqual(self.data(),data)
        names.save(self.path,p);self.assertEqual(self.data()['records'][names.key(self.h['store'],self.h['warehouse'])]['name'],'')
        with self.assertRaises(ValueError):names.prepare(data,[{'store':self.h['store'],'warehouse':self.h['warehouse'],'value':'x','budget':0}])
    def test_template_roundtrip_unknown_duplicate_extra_columns_and_no_side_effect_on_read(self):
        self.change('=文本店名');data=self.data();file=self.f.f.root/'names.xlsx';xlsx.export(file,data)
        with self.assertRaises(ValueError):xlsx.export(file,data)
        imported=xlsx.read(file,data);self.assertTrue(all(r['status']=='保持原值' for r in imported['rows']));self.assertEqual(xlsx.prepare(data,imported,list(range(len(imported['rows']))))['changes'],[])
        w=openpyxl.load_workbook(file);s=w.active;s.cell(1,5,'预算');s.cell(2,5,999);s.append(['UNKNOWN','未知','坏名称']);s.append([s.cell(2,1).value,'重复','重复']);w.save(file);w.close()
        before=self.path.read_bytes();r=xlsx.read(file,data);self.assertIn('预算',r['notices'][0]);self.assertEqual(self.path.read_bytes(),before)
        with self.assertRaises(ValueError):xlsx.prepare(data,r,list(range(len(r['rows']))))
        p=xlsx.prepare(data,r,[i for i,v in enumerate(r['rows']) if not v['error']]);self.assertEqual(p['changes'],[])
    def test_text_store_code_leading_zero_and_formula_rejection(self):
        data={'names':{'001':'合成店'},'records':{}};file=self.f.f.root/'zeros.xlsx';xlsx.export(file,data)
        w=openpyxl.load_workbook(file);s=w.active;self.assertEqual(s.cell(2,1).value,'001');s.cell(2,3,'=1+1');w.save(file);w.close()
        self.assertIn('公式',xlsx.read(file,data)['rows'][0]['error'])
    def test_unique_configured_name_allows_quantity_and_money_differences(self):
        self.change();doc=self.f.document(name='聚水潭独立店名',change=lambda v:v.update(数量='7',商品金额='8.25'))
        r=self.analyze(doc);self.assertTrue(r['automatic']);self.assertEqual(r['proof'],'configured_name')
        plan=self.plan(doc);self.assertEqual(plan['plans'][0]['head']['number'],self.h['number'])
        raw=plan['plans'][0]['payload']['raw_rows'];self.assertEqual(raw[0]['quantity'],'7');self.assertEqual(raw[0]['amount'],'8.25')
        first=lc.commit(self.path,plan)[0];self.assertTrue(first['changed']);self.assertFalse(lc.commit(self.path,self.plan(doc))[0]['changed'])
        correction=self.f.document(name='聚水潭独立店名',change=lambda v:v.update(数量='9',商品金额='12.50'))
        self.assertEqual(self.analyze(correction)['proof'],'previous_source');lc.commit(self.path,self.plan(correction))
    def test_same_name_two_warehouses_allows_manual_choice_without_learning_warehouse(self):
        self.change('两仓同名');other=next(h for h in self.f.index['heads'] if h['store']==self.h['store'] and h['warehouse']!=self.h['warehouse']);self.change('两仓同名',other)
        doc=self.f.document(name='两仓同名',change=lambda v:v.update(商品金额='2.31'));r=self.analyze(doc)
        self.assertFalse(r['automatic']);self.assertFalse(r['prevent_rebind']);self.assertEqual(len(r['candidates']),2)
        doc['target']=self.h;lc.commit(self.path,self.plan(doc))
        self.assertEqual(self.analyze(doc)['proof'],'previous_source')
        new=self.f.document(name='两仓同名',internal='NEW',change=lambda v:v.update(商品金额='2.31'));self.assertEqual(len(self.analyze(new)['candidates']),2)
        self.assertFalse(self.analyze(new)['automatic']);self.assertEqual(pm.aliases(self.path),[])
    def test_cross_day_candidates_and_hidden_display_never_create_uniqueness(self):
        self.change();self.f.duplicate_previous_day();doc=self.f.document(name='聚水潭独立店名',change=lambda v:v.update(商品金额='2.31'))
        r=pm.analyze(self.path,doc,pm.build(self.path),scope=[self.h]);self.assertFalse(r['automatic']);self.assertEqual(len(r['name_context']['identities']),2)
        doc['target']=self.h;lc.commit(self.path,self.plan(doc))
    def test_only_relevant_name_change_invalidates_payment_preview(self):
        self.change();doc=self.f.document(name='聚水潭独立店名');plan=self.plan(doc)
        other=next(h for h in self.f.index['heads'] if h['store']!=self.h['store']);self.change('无关店名',other)
        lc.commit(self.path,plan)
        doc=self.f.document(internal='J2',name='聚水潭独立店名');plan=self.plan(doc);self.change('新名称')
        with self.assertRaisesRegex(wr.Conflict,'店铺名称对应已变化'):lc.commit(self.path,plan)
    def test_explicit_wrong_name_conflict_and_manual_outside_candidates_block(self):
        self.change();other=next(h for h in self.f.index['heads'] if h['store']!=self.h['store'])
        doc=self.f.document(name='聚水潭独立店名',headers=__import__('lifecycle_io').PAYMENT_HEADERS+['签发单号'],change=lambda v:v.update(签发单号=other['number'],数量='7',商品金额='8.25'))
        self.assertTrue(self.analyze(doc)['prevent_rebind'])
        doc=self.f.document(name='聚水潭独立店名',change=lambda v:v.update(商品金额='8.25'));doc['target']=other
        with self.assertRaises(ValueError):self.plan(doc)
    def test_payment_and_remembered_manual_link_rollback_together(self):
        doc=self.f.document(name='未配置别称',change=lambda v:v.update(商品金额='8.25'));doc['target']=self.h;p=self.plan(doc)
        with patch('payment_matching.apply',side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):lc.commit(self.path,p)
        with closing(pm.readonly(self.path)) as db:self.assertEqual(pm.mappings(db),{})
        lc.commit(self.path,p);self.assertTrue(self.analyze(doc)['automatic'])
    def test_second_store_same_shop_name_and_new_pair_after_preview_keep_ambiguity(self):
        self.change('共享名称');doc=self.f.document(name='共享名称',change=lambda v:v.update(商品金额='8.25'));plan=self.plan(doc)
        other=next(h for h in self.f.index['heads'] if h['store']!=self.h['store']);self.change('共享名称',other)
        with self.assertRaisesRegex(wr.Conflict,'店铺名称对应已变化'):lc.commit(self.path,plan)
        self.assertEqual(len(self.analyze(doc)['candidates']),2)
    def test_optional_application_name_with_explicit_warehouse_and_no_accounts(self):
        self.f.accounts_file.unlink();doc=self.f.document(change=lambda v:v.update(数量='7',商品金额='8.25'))
        for r in doc['scan']['rows']:r['warehouse']=self.h['warehouse']
        result=self.analyze(doc);self.assertTrue(result['automatic']);self.assertEqual(result['proof'],'application_name')
        lc.commit(self.path,self.plan(doc))
    def test_short_reader_at_commit_waits_without_replaying_changes(self):
        data=self.data();plan=names.prepare(data,[{'store':self.h['store'],'warehouse':self.h['warehouse'],'value':'短读锁后保存'}])
        ready=threading.Event();release=threading.Event()
        def reader():
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall();ready.set();release.wait(3);db.rollback()
        thread=threading.Thread(target=reader);thread.start();ready.wait(3)
        timer=threading.Timer(.6,release.set);timer.start()
        try:self.assertEqual(names.save(self.path,plan)['changed'],1)
        finally:release.set();thread.join();timer.cancel()
        self.assertEqual(self.data()['records'][names.key(self.h['store'],self.h['warehouse'])]['name'],'短读锁后保存')
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_mappings WHERE name LIKE ?',(names.HISTORY+'%',)).fetchone()[0],1)
    def test_long_reader_rolls_back_and_keeps_original_names(self):
        self.change();before=self.data();plan=names.prepare(before,[{'store':self.h['store'],'warehouse':self.h['warehouse'],'value':'不可部分保存'}])
        with closing(sqlite3.connect(self.path)) as db:
            db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall()
            with patch.object(names,'BUSY_SECONDS',.2),self.assertRaisesRegex(wr.Conflict,'本次名称未保存'):names.save(self.path,plan)
        self.assertEqual(self.data(),before)

if __name__=='__main__':unittest.main()
