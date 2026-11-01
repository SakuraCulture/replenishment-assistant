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
import test_lifecycle as fixture
from runtime_support import Cancelled,CommitProgress


def catalog(warehouse='河北仓', stock=100, step=1, price=2):
    items={'0001':{'商品编码':'0001','分类':'测试','基本售价':price,'出库箱规':step,'商品状态':'启用','可用数':stock,'商品名称':'测试','_row':2}}
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
        self.assertEqual(b.sheetnames,['订单明细']);self.assertEqual(list(b.active.values),[('商品编码','单价','数量'),('0001',2,40)])
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


if __name__=='__main__':unittest.main()
