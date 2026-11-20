"""Owned previews, exact stored facts, deletion-aware run reuse and safe lock retry."""
import copy,hashlib,sqlite3,sys,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
import test_lifecycle as fixture
import test_jst as jst_fixture
import test_jst_batches as batch_fixture
import order_lifecycle as lc,lifecycle_batch as batch,work_repository as wr
import jst_orders as jo,jst_batches as jb,purchases
from runtime_support import Cancelled


class PaymentReceiptProofs(unittest.TestCase):
    def setUp(self):
        self.f=fixture.LifecycleTests();self.f.setUp()
        self.f.save('return');self.f.save('operations')
    def tearDown(self):self.f.tearDown()
    def plan(self,stage):return self.f.plan(self.f.scan(stage))
    def test_payment_receipt_initial_and_unchanged_keep_exact_full_payload(self):
        for stage in ('payment','receipt'):
            scan=self.f.scan(stage)
            for repeated in (False,True):
                with self.subTest(stage=stage,repeated=repeated):
                    plan=self.f.plan(scan);p=plan['plans'][0];raw=wr.dumps(p['payload'])
                    self.assertIn('return',p['_read_proofs']);self.assertIn('operations',p['_read_proofs'])
                    if stage=='receipt':self.assertIn('payment',p['_read_proofs'])
                    with patch('lifecycle_facts.decode',wraps=lc.decode) as decode:result=lc.commit(self.f.path,plan)
                    self.assertFalse(any(c.args[0] is not None for c in decode.call_args_list))
                    self.assertEqual(result[0]['changed'],not repeated)
                    with closing(sqlite3.connect(self.f.path)) as db:
                        actual=db.execute('SELECT payload,digest FROM lifecycle_versions WHERE stage=? ORDER BY version DESC LIMIT 1',(stage,)).fetchone()
                    self.assertEqual(actual,(raw,lc.fingerprint(p['payload'])));self.assertNotIn('_read_proofs',actual[0])
    def test_payment_upstream_changed_bytes_still_runs_complete_validation(self):
        plan=self.plan('payment')
        with closing(sqlite3.connect(self.f.path)) as db,db:
            payload=wr.loads(db.execute("SELECT payload FROM lifecycle_versions WHERE stage='operations'").fetchone()[0])
            payload['rows'][0]['base_quantity']='999'
            db.execute("UPDATE lifecycle_versions SET payload=? WHERE stage='operations'",(wr.dumps(payload),))
        with self.assertRaisesRegex(ValueError,'快照校验失败'):lc.commit(self.f.path,plan)
        self.assertIsNone(self._stored('payment'))
    def _stored(self,stage):
        with closing(sqlite3.connect(self.f.path)) as db:return lc.current(db,self.f.h,stage)
    def test_receipt_changed_payment_amount_never_reuses_old_proof(self):
        lc.commit(self.f.path,self.plan('payment'));plan=self.plan('receipt')
        with closing(sqlite3.connect(self.f.path)) as db,db:
            payload=wr.loads(db.execute("SELECT payload FROM lifecycle_versions WHERE stage='payment'").fetchone()[0])
            payload['rows'][0]['amount']='12.01'
            db.execute("UPDATE lifecycle_versions SET payload=?,digest=? WHERE stage='payment'",(wr.dumps(payload),lc.fingerprint(payload)))
        with self.assertRaisesRegex(wr.Conflict,'上游单据'):lc.commit(self.f.path,plan)
        self.assertIsNone(self._stored('receipt'))
    def test_old_or_edited_payment_proof_uses_original_decoder(self):
        for mode in ('absent','edited'):
            with self.subTest(mode=mode):
                plan=self.plan('payment')
                if mode=='absent':plan['plans'][0].pop('_read_proofs')
                else:plan['plans'][0]['_read_proofs']['operations']['value'][5]='{}'
                with patch('lifecycle_facts.decode',wraps=lc.decode) as decode:lc.commit(self.f.path,plan)
                self.assertTrue(any(c.args[0] is not None for c in decode.call_args_list))


