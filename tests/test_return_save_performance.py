"""Exact JSON, proof fallback and per-order validation; isolated fixtures only."""
import copy,hashlib,json,math,sqlite3,sys,unittest,threading
from contextlib import closing
from datetime import date,datetime,timezone
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
import test_lifecycle as fixture
import order_lifecycle as lc,work_repository as wr,purchases
import lifecycle_save_encoding as encoding,lifecycle_read_proofs as proofs
import storage_chunks
from runtime_support import Cancelled


class Encoding(unittest.TestCase):
    def test_exact_existing_json_and_digest(self):
        for i in range(25):
            payload={'schema':2,'files':[{'path':'C:/测试/原表.xlsx','sha':'abc','name':'原表.xlsx','extra':i}],
                     'rows':[{'sku':'00001','name':'汉字\\\"\n\t🙂','quantity':-0.0,'empty':None,'values':[True,False,i,i/7]}],
                     'saved':datetime(2026,11,15,3,4,tzinfo=timezone.utc),'day':date(2026,11,15),
                     'basis':{'products':{'a':{'quantity':'0'}}},'odd-key-\0':{'b':[{'a':'é'}]}}
            raw,digest=encoding.encode(payload)
            self.assertEqual(raw,wr.dumps(payload));self.assertEqual(digest,lc.fingerprint(payload))
            self.assertEqual(encoding.encode(payload,retain=False),(None,digest))

    def test_invalid_full_file_metadata_still_checked_when_unchanged(self):
        for value in (float('nan'),float('inf'),object()):
            with self.subTest(value=type(value).__name__):
                p={'schema':2,'files':[{'name':'x','sha':'a','unused':value}],'rows':[]}
                with self.assertRaises((ValueError,TypeError)):wr.dumps(p)
                with self.assertRaises((ValueError,TypeError)):encoding.encode(p,retain=False)

    def test_non_string_top_keys_keep_existing_failure(self):
        p={'files':[],1:'integer'}
        with self.assertRaises(TypeError):lc.fingerprint(p)
        with self.assertRaises(TypeError):encoding.encode(p)


