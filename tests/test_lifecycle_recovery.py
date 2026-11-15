"""Recovery must resolve commits, not roll business facts backwards or guess."""
import copy
import gzip
import hashlib
import json
import sqlite3
import threading
import unittest
from contextlib import closing
from unittest.mock import patch
from pathlib import Path
import test_lifecycle as fixture
import lifecycle_recovery as recovery
import order_lifecycle as lc
import work_repository as wr
from runtime_support import Cancelled


class Recovery(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp();self.path=self.f.path
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start()
    def tearDown(self):self.backup.stop();self.f.tearDown()
    def task(self):return recovery.recent(self.path)[0]
    def doc(self,stage='return'):
        with closing(sqlite3.connect(self.path)) as db:return lc.current(db,self.f.h,stage)
    def interrupted(self,plan):
        with patch.object(lc,'_commit',side_effect=OSError('synthetic before business commit')):
            with self.assertRaisesRegex(OSError,'before business'):lc.commit(self.path,plan)
        return self.task()
    def test_reopen_retry_original_return_once(self):
        p=self.f.plan();task=self.interrupted(p)
        self.assertIsNone(self.doc());self.assertEqual(recovery.inspect(self.path,task['id'])['action'],'retry')
        r=recovery.recover(self.path,task['id']);self.assertEqual(r['action'],'completed')
        self.assertEqual(self.doc()['version'],1);self.assertEqual(self.doc()['digest'],p['plans'][0]['digest'])
        again=recovery.recover(self.path,task['id']);self.assertEqual(again['receipt'],r['receipt']);self.assertEqual(self.doc()['version'],1)
    def test_saved_receipt_failure_is_recovered_from_actual_database(self):
        p=self.f.plan()
        with patch.object(recovery,'_publish',side_effect=OSError('receipt disk unavailable')):result=lc.commit(self.path,p)
        task=self.task();self.assertEqual(task['state'],'prepared')
        with patch.object(lc,'_commit',side_effect=AssertionError('must not re-save')):
            r=recovery.recover(self.path,task['id'])
        self.assertEqual(r['receipt'],result);self.assertEqual(self.doc()['version'],1)
    def test_exception_after_actual_commit_does_not_replay(self):
        p=self.f.plan();commit=lc._commit
        def committed(*a,**kw):commit(*a,**kw);raise RuntimeError('lost reply after COMMIT')
        with patch.object(lc,'_commit',committed),self.assertRaisesRegex(RuntimeError,'lost reply'):lc.commit(self.path,p)
        task=self.task();self.assertEqual(task['error']['message'],'lost reply after COMMIT')
        self.assertIn('RuntimeError',task['error']['traceback'])
        self.assertEqual(recovery.recover(self.path,task['id'])['action'],'completed');self.assertEqual(self.doc()['version'],1)
    def test_recovery_preserves_newer_fact(self):
        p=self.f.plan();task=self.interrupted(p)
        self.f.save('return',[{'门店确认数量':27}])
        before=self.doc()
        with self.assertRaises(wr.Conflict):recovery.recover(self.path,task['id'])
        self.assertEqual(self.doc(),before)
    def test_upstream_change_does_not_bypass_original_validator(self):
        self.f.save('return');p=self.f.plan(self.f.scan('payment'));task=self.interrupted(p)
        self.f.save('return',[{'门店确认数量':31}])
        with self.assertRaisesRegex(wr.Conflict,'上游'):recovery.recover(self.path,task['id'])
        self.assertIsNone(self.doc('payment'))
    def test_missing_or_modified_original_file_is_not_trusted(self):
        for changed in (False,True):
            with self.subTest(changed=changed):
                p=self.f.plan();task=self.interrupted(p);f=Path(p['files'][0]['path'])
                if changed:f.write_bytes(b'not the frozen source')
                else:f.unlink()
                with self.assertRaises((ValueError,FileNotFoundError)):recovery.recover(self.path,task['id'])
                self.assertIsNone(self.doc())
    def test_checkpoint_digest_failure_cannot_save(self):
        task=self.interrupted(self.f.plan());file=recovery.directory(self.path)/(task['id']+'.json.gz')
        raw=gzip.decompress(file.read_bytes()).replace(b'"changed": true',b'"changed":false')
        self.assertNotEqual(hashlib.sha256(raw).hexdigest(),task['preview_sha256']);file.write_bytes(gzip.compress(raw))
        with self.assertRaisesRegex(ValueError,'摘要'):recovery.recover(self.path,task['id'])
        self.assertIsNone(self.doc())
    def test_cancel_before_save_has_no_checkpoint_or_business_mutation(self):
        p=self.f.plan();stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):lc.commit(self.path,p,cancel=stop)
        self.assertEqual(recovery.recent(self.path),[]);self.assertIsNone(self.doc())
    def test_recovery_can_be_cancelled_then_retried(self):
        task=self.interrupted(self.f.plan());stop=threading.Event();stop.set()
        with self.assertRaises(Cancelled):recovery.recover(self.path,task['id'],cancel=stop)
        self.assertIsNone(self.doc());self.assertEqual(recovery.recover(self.path,task['id'])['action'],'completed')
    def test_failed_recovery_retains_original_preview_for_next_attempt(self):
        task=self.interrupted(self.f.plan())
        with patch.object(lc,'_commit',side_effect=OSError('recovery attempt failed')),self.assertRaises(OSError):recovery.recover(self.path,task['id'])
        self.assertEqual(self.task()['id'],task['id']);self.assertTrue((recovery.directory(self.path)/(task['id']+'.json.gz')).exists())
        recovery.recover(self.path,task['id']);self.assertEqual(self.doc()['version'],1)
    def test_complete_return_operations_payment_receipt_share_same_recovery(self):
        for stage in ('return','operations','payment','receipt'):
            with self.subTest(stage=stage):
                if stage=='operations':
                    import operations_flow
                    plan=operations_flow.adopt_plan(self.path,[self.f.h])
                else:plan=self.f.plan(self.f.scan(stage))
                task=self.interrupted(plan);recovery.recover(self.path,task['id'])
                self.assertEqual(self.doc(stage)['digest'],plan['plans'][0]['digest'])
                self.assertEqual(self.doc(stage)['version'],1)
                self.assertFalse((recovery.directory(self.path)/(task['id']+'.json.gz')).exists())
    def test_stale_preview_recovery_does_not_mutate_other_warehouse(self):
        task=self.interrupted(self.f.plan());other=self.f.heads[1]
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(lc.current(db,other,'return'))
        recovery.recover(self.path,task['id'])
        with closing(sqlite3.connect(self.path)) as db:self.assertIsNone(lc.current(db,other,'return'))
    def test_scope_search_never_changes_atomic_batch_scope(self):
        from lifecycle_batch import prepare
        scans=[]
        for h in self.f.heads:
            old=self.f.h;self.f.h=h
            try:scans.append({'scan':self.f.scan(items=[{'供货仓':h['warehouse'],'商品编码':'0001' if h['warehouse']=='河北仓' else '0002'}]),'target':h,'selected':True})
            finally:self.f.h=old
        plan=prepare(self.path,scans);task=self.interrupted(plan)
        found=recovery.recent(self.path,[self.f.h]);self.assertEqual(found[0]['id'],task['id']);self.assertEqual(len(found[0]['entries']),2)
        recovery.recover(self.path,task['id'])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM lifecycle_versions').fetchone()[0],2)
    def test_deletion_removes_draft_and_late_reply_cannot_recreate(self):
        p=self.f.plan();task=self.interrupted(p)
        import data_maintenance as maintenance
        plan=maintenance.prepare(self.path,[self.f.h]);maintenance.execute(self.path,plan,True)
        self.assertEqual(recovery.recent(self.path),[])
        self.assertFalse(recovery._publish(self.path,task))
        self.assertFalse((recovery.directory(self.path)/(task['id']+'.json.gz')).exists())
    def test_identifiers_are_validated_before_access(self):
        for value in ('../workbench.sqlite3','x','A'*32):
            with self.subTest(value=value),self.assertRaises(ValueError):recovery.inspect(self.path,value)
    def test_bad_checkpoint_does_not_hide_other_recoverable_operations(self):
        task=self.interrupted(self.f.plan());root=recovery.directory(self.path)
        (root/('f'*32+'.json')).write_text('corrupted','utf-8')
        records=recovery.recent(self.path)
        self.assertEqual({r['id'] for r in records},{task['id'],'f'*32})
        self.assertEqual(next(r for r in records if r['id']=='f'*32)['state'],'unreadable')
        self.assertEqual(recovery.recover(self.path,task['id'])['action'],'completed')
