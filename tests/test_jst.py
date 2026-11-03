"""Portable independent execution/stock fixtures, without user files or Qt."""
import copy
import sqlite3
import sys
import threading
import unittest
from pathlib import Path
from decimal import Decimal
from contextlib import closing
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import jst_allocation as a
import jst_orders as j
import jst_exports as ex
import warehouse_catalog as cat
import order_lifecycle as lc
import work_repository as wr
import normal_orders as no
import purchases
import test_lifecycle as fixture
from runtime_support import Cancelled,CommitProgress


def catalog(warehouse='河北仓', stock=100, step=1, price=2):
    # A valid new execution fixture explicitly supplies an empty tag field.
    # Missing-field blocking is covered separately in test_admission.
    items={'0001':{'商品编码':'0001','分类':'测试','基本售价':price,'出库箱规':step,'商品状态':'启用','可用数':stock,'商品名称':'测试','_row':2,'商品标签':''}}
    return {'schema':1,'warehouse':warehouse,'items':items,'revision':cat.revision(items),'imported_at':'2026-11-01T10:00:00','source_path':'synthetic-not-a-real-file.xlsx','source_sha256':'synthetic','sheet':'商品资料'}


class Allocation(unittest.TestCase):
    def test_carton_boundaries(self):
        for stock,expected in [(0,0),(23,0),(24,24),(30,24),(47,24),(48,48)]:
            with self.subTest(stock=stock):
                rows=a.allocate([{'sku':'0001','base_quantity':'48'}],catalog(stock=stock,step=24)['items'],{})
                self.assertEqual(Decimal(rows[0]['quantity']),expected);self.assertEqual(Decimal(rows[0]['after']),stock-expected)
    def test_reserved_balance_not_source_stock(self):
        r=a.allocate([{'sku':'0001','base_quantity':'48'}],catalog(stock=30,step=24)['items'],{'0001':Decimal(20)})[0]
        self.assertEqual(r['quantity'],'0');self.assertEqual(r['reason'],'库存不足一箱')
    def test_unknowns_disabled_and_no_price_not_zero(self):
        for field,value in [('可用数',None),('可用数',-1),('基本售价',''),('商品状态','禁用'),('出库箱规',0)]:
            c=catalog();c['items']['0001'][field]=value
            r=a.allocate([{'sku':'0001','base_quantity':'48'}],c['items'],{})[0]
            self.assertTrue(r['issue']);self.assertEqual(r['quantity'],'0')
    def test_decimal_price_and_exact_identifier(self):
        r=a.allocate([{'sku':'0001','base_quantity':'3'},{'sku':'1','base_quantity':'3'}],catalog(price='0.1')['items'],{})
        self.assertEqual(r[0]['amount'],'0.3');self.assertTrue(r[1]['issue'])
    def test_invalid_return_never_rounds(self):
        r=a.allocate([{'sku':'0001','base_quantity':'23'}],catalog(step=24)['items'],{})[0]
        self.assertTrue(r['issue']);self.assertEqual(r['quantity'],'0')


