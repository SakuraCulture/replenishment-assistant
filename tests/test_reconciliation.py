"""Authorized 0.16 differences and immutable old-record regressions."""
import copy,sqlite3,unittest
from contextlib import closing
from pathlib import Path
import test_lifecycle as fixture
import lifecycle_io as io,order_lifecycle as lc,lifecycle_rules as rules
import work_repository as wr,openpyxl

class ReconciliationTests(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_ordered_zero_missing_and_extra_not_invented_sources(self):
        f=self.f;raw=f.scan(items=[{'商品编码':'C','门店确认数量':0},{'商品编码':'A','门店确认数量':None},{'商品编码':'X','门店确认数量':9}])['rows']
        products={s:{'name':s,'quantity':12} for s in ['A','B','C']}
        for r in raw:r['sources']=[r.pop('source')]
        rows=rules.reconcile(raw,products)
        self.assertEqual([r['sku'] for r in rows],['A','B','C','X'])
        self.assertEqual([r['base_quantity'] for r in rows],['0','0','0','9'])
        self.assertEqual([r['quantity_origin'] for r in rows],['blank','missing','zero','value'])
        self.assertEqual(rows[1]['sources'],[]);self.assertIsNone(raw[1]['quantity']);self.assertEqual(rows[-1]['difference'],None)
    def test_payment_receipt_positive_predecessor_and_missing_zero(self):
        f=self.f;f.save('return',[{'门店确认数量':6},{'商品编码':'X','门店确认数量':0},{'商品编码':'Y','门店确认数量':2}])
        p=f.plan(f.scan('payment',[{'付款单商品数量':4}]))
        self.assertEqual(list(p['plans'][0]['payload']['basis']['products']),['0001','Y'])
        self.assertEqual([(r['sku'],r['base_quantity']) for r in p['plans'][0]['payload']['rows']],[('0001','4'),('Y','0')]);lc.commit(f.path,p)
        p=f.plan(f.scan('receipt',[{'实际收货数量':None}]))
        self.assertEqual(list(p['plans'][0]['payload']['basis']['products']),['0001']);lc.commit(f.path,p)
        row=f.view()['rows'][0];self.assertEqual(row[3:6],[6,4,0]);self.assertEqual(row[7:9],[-2,-4])
    def test_upstream_changes_after_preview_reject_no_new_payment(self):
        f=self.f;f.save();p=f.plan(f.scan('payment'));f.save('return',[{'门店确认数量':2}])
        with self.assertRaisesRegex(wr.Conflict,'上游'):lc.commit(f.path,p)
        self.assertEqual(lc.history(f.path,f.h,'payment'),[])
    def test_saved_dependency_is_stale_until_explicit_recheck(self):
        f=self.f;f.save();f.save('payment');f.save('receipt');old=f.view()['documents']['payment'];f.save('return',[{'门店确认数量':3}])
        v=f.view();self.assertEqual(v['next'],'payment');self.assertEqual(v['documents']['payment'],old)
        for s in ('payment','receipt'):
            p=lc.recheck(f.path,f.h,s);lc.commit(f.path,p)
        self.assertIsNone(f.view()['next']);self.assertEqual(f.view()['rows'][0][3:6],[3,5,5])
    def test_recheck_uses_archived_original_after_source_removed(self):
        f=self.f;f.save();p,_=f.save('payment');Path(p['files'][0]['path']).unlink();f.save('return',[{'门店确认数量':3}])
        lc.commit(f.path,lc.recheck(f.path,f.h,'payment'));self.assertTrue(f.view()['health']['payment'])
    def test_all_zero_without_fabricated_next_documents(self):
        f=self.f;p=lc.zero_plan(f.path,f.h,'return');lc.commit(f.path,p);v=f.view()
        self.assertIsNone(v['next']);self.assertIn('无要货',v['note']);self.assertIsNone(v['documents']['payment']);self.assertIsNone(v['documents']['receipt'])
        self.assertEqual(f.read()['records'][0]['note'],v['note'])
    def test_pending_keeps_facts_but_never_invents_missing_zero(self):
        f=self.f;f.save('payment',[{'商品编码':'X','付款单商品数量':3}]);v=f.view();self.assertIsNone(v['rows'][0][4]);self.assertTrue(v['documents']['payment']['payload']['pending'])
        f.save();self.assertEqual(f.view()['next'],'payment');lc.commit(f.path,lc.recheck(f.path,f.h,'payment'))
        self.assertEqual(f.view()['rows'][0][4],0)
    def test_old_schema_one_missing_read_unchanged(self):
        f=self.f;f.save('return',[{'商品编码':'X'}]);doc=f.view()['documents']['return'];p=copy.deepcopy(doc['payload']);p['schema']=1;p['rows']=p['raw_rows']
        for key in ('raw_rows','basis','rule','pending','explicit_zero'):p.pop(key,None)
        with closing(sqlite3.connect(f.path)) as db,db:
            db.execute('UPDATE lifecycle_versions SET digest=?,payload=?',(lc.fingerprint(p),wr.dumps(p)))
        before=f.path.read_bytes();v=f.view();self.assertIsNone(v['rows'][0][3]);self.assertEqual(f.path.read_bytes(),before)
        lc.commit(f.path,lc.recheck(f.path,f.h,'return'));history=lc.history(f.path,f.h,'return');self.assertEqual(history[1]['payload'],p);self.assertEqual(f.view()['rows'][0][3],0)
    def payment(self,qty=48,sku='0001',aux=''):
        f=self.f;p=f.root/'source-paid.xlsx';b=openpyxl.Workbook();s=b.active;s.append(io.PAYMENT_HEADERS)
        s.append(['1298602','S261005150224909','测试店','001588','2026-10-05 15:01:24','线下店铺',sku,'商品',24,qty,1.3,62.4,aux]);b.save(p);b.close()
        info=io.inspect([p])[0];return io.scan('payment',io.payment_specs(info))
    def test_payment_original_format_no_time_code_unit_columns_required(self):
        f=self.f;f.save('return',[{'门店确认数量':48}]);scan=self.payment();row=scan['rows'][0]
        self.assertEqual(row['base_quantity'],'48');self.assertEqual(row['amount'],'62.4');self.assertIsNone(row['document_date']);self.assertEqual(row['currency'],'');self.assertEqual(row['source_order']['线上订单号'],'S261005150224909')
        p=lc.prepare(f.path,scan,{lc.binding_key(row):f.h});lc.commit(f.path,p)
        self.assertEqual(f.view()['rows'][0][4],48);self.assertEqual(f.view()['rows'][0][9],'62.4')
    def test_blank_payment_source_kept_and_zero_effect(self):
        f=self.f;f.save();scan=self.payment(None);p=lc.prepare(f.path,scan,{lc.binding_key(scan['rows'][0]):f.h})
        self.assertEqual(p['plans'][0]['payload']['rows'][0]['base_quantity'],'0');self.assertIsNone(p['plans'][0]['payload']['raw_rows'][0]['quantity'])
    def test_formula_invalid_quantity_and_missing_column_not_zero(self):
        f=self.f
        for q in ('abc',-1,'=SUM(1,2)'):
            with self.subTest(q=q),self.assertRaises(ValueError):f.scan(items=[{'门店确认数量':q}])
        scan=f.scan();info=io.inspect([scan['files'][0]['path']])[0];s=info['sheets'][0];mapping={h:i for i,h in enumerate(s['headers'])};mapping['门店确认数量']=None
        with self.assertRaisesRegex(ValueError,'字段映射'):io.scan('return',[{**info,**s,'mapping':mapping}])
    def test_home_cache_updates_versions_and_no_cross_warehouse_effect(self):
        f=self.f;f.save();f.save('payment');f.read();f.save('return',[{'门店确认数量':2}]);records=f.read()['records']
        self.assertEqual(records[0]['next'],'payment');self.assertIn('重新核对',records[0]['note']);self.assertEqual(records[1]['status'],'待回传')
    def test_old_feedback_can_be_payment_basis_without_rewriting(self):
        f=self.f
        with closing(sqlite3.connect(f.path)) as db,db:
            db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)');db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',('oid',1,1,wr.now(),wr.dumps({'values':{'0001':8},'warehouses':{}})))
        before=f.old_tables();f.save('payment');self.assertEqual(f.view()['next'],'receipt');self.assertEqual(f.old_tables(),before)

if __name__=='__main__':unittest.main()
