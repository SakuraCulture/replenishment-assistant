"""Portable batch stock contract; all data generated in isolated SQLite/Excel."""
import copy
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
from decimal import Decimal

import test_jst as fixture
import jst_orders as j
import jst_batches as b
import jst_exports as ex
import work_repository as wr


class Batches(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Service();self.f.setUp();self.path=self.f.path;self.h=self.f.h
    def tearDown(self):self.f.tearDown()
    def new(self,heads=None,stock=100):
        return b.preview_new(self.path,heads or [self.h],{'河北仓':fixture.catalog(stock=stock)})
    def balance(self,bid):
        with closing(sqlite3.connect(self.path)) as db:
            c=b.context(db,bid);stock=c['stocks']['河北仓']['catalog']['items']['0001']['可用数']
            return Decimal(str(stock))-b.reservations(db,bid,'河北仓').get('0001',Decimal(0))
    def test_same_batch_order_quantities_and_idempotence(self):
        h=self.f.second_store();p=self.new([h,self.h]);r=b.execute(self.path,p)
        self.assertFalse(r['errors']);self.assertEqual([j.get(self.path,g)['summary']['quantity'] for g in r['generated']],['40','60'])
        self.assertEqual(self.balance(r['batch_id']),0)
        again=b.execute(self.path,p);self.assertEqual(again['generated'],r['generated']);self.assertEqual(self.balance(r['batch_id']),0)
    def test_new_batch_does_not_subtract_old_unlocked_or_locked(self):
        p=self.new();r=b.execute(self.path,p);h=self.f.second_store();gid=r['generated'][0]
        frozen=copy.deepcopy(j.get(self.path,gid))
        for locked in (False,True):
            if locked:j.lock(self.path,[gid])
            q=self.new([h],80);self.assertEqual(q['payload']['plans'][0]['summary']['quantity'],'80')
        second=b.execute(self.path,q);self.assertFalse(second['errors']);self.assertEqual(self.balance(r['batch_id']),60)
        self.assertEqual(self.balance(second['batch_id']),0);self.assertEqual(j.get(self.path,gid),frozen)
    def test_cancel_and_queue_tick_cannot_resurrect(self):
        import jst_tasks
        p=self.new();r=b.execute(self.path,p);gid=r['generated'][0]
        j.cancel_generated(self.path,[gid],'未使用')
        self.assertEqual(self.balance(r['batch_id']),100)
        jst_tasks.drain(self.path,self.f.f.root/'settings')
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(j.active(db,self.h))
        self.assertEqual(self.balance(r['batch_id']),100)
        rejected=b.execute(self.path,p);self.assertTrue(rejected['errors'])
    def test_correction_subset_replaces_only_batch_selected(self):
        h=self.f.second_store();p=self.new([self.h,h]);r=b.execute(self.path,p)
        old=[j.get(self.path,g) for g in r['generated']]
        correction=b.preview_existing(self.path,r['batch_id'],[self.h],True)
        self.assertEqual(correction['payload']['plans'][0]['summary']['quantity'],'40')
        new=b.execute(self.path,correction);self.assertFalse(new['errors']);self.assertEqual(self.balance(r['batch_id']),0)
        self.assertEqual([j.get(self.path,g) for g in r['generated']],old)
        repeated=b.execute(self.path,correction);self.assertFalse(repeated['errors']);self.assertEqual(self.balance(r['batch_id']),0)
    def test_pause_resume_uses_frozen_stock_after_new_global_catalog(self):
        h=self.f.second_store();p=self.new([self.h,h]);stop=threading.Event()
        def progress(v):
            if getattr(v,'business_event',None):stop.set()
        r=b.execute(self.path,p,progress=progress,cancel=stop);self.assertTrue(r['cancelled'])
        self.f.refresh(1000)
        resumed=b.continue_batch(self.path,r['batch_id']);self.assertFalse(resumed['errors'])
        self.assertEqual(j.get(self.path,resumed['generated'][0])['summary']['quantity'],'60')
    def test_first_failure_prevents_later_consumption(self):
        h=self.f.second_store();p=self.new([self.h,h])
        with closing(sqlite3.connect(self.path)) as db,db:db.execute("CREATE TRIGGER fail_t BEFORE INSERT ON jst_generations WHEN NEW.oid='oid' BEGIN SELECT RAISE(ABORT,'injected'); END")
        r=b.execute(self.path,p);self.assertEqual(r['generated'],[]);self.assertEqual(self.balance(r['batch_id']),100)
        with closing(sqlite3.connect(self.path)) as db,db:db.execute('DROP TRIGGER fail_t')
        resumed=b.continue_batch(self.path,r['batch_id']);self.assertEqual([j.get(self.path,g)['summary']['quantity'] for g in resumed['generated']],['40','60'])
    def test_old_queued_task_requires_explicit_batch(self):
        import jst_tasks
        self.assertEqual(jst_tasks.drain(self.path,self.f.f.root/'settings'),[])
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(j.active(db,self.h))
    def test_preview_does_not_write_or_consume(self):
        self.new();self.assertEqual(b.listing(self.path),[])
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(j.active(db,self.h))

    def test_global_refresh_does_not_stale_batch_export_or_change_cells(self):
        import jst_results as results
        from test_jst_results import cells
        p=self.new();r=b.execute(self.path,p);gid=r['generated'][0]
        first=ex.export_checked(self.path,[gid],self.f.f.root/'first')['files'][0]['path']
        self.f.refresh(20)
        second=ex.export_checked(self.path,[gid],self.f.f.root/'second')['files'][0]['path']
        self.assertEqual(cells(first),cells(second))
        data=results.read(self.path,self.f.f.root/'cache.sqlite3',self.h['day'],batch_id=r['batch_id'])
        self.assertEqual(data['counts']['ready'],1);self.assertEqual(self.balance(r['batch_id']),60)

    def test_next_batch_pause_then_original_resume(self):
        h=self.f.second_store();p=self.new();b.register(self.path,p)
        q=self.new([h],60);second=b.execute(self.path,q)
        self.assertEqual(j.get(self.path,second['generated'][0])['summary']['quantity'],'60')
        self.assertEqual(b.listing(self.path)[1]['status'],'paused')
        first=b.continue_batch(self.path,p['payload']['batch_id'])
        self.assertEqual(j.get(self.path,first['generated'][0])['summary']['quantity'],'40')

    def test_cancel_after_register_persists_pause_before_queue_tick(self):
        p=self.new();e=threading.Event();register=b.register
        def registered(*a,**k):
            out=register(*a,**k);e.set();return out
        from runtime_support import Cancelled
        with patch.object(b,'register',registered),self.assertRaises(Cancelled):b.execute(self.path,p,cancel=e)
        self.assertEqual(b.listing(self.path)[0]['status'],'paused');self.assertEqual(b.drain(self.path),[])
        self.assertEqual(len(b.continue_batch(self.path,p['payload']['batch_id'])['generated']),1)

    def test_cancel_pending_blocks_inflight_and_allows_explicit_new_batch(self):
        p=b.register(self.path,self.new());bid=p['payload']['batch_id']
        self.assertEqual(b.cancel_unfinished(self.path,bid,'不再使用'),1)
        with self.assertRaises(wr.Conflict):b.execute(self.path,p)
        self.assertEqual(b.drain(self.path),[])
        self.assertEqual(len(b.execute(self.path,self.new())['generated']),1)

    def test_two_warehouses_same_sku_are_independent(self):
        import order_lifecycle as lc
        h=self.f.second_store()
        # Add an independent signed SD head using the existing fixture contract.
        with closing(sqlite3.connect(self.path)) as db,db:
            raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=?',(h['oid'],)).fetchone()[0]
            import normal_orders as no
            value=no.decode(raw)
            for row in value['result']['rows']:
                if row.get('E')=='0001':row['AU']='山东仓'
            db.execute('UPDATE normal_revisions SET payload=?,digest=? WHERE oid=?',(no.encode(value),no.fingerprint_owned(value['result'],value.get('new_focus')),h['oid']))
        # Use normal lifecycle APIs so source rows/units remain validated.
        targets=lc.current_targets(self.path);sd=next(x for x in targets if x['store']=='U' and x['warehouse']=='山东仓')
        self.f.f.h=sd;self.f.f.save('return',[{'门店编码':'U','供货仓':'山东仓','商品编码':'0001','门店确认数量':80}]);self.f.f.h=self.h
        p=b.preview_new(self.path,[self.h,sd],{'河北仓':fixture.catalog(stock=100),'山东仓':fixture.catalog('山东仓',stock=70)})
        r=b.execute(self.path,p);self.assertFalse(r['errors'])
        self.assertEqual([j.get(self.path,g)['summary']['quantity'] for g in r['generated']],['40','70'])

    def test_deleted_order_is_removed_from_members_and_locked_usage_kept(self):
        import data_maintenance as dm
        h=self.f.second_store();p=self.new([self.h,h]);r=b.execute(self.path,p);j.lock(self.path,[r['generated'][0]])
        dm.execute(self.path,dm.prepare(self.path,[self.h]),True)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(len(b.context(db,r['batch_id'])['members']),1)
        self.assertEqual(self.balance(r['batch_id']),0)


if __name__=='__main__':unittest.main()
