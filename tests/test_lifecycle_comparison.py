"""Self-contained order/store comparison, historical/zero/stale and immutable export checks."""
import copy,hashlib,sqlite3,sys,threading,unittest
from pathlib import Path
from contextlib import closing
from decimal import Decimal
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
import openpyxl
import lifecycle_comparison as c,order_lifecycle as lc,daily_batch,normal_orders as no,work_repository as wr
from runtime_support import Cancelled
import test_lifecycle as fixtures

class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.data=fixtures.LifecycleTests();self.data.setUp();self.root=self.data.root;self.path=self.data.path;self.h=self.data.h
    def tearDown(self):self.data.tearDown()
    def item(self):return c.collect(self.path,[self.h])[0]
    def complete(self):
        self.data.save('return',[{'门店确认数量':12}]);self.data.save('payment',[{'付款单商品数量':6,'实际金额':19.5}]);self.data.save('receipt',[{'实际收货数量':3}])
    def test_fixed_signed_price_and_source_payment_independent(self):
        self.complete();item=self.item();m=item['metrics']
        self.assertEqual(item['status'],'已结束')
        self.assertEqual([m[s]['value'] for s in c.STAGES],[Decimal('163.2'),Decimal('81.6'),Decimal('40.8'),Decimal('20.4')])
        self.assertEqual([p['value'] for p in item['pairs']],[Decimal('-81.6'),Decimal('-40.8'),Decimal('-20.4')]);self.assertEqual(c.payment_total(item['payment']),(Decimal('19.5'),'CNY'))
        self.assertEqual(item['details'][0][9:13],[24,12,6,3])
    def test_absent_and_confirmed_zero_not_confused(self):
        a=self.item();self.assertFalse(a['metrics']['return']['valid']);self.assertIsNone(c.order_row(a)[11])
        self.data.save('return',[{'门店确认数量':None}]);b=self.item();self.assertTrue(b['metrics']['return']['valid']);self.assertEqual(b['metrics']['return']['value'],0);self.assertEqual(b['metrics']['payment']['state'],'无需此环节');self.assertFalse(b['pairs'][1]['valid']);self.assertEqual(b['pairs'][0]['value'],Decimal('-163.2'))
    def test_modern_missing_line_zero_but_extra_without_price_not_invented(self):
        self.data.save('return',[{'商品编码':'NEW','门店确认数量':2}]);item=self.item()
        self.assertEqual(item['metrics']['return']['unpriced'],1);self.assertEqual(item['metrics']['return']['value'],0);self.assertEqual(item['metrics']['return']['skus'],{'NEW'})
        self.assertEqual(item['pairs'][0]['unknown'],1);self.assertIsNone(c.order_row(item)[18]);self.assertEqual([r[5] for r in item['details']],['0001','NEW'])
        self.assertEqual(item['details'][0][10],0);self.assertEqual(item['details'][1][9],0);self.assertIsNone(item['details'][1][8])
    def test_stale_downstream_facts_kept_but_excluded_from_comparable_totals(self):
        self.complete();self.data.save('return',[{'门店确认数量':10}]);item=self.item()
        self.assertEqual(item['metrics']['payment']['state'],'待重新核对');self.assertFalse(item['pairs'][1]['valid']);self.assertEqual(item['details'][0][11],6)
        row=dict(zip(c.STORE_HEADERS,c.store_rows([item])[0]));self.assertEqual(row['付款有效订单数'],0);self.assertIsNone(row['付款已计价货值（签发价）']);self.assertIsNone(row['付款实际金额（有效单）']);self.assertEqual(c.payment_total(item['payment'])[0],Decimal('19.5'))
    def test_partial_store_scope_denominators_and_per_warehouse(self):
        self.complete();items=c.collect(self.path,self.data.heads);row=dict(zip(c.STORE_HEADERS,c.store_rows(items)[0]))
        self.assertEqual(row['订单数'],2);self.assertEqual(row['签发有效订单数'],2);self.assertEqual(row['回传有效订单数'],1);self.assertEqual(row['回传−签发可比订单数'],1);self.assertEqual(row['回传−签发货值差额'],Decimal('-81.6'))
        self.assertEqual(len(c.store_rows(items,True)),2);self.assertEqual([r[2] for r in c.store_rows(items,True)],['河北仓','山东仓'])
    def test_same_code_across_days_warehouses_skus_dedup_amounts_add(self):
        self.complete();first=self.item();second=copy.deepcopy(first);second['head']['day']='2026-11-01';second['head']['warehouse']='山东仓';second['prefix'][3]='山东仓'
        r=dict(zip(c.STORE_HEADERS,c.store_rows([first,second])[0]));self.assertEqual(r['签发商品款数（去重）'],1);self.assertEqual(r['签发已计价货值（签发价）'],Decimal('326.4'));self.assertEqual(r['订单数'],2);self.assertEqual(r['最早业务日期'],'2026-11-01')
    def test_currency_never_merged_and_unmarked_not_defaulted(self):
        self.data.save('return');self.data.save('payment',[{'实际金额':12,'币种':'CNY'},{'商品编码':'EXTRA','实际金额':9,'币种':'USD'}]);item=self.item()
        self.assertEqual(c.payment_total(item['payment']),(None,'多币种，见实际付款金额'));self.assertEqual(list(item['payment']),['CNY','USD'])
        self.assertIsNone(c.store_rows([item])[0][-3]);self.data.save('payment',[{'币种':None}]);self.assertEqual(c.payment_total(self.item()['payment'])[1],'未标币种')
    def test_legacy_partial_return_stays_unconfirmed(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)');db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',('oid',1,1,wr.now(),wr.dumps({'values':{}})))
        before=self.path.read_bytes();item=self.item();self.assertEqual(item['metrics']['return']['state'],'旧回传不完整');self.assertFalse(item['metrics']['return']['valid']);self.assertEqual(before,self.path.read_bytes())
    def test_same_snapshot_api_with_cached_order_exactly_equivalent(self):
        self.complete();old=lc.snapshot(self.path,self.h);order=self.data.repo.get(self.h['day'],self.h['store'],self.h['revision'],view='export')
        self.assertEqual(old,lc.snapshot(self.path,self.h,order=order))
    def test_duplicate_heads_dedup_order_and_old_revision_rejected(self):
        heads=list(reversed(self.data.heads));items=c.collect(self.path,[*heads,heads[0]]);self.assertEqual([i['head'] for i in items],heads)
        self.data.second_revision()
        with self.assertRaises((wr.Conflict,ValueError)):c.collect(self.path,[self.h])
    def test_one_signed_read_for_two_warehouse_orders(self):
        repo=c.repository(self.path)
        with patch.object(repo,'get',wraps=repo.get) as get,patch.object(c,'repository',return_value=repo):
            c.collect(self.path,self.data.heads);self.assertEqual(get.call_count,1)
    def test_excel_columns_types_styles_scope_and_readonly(self):
        self.complete();before=self.path.read_bytes();items=c.collect(self.path,self.data.heads)
        for level in ('order','store'):
            target=self.root/(level+'.xlsx');receipt=c.export(self.path,self.data.heads,target,level);b=openpyxl.load_workbook(target)
            expected=['订单级对比','差异商品明细','实际付款金额','统计口径']
            if level=='store':expected=['门店级对比','门店分仓对比',*expected]
            self.assertEqual(b.sheetnames,expected);self.assertEqual([x.value for x in b['订单级对比'][1]],c.ORDER_HEADERS)
            self.assertEqual(list(b['订单级对比'].values)[1],tuple(float(v) if isinstance(v,Decimal) else None if v=='' else v for v in c.order_row(items[0])))
            self.assertEqual(b['差异商品明细']['F2'].value,'0001');self.assertEqual(b['差异商品明细']['F2'].data_type,'s');self.assertEqual(b['订单级对比']['I2'].data_type,'n');self.assertEqual(b['订单级对比'].freeze_panes,'C2');self.assertTrue(b['订单级对比'].auto_filter.ref);b.close()
        self.assertEqual(before,self.path.read_bytes())
    def test_cancel_during_read_or_write_and_retry_no_partial_file(self):
        event=threading.Event();target=self.root/'cancel.xlsx'
        def stop(message):event.set()
        with self.assertRaises(Cancelled):c.export(self.path,self.data.heads,target,progress=stop,cancel=event)
        self.assertFalse(target.exists());event.clear()
        def stop_write(message):
            if '订单级对比' in str(message):event.set()
        with self.assertRaises(Cancelled):c.export(self.path,self.data.heads,target,progress=stop_write,cancel=event)
        self.assertFalse(target.exists());event.clear();c.export(self.path,self.data.heads,target,cancel=event);before=target.read_bytes()
        with self.assertRaises(ValueError):c.export(self.path,self.data.heads,target)
        self.assertEqual(before,target.read_bytes())
    def test_all_filtered_cross_page_same_conditions_and_order(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            for n in range(27):
                payload=copy.deepcopy(self.data.payload);r=payload['result'];r['store']=f'T{n}';r['store_name']=f'门店{n}';oid=f'oid{n}x';digest=no.fingerprint(r)
                db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),r['store'],'normal_model',oid,'BH-'+r['store'],1));db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,digest,wr.now(),no.encode(payload, db)))
        cache=self.root/'index.sqlite3';request={'from':wr.today(),'to':wr.today()};first=daily_batch.read(self.path,cache,request);heads=c.all_heads(self.path,cache,request)
        self.assertEqual(len(first['records']),50);self.assertEqual(len(heads),56);self.assertEqual(heads[:50],first['records'])
        filtered=c.all_heads(self.path,cache,{**request,'warehouse':'河北仓','search':'T1'})
        self.assertTrue(filtered);self.assertTrue(all(h['warehouse']=='河北仓' and 'T1' in h['store'] for h in filtered))
        self.assertEqual(c.all_heads(self.path,cache,{**request,'status':'待收货'}),[])

if __name__=='__main__':unittest.main()
