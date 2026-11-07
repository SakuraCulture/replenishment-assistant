"""Synthetic isolation only: no user database, browser, or platform calls."""
import copy,sqlite3,sys,tempfile,threading,unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_archive_orders import seed
import data_maintenance as m,normal_orders as no,purchases,work_repository as wr
from aoxiang_workflows import Workflows
from aoxiang_workflow_state import deletion_issue,can_end_local,counts
from runtime_support import Cancelled


class Consistency(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
        self.w,self.r,p=seed(self.root);self.path=self.w.path
        p=copy.deepcopy(p);p['result']['store']='U';p['result']['store_name']='未选店'
        for row in p['result']['rows']:row['B']='U'
        with closing(sqlite3.connect(self.path)) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'U','normal_model','other','BH-U-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('other',1,no.fingerprint(p['result']),wr.now(),no.encode(p, db)))
        heads=purchases.targets(self.path);self.a=next(h for h in heads if h['store']=='T');self.b=next(h for h in heads if h['store']=='U')
        self.store=Workflows(self.path.parent/'aoxiang')
        self.before_b=self.r.get(wr.today(),'U')

    def failed(self,heads=None):
        j=self.store.prepare(heads or [self.a,self.b])
        for n in range(len(j['items'])):
            j=self.store.update(j['id'],position=n,changes={'state':'REVIEW','error':{'code':'BUSINESS_REVIEW','message':'付款单尚未确认或对照依据已过期'}})
        return self.store.update(j['id'],state='COMPLETED_WITH_ERRORS')

    def test_reported_local_review_deletes_only_selected_and_keeps_other_snapshot(self):
        j=self.failed();before=copy.deepcopy(j['items'][1]);before['position']=0
        plan=m.inspect_deletion(self.path,[self.a]);self.assertEqual(plan['blockers'],[])
        self.assertEqual(len(plan['tasks']),1);self.assertEqual(plan['external'],[])
        m.execute(self.path,plan)
        kept=self.store.get(j['id']);self.assertEqual(kept['items'],[before]);self.assertEqual(counts(kept)['review'],1)
        self.assertEqual(self.r.get(wr.today(),'U'),self.before_b);self.assertIsNone(self.r.get(wr.today(),'T'))
        self.assertEqual(len(purchases.targets(self.path)),2)

    def test_partial_removal_reconciles_state_and_keeps_external_identity(self):
        j=self.failed();j=self.store.update(j['id'],position=1,changes={'state':'PREPARED','package':{'id':'keep-external-identity'},'error':None})
        survivor=copy.deepcopy(j['items'][1]);survivor['position']=0
        m.execute(self.path,m.prepare(self.path,[self.a]))
        kept=self.store.get(j['id']);self.assertEqual(kept['state'],'READY');self.assertEqual(kept['items'],[survivor])
        with self.store.db() as db:self.assertEqual(db.execute('SELECT state FROM ax_workflows WHERE id=?',(j['id'],)).fetchone()[0],'READY')
        self.assertEqual(self.r.get(wr.today(),'U'),self.before_b)

    def test_inspection_lists_all_blockers_and_never_writes(self):
        j=self.store.prepare([self.a,self.b]);before=self.store.get(j['id'])
        plan=m.inspect_deletion(self.path,[self.a,self.b])
        self.assertEqual([b['order'] for b in plan['blockers']],[self.a['number'],self.b['number']])
        self.assertTrue(all(b['can_end'] for b in plan['blockers']))
        self.assertEqual(self.store.get(j['id']),before)
        with self.assertRaises(ValueError):m.execute(self.path,plan)
        self.assertIsNotNone(self.r.get(wr.today(),'T'))

    def test_end_only_selected_then_delete_preserves_queued_other_and_new_task_allowed(self):
        j=self.store.prepare([self.a,self.b]);other=copy.deepcopy(j['items'][1]);other['position']=0
        ended=self.store.end_local_items(j['id'],[self.a['number']],j['sequence'])
        self.assertEqual(ended['state'],'QUEUED');self.assertIsNone(deletion_issue(ended,ended['items'][0]))
        m.execute(self.path,m.prepare(self.path,[self.a]))
        kept=self.store.get(j['id']);self.assertEqual(kept['state'],'QUEUED');self.assertEqual(kept['items'],[other])
        self.assertEqual(self.store.next_queued()['items'],[other])
        self.assertEqual(self.r.get(wr.today(),'U'),self.before_b)

    def test_end_all_local_then_fresh_prepare_no_old_cancel_leak(self):
        j=self.store.prepare([self.a]);ended=self.store.end_local_items(j['id'],[self.a['number']],j['sequence'])
        self.assertEqual(ended['state'],'CANCELLED');fresh=self.store.prepare([self.a])
        self.assertEqual(fresh['items'][0]['state'],'PENDING');self.assertNotEqual(j['id'],fresh['id'])

    def test_stale_end_or_running_batch_is_rejected_without_partial_change(self):
        j=self.store.prepare([self.a,self.b]);current=self.store.update(j['id'],state='RUNNING')
        for seq in (j['sequence'],current['sequence']):
            with self.assertRaises(ValueError):self.store.end_local_items(j['id'],[self.a['number']],seq)
        self.assertEqual(self.store.get(j['id']),current)

    def test_preview_then_execution_start_rejects_all_deletion(self):
        j=self.failed();plan=m.prepare(self.path,[self.a]);self.store.update(j['id'],state='RUNNING')
        with self.assertRaises(ValueError):m.execute(self.path,plan)
        self.assertIsNotNone(self.r.get(wr.today(),'T'));self.assertEqual(self.r.get(wr.today(),'U'),self.before_b)

    def test_upload_unknown_cannot_be_cleared_by_cancel_or_missing_browser(self):
        j=self.failed([self.a]);j=self.store.update(j['id'],state='CANCELLED',kind='upload',position=0,changes={'package':{'id':'purchase-unknown'},'error':{'code':'SUBMISSION_UNKNOWN','message':'平台受理待核'}})
        plan=m.inspect_deletion(self.path,[self.a]);self.assertEqual(plan['blockers'][0]['action'],'平台结果待核')
        self.assertFalse(plan['blockers'][0]['can_end'])
        with self.assertRaises(ValueError):self.store.end_local_items(j['id'],[self.a['number']],j['sequence'])
        with self.assertRaises(ValueError):m.execute(self.path,plan,True)
        self.assertEqual(self.store.get(j['id']),j)

    def test_purchase_identity_never_treated_as_local_failure(self):
        for changes in ({'package':{'id':'p'}},{'purchase_id':'p'},{'result':{'binding':{'orderId':'PO'}}},{'error':{'code':'SUBMISSION_UNKNOWN'}},{'uploadAttempted':True}):
            with self.subTest(changes=changes):
                item={'state':'REVIEW',**changes};job={'state':'COMPLETED_WITH_ERRORS','kind':'prepare'}
                self.assertFalse(can_end_local(job,item));self.assertIsNotNone(deletion_issue(job,item))

    def test_duplicate_package_does_not_block_but_original_upload_still_does(self):
        duplicate=self.failed();duplicate=self.store.update(duplicate['id'],position=0,changes={
            'package':{'id':'never-reserved'},'error':{'code':'PURCHASE_ALREADY_RESERVED','message':'原任务存在'}})
        original=self.failed([self.a]);original=self.store.update(original['id'],kind='upload',position=0,changes={
            'package':{'id':'original'},'error':{'code':'PURCHASE_CREATED_MISMATCH'},
            'purchase_observation':{'taskId':'original-task','uploadAttempted':True}})
        other=copy.deepcopy(duplicate['items'][1])
        plan=m.inspect_deletion(self.path,[self.a]);self.assertEqual(len(plan['blockers']),1)
        self.assertEqual(plan['blockers'][0]['id'],original['id'])
        self.assertTrue(can_end_local(duplicate,duplicate['items'][0]))
        ended=self.store.end_local_items(duplicate['id'],[self.a['number']],duplicate['sequence'])
        self.assertEqual(ended['items'][1],other)
        with self.assertRaises(ValueError):m.execute(self.path,m.inspect_deletion(self.path,[self.a]),True)
        # The isolated component boundary reports full native verification; no direct
        # production DB modification or screenshot is used as terminal evidence.
        self.store.update(original['id'],position=0,changes={'state':'BOUND','error':None,
            'result':{'binding':{'orderId':'PO-original','status':'已作废','method':'known_import_task_and_exact_native_rows'}}})
        plan=m.prepare(self.path,[self.a]);self.assertFalse(plan['blockers'])
        self.assertEqual(len(plan['parents'][0]['warehouses']),2)
        m.execute(self.path,plan,True)
        self.assertIsNone(self.r.get(wr.today(),'T'));self.assertEqual(self.r.get(wr.today(),'U'),self.before_b)
        remaining=self.store.get(duplicate['id']);other['position']=0
        self.assertEqual(remaining['items'],[other])

    def test_duplicate_error_never_overrides_upload_receipt_or_accepted_identity(self):
        job={'kind':'prepare','state':'COMPLETED_WITH_ERRORS'}
        base={'state':'REVIEW','package':{'id':'local-file'},'error':{'code':'PURCHASE_ALREADY_RESERVED'}}
        for record in ('item','result','purchase_observation'):
            for field,value in [('purchase_id','p'),('binding',{'orderId':'PO'}),('uploadAttempted',True),
                                ('taskId','T'),('receipt',{'taskId':'T'}),('orderId','PO'),('additionalTaskIds',['T'])]:
                with self.subTest(record=record,field=field):
                    item=copy.deepcopy(base)
                    (item if record=='item' else item.setdefault(record,{}))[field]=value
                    self.assertFalse(can_end_local(job,item));self.assertIsNotNone(deletion_issue(job,item))
        for kind in ('upload','receipt'):
            self.assertFalse(can_end_local({**job,'kind':kind},base))
        self.assertFalse(can_end_local({**job,'state':'RUNNING'},base))

    def test_duplicate_local_end_still_checks_sequence(self):
        job=self.failed([self.a]);job=self.store.update(job['id'],position=0,changes={
            'package':{'id':'local-only'},'error':{'code':'PURCHASE_ALREADY_RESERVED'}})
        current=self.store.update(job['id'],message='changed')
        with self.assertRaises(ValueError):self.store.end_local_items(job['id'],[self.a['number']],job['sequence'])
        self.assertEqual(self.store.get(job['id']),current)

    def test_cancel_and_cross_database_failure_restore_all_task_payloads(self):
        j=self.failed();plan=m.prepare(self.path,[self.a]);cancel=threading.Event()
        def progress(value):
            if '正在删除' in str(value):cancel.set()
        with self.assertRaises(Cancelled):m.execute(self.path,plan,cancel=cancel,progress=progress)
        self.assertEqual(self.store.get(j['id']),j)
        with patch('data_maintenance.collect_shared',side_effect=RuntimeError('simulated write failure')):
            with self.assertRaises(RuntimeError):m.execute(self.path,plan)
        self.assertEqual(self.store.get(j['id']),j);self.assertIsNotNone(self.r.get(wr.today(),'T'))
        m.execute(self.path,plan);self.assertEqual(self.r.get(wr.today(),'U'),self.before_b)


if __name__=='__main__':unittest.main()
