"""Portable inventory invariants: independently hand-calculated, no user files."""
import copy
from contextlib import closing
from decimal import Decimal as D
from datetime import date,timedelta
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import inventory_structure as s
import inventory_structure_exports as ex
import test_order_analytics as fixtures
import normal_orders as no
import work_repository as wr
from runtime_support import Cancelled


def sample():
    rows=[]
    for sku,q,p,status,must,wh in [('0001',10,2,'启用',True,'山东仓'),('0002',5,4,'备用',False,'河北仓'),('0003',3,10,'禁用',True,'山东仓'),('0004',4,5,'启用',False,'地采')]:
        rows.append({'A':'合成测试店','B':'T','E':sku,'D':'商品'+sku,'G':'包','K':q,'L':0,'V':p,'S':status,'AU':wh,'_must_sell':must,'_source':'地采商品' if wh=='地采' else '中心仓商品',
            '_inventory_analysis_price':p,'_inventory_analysis_unit':'包','_inventory_analysis_source':'合成验收明确单价','_meta':{'export_row':len(rows)+2,'export_status':'停采' if sku=='0003' else '正常'}})
    return {'rows':rows,'must_list':{'revision':'synthetic-list','total':2},'must_list_audit':[{'sku':'0001'},{'sku':'0003'}]}


