"""Bounded v2 encoding, complete-batch atomicity and temporary-file failures."""
import copy
import hashlib
import sqlite3
import tempfile
import threading
import subprocess
import sys
import os
import unittest
from contextlib import closing
from datetime import date,datetime
from pathlib import Path
from unittest.mock import patch

import encoding_store as spool
import storage_chunks as chunks
import normal_orders
import warehouse_execution as execution
import work_repository as wr
from runtime_support import Cancelled
import test_signing_failures as failures


def interrupted_worker(connection,path,cancel):
    import work_io_process
    original=chunks.prepare
    def stop(*args,**kw):
        if kw.get('store') is not None:
            kw['store']._write(b'isolated incomplete encoding')
            os._exit(29)
        return original(*args,**kw)
    chunks.prepare=stop
    work_io_process.serve(connection,path,cancel)


class SpoolFormat(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'workbench.sqlite3'

    def compare(self,value):
        before=copy.deepcopy(value);old=chunks.prepare(value,binding='identity')
        with spool.Store(self.path,cache_bytes=1024) as store,closing(sqlite3.connect(':memory:')) as db:
            new=chunks.prepare(value,binding='identity',store=store)
            self.assertEqual((new.tree,new.digest,new.size,new.blob()),(old.tree,old.digest,old.size,old.blob()))
            self.assertEqual(dict(new.chunks.items()),old.chunks)
            blob=chunks.put(db,new)
            self.assertEqual(chunks.load(blob,db,binding='identity'),wr.loads(wr.dumps(value)))
            self.assertLessEqual(store.stats()['cache_peak_bytes'],1024)
            self.assertEqual(spool.digest(value),hashlib.sha256(wr.dumps(value).encode()).hexdigest())
            name=store.location
        self.assertFalse(Path(name).exists());self.assertEqual(value,before)

    def test_v2_bytes_manifest_binding_order_and_special_scalars(self):
        self.compare({'嵌套':{'数组':[None,True,False,0,-0.0,1.25,'换行\n汉字😀\\"',date(2026,11,16),datetime(2026,11,16,1,2)]},'small':tuple(range(3))})

    def test_large_map_list_and_deep_large_block_match_existing_format(self):
        rows=[{'sku':str(n),'text':'合成商品'+str(n),'quantity':n%9,'notes':'说明'*31} for n in range(1500)]
        self.compare({'map':{f'k{n:04}':rows[n] for n in range(700)},'list':rows,
                      'deep':{'a':{'b':{'c':{'d':{'e':{'rows':rows}}}}}}})

    def test_long_unicode_scalar_and_fragment_boundaries(self):
        self.compare({'large':['😀汉字\\"\n'*40000]+list(range(520))})

    def test_nonfinite_and_unsupported_values_rejected(self):
        for value in (float('nan'),float('inf'),object()):
            with spool.Store(self.path) as store,self.assertRaises((TypeError,ValueError)):
                chunks.prepare({'value':value},store=store)
            self.assertEqual(list(self.path.parent.glob('.encoding-cache/*.tmp')),[])

    def test_shared_spool_dedup_and_database_reuse_do_not_need_known_index(self):
        with spool.Store(self.path) as store,closing(sqlite3.connect(':memory:')) as db:
            one=chunks.prepare({'rows':list(range(2000))},store=store)
            size=store.stats()['temporary_bytes'];two=chunks.prepare({'rows':list(range(2000))},store=store)
            self.assertEqual(store.stats()['temporary_bytes'],size)
            chunks.put(db,one);count=db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0]
            chunks.put(db,two);self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0],count)

    def test_prepared_business_hash_matches_original_with_focus_and_root_omissions(self):
        import new_product_focus as focus
        value={'rows':[{'sku':str(n),'quantity':n,'nested':{'task':'keep'}} for n in range(1000)],
               'created':'ignored','task':{'pid':1},'version':'ignored','metadata':{'version':'keep'}}
        for profile in (None,focus.empty(wr.today()[:7])):
            revision=focus.validate(profile)['revision'] if profile else None
            with spool.Store(self.path) as store:
                prepared=chunks.prepare({'result':value},store=store)
                self.assertEqual(chunks.business_digest(prepared,('result',),revision),normal_orders.fingerprint(value,profile))

    def test_process_exit_deletes_only_its_private_temporary_file(self):
        pid_file=self.path.parent/'temp-path.txt'
        script="import sys,os;sys.path.insert(0,sys.argv[1]);from encoding_store import Store;s=Store(sys.argv[2]);s._write(b'test');open(sys.argv[3],'w').write(s.location);os._exit(17)"
        process=subprocess.run([sys.executable,'-c',script,str(Path(spool.__file__).parent),str(self.path),str(pid_file)])
        self.assertEqual(process.returncode,17);self.assertFalse(Path(pid_file.read_text()).exists())
        self.assertTrue(pid_file.exists())

    def test_encoding_cache_is_not_order_migration_content(self):
        import order_migration
        self.assertFalse(order_migration.owned('.encoding-cache/signing-example.tmp'))

    def test_corrupt_or_missing_temporary_block_rolls_back(self):
        for mode in ('corrupt','truncate'):
            with self.subTest(mode=mode),spool.Store(self.path,cache_bytes=0) as store,closing(sqlite3.connect(self.path)) as db:
                p=chunks.prepare({'data':'unique'+mode},store=store)
                if mode=='truncate':store.file.truncate(1)
                else:store.file.seek(0);store.file.write(b'bad')
                with self.assertRaisesRegex(ValueError,'临时内容块'),db:chunks.put(db,p)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0],0)