class PreviewOwnership(unittest.TestCase):
    def setUp(self):self.f=fixture.LifecycleTests();self.f.setUp()
    def tearDown(self):self.f.tearDown()
    def test_batch_inputs_stay_exact_across_repreview_and_plan_mutation(self):
        documents=batch.group_scan(self.f.scan(items=[{},{}]));before=copy.deepcopy(documents)
        first=batch.prepare(self.f.path,documents);again=batch.prepare(self.f.path,documents)
        self.assertEqual(documents,before);self.assertEqual(first['plans'][0]['digest'],again['plans'][0]['digest'])
        self.assertEqual(first['plans'][0]['duplicates'],1)
        first['plans'][0]['payload']['raw_rows'][0]['raw_values'][0]='private-edit'
        self.assertEqual(documents,before);self.assertNotEqual(first['plans'][0]['payload'],again['plans'][0]['payload'])
    def test_public_numbered_entry_owns_copy_and_retains_source_rows(self):
        scan=self.f.scan();before=copy.deepcopy(scan);plan=lc.prepare_numbered(self.f.path,scan)
        self.assertEqual(scan,before);lc.commit(self.f.path,plan)
        self.assertEqual(scan,before);self.assertEqual(plan['plans'][0]['payload']['raw_rows'][0]['sources'][0]['row'],2)
    def test_public_low_level_prepare_still_protects_caller_scan(self):
        scan=self.f.scan();before=copy.deepcopy(scan);bindings={lc.binding_key(r):self.f.h for r in scan['rows']}
        lc.prepare(self.f.path,scan,bindings);self.assertEqual(scan,before)
    def test_one_exact_number_resolution_for_many_skus_without_losing_duplicates(self):
        scan=self.f.scan(items=[{} for _ in range(50)]);original=copy.deepcopy(scan)
        with patch('purchases.wo.resolve',wraps=purchases.wo.resolve) as resolve:
            p=batch.prepare(self.f.path,batch.group_scan(scan))
        self.assertEqual(resolve.call_count,1);self.assertEqual(p['plans'][0]['duplicates'],49);self.assertEqual(scan,original)
        self.assertEqual(len(p['plans'][0]['payload']['raw_rows'][0]['sources']),50)
    def test_resolution_cache_invalidates_after_external_identity_commit(self):
        scan=self.f.scan(items=[{},{}]);original=purchases.wo.resolve;count=[]
        def resolve(db,number):
            value=original(db,number);count.append(1)
            if len(count)==1:
                with closing(sqlite3.connect(self.f.path)) as writer,writer:
                    writer.execute('UPDATE normal_orders SET number=number||?',('-changed',))
            return value
        with patch('purchases.wo.resolve',resolve):
            with self.assertRaisesRegex(ValueError,'未找到补货单号'):lc.available(self.f.path,scan)
        self.assertEqual(len(count),1) # The next real resolver raises before append.
    def test_unrelated_commit_invalidates_cache_without_false_rejection(self):
        scan=self.f.scan(items=[{},{}]);original=purchases.wo.resolve;count=[]
        def resolve(db,number):
            value=original(db,number);count.append(1)
            if len(count)==1:
                with closing(sqlite3.connect(self.f.path)) as writer,writer:writer.execute('CREATE TABLE unrelated(value)')
            return value
        with patch('purchases.wo.resolve',resolve):heads=lc.available(self.f.path,scan)
        self.assertEqual(len(count),2);self.assertIn(self.f.h['number'],[h['number'] for h in heads])
    def test_commit_after_cached_resolution_cannot_hide_removed_order_number(self):
        scan=self.f.scan(items=[{},{}]);path=self.f.path
        class Observer:
            calls=0
            def is_set(s):
                s.calls+=1
                if s.calls==2:
                    with closing(sqlite3.connect(path)) as writer,writer:writer.execute('UPDATE normal_orders SET number=number||?',('-changed',))
                return False
        with patch('purchases.wo.resolve',wraps=purchases.wo.resolve) as resolve:
            with self.assertRaisesRegex(ValueError,'未找到补货单号'):lc.available(path,scan,cancel=Observer())
        self.assertEqual(resolve.call_count,2)
    def test_cached_number_does_not_skip_conflicting_store_or_revision_checks(self):
        scan=self.f.scan(items=[{}, {'门店编码':'OTHER'}])
        with self.assertRaisesRegex(ValueError,'不一致'):lc.prepare_numbered(self.f.path,scan)
    def test_mixed_stage_order_and_inputs_preserved_through_actual_save(self):
        documents=[]
        for stage in ('receipt','payment','operations','return'):documents.extend(batch.group_scan(self.f.scan(stage)))
        before=copy.deepcopy(documents);plan=batch.prepare(self.f.path,documents)
        self.assertEqual([p['stage'] for p in plan['plans']],['return','operations','payment','receipt'])
        self.assertEqual(documents,before);self.assertFalse(any(p['payload']['pending'] for p in plan['plans']))
        lc.commit(self.f.path,plan);self.assertEqual(documents,before)
        self.assertEqual(self.f.view()['rows'][0][3:6],[5,5,5])
    def test_available_cancellation_remains_active_inside_cached_rows(self):
        scan=self.f.scan(items=[{} for _ in range(50)])
        class Stop:
            calls=0
            def is_set(s):s.calls+=1;return s.calls>5
        with self.assertRaises(Cancelled):lc.available(self.f.path,scan,cancel=Stop())
    def test_failed_preview_keeps_original_scans_and_duplicate_facts_exact(self):
        documents=batch.group_scan(self.f.scan(items=[{}, {'门店确认数量':6}]))
        before=copy.deepcopy(documents)
        with self.assertRaisesRegex(ValueError,'冲突'):batch.prepare(self.f.path,documents)
        self.assertEqual(documents,before)


