"""Deletion with live queues, shared facts and stale writers; isolated DBs only."""
import copy,json,sqlite3,sys,tempfile,threading,unittest,subprocess,os
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from test_archive_orders import seed
import data_maintenance as dm,work_repository as wr,normal_orders as no,purchases
import deletion_barrier as fence
from aoxiang_workflows import Workflows

class LifecycleDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work,self.repo,self.payload=seed(self.root);self.path=self.work.path
        self.head=purchases.targets(self.path)[0]
        self.parent=self.repo.headers()[0]  # Whole-store deletion safety; child tests are separate.
    def other(self):
        p=copy.deepcopy(self.payload);r=p['result'];r['store']='U';r['store_name']='保留店'
        for d in r['rows']:d['B']='U'
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(wr.today(),'U','normal_model','other','BH-U-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('other',1,no.fingerprint(r),wr.now(),no.encode(p,db)))
        return next(h for h in purchases.targets(self.path) if h['oid']=='other')
    def remove(self):return dm.execute(self.path,dm.prepare(self.path,[self.parent]),True)
    def test_all_lifecycle_states_are_not_deletion_gates(self):
        w=Workflows(self.path.parent/'aoxiang',business_path=self.path)
        j=w.prepare([self.head])
        for state in ('PENDING','RUNNING','REVIEW','FAILED','PREPARED','BOUND','SYNCED'):
            w.update(j['id'],state='RUNNING' if state=='RUNNING' else 'PAUSED',position=0,changes={'state':state,'purchase_id':'p-'+state,'error':{'code':'TASK_PENDING'}})
            self.assertFalse(dm.prepare(self.path,[self.parent])['blockers'])
        plan=dm.prepare(self.path,[self.parent]);self.assertFalse(plan['blockers'])
        self.assertEqual(len(plan['tasks']),1);dm.execute(self.path,plan,True)
        self.assertEqual(w.list(),[]);self.assertEqual(self.repo.headers(),[])
        self.assertEqual(len(fence.pending(self.path)),1)
    def test_stable_item_and_execution_fences_preserve_remaining_queue(self):
        other=self.other();w=Workflows(self.path.parent/'aoxiang',business_path=self.path)
        j=w.claim(w.prepare([self.head,other])['id']);gone,kept=j['items'];token=j['execution_token']
        self.remove();current=w.get(j['id'])
        self.assertEqual(current['state'],'RUNNING');self.assertEqual(current['items'][0]['item_id'],kept['item_id'])
        with self.assertRaises(fence.SourceDeleted):w.update(j['id'],item_id=gone['item_id'],oid='oid',execution_token=token,changes={'state':'BOUND'})
        w.update(j['id'],item_id=kept['item_id'],oid='other',execution_token=token,changes={'state':'PREPARED'})
        fresh=w.claim(j['id'])
        with self.assertRaises(fence.SourceDeleted):w.update(j['id'],item_id=kept['item_id'],oid='other',execution_token=token,changes={'state':'FAILED'})
        self.assertEqual(fresh['items'][0]['state'],'PREPARED')
        with self.assertRaises(ValueError):w.update(j['id'],item_id=kept['item_id'],oid='other',changes={'head':self.head})
    def test_actual_runner_late_result_does_not_shift_to_other_order(self):
        from aoxiang_workflow_runner import Runner
        other=self.other();w=Workflows(self.path.parent/'aoxiang',business_path=self.path);job=w.prepare([self.head,other])
        entered=threading.Event();release=threading.Event();calls=[];errors=[]
        class Client:
            def request(self,*a,**k):return {}
        runner=Runner(w,Client(),self.root,self.path,cancel=threading.Event())
        def prepare(job,item,configuration,suppliers):
            oid=item['head']['oid'];calls.append(oid)
            if oid=='oid':entered.set();self.assertTrue(release.wait(20))
            return 'PREPARED',{'proof':oid}
        runner.prepare_item=prepare
        def execute():
            try:runner.run(job['id'])
            except BaseException as exc:errors.append(exc)
        with patch('aoxiang_workflow_runner.Directory') as directory:
            directory.return_value.suppliers.return_value={}
            thread=threading.Thread(target=execute);thread.start()
            try:self.assertTrue(entered.wait(20));self.remove()
            finally:release.set();thread.join(20)
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[]);self.assertEqual(calls,['oid','other'])
        current=w.get(job['id']);self.assertEqual(len(current['items']),1)
        self.assertEqual(current['items'][0]['result'],{'proof':'other'});self.assertEqual(current['state'],'READY')
    def test_all_removed_runner_exits_and_schedule_cannot_reappear(self):
        w=Workflows(self.path.parent/'aoxiang',business_path=self.path);job=w.claim(w.prepare([self.head])['id'])
        self.remove()
        with self.assertRaises(fence.SourceDeleted):w.update(job['id'],execution_token=job['execution_token'],state='COMPLETED')
        self.assertEqual(w.list(),[])
        with self.assertRaises(fence.SourceDeleted):self.repo.log(self.head,'late',{})
    def test_old_draft_repreview_and_unselected_sign_cannot_revive_deleted_store(self):
        other=self.other();day=wr.today()
        p={'schema':1,'day':day,'model_version':'0.3.4-rules','states':{'T':{'name':'T','state':'待计算'},'U':{'name':'U','state':'待计算'}},'results':{}}
        saved=self.work.save_job(day,p);self.remove()
        current=self.work.get_job(day);self.assertEqual(set(current['payload']['states']),{'U'})
        # Re-preparing against a current CAS revision must still reject old data.
        with self.assertRaisesRegex(wr.Conflict,'代次'):self.work.save_job(day,p,current['revision'])
        with closing(self.work.connect()) as db:
            with self.assertRaises(wr.Conflict):fence.validate(db,fence.basis(p))
        # A fresh computation may intentionally produce a new same-day order.
        p['deletion_generations']=fence.read_epochs(self.path,day,['T','U'])
        self.work.save_job(day,p,current['revision'])
        self.assertEqual(set(self.work.get_job(day)['payload']['states']),{'T','U'})
        self.assertEqual(self.repo.headers()[0]['oid'],'other')
    def test_deletion_during_sign_preparation_rejected_under_writer(self):
        from test_preissue_archive import ArchiveAdjustmentTests
        from test_preissue import result
        payload=ArchiveAdjustmentTests.payload(self,result())
        with closing(self.work.connect()) as db,db:fence.schema(db)
        def progress(message):
            if '准备正常单：1 /' in str(message):
                with closing(self.work.connect()) as db,db:db.execute("INSERT OR IGNORE INTO deletion_generations VALUES(?,?,1)",(wr.today(),'S1'))
        with self.assertRaisesRegex(wr.Conflict,'代次'):self.repo.confirm(payload,['S1'],None,{'S1':None},progress=progress)
        self.assertEqual([h['oid'] for h in self.repo.headers()],['oid'])
    def test_sign_other_store_cannot_save_whole_day_with_deleted_unselected_content(self):
        from test_preissue_archive import ArchiveAdjustmentTests
        from test_preissue import result
        payload=ArchiveAdjustmentTests.payload(self,result());payload['states']['T']={'name':'已删店','state':'待计算'}
        self.remove()
        with self.assertRaisesRegex(wr.Conflict,'代次'):self.repo.confirm(payload,['S1'],None,{'S1':None})
        self.assertEqual(self.repo.headers(),[]);self.assertIsNone(self.work.get_job(wr.today()))
    def test_shared_purchase_all_versions_keep_unselected_quantities_and_raw_facts(self):
        other=self.other();row={'line_id':'line','sku':'0001','quantity':'100','base_quantity':'100','price':'2','amount':'200','raw':{'note':'完整共同原表'},'links':[{'head':self.head,'quantity':'20'},{'head':other,'quantity':'30'}]}
        with closing(self.work.connect()) as db,db:
            purchases.schema(db);db.execute("INSERT INTO purchase_documents VALUES('shared','identity',2)")
            for v in (1,2):
                p={'schema':1,'rows':[copy.deepcopy(row)],'files':[]}
                if v==2:p['rows'][0]['links'][1]['quantity']='35'
                db.execute('INSERT INTO purchase_versions VALUES(?,?,?,?,?)',('shared',v,'saved-'+str(v),purchases.fingerprint(p),wr.dumps(p)))
        self.remove()
        with closing(self.work.connect()) as db:
            for v in (1,2):
                r=purchases.current(db,'shared',v);actual=r['payload']['rows'][0]
                self.assertEqual(actual['links'],[{'head':other,'quantity':'30' if v==1 else '35'}])
                self.assertEqual({k:x for k,x in actual.items() if k!='links'},{k:x for k,x in row.items() if k!='links'})
                self.assertEqual(r['saved'],'saved-'+str(v))
            self.assertEqual(db.execute('SELECT DISTINCT oid FROM purchase_link_index').fetchall(),[('other',)])
    def test_wal_rejected_before_mutation_and_outbox_rollback(self):
        plan=dm.prepare(self.path,[self.parent])
        with closing(sqlite3.connect(self.path)) as db:self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
        with self.assertRaisesRegex(wr.Conflict,'WAL'):dm.execute(self.path,plan,True)
        self.assertEqual(len(self.repo.headers()),1);self.assertEqual(fence.pending(self.path),[])
    def test_journal_mode_changed_after_preparation_is_rechecked_under_writer(self):
        original=dm.prepare_rewrites
        def prepare(db,*args):
            value=original(db,*args)
            if not db.in_transaction:db.execute('PRAGMA main.journal_mode=WAL')
            return value
        with patch.object(dm,'prepare_rewrites',side_effect=prepare),self.assertRaisesRegex(wr.Conflict,'WAL'):
            self.remove()
        self.assertEqual(len(self.repo.headers()),1);self.assertEqual(fence.pending(self.path),[])
    def test_order_explanation_journal_removed_and_late_update_cannot_restore(self):
        from automation_tasks import Journal
        from automation_protocol import CommandError
        journal=Journal(self.path,'isolated');task,_=journal.create('old-explanation',{},'explanation',order_ref='oid')
        journal.update(task['task_id'],message='history')
        other,_=journal.create('other-task',{},'prepare')
        self.remove()
        with self.assertRaises(CommandError):journal.get(task['task_id'])
        with self.assertRaises(CommandError):journal.update(task['task_id'],state='completed',order_ref='oid')
        self.assertEqual(journal.events(task['task_id']),[]);self.assertEqual(journal.get(other['task_id']),other)
    def test_late_local_package_rearms_outbox_without_lost_acknowledgment(self):
        package='purchase-'+'a'*32;fence.register_package(self.path,'oid',package)
        result=self.remove();p=fence.pending(self.path)[0]
        fence.acknowledge(self.path,p['operation_id'],p['generation']);self.assertEqual(fence.pending(self.path),[])
        fence.package_finished(self.path,'oid',package)
        fence.acknowledge(self.path,p['operation_id'],p['generation'])
        newer=fence.pending(self.path)[0];self.assertEqual(newer['generation'],1)
        fence.acknowledge(self.path,newer['operation_id'],newer['generation']);self.assertEqual(fence.pending(self.path),[])
        with self.assertRaises(fence.SourceDeleted):fence.register_package(self.path,'oid','purchase-'+'b'*32)
    def test_preview_package_finishing_after_deletion_rearms_local_cleanup(self):
        import aoxiang_purchase_tasks
        def generate(*args,**kwargs):
            self.remove();p=fence.pending(self.path)[0];fence.acknowledge(self.path,p['operation_id'],p['generation'])
            return {'id':args[7]}
        with patch.object(aoxiang_purchase_tasks,'_prepare',side_effect=generate):
            aoxiang_purchase_tasks.prepare(self.path,self.head,{}, {},{},self.root,self.root)
        self.assertEqual(fence.pending(self.path)[0]['generation'],1)
    def test_payment_bindings_and_candidate_cache_pruned_but_shared_alias_kept(self):
        import order_lifecycle as lc,payment_matching as pm
        other=self.other();removed_key=pm.checksum(['signed','oid',1,self.head['digest']]);kept_key=pm.checksum(['signed','other',1,other['digest']])
        with closing(self.work.connect()) as db,db:
            lc.schema(db)
            for name,value in [('payment_match.order.deleted',{'head':self.head}),('jst_order_reference.deleted',{'head':self.head,'number':'JST-X'}),('payment_match.order.keep',{'head':other}),('payment_match.store.shared',{'store':'T','original_name':'别称'})]:db.execute('INSERT INTO lifecycle_mappings VALUES(?,?,?)',('payment',name,wr.dumps(value)))
        with closing(pm.Cache(self.path)) as cache:cache.put(removed_key,{'name':'已删店'});cache.put(kept_key,{'name':'保留店'})
        self.remove()
        with closing(self.work.connect()) as db:self.assertEqual({r[0] for r in db.execute('SELECT name FROM lifecycle_mappings')},{'payment_match.order.keep','payment_match.store.shared'})
        with closing(pm.Cache(self.path)) as cache:self.assertIsNone(cache.get(removed_key));self.assertEqual(cache.get(kept_key),{'name':'保留店'})
    def test_export_receipt_crops_shared_heads_and_late_save_cannot_restore(self):
        import export_receipts as er
        other=self.other();external=self.root/'user-output.xlsx';external.write_bytes(b'keep original output')
        receipt=er.begin(self.path,'archive',[{'path':str(external),'state':'已完成','heads':[self.head,other]},{'path':str(external),'state':'已完成','head':self.head}])
        self.remove();er.prune_deleted(self.path)
        er.save(self.path,receipt)  # stale in-memory callback
        saved=json.loads((er.directory(self.path)/(receipt['id']+'.json')).read_text('utf-8'))
        self.assertEqual(len(saved['files']),1);self.assertEqual(saved['files'][0]['heads'],[other]);self.assertEqual(len(receipt['files']),2)
        only=er.begin(self.path,'merchant',[{'path':str(external),'state':'已完成','head':self.head}])
        self.assertFalse((er.directory(self.path)/(only['id']+'.json')).exists());self.assertEqual(external.read_bytes(),b'keep original output')
        with self.assertRaises(ValueError):er.save(self.path,{**only,'id':'../outside'})
    def test_interrupted_isolated_process_recovers_main_and_attached_queue(self):
        # Controlled process loss before COMMIT and after COMMIT, not a physical
        # power cut or a claim to inject torn disk sectors during COMMIT.
        w=Workflows(self.path.parent/'aoxiang',business_path=self.path);w.prepare([self.head])
        script='''import sys,os\nsys.path.insert(0,sys.argv[1])\nimport data_maintenance as m,purchases\np=sys.argv[2]\ndef progress(s):\n if sys.argv[3]=='before' and '当天作业清理已写入事务' in str(s):os._exit(73)\n if sys.argv[3]=='after' and getattr(s,'committing',None) is False:os._exit(74)\nm.execute(p,m.prepare(p,[purchases.targets(p)[0]]),True,progress)\nos._exit(74)\n'''
        script=script.replace('m.prepare(p,[purchases.targets(p)[0]])',"m.prepare(p,[{k:v for k,v in purchases.targets(p)[0].items() if k!='warehouse'}])")
        args=[sys.executable,'-c',script,str(Path(__file__).resolve().parents[1]/'src'),str(self.path)]
        r=subprocess.run([*args,'before'],capture_output=True,timeout=40);self.assertEqual(r.returncode,73,r.stderr)
        self.assertEqual(len(self.repo.headers()),1);self.assertEqual(len(w.list()),1);self.assertEqual(fence.pending(self.path),[])
        r=subprocess.run([*args,'after'],capture_output=True,timeout=40);self.assertEqual(r.returncode,74,r.stderr)
        self.assertEqual(self.repo.headers(),[]);self.assertEqual(w.list(),[]);self.assertEqual(len(fence.pending(self.path)),1)
        for path in (self.path,w.path):
            with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')