class Save(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def plan(self,stage='return',items=None):return self.f.plan(self.f.scan(stage,items))
    def stored(self,stage):
        with closing(sqlite3.connect(self.f.path)) as db:
            if not lc.exists(db):return None
            return db.execute('SELECT digest,payload FROM lifecycle_versions WHERE stage=? ORDER BY version DESC LIMIT 1',(stage,)).fetchone()

    def test_stored_payload_and_fingerprint_byte_identical(self):
        for stage in ('return','operations','payment','receipt'):
            plan=self.plan(stage);p=plan['plans'][0];expected=wr.dumps(p['payload'])
            lc.commit(self.f.path,plan);digest,raw=self.stored(stage)
            self.assertEqual(raw,expected);self.assertEqual(digest,lc.fingerprint(p['payload']))
            self.assertNotIn('_read_proofs',raw)

    def test_disk_staging_keeps_exact_text(self):
        plan=self.plan()
        p=plan['plans'][0]
        with patch('adaptive_resources.Budget') as budget:
            budget.return_value.cache_bytes=0
            receipt=lc.commit(self.f.path,plan)
        self.assertTrue(receipt[0]['changed'])
        self.assertEqual(self.stored('return'),(p['digest'],wr.dumps(p['payload'])))

    def test_operations_reuses_exact_verified_return_without_decoding_large_payload(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        self.assertIn('return',plan['plans'][0]['_read_proofs'])
        with patch('order_lifecycle.decode',wraps=lc.decode) as decode:
            lc.commit(self.f.path,copy.deepcopy(plan))
        self.assertFalse(any(call.args[0] is not None for call in decode.call_args_list))
        self.assertEqual(self.f.view()['documents']['operations']['payload']['rows'][0]['base_quantity'],'5')

    def test_changed_raw_bytes_same_version_and_digest_cannot_reuse_proof(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        with closing(sqlite3.connect(self.f.path)) as db,db:
            raw=db.execute("SELECT payload FROM lifecycle_versions WHERE stage='return'").fetchone()[0]
            value=wr.loads(raw);value['rows'][0]['base_quantity']='888'
            db.execute("UPDATE lifecycle_versions SET payload=? WHERE stage='return'",(wr.dumps(value),))
        with self.assertRaisesRegex(ValueError,'快照校验失败'):lc.commit(self.f.path,plan)
        self.assertIsNone(self.stored('operations'))

    def test_current_version_change_remains_conflict(self):
        lc.commit(self.f.path,self.plan());plan=self.plan()
        with closing(sqlite3.connect(self.f.path)) as db,db:
            db.execute("UPDATE lifecycle_versions SET version=2 WHERE stage='return'")
        with self.assertRaisesRegex(wr.Conflict,'预览后被更新'):lc.commit(self.f.path,plan)

    def test_upstream_valid_revision_change_remains_conflict(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        lc.commit(self.f.path,self.plan(items=[{'门店确认数量':8}]))
        with self.assertRaisesRegex(wr.Conflict,'上游单据'):lc.commit(self.f.path,plan)

    def test_valid_changed_payload_with_recomputed_digest_not_reused(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        with closing(sqlite3.connect(self.f.path)) as db,db:
            value=wr.loads(db.execute("SELECT payload FROM lifecycle_versions WHERE stage='return'").fetchone()[0])
            value['rows'][0]['base_quantity']='9'
            db.execute("UPDATE lifecycle_versions SET payload=?,digest=? WHERE stage='return'",(wr.dumps(value),lc.fingerprint(value)))
        with self.assertRaisesRegex(wr.Conflict,'上游单据'):lc.commit(self.f.path,plan)

    def test_nonbusiness_file_path_change_requires_full_decode_without_false_block(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        with closing(sqlite3.connect(self.f.path)) as db,db:
            value=wr.loads(db.execute("SELECT payload FROM lifecycle_versions WHERE stage='return'").fetchone()[0])
            value['files'][0]['path']='retained-original-different-path'
            db.execute("UPDATE lifecycle_versions SET payload=? WHERE stage='return'",(wr.dumps(value),))
        with patch('order_lifecycle.decode',wraps=lc.decode) as decode:lc.commit(self.f.path,plan)
        self.assertTrue(any(c.args[0] is not None for c in decode.call_args_list))

    def test_schema_one_dependency_has_identical_quantity_semantics(self):
        lc.commit(self.f.path,self.plan())
        old={'schema':1,'files':[],'rows':[{'sku':'0001','name':'测试商品','base_quantity':'5'}]}
        with closing(sqlite3.connect(self.f.path)) as db,db:db.execute("UPDATE lifecycle_versions SET payload=?,digest=? WHERE stage='return'",(wr.dumps(old),lc.fingerprint(old)))
        result=lc.commit(self.f.path,self.plan('operations'))
        self.assertTrue(result[0]['changed']);self.assertEqual(wr.loads(self.stored('operations')[1])['rows'][0]['base_quantity'],'5')

    def test_forged_or_edited_proofs_fall_back_to_original_decoder(self):
        lc.commit(self.f.path,self.plan())
        for mode in ('edit','wrong_key','bad_type','foreign_key'):
            with self.subTest(mode=mode):
                plan=self.plan('operations');p=plan['plans'][0];proof=p['_read_proofs']['return']
                if mode=='edit':proof['value'][5]='{}'
                elif mode=='wrong_key':proof['value'][0][0]='someone-else'
                elif mode=='bad_type':p['_read_proofs']=[]
                else:
                    with patch('lifecycle_read_proofs._KEY',b'other-process'):
                        with patch('order_lifecycle.decode',wraps=lc.decode) as decode:lc.commit(self.f.path,plan)
                    self.assertTrue(any(c.args[0] is not None for c in decode.call_args_list));continue
                with patch('order_lifecycle.decode',wraps=lc.decode) as decode:lc.commit(self.f.path,plan)
                self.assertTrue(any(c.args[0] is not None for c in decode.call_args_list))

    def test_old_plan_without_proofs_still_works(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations');plan['plans'][0].pop('_read_proofs')
        self.assertTrue(lc.commit(self.f.path,plan)[0]['changed'])

    def test_same_content_new_saved_stamp_falls_back_without_false_block(self):
        lc.commit(self.f.path,self.plan());plan=self.plan('operations')
        with closing(sqlite3.connect(self.f.path)) as db,db:db.execute("UPDATE lifecycle_versions SET saved='2026-11-15T13:00:00+08:00'")
        self.assertTrue(lc.commit(self.f.path,plan)[0]['changed'])

    def test_unchanged_save_validates_and_does_not_append(self):
        scan=self.f.scan();lc.commit(self.f.path,self.f.plan(scan));plan=self.f.plan(scan)
        self.assertFalse(plan['plans'][0]['changed'])
        with patch('lifecycle_save_encoding.encode',wraps=encoding.encode) as encode:
            result=lc.commit(self.f.path,plan)
        self.assertFalse(result[0]['changed']);self.assertFalse(encode.call_args.kwargs['retain'])
        with closing(sqlite3.connect(self.f.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_versions').fetchone()[0],1)

    def test_unchanged_save_invalid_extra_file_metadata_still_fails(self):
        scan=self.f.scan();lc.commit(self.f.path,self.f.plan(scan));plan=self.f.plan(scan)
        plan['plans'][0]['payload']['files'][0]['unused']=float('nan')
        with self.assertRaises(ValueError):lc.commit(self.f.path,plan)
        with closing(sqlite3.connect(self.f.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_versions').fetchone()[0],1)

    def test_mutated_preview_and_original_file_still_rejected(self):
        plan=self.plan();plan['plans'][0]['payload']['rows'][0]['base_quantity']='99'
        with self.assertRaisesRegex(ValueError,'预览内容变化'):lc.commit(self.f.path,plan)
        plan=self.plan();Path(plan['files'][0]['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError,'原文件'):lc.commit(self.f.path,plan)
        self.assertIsNone(self.stored('return'))

    def test_validation_scales_by_order_not_sku(self):
        scan=self.f.scan(items=[{'商品编码':f'{i:04d}'} for i in range(100)])
        with patch('purchases.validate_head',wraps=purchases.validate_head) as validate:self.f.plan(scan)
        self.assertEqual(validate.call_count,2)

    def test_external_commit_invalidates_preparation_head_cache(self):
        scan=self.f.scan(items=[{},{}]);original=purchases.validate_head;calls=[]
        def change(db,head):
            original(db,head);calls.append(1)
            if len(calls)==1:
                with closing(sqlite3.connect(self.f.path)) as other,other:other.execute('UPDATE normal_orders SET current=2')
        with patch('purchases.validate_head',change):
            with self.assertRaises(wr.Conflict):self.f.plan(scan)

    def test_changed_head_metadata_cannot_hit_preparation_cache(self):
        scan=self.f.scan(items=[{},{}]);f=self.f
        class Cancel:
            calls=0
            def is_set(self):
                self.calls+=1
                if self.calls==2:f.h['visibility_token']=999
                return False
        bindings={lc.binding_key(r):f.h for r in scan['rows']}
        with self.assertRaises(wr.Conflict):lc.prepare(f.path,scan,bindings,cancel=Cancel())

    def test_unrelated_commit_preserves_prepared_document(self):
        scan=self.f.scan(items=[{},{}]);original=purchases.validate_head;calls=[]
        before=self.f.plan(scan)['plans'][0]
        def change(db,head):
            original(db,head);calls.append(1)
            if len(calls)==1:
                with closing(sqlite3.connect(self.f.path)) as other,other:other.execute("INSERT INTO stores VALUES('unrelated','{}')")
        with patch('purchases.validate_head',change):plan=self.f.plan(scan)
        self.assertEqual(len(plan['plans']),1)
        for field in ('payload','digest','expected','changed'):
            self.assertEqual(plan['plans'][0][field],before[field])
        self.assertEqual(lc.commit(self.f.path,plan)[0]['version'],1)

    def hold_reader(self):
        storage_chunks.backup_before_write(self.f.path)
        ready=threading.Event();release=threading.Event()
        def read():
            with closing(sqlite3.connect(self.f.path)) as db:
                db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall();ready.set();release.wait(5);db.rollback()
        thread=threading.Thread(target=read);thread.start();self.assertTrue(ready.wait(2))
        return release,thread

    def test_short_reader_overlap_retries_only_after_rollback_and_saves_once(self):
        plan=self.plan();release,thread=self.hold_reader();timer=threading.Timer(.45,release.set);timer.start();messages=[]
        try:result=lc.commit(self.f.path,plan,progress=messages.append)
        finally:release.set();thread.join();timer.cancel()
        self.assertEqual(result[0]['version'],1);self.assertTrue(any('已安全回滚' in str(m) for m in messages))
        self.assertTrue(any(getattr(m,'active',None) is False for m in messages))
        with closing(sqlite3.connect(self.f.path)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_versions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_events').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_tasks').fetchone()[0],1)

    def test_persistent_reader_remains_bounded_and_rolls_back_all(self):
        plan=self.plan();release,thread=self.hold_reader()
        try:
            with patch('order_lifecycle._SAVE_BUSY_SECONDS',.1):
                with self.assertRaises(sqlite3.OperationalError):lc.commit(self.f.path,plan)
        finally:release.set();thread.join()
        self.assertIsNone(self.stored('return'))

    def test_cancellation_after_busy_rollback_is_safe(self):
        plan=self.plan();release,thread=self.hold_reader();cancel=threading.Event()
        def progress(message):
            if '已安全回滚' in str(message):cancel.set()
        try:
            with self.assertRaises(Cancelled):lc.commit(self.f.path,plan,progress=progress,cancel=cancel)
        finally:release.set();thread.join()
        self.assertIsNone(self.stored('return'))

    def test_busy_retry_revalidates_changed_source_before_saving(self):
        plan=self.plan();release,thread=self.hold_reader()
        def progress(message):
            if '已安全回滚' in str(message):
                release.set();thread.join()
                with closing(sqlite3.connect(self.f.path)) as db,db:db.execute('UPDATE normal_orders SET current=2')
        try:
            with self.assertRaises(wr.Conflict):lc.commit(self.f.path,plan,progress=progress)
        finally:release.set();thread.join()
        self.assertIsNone(self.stored('return'))

    def test_unknown_commit_outcome_never_replays(self):
        plan=self.plan();storage_chunks.backup_before_write(self.f.path);connect=sqlite3.connect;calls=[]
        class AfterCommit(sqlite3.Connection):
            def commit(db):
                super(AfterCommit,db).commit();calls.append(1);raise sqlite3.OperationalError('database is locked')
        def connection(*a,**kw):
            if kw.get('timeout')==.15:kw['factory']=AfterCommit
            return connect(*a,**kw)
        with patch('sqlite3.connect',connection):
            with self.assertRaises(sqlite3.OperationalError):lc.commit(self.f.path,plan)
        self.assertEqual(len(calls),1)
        with closing(sqlite3.connect(self.f.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM lifecycle_versions').fetchone()[0],1)

    def test_non_busy_commit_failure_never_replays(self):
        plan=self.plan();storage_chunks.backup_before_write(self.f.path);connect=sqlite3.connect;calls=[]
        class IOErrorCommit(sqlite3.Connection):
            def commit(db):calls.append(1);raise sqlite3.OperationalError('disk I/O error')
        def connection(*a,**kw):
            if kw.get('timeout')==.15:kw['factory']=IOErrorCommit
            return connect(*a,**kw)
        with patch('sqlite3.connect',connection):
            with self.assertRaisesRegex(sqlite3.OperationalError,'disk I/O'):lc.commit(self.f.path,plan)
        self.assertEqual(len(calls),1);self.assertIsNone(self.stored('return'))

    def test_failed_rollback_never_replays_unknown_attempt(self):
        plan=self.plan();storage_chunks.backup_before_write(self.f.path);connect=sqlite3.connect;calls=[]
        class FailedRollback(sqlite3.Connection):
            def commit(db):calls.append(1);raise sqlite3.OperationalError('database is locked')
            def rollback(db):raise sqlite3.OperationalError('rollback I/O error')
        def connection(*a,**kw):
            if kw.get('timeout')==.15 and not kw.get('uri'):kw['factory']=FailedRollback
            return connect(*a,**kw)
        with patch('sqlite3.connect',connection):
            with self.assertRaisesRegex(sqlite3.OperationalError,'rollback I/O'):lc.commit(self.f.path,plan)
        self.assertEqual(len(calls),1);self.assertIsNone(self.stored('return'))

    def test_encoding_releases_writer_and_commit_revalidates_the_order(self):
        plan=self.plan()
        original=encoding.encode
        def encode_and_reissue(*args,**kwargs):
            value=original(*args,**kwargs)
            with closing(sqlite3.connect(self.f.path,timeout=0)) as other,other:
                other.execute('UPDATE normal_orders SET current=2')
            return value
        with patch.object(encoding,'encode',encode_and_reissue):
            with self.assertRaises(wr.Conflict):lc.commit(self.f.path,plan)
        self.assertIsNone(self.stored('return'))


if __name__=='__main__':unittest.main()
