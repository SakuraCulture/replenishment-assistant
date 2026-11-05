"""Portable frozen-order analytics fixtures; no user settings or external files."""
import copy
import hashlib
import io
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import order_analytics as a
import order_analytics_exports as ex
import normal_orders as no
import work_repository as wr
import source_archive
from runtime_support import Cancelled
import test_lifecycle as fixtures


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.LifecycleTests();self.f.setUp();self.path=self.f.path;self.root=self.f.root
        self.p=copy.deepcopy(self.f.payload)
        self.p['result']['must_list']={'revision':'test-list','total':1}
        self.p['result']['must_list_audit']=[{'sku':'0001'}]
        self.p['result']['rows'][0]['_must_sell']=True
        self.catalogs={}
        for wh in ('河北仓','山东仓'):
            self.set_catalog(wh,'')
        self.save_payload()
        import purchases
        self.f.heads=purchases.targets(self.path);self.f.h=self.f.heads[0]
        self.spec={'from':wr.today(),'to':wr.today()}
    def tearDown(self):self.f.tearDown()
    def set_catalog(self,wh,tag):
        b=openpyxl.Workbook();s=b.active;s.title='原资料';s.append(['商品编码','商品状态','虚拟分类'])
        for sku in ('0001','0002'):s.append([sku,'启用',tag])
        p=self.root/(wh+'-analytics.xlsx');b.save(p);b.close();sha=hashlib.sha256(p.read_bytes()).hexdigest()
        source_archive.capture(self.path,p,sha)
        self.catalogs[wh]={'source_sha256':sha,'sheet':'原资料','revision':sha,'items':{}}
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('UPDATE normal_work_revisions SET payload=?',(no.encode({'catalogs':self.catalogs}),))
    def save_payload(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('UPDATE normal_revisions SET payload=?,digest=?',(no.encode(self.p),no.fingerprint(self.p['result'])))
    def report(self,**kwargs):return a.collect(self.path,{**self.spec,**kwargs})
    def test_five_stages_price_missing_and_membership(self):
        r=self.report();x=r['rows'][0]
        self.assertEqual(x['scope'],'纳入');self.assertEqual(x['group'],'必采')
        self.assertEqual(x['quantities']['model'],24);self.assertEqual(x['quantities']['signed'],24)
        self.assertIsNone(x['quantities']['return']);self.assertIsNone(r['metrics']['pairs'][1]['delta'])
        self.assertEqual([g['warehouse'] for g in r['warehouses']],['河北仓','山东仓'])
    def test_budget_zero_still_in_model_cohort(self):
        x=self.p['result']['rows'][0];x.update(_budget_active=True,_allocated_qty=0,_allocated_amount=0);self.save_payload()
        r=self.report();x=r['rows'][0]
        self.assertEqual(x['quantities']['model'],24);self.assertEqual(x['quantities']['signed'],0)
        self.assertEqual(r['groups']['必采']['pairs'][0]['decrease'],a.num('163.2'))
    def test_virtual_tag_excludes_all_stages_in_assigned_warehouse(self):
        self.set_catalog('河北仓','标杆淘汰（夏季）');r=self.report()
        self.assertEqual(r['metrics']['excluded'],1);self.assertEqual(r['metrics']['included'],1)
        self.assertTrue(all(r['groups']['必采']['stages'][s]['value'] is None for s in a.STAGES))
        self.assertEqual(r['rows'][1]['scope'],'纳入')
    def test_missing_virtual_column_not_blank(self):
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DELETE FROM source_files')
        r=self.report();self.assertEqual(r['metrics']['pending'],2);self.assertEqual(r['metrics']['included'],0)
    def test_do_not_overmatch_retired_words(self):
        for tag in ('待淘汰','不淘汰','厂家下架','淘便考核'):
            with self.subTest(tag=tag):
                self.set_catalog('河北仓',tag);self.assertEqual(self.report()['rows'][0]['scope'],'纳入')
    def test_store_stop_and_warehouse_disabled_no_double_count(self):
        r=self.p['result']['rows'][0];r['S']='禁用';r['_meta']['export_status']='停采';self.save_payload()
        report=self.report();self.assertEqual(report['metrics']['excluded'],1);self.assertEqual(len(report['rows'][0]['reasons']),2)
    def test_false_without_list_is_unknown_not_nonmandatory(self):
        self.p['result']['must_list']=None;self.save_payload()
        self.assertTrue(all(x['group']=='属性待核' for x in self.report()['rows']))
    def test_complete_lifecycle_and_actual_payment(self):
        self.f.save('return',[{'门店确认数量':12}]);self.f.save('payment',[{'付款单商品数量':6,'实际金额':19.5}]);self.f.save('receipt',[{'实际收货数量':3}])
        r=self.report();m=r['groups']['必采']
        self.assertEqual([m['stages'][s]['value'] for s in a.STAGES],list(map(a.num,['163.2','163.2','81.6','40.8','20.4'])))
        self.assertEqual(m['payment']['CNY']['amount'],a.num('19.5'));self.assertEqual(m['pairs'][1]['ratio'],a.num('.5'))
    def test_confirmed_blank_zero_and_stale_downstream(self):
        self.f.save('return',[{'门店确认数量':None}]);r=self.report();self.assertEqual(r['rows'][0]['quantities']['return'],0)
        self.assertTrue(r['rows'][0]['valid']['return']);self.assertFalse(r['rows'][0]['valid']['payment'])
    def test_changed_source_rejects_export_no_half_file(self):
        report=self.report();self.f.save('return');target=self.root/'changed.xlsx'
        with self.assertRaisesRegex(ValueError,'变化'):ex.export(self.path,report,target)
        self.assertFalse(target.exists())
    def test_export_types_all_rows_and_source_immutable(self):
        report=self.report();before=self.path.read_bytes();target=self.root/'report.xlsx';receipt=ex.export(self.path,report,target,level='detail')
        b=openpyxl.load_workbook(target);self.assertEqual(b.sheetnames,receipt['sheets'])
        self.assertEqual(b['全量商品五段'].max_row,3);self.assertEqual(b['全量商品五段']['F2'].value,'0001')
        self.assertEqual(b['全量商品五段']['F2'].data_type,'s');self.assertEqual(b['全量商品五段']['L2'].data_type,'n')
        self.assertEqual(list(b['全量商品五段'].values)[0],tuple(ex.DETAIL_HEADERS));b.close()
        self.assertEqual(self.path.read_bytes(),before)
    def test_cancel_and_retry(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):a.collect(self.path,self.spec,cancel=stop)
        stop.clear();r=self.report();target=self.root/'cancel.xlsx'
        def progress(v):stop.set()
        with self.assertRaises(Cancelled):ex.export(self.path,r,target,progress=progress,cancel=stop)
        self.assertFalse(target.exists());stop.clear();ex.export(self.path,r,target,cancel=stop)
    def test_exact_query_and_order_snapshot(self):
        first=self.report();second=self.report();self.assertEqual(first['snapshot_id'],second['snapshot_id'])
        with self.assertRaisesRegex(ValueError,'没有有效签发'):self.report(stores=['not-T'])
        with self.assertRaisesRegex(ValueError,'不存在'):self.report(orders=['not-an-order'])
    def test_each_frozen_warehouse_source_read_once(self):
        with patch.object(source_archive,'read',wraps=source_archive.read) as read:
            self.report();self.assertEqual(read.call_count,len({c['source_sha256'] for c in self.catalogs.values()}))
    def test_no_mutation_during_collection(self):
        before=self.path.read_bytes();self.report();self.assertEqual(self.path.read_bytes(),before)
    def test_added_rows_unknown_not_zero_and_group_sums(self):
        self.f.save('return',[{'商品编码':'EXTRA','门店确认数量':3}]);r=self.report()
        extra=r['rows'][1];self.assertEqual(extra['sku'],'EXTRA');self.assertIsNone(extra['quantities']['model']);self.assertEqual(extra['scope'],'待核')
        for s in a.STAGES:
            self.assertEqual(r['metrics']['stages'][s]['value'] or 0,sum((g['stages'][s]['value'] or 0 for g in r['groups'].values())))

if __name__=='__main__':unittest.main()