class SigningSpool(unittest.TestCase):
    setUp=failures.SigningFailures.setUp
    tearDown=failures.SigningFailures.tearDown
    payload=failures.SigningFailures.payload

    def confirm(self,payload,**kwargs):
        codes=list(payload['results'])
        return self.repo.confirm(payload,codes,None,{s:None for s in codes},headers_only=True,**kwargs)

    def test_entire_day_and_independent_warehouses_commit_once(self):
        p=self.payload(3);p['loaded']={'rows':[{'原始库存':n} for n in range(1000)]}
        signals=[];prepared_in_writer=[];original=chunks.prepare
        def prepare(*args,**kw):
            with closing(self.work.connect()) as other:
                other.execute('PRAGMA busy_timeout=0');other.execute('BEGIN IMMEDIATE');other.rollback()
            prepared_in_writer.append(True)
            return original(*args,**kw)
        with patch.object(chunks,'prepare',side_effect=prepare),patch.object(self.work,'known_chunks',side_effect=AssertionError('unbounded hash index')):
            heads=self.confirm(p,progress=lambda v:signals.append(v.active) if hasattr(v,'active') else None)
        self.assertTrue(prepared_in_writer);self.assertEqual(signals,[True,False])
        self.assertEqual(self.work.get_job(p['day'])['payload'],p)
        self.assertEqual([h['store'] for h in heads],list(p['results']))
        for head in heads:
            saved=self.repo.get(p['day'],head['store']);self.assertEqual(saved['payload']['result'],p['results'][head['store']])
            self.assertEqual(saved['digest'],normal_orders.fingerprint(saved['payload']['result']))
            self.assertEqual(len(head['warehouse_heads']),2)
        self.assertEqual(list((self.work.path.parent/'.encoding-cache').iterdir()),[])

    def test_disk_full_retains_source_and_original_exception(self):
        p=self.payload(2)
        with patch.object(spool.shutil,'disk_usage',return_value=type('Space',(),{'free':1})()):
            with self.assertRaisesRegex(OSError,'空间不足') as caught:self.confirm(p)
        self.assertIn('temporary_location',caught.exception.encoding_stats)
        self.assertEqual(self.repo.headers(),[]);self.assertIsNone(self.work.get_job(p['day']))
        self.assertEqual(list((self.work.path.parent/'.encoding-cache').iterdir()),[])

    def test_cancel_encoding_or_writer_never_publishes_partial_batch(self):
        for stage in ('编码','写锁'):
            cancel=threading.Event()
            with self.assertRaises(Cancelled):
                self.confirm(self.payload(2),cancel=cancel,progress=lambda v:cancel.set() if stage in str(v) else None)
            self.assertEqual(self.repo.headers(),[])
            self.assertEqual(list((self.work.path.parent/'.encoding-cache').iterdir()),[])

    def test_cancel_inside_one_large_streamed_block_is_checked(self):
        p=self.payload();p['deep']={'a':{'b':{'c':{'d':{'large':'长字段'*100000}}}}}
        cancel=threading.Event();original=spool.Store.encode
        def encode(store,*args,**kw):store.last_tick=0;return original(store,*args,**kw)
        with patch.object(spool.Store,'encode',encode),self.assertRaises(Cancelled):
            self.confirm(p,cancel=cancel,progress=lambda value:cancel.set() if '流式编码' in str(value) else None)
        self.assertFalse(self.repo.headers());self.assertFalse(list((self.work.path.parent/'.encoding-cache').iterdir()))

    def test_waiting_for_real_writer_is_cancellable_without_partial_commit(self):
        chunks.backup_before_write(self.work.path)
        cancel=threading.Event();timer=None
        def progress(value):
            nonlocal timer
            if '等待归档写锁' in str(value):timer=threading.Timer(.2,cancel.set);timer.start()
        with closing(self.work.connect()) as blocker:
            blocker.execute('BEGIN IMMEDIATE')
            try:
                with self.assertRaises(Cancelled):self.confirm(self.payload(),cancel=cancel,progress=progress)
            finally:
                blocker.rollback()
                if timer:timer.join()
        self.assertFalse(self.repo.headers());self.assertIsNone(self.work.get_job(wr.today()))

    def test_concurrent_draft_change_preserves_the_other_writer(self):
        p=self.payload();saved=[]
        def concurrent(heads):saved.append(self.work.save_job(p['day'],p,None))
        with self.assertRaises(wr.Conflict):self.confirm(p,prepared_heads=concurrent)
        self.assertFalse(self.repo.headers());self.assertEqual(self.work.get_job(p['day'])['revision'],saved[0]['revision'])

    def test_actual_worker_exit_leaves_no_business_records_or_private_file(self):
        from ui_work_io import Session
        p=self.payload()
        with patch('ui_work_io.serve',interrupted_worker),Session(self.work.path) as session:
            session.call('delivery_prepare',[p,list(p['results'])],lambda _:None)
            with self.assertRaisesRegex(RuntimeError,'处理进程退出'):
                session.call('delivery_execute',str(self.output),lambda _:None)
        self.assertFalse(self.repo.headers());self.assertIsNone(self.work.get_job(p['day']))
        self.assertFalse(list((self.work.path.parent/'.encoding-cache').iterdir()))

    def test_failure_after_first_store_rolls_back_all_orders(self):
        with closing(self.work.connect()) as db,db:
            db.execute("CREATE TRIGGER fail_second BEFORE INSERT ON normal_revisions WHEN (SELECT count(*) FROM normal_revisions)>0 BEGIN SELECT RAISE(ABORT,'second store fails'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,'second store fails'):self.confirm(self.payload(3))
        self.assertEqual(self.repo.headers(),[]);self.assertIsNone(self.work.get_job(wr.today()))

    def test_unchanged_confirmation_does_not_reencode_each_child(self):
        p=self.payload(2);heads=self.confirm(p);job=self.work.get_job(p['day']);original=chunks.prepare
        calls=[]
        def counted(*args,**kw):calls.append(True);return original(*args,**kw)
        with patch.object(chunks,'prepare',side_effect=counted):
            again=self.repo.confirm(p,list(p['results']),job['revision'],{h['store']:h['revision'] for h in heads},headers_only=True)
        self.assertEqual(again,heads);self.assertEqual(len(calls),1)

    def test_legacy_history_prepared_before_writer_preserves_sibling(self):
        from test_archive_orders import seed
        work,repo,old=seed(self.root/'legacy')
        with closing(work.connect()) as db,spool.Store(work.path) as store:
            prepared=execution.prepare_parent(db,'oid',store)
            db.execute('BEGIN IMMEDIATE');execution.ensure_parent(db,'oid',prepared=prepared);db.commit()
        with closing(work.connect()) as db:
            heads=execution.heads(db)
            self.assertEqual({h['warehouse'] for h in heads},{'河北仓','山东仓'})
            for head in heads:
                order=execution.get_order(repo,head)
                self.assertEqual(order['digest'],normal_orders.fingerprint(old['result']))
                self.assertTrue(all(row['AU']==head['warehouse'] for row in order['payload']['result']['rows']))

    def test_stale_legacy_history_refused(self):
        from test_archive_orders import seed
        work,repo,old=seed(self.root/'legacy')
        with closing(work.connect()) as db,spool.Store(work.path) as store:
            prepared=execution.prepare_parent(db,'oid',store)
            db.execute("UPDATE normal_revisions SET saved='changed'");db.commit()
            with self.assertRaises(wr.Conflict):execution.ensure_parent(db,'oid',prepared=prepared)

    def test_worker_keeps_review_export_after_failed_delivery(self):
        from ui_work_io import Session
        from work_failures import WorkFailure
        p=self.payload(2)
        with Session(self.work.path) as session:
            session.call('delivery_prepare',[p,list(p['results'])],lambda _:None)
            one=self.root/'before.json';session.call('review_export',str(one),lambda _:None)
            with closing(self.work.connect()) as db,db:
                db.execute("CREATE TRIGGER fail_sign BEFORE INSERT ON normal_revisions BEGIN SELECT RAISE(ABORT,'isolated failure'); END")
            with self.assertRaises(WorkFailure):session.call('delivery_execute',str(self.output),lambda _:None)
            two=self.root/'after.json';session.call('review_export',str(two),lambda _:None)
            self.assertEqual(one.read_bytes(),two.read_bytes())
            with closing(self.work.connect()) as db,db:db.execute('DROP TRIGGER fail_sign')
            result=session.call('delivery_execute',str(self.output),lambda _:None)
            self.assertEqual(len(result['paths']),4)


if __name__=='__main__':unittest.main()