class Service(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp();self.path=self.f.path;self.h=self.f.h
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start()
        self.f.save('return',[{'门店确认数量':40}]);self.refresh(100)
    def tearDown(self):self.backup.stop();self.f.tearDown()
    def refresh(self,stock,operation=None):
        p=j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=stock),operation=operation)
        return j.commit_refresh(self.path,p)
    def plan(self):return j.prepare(self.path,[self.h])
    def generated(self):
        p=self.plan();r=j.commit(self.path,p);self.assertFalse(r['errors']);return p,r['generated'][0]
    def balances(self):
        with closing(sqlite3.connect(self.path)) as db:
            b=j.baseline(db,'河北仓');return Decimal(str(b['catalog']['items']['0001']['可用数']))-j.reservations(db,'河北仓',set(b['included'])).get('0001',Decimal(0))
    def test_locked_fresh_stock_no_double_deduction(self):
        _,gid=self.generated();self.assertEqual(self.balances(),60)
        j.lock(self.path,[gid]);self.assertEqual(self.balances(),60)
        self.refresh(60);self.assertEqual(self.balances(),60)
        self.assertEqual(self.plan()['payload']['skipped'][0]['reason'],'已锁单')
    def test_unlocked_reservation_survives_refresh_and_late_lock(self):
        _,gid=self.generated();self.refresh(90);self.assertEqual(self.balances(),50)
        j.lock(self.path,[gid]);self.assertEqual(self.balances(),50)
        self.refresh(50);self.assertEqual(self.balances(),50)
    def test_retry_and_exports_do_not_reserve_twice(self):
        p,gid=self.generated();self.assertEqual(j.commit(self.path,p)['generated'],[gid]);self.assertEqual(self.balances(),60)
        for _ in range(2):self.assertEqual(len(ex.export(self.path,[gid],self.f.root/'out')['files']),1)
        self.assertEqual(self.balances(),60)
        import openpyxl
        b=openpyxl.load_workbook(next((self.f.root/'out').glob('*.xlsx')))
        self.assertEqual(b.sheetnames,['订单明细']);self.assertEqual(list(b.active.values),[('商品编码','单价','数量','订单号'),('0001',2,40,self.h['number'])])
        self.assertEqual(b.active['A2'].data_type,'s');b.close()
    def test_generation_draft_is_not_actual_payment(self):
        _,gid=self.generated();self.assertIsNone(self.f.view()['documents']['payment'])
        plan=j.payment_plan(self.path,gid);lc.commit(self.path,plan)
        self.assertEqual(self.f.view()['status'],'待收货');self.assertEqual(self.f.view()['rows'][0][4],40)
    def test_stale_return_and_baseline_rejected(self):
        p=self.plan();self.refresh(80);r=j.commit(self.path,p);self.assertTrue(r['errors']);self.assertEqual(r['generated'],[])
        p=self.plan();self.f.save('return',[{'门店确认数量':30}]);self.assertTrue(j.commit(self.path,p)['errors'])
    def test_refresh_lock_race_rejected(self):
        _,gid=self.generated();p=j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=60));j.lock(self.path,[gid])
        with self.assertRaises(wr.Conflict):j.commit_refresh(self.path,p)
    def test_cancel_before_commit_and_boundary(self):
        p=self.plan();e=threading.Event();e.set()
        with self.assertRaises(Cancelled):j.commit(self.path,p,cancel=e)
        e.clear()
        def progress(v):
            if isinstance(v,CommitProgress) and v.active:e.set()
        r=j.commit(self.path,p,progress=progress,cancel=e);self.assertEqual(len(r['generated']),1)
    def test_zero_allocation_no_fake_order_and_retry(self):
        self.refresh(0);p=self.plan();r=j.commit(self.path,p);self.assertEqual(r['generated'],[]);self.assertEqual(len(r['zero']),1)
        with closing(sqlite3.connect(self.path)) as db:before=db.execute('SELECT count(*) FROM jst_events').fetchone()
        self.assertEqual(j.commit(self.path,p)['zero'],r['zero'])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM jst_events').fetchone(),before)
    def test_refresh_retry_and_tamper(self):
        p=j.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=70));j.commit_refresh(self.path,p);j.commit_refresh(self.path,p)
        p['payload']['catalog']['items']['0001']['可用数']=999
        with self.assertRaises(ValueError):j.commit_refresh(self.path,p)
    def test_locked_return_update_and_resign_guard(self):
        _,gid=self.generated();j.lock(self.path,[gid])
        with self.assertRaises(wr.Conflict):self.f.save('return',[{'门店确认数量':20}])
        with closing(sqlite3.connect(self.path)) as db:
            with self.assertRaises(wr.Conflict):j.guard_resign(db,self.h['oid'])

    def second_store(self):
        p=copy.deepcopy(self.f.payload);p['result']['store']='U';p['result']['store_name']='第二店';d=no.fingerprint(p['result'])
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'U','normal_model','oid-u','BH-U-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid-u',1,d,wr.now(),no.encode(p)))
        h=next(h for h in purchases.targets(self.path) if h['store']=='U' and h['warehouse']=='河北仓')
        old=self.f.h;self.f.h=h
        try:self.f.save('return',[{'门店编码':'U','门店确认数量':80}])
        finally:self.f.h=old
        return h

    def test_multi_order_priority_manual_order_and_lock_while_running(self):
        h=self.second_store();p=j.prepare(self.path,[h,self.h])
        self.assertEqual([x['head']['store'] for x in p['payload']['plans']],['T','U'])
        self.assertEqual([x['summary']['quantity'] for x in p['payload']['plans']],['40','60'])
        reverse=j.prepare(self.path,[h,self.h],True)
        self.assertEqual([x['summary']['quantity'] for x in reverse['payload']['plans']],['80','20'])
        def progress(v):
            if hasattr(v,'business_event') and v.business_event['gid']==p['payload']['plans'][0]['gid']:j.lock(self.path,[v.business_event['gid']])
        result=j.commit(self.path,p,progress=progress)
        self.assertEqual(len(result['generated']),2);self.assertFalse(result['errors']);self.assertEqual(self.balances(),0)

    def test_failure_second_order_retains_first_then_retry_requires_repreview(self):
        h=self.second_store();p=j.prepare(self.path,[self.h,h])
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_u BEFORE INSERT ON jst_generations WHEN NEW.oid='oid-u' BEGIN SELECT RAISE(ABORT,'injected'); END")
        r=j.commit(self.path,p);self.assertEqual(len(r['generated']),1);self.assertTrue(r['errors']);self.assertEqual(self.balances(),60)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DROP TRIGGER fail_u')
        r=j.commit(self.path,p);self.assertEqual(len(r['generated']),2);self.assertEqual(self.balances(),0)

    def test_correction_is_atomic_and_export_exact_old_generation(self):
        h=self.second_store();old=j.commit(self.path,j.prepare(self.path,[self.h,h]))['generated']
        p=j.prepare(self.path,[h,self.h],True,True)
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("CREATE TRIGGER fail_t BEFORE INSERT ON jst_generations WHEN NEW.oid='oid' BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.assertTrue(j.commit(self.path,p)['errors'])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(j.active(db,self.h),old[0]);self.assertEqual(j.active(db,h),old[1])
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DROP TRIGGER fail_t')
        r=j.commit(self.path,p);self.assertFalse(r['errors']);self.assertEqual(self.balances(),0)
        self.assertEqual(j.get(self.path,old[0])['summary']['quantity'],'40')
        self.assertEqual(j.get(self.path,r['generated'][1])['summary']['quantity'],'20')
        import openpyxl
        x=ex.export(self.path,[old[0]],self.f.root/'old-output')['files'][0]
        book=openpyxl.load_workbook(x['path']);self.assertEqual(book.active['C2'].value,40);book.close()

    def test_cancel_after_first_keeps_first_and_retry_no_old_cancel(self):
        h=self.second_store();p=j.prepare(self.path,[self.h,h]);e=threading.Event()
        def progress(v):
            if hasattr(v,'business_event'):e.set()
        r=j.commit(self.path,p,progress=progress,cancel=e)
        self.assertTrue(r['cancelled']);self.assertEqual(len(r['generated']),1);self.assertEqual(self.balances(),60)
        e.clear();r=j.commit(self.path,p,cancel=e);self.assertEqual(len(r['generated']),2);self.assertEqual(self.balances(),0)

    def test_waiting_write_lock_cancelled_no_generation(self):
        import time
        p=self.plan();e=threading.Event();results=[];db=sqlite3.connect(self.path);db.execute('BEGIN IMMEDIATE')
        t=threading.Thread(target=lambda:results.append(j.commit(self.path,p,cancel=e)));t.start();time.sleep(.15);e.set();t.join(3);db.rollback();db.close()
        self.assertFalse(t.is_alive());self.assertTrue(results[0]['cancelled']);self.assertEqual(self.balances(),100)

    def test_daily_projection_and_logs_do_not_promote_draft(self):
        import daily_batch
        cache=self.f.root/'daily.db';request={'from':wr.today(),'to':wr.today()}
        rows=daily_batch.read(self.path,cache,request)['records'];self.assertEqual(rows[0]['status'],'待付款')
        _,gid=self.generated();rows=daily_batch.read(self.path,cache,request)['records'];self.assertEqual(rows[0]['status'],'待付款')
        j.lock(self.path,[gid]);rows=daily_batch.read(self.path,cache,request)['records'];self.assertTrue(rows[0]['locked'])
        logs=daily_batch.logs(self.path,wr.today(),wr.today());self.assertTrue(any(r['action']=='确认下单并锁定' for r in logs))
        self.assertFalse(self.f.view()['present']['payment'])

    def test_cancel_unused_generation_releases_only_local_reservation(self):
        _,gid=self.generated();before=self.f.view()['documents']['return']
        with self.assertRaises(ValueError):j.cancel_generated(self.path,[gid],'')
        j.cancel_generated(self.path,[gid],'门店更正，尚未下单')
        self.assertEqual(self.balances(),100);self.assertEqual(j.get(self.path,gid)['summary']['quantity'],'40')
        self.assertEqual(self.f.view()['documents']['return'],before)
        _,new=self.generated();j.lock(self.path,[new])
        with self.assertRaises(ValueError):j.cancel_generated(self.path,[new],'不可绕过锁单')

    def test_legacy_return_preserves_original_signed_dependency(self):
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute("DELETE FROM lifecycle_versions WHERE stage='return'")
            db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT)')
            db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',('oid',1,1,wr.now(),wr.dumps({'values':{'0001':40},'warehouses':{}})))
        p,gid=self.generated();self.assertIn('signed_products',p['payload']['plans'][0])
        j.lock(self.path,[gid]);lc.commit(self.path,j.payment_plan(self.path,gid))
        self.assertTrue(self.f.view()['present']['payment'])

    def test_original_catalog_is_exact_bytes_and_missing_never_substituted(self):
        import openpyxl,hashlib
        path=self.f.root/'source.xlsx';book=openpyxl.Workbook();ws=book.active;ws.append(cat.FIELDS)
        item=catalog()['items']['0001'];ws.append([item.get(k) for k in cat.FIELDS]);book.save(path);book.close()
        j.commit_refresh(self.path,j.prepare_refresh(self.path,'河北仓',source=path))
        _,gid=self.generated();expected=hashlib.sha256(path.read_bytes()).hexdigest();path.unlink()
        target=self.f.root/'original.xlsx';j.export_source(self.path,gid,target)
        self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(),expected)

    def test_payment_preview_bound_to_generation_and_failures_are_journaled(self):
        _,gid=self.generated();old=j.payment_plan(self.path,gid)
        changed=j.commit(self.path,j.prepare(self.path,[self.h],correction=True));self.assertTrue(changed['generated'])
        with self.assertRaises(wr.Conflict):lc.commit(self.path,old)
        p=j.prepare(self.path,[self.h],correction=True);self.refresh(90)
        r=j.commit(self.path,p);self.assertTrue(r['errors'])
        with closing(sqlite3.connect(self.path)) as db:self.assertTrue(db.execute("SELECT 1 FROM jst_events WHERE action='批次存在未完成订单'").fetchone())

    def test_written_file_is_not_reported_missing_when_journal_fails(self):
        _,gid=self.generated()
        with patch('jst_orders.event',side_effect=sqlite3.OperationalError('injected journal failure')),self.assertLogs(level='ERROR'):
            r=ex.export(self.path,[gid],self.f.root/'journal-failed')
        self.assertEqual(len(r['files']),1);self.assertEqual(r['failed'],[]);self.assertTrue(r['warnings'])
        self.assertTrue(Path(r['files'][0]['path']).is_file())

    def test_focused_stage_queries_are_disjoint_and_do_not_mutate_facts(self):
        import daily_batch
        cache=self.f.root/'focus-index.db'
        def read(stage,kind='待处理',scope=None):return daily_batch.read(self.path,cache,{'stage_view':stage,'stage_filter':kind,'scope':scope})['records']
        self.assertEqual([r['warehouse'] for r in read('return')],['山东仓'])
        self.assertEqual([r['warehouse'] for r in read('jst','待生成')],['河北仓'])
        self.assertEqual(read('jst','待生成',[['other','河北仓']]),[])
        _,gid=self.generated();self.assertEqual(read('jst','待生成'),[])
        self.assertEqual(len(read('jst','已生成未锁')),1);self.assertEqual(len(read('payment')),1)
        j.lock(self.path,[gid]);self.assertEqual(read('jst','已生成未锁'),[]);self.assertEqual(len(read('jst','已锁')),1)
        lc.commit(self.path,j.payment_plan(self.path,gid));self.assertEqual(read('payment'),[]);self.assertEqual(len(read('receipt')),1)


if __name__=='__main__':unittest.main()