class MetricsTests(unittest.TestCase):
    def setUp(self):self.raw=sample();self.head={'day':'2026-11-06','store':'T','number':'BH-SYNTHETIC'}
    def rows(self):return s.facts(self.raw,self.head)
    def test_hand_calculated_partition(self):
        m=s.structure(self.rows(),'stock');self.assertEqual(m['total']['value'],90)
        self.assertEqual([m['sources'][k]['value'] for k in s.SOURCES],[70,20,0])
        self.assertEqual([m['statuses'][k]['value'] for k in s.STATUSES],[20,20,30,0])
        self.assertEqual([m['groups'][k]['value'] for k in s.GROUPS],[50,20,0])
    def test_zero_recommendation_restricted_inventory_retained(self):
        for r in self.raw['rows']:r.update(AS=0,_excluded=True,_admission_blocked=True,_budget_active=True,_allocated_qty=0)
        self.assertEqual(s.structure(self.rows(),'stock')['total']['value'],90)
    def test_missing_zero_negative_price_and_stock(self):
        for p in (None,0,-1,'NaN',True):
            with self.subTest(p=p):
                self.raw['rows'][0]['V']=p;m=s.metric(self.rows(),'stock')
                self.assertIsNone(m['value']);self.assertEqual(m['known_value'],70);self.assertEqual(m['unpriced'],1)
        self.raw['rows'][0]['K']=0;m=s.metric(self.rows(),'stock');self.assertEqual(m['value'],70)
        for q in (None,-1,'bad',True):
            self.raw['rows'][0]['K']=q;self.assertEqual(s.metric(self.rows(),'stock')['invalid_quantity'],1)
    def test_each_view_independent(self):
        self.raw['rows'][0]['L']=None;r=self.rows()[0]
        self.assertEqual(s.value(r,'stock'),20);self.assertIsNone(s.value(r,'position'));self.assertIsNone(s.value(r,'inbound'))
    def test_missing_unit_and_duplicate_never_double_count(self):
        self.raw['rows'][0]['G']=None;self.assertIsNone(s.value(self.rows()[0],'stock'))
        self.raw['rows'][0]['G']='包';self.raw['rows'].append(copy.deepcopy(self.raw['rows'][0]));m=s.metric(self.rows(),'stock')
        self.assertEqual(m['known_value'],70);self.assertEqual(m['invalid_quantity'],2)
        d=s.changes([],self.rows(),'stock',True)['details'];self.assertEqual(len(d),5)
        self.assertEqual([r['sku'] for r in d],['0001','0002','0003','0004','0001'])
    def test_unmatched_local_price_not_guessed(self):
        self.raw['rows'][3].pop('_inventory_analysis_source');m=s.metric(self.rows(),'stock')
        self.assertEqual(m['known_value'],70);self.assertIsNone(m['value'])
    def test_unknown_source_and_status(self):
        self.raw['rows'][0]['_source']='匹配待核';self.raw['rows'][1]['S']='其他';m=s.structure(self.rows(),'stock')
        self.assertEqual(m['sources']['来源待核']['unpriced'],1);self.assertEqual(m['statuses']['状态待核']['value'],20)
    def test_membership_no_list_vs_explicit_empty(self):
        self.raw['must_list']=None;self.assertEqual(s.structure(self.rows(),'stock')['groups']['属性待核']['value'],70)
        self.raw['must_list']={'revision':'empty','total':0};self.raw['must_list_audit']=[]
        for r in self.raw['rows']:r.pop('_must_sell')
        self.assertEqual(s.structure(self.rows(),'stock')['groups']['非必采']['value'],70)
    def test_quantity_price_migration_conservation(self):
        before=self.rows();self.raw['rows'][0].update(K=12,V=3,S='禁用',_must_sell=False,AU='河北仓');after=self.rows();c=s.changes(before,after,'stock',True)
        self.assertEqual(c['quantity_effect'],4);self.assertEqual(c['price_effect'],12);self.assertEqual(c['comparable_delta'],16)
        self.assertEqual(len(c['migrations']),3);self.assertEqual(c['migrations'][0]['value'],36)
    def test_missing_new_unit_and_valuation_separate(self):
        before=self.rows();self.raw['rows'][0]['G']='瓶';self.raw['rows'][1]['V']=None;self.raw['rows'].pop();self.raw['rows'].append({**self.raw['rows'][2],'E':'0005'})
        c=s.changes(before,self.rows(),'stock',True)
        for k in ('库存单位变化','估值范围待核或变化','本轮新出现记录','本轮未提供记录'):self.assertEqual(c['counts'][k],1)
    def test_preview_no_mutation(self):
        before=copy.deepcopy(self.raw);r=s.preview({'T':self.raw},self.head['day'])
        self.assertTrue(r['preview']);self.assertEqual(self.raw,before);self.assertEqual(r['overview']['total']['value'],90)
        frozen=s.preview_inputs({'T':self.raw});self.assertEqual(s.preview(frozen,self.head['day'])['snapshot_id'],r['snapshot_id'])
        self.raw['rows'][0]['K']=50;self.assertEqual(frozen['T']['rows'][0]['K'],10)


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.AnalyticsTests();self.f.setUp();self.path=self.f.path;self.root=self.f.root
        self.f.p['result']['rows']=sample()['rows'];self.f.p['result']['must_list']=sample()['must_list'];self.f.save_payload();self.spec=self.f.spec
    def tearDown(self):self.f.tearDown()
    def old(self):
        p=copy.deepcopy(self.f.p);p['result']['rows'][0]['K']=8;day=(date.today()-timedelta(days=7)).isoformat()
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,'T','normal_model','old-inventory','BH-OLD',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('old-inventory',1,no.fingerprint(p['result']),wr.now(),no.encode(p)))
        return day
    def test_previous_outside_query_and_no_duplicate_warehouse(self):
        old=self.old();r=s.collect(self.path,self.spec)
        self.assertEqual(len(r['rounds']),1);self.assertEqual(r['rounds'][0]['previous_day'],old);self.assertEqual(r['rounds'][0]['delta'],4)
        self.assertEqual(r['overview']['total']['value'],90);self.assertEqual(len(s.detail(self.path,r,'T',self.spec['to'])),4)
    def test_latest_day_never_sum_rounds(self):
        old=self.old();r=s.collect(self.path,{**self.spec,'from':old});self.assertEqual(len(r['rounds']),2);self.assertEqual(r['overview']['total']['value'],90)
    def test_delete_invalidates_and_partial_scope(self):
        import archive_orders as ao
        r=s.collect(self.path,self.spec);head={**r['rounds'][0],'warehouse':'河北仓','visibility_token':0}
        ao.change(self.path,head,True)
        with self.assertRaisesRegex(ValueError,'变化'):s.validate_basis(self.path,r)
        current=s.collect(self.path,self.spec);self.assertTrue(current['rounds'][0]['partial']);self.assertEqual(current['overview']['total']['value'],70)
    def test_full_parent_deleted(self):
        r=s.collect(self.path,self.spec)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DELETE FROM normal_orders')
        with self.assertRaises(ValueError):s.validate_basis(self.path,r)
        self.assertEqual(s.collect(self.path,self.spec)['rounds'],[])
    def test_export_value_type_format_and_immutability(self):
        import openpyxl
        r=s.collect(self.path,self.spec);before=self.path.read_bytes();target=self.root/'inventory.xlsx';receipt=ex.export(self.path,r,target,True)
        b=openpyxl.load_workbook(target)
        try:
            self.assertEqual(b.sheetnames,receipt['sheets']);self.assertEqual(len(b.sheetnames),5)
            self.assertEqual(b['货盘逐轮总览']['G2'].value,90);self.assertEqual(b['货盘逐轮总览']['G2'].number_format,'#,##0.00;[Red]-#,##0.00;0.00')
            self.assertEqual(b['商品与变化明细']['D2'].value,'0001');self.assertEqual(b['商品与变化明细']['D2'].data_type,'s')
        finally:b.close()
        self.assertEqual(before,self.path.read_bytes())
    def test_cancel_and_retry(self):
        stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):s.collect(self.path,self.spec,cancel=stop)
        r=s.collect(self.path,self.spec);target=self.root/'cancel.xlsx'
        with self.assertRaises(Cancelled):ex.export(self.path,r,target,True,cancel=stop)
        self.assertFalse(target.exists());self.assertEqual(s.collect(self.path,self.spec)['overview']['total']['value'],90)
    def test_unknown_and_zero_excel(self):
        import openpyxl
        self.f.p['result']['rows'][0].update(V=None);self.f.p['result']['rows'][1].update(K=0,V=None);self.f.save_payload()
        r=s.collect(self.path,self.spec);target=self.root/'unknown.xlsx';ex.export(self.path,r,target,True)
        b=openpyxl.load_workbook(target)
        try:self.assertIsNone(b['货盘逐轮总览']['H2'].value);self.assertIsNone(b['商品与变化明细']['O2'].value);self.assertEqual(b['商品与变化明细']['O3'].value,0)
        finally:b.close()
    def test_concurrent_change_not_blocked_or_published(self):
        changed=[]
        def progress(_):
            if not changed:
                with closing(sqlite3.connect(self.path,timeout=0)) as db,db:db.execute("UPDATE normal_orders SET number=number||'-changed'")
                changed.append(True)
        # Number participates in the identity stamp, not just its content digest.
        with self.assertRaisesRegex(ValueError,'变化'):s.collect(self.path,self.spec,progress)
        self.assertEqual(changed,[True])


if __name__=='__main__':unittest.main()