class FrozenAllocationDeletion(unittest.TestCase):
    def setUp(self):
        import test_jst_batches
        self.helper=test_jst_batches.Batches();self.helper.setUp();self.addCleanup(self.helper.tearDown)
        self.path=self.helper.path;self.h=self.helper.h
    def test_delete_after_first_commit_remaining_original_quantity_and_replay(self):
        import jst_batches as b,jst_orders as j
        other=self.helper.f.second_store();plan=self.helper.new([self.h,other]);deleted=[]
        def progress(p):
            if hasattr(p,'business_event') and not deleted:
                deleted.append(True);dm.execute(self.path,dm.prepare(self.path,[self.h]),True)
        result=b.execute(self.path,plan,progress=progress)
        self.assertEqual(result['errors'],[]);self.assertEqual(len(result['generated']),2)
        self.assertEqual(j.get(self.path,result['generated'][1])['summary']['quantity'],'60')
        # Reuse the original frozen run (including the now removed first step).
        frozen=b.register(self.path,plan);replay=j.commit(self.path,frozen,True)
        self.assertEqual(replay['errors'],[]);self.assertEqual(replay['generated'],[result['generated'][1]])
        with closing(sqlite3.connect(self.path)) as db:
            for raw, in db.execute('SELECT payload FROM jst_runs'):
                self.assertNotIn(self.h['oid'],[p['head']['oid'] for p in wr.loads(raw)['plans']])
    def test_delete_before_first_commit_keeps_other_frozen_allocation(self):
        import jst_batches as b,jst_orders as j
        other=self.helper.f.second_store();plan=b.activate(self.path,b.register(self.path,self.helper.new([self.h,other])))
        dm.execute(self.path,dm.prepare(self.path,[self.h]),True)
        result=j.commit(self.path,plan,True)
        self.assertEqual(result['errors'],[]);self.assertEqual(len(result['generated']),1)
        self.assertEqual(j.get(self.path,result['generated'][0])['summary']['quantity'],'60')

if __name__=='__main__':unittest.main()