class RunEncoding(unittest.TestCase):
    def setUp(self):self.f=batch_fixture.Batches();self.f.setUp();self.path=self.f.path
    def tearDown(self):self.f.tearDown()
    def test_one_run_encoding_keeps_exact_canonical_payload_sequence_and_replay(self):
        other=self.f.f.second_store();plan=jb.register(self.path,self.f.new([other,self.f.h]));original=wr.dumps;encodings=[]
        def observed(v):
            value=original(v)
            if type(v) is dict and {'runid','plans','states','reserved'}<=v.keys():encodings.append(value)
            return value
        with patch('work_repository.dumps',observed):receipt=jo.commit(self.path,plan)
        self.assertFalse(receipt['errors']);self.assertEqual(len(encodings),2);self.assertEqual(encodings[0],encodings[1])
        with closing(sqlite3.connect(self.path)) as db:
            raw,digest=db.execute('SELECT payload,digest FROM jst_runs').fetchone()
            before={n:db.execute('SELECT * FROM '+n+' ORDER BY rowid').fetchall() for n in ('jst_runs','jst_generations','jst_reservations','jst_steps','jst_events')}
        self.assertEqual(raw,original(plan['payload']));self.assertEqual(digest,hashlib.sha256(raw.encode()).hexdigest())
        self.assertEqual([jo.get(self.path,g)['summary']['quantity'] for g in receipt['generated']],['40','60'])
        self.assertEqual(jo.commit(self.path,plan)['generated'],receipt['generated'])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(before,{n:db.execute('SELECT * FROM '+n+' ORDER BY rowid').fetchall() for n in before})
    def test_latest_deletion_scope_reencodes_without_reviving_removed_member(self):
        import data_maintenance as dm
        other=self.f.f.second_store();plan=jb.register(self.path,self.f.new([self.f.h,other]))
        parent=[h for h in purchases.targets(self.path) if h['oid']==self.f.h['oid']]
        dm.execute(self.path,dm.prepare(self.path,parent),True)
        receipt=jo.commit(self.path,plan,True);self.assertFalse(receipt['errors']);self.assertEqual(len(receipt['generated']),1)
        clean={**plan['payload'],'plans':[p for p in plan['payload']['plans'] if p['head']['oid']!=self.f.h['oid']],
               'skipped':[p for p in plan['payload'].get('skipped',[]) if p.get('head',{}).get('oid')!=self.f.h['oid']]}
        with closing(sqlite3.connect(self.path)) as db:raw,digest=db.execute('SELECT payload,digest FROM jst_runs').fetchone()
        self.assertEqual(raw,wr.dumps(clean));self.assertEqual(digest,jo.digest(clean))
    def test_altered_batch_rejected_before_any_generation(self):
        plan=jb.register(self.path,self.f.new());plan['payload']['plans'][0]['rows'][0]['quantity']='888'
        with self.assertRaisesRegex(ValueError,'预览被修改'):jo.commit(self.path,plan)
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM jst_generations').fetchone()[0],0)
    def test_child_only_deletion_keeps_existing_run_filter_and_other_warehouse(self):
        import data_maintenance as dm
        other=self.f.f.second_store();plan=jb.register(self.path,self.f.new([self.f.h,other]))
        dm.execute(self.path,dm.prepare(self.path,[self.f.h]),True)
        receipt=jo.commit(self.path,plan,True);self.assertFalse(receipt['errors']);self.assertEqual(len(receipt['generated']),1)
        with closing(sqlite3.connect(self.path)) as db:
            removed={r[0] for r in db.execute('SELECT oid FROM deleted_order_tokens')}
            self.assertIsNone(jo.active(db,self.f.h));raw,digest=db.execute('SELECT payload,digest FROM jst_runs').fetchone()
        clean={**plan['payload'],'plans':[p for p in plan['payload']['plans'] if p['head']['oid'] not in removed],
               'skipped':[p for p in plan['payload'].get('skipped',[]) if p.get('head',{}).get('oid') not in removed]}
        self.assertEqual(raw,wr.dumps(clean));self.assertEqual(digest,jo.digest(clean))
        self.assertTrue(any(h['oid']==self.f.h['oid'] and h['warehouse']=='山东仓' for h in purchases.targets(self.path)))


class LockRetry(unittest.TestCase):
    def setUp(self):
        self.f=jst_fixture.Service();self.f.setUp();self.path=self.f.path;_,self.gid=self.f.generated()
    def tearDown(self):self.f.tearDown()
    def state(self):
        with closing(sqlite3.connect(self.path)) as db:
            return (db.execute('SELECT locked FROM jst_locks').fetchone(),db.execute("SELECT COUNT(*) FROM jst_events WHERE action IN ('确认下单并锁定','更正解除锁定')").fetchone()[0])
    def reader(self):
        ready=threading.Event();release=threading.Event()
        def target():
            with closing(sqlite3.connect(self.path)) as db:
                db.execute('BEGIN');db.execute('SELECT * FROM normal_orders').fetchall();ready.set();release.wait(5);db.rollback()
        t=threading.Thread(target=target);t.start();self.assertTrue(ready.wait(2));return release,t
    def test_short_reader_retries_with_one_lock_and_one_event(self):
        release,t=self.reader();timer=threading.Timer(.35,release.set);timer.start();messages=[]
        try:self.assertEqual(jo.lock(self.path,[self.gid],progress=messages.append),[self.gid])
        finally:release.set();t.join();timer.cancel()
        self.assertEqual(self.state(),((1,),1));self.assertTrue(any('安全回滚' in str(m) for m in messages))
        self.assertTrue(any(getattr(m,'active',None) is False for m in messages))
    def test_unlock_keeps_reason_and_only_one_event_after_reader(self):
        jo.lock(self.path,[self.gid]);release,t=self.reader();timer=threading.Timer(.35,release.set);timer.start()
        try:jo.lock(self.path,[self.gid],'外部订单已撤销',True)
        finally:release.set();t.join();timer.cancel()
        self.assertEqual(self.state(),((0,),2))
    def test_persistent_reader_is_bounded_and_rolls_back_lock(self):
        release,t=self.reader()
        try:
            with patch('jst_orders._LOCK_BUSY_SECONDS',.05):
                with self.assertRaises(sqlite3.OperationalError):jo.lock(self.path,[self.gid])
        finally:release.set();t.join()
        self.assertEqual(self.state(),(None,0))
    def test_cancel_after_safe_rollback_keeps_no_lock(self):
        release,t=self.reader();cancel=threading.Event()
        def progress(m):
            if '安全回滚' in str(m):cancel.set()
        try:
            with self.assertRaises(Cancelled):jo.lock(self.path,[self.gid],progress=progress,cancel=cancel)
        finally:release.set();t.join()
        self.assertEqual(self.state(),(None,0))
    def test_source_change_during_retry_is_rechecked(self):
        release,t=self.reader()
        def progress(m):
            if '安全回滚' in str(m):
                release.set();t.join()
                with closing(sqlite3.connect(self.path)) as db,db:db.execute('UPDATE normal_orders SET current=2')
        try:
            with self.assertRaises(wr.Conflict):jo.lock(self.path,[self.gid],progress=progress)
        finally:release.set();t.join()
        self.assertEqual(self.state(),(None,0))
    def test_generation_change_during_retry_is_rechecked(self):
        release,t=self.reader()
        def progress(m):
            if '安全回滚' in str(m):
                release.set();t.join()
                with closing(sqlite3.connect(self.path)) as db,db:db.execute("UPDATE jst_active SET gid='replacement'")
        try:
            with self.assertRaisesRegex(wr.Conflict,'生成单已变化'):jo.lock(self.path,[self.gid],progress=progress)
        finally:release.set();t.join()
        self.assertEqual(self.state(),(None,0))
    def test_cancellation_at_commit_boundary_cannot_interrupt_lock_commit(self):
        cancel=threading.Event()
        def progress(m):
            if getattr(m,'active',False):cancel.set()
        self.assertEqual(jo.lock(self.path,[self.gid],progress=progress,cancel=cancel),[self.gid])
        self.assertEqual(self.state(),((1,),1))
    def inject(self,kind):
        connect=sqlite3.connect;calls=[]
        class Connection(sqlite3.Connection):
            def commit(db):
                calls.append(1)
                if kind=='unknown':super(Connection,db).commit()
                raise sqlite3.OperationalError('disk I/O error' if kind=='io' else 'database is locked')
            def rollback(db):
                if kind=='rollback':raise sqlite3.OperationalError('rollback I/O error')
                return super(Connection,db).rollback()
        def connection(*a,**kw):
            if kw.get('timeout')==.1:kw['factory']=Connection
            return connect(*a,**kw)
        with patch('sqlite3.connect',connection):
            with self.assertRaises(sqlite3.OperationalError):jo.lock(self.path,[self.gid])
        self.assertEqual(len(calls),1)
    def test_unknown_committed_outcome_never_replayed(self):
        self.inject('unknown');self.assertEqual(self.state(),((1,),1))
    def test_nonbusy_error_never_replayed(self):
        self.inject('io');self.assertEqual(self.state(),(None,0))
    def test_failed_rollback_never_replayed(self):
        self.inject('rollback');self.assertEqual(self.state(),(None,0))


if __name__=='__main__':unittest.main()
