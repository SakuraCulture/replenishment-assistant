"""Warehouse-scoped deletion with synthetic data and private database files."""
import copy
import sqlite3
import sys
import tempfile
import threading
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
from test_archive_orders import seed
import data_maintenance as maintenance
import warehouse_execution as execution
import deletion_barrier as barrier
import purchases
import work_repository as wr
import storage_chunks as chunks
from runtime_support import Cancelled


class WarehouseDeletion(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work,self.repo,self.payload=seed(self.root)
        self.path=self.work.path;heads=purchases.targets(self.path)
        self.hb=next(h for h in heads if h['warehouse']=='河北仓')
        self.sd=next(h for h in heads if h['warehouse']=='山东仓')

    def test_one_child_deleted_other_snapshot_identity_and_values_retained(self):
        before=execution.get_order(self.repo,self.sd)
        plan=maintenance.prepare(self.path,[self.hb])
        self.assertEqual(plan['parents'][0]['warehouses'],['河北仓'])
        self.assertEqual(plan['parents'][0]['remaining_warehouses'],['山东仓'])
        receipt=maintenance.execute(self.path,plan,True)
        self.assertEqual(receipt['deleted'],1)
        heads=purchases.targets(self.path)
        self.assertEqual({h['warehouse'] for h in heads},{'山东仓'})
        after=execution.get_order(self.repo,next(h for h in heads if h['warehouse']=='山东仓'))
        for key in ('oid','revision','digest'):self.assertEqual(after[key],before[key])
        self.assertEqual(after['payload']['result']['rows'],[r for r in before['payload']['result']['rows'] if r['AU']=='山东仓'])
        with closing(self.work.connect()) as db:
            for raw, in db.execute('SELECT payload FROM normal_revisions'):
                self.assertTrue(all(r['AU']!='河北仓' for r in chunks.load(raw,db)['result']['rows']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM deleted_order_tokens').fetchone()[0],0)
            self.assertFalse(barrier.active(db,self.hb['oid'],'河北仓'))
            self.assertTrue(barrier.active(db,self.sd['oid'],'山东仓'))
        pending=barrier.pending(self.path)
        self.assertEqual(pending[0]['scopes'],[{'oid':self.hb['oid'],'warehouse':'河北仓'}])

    def test_both_selected_reclaim_entire_parent_and_full_barrier(self):
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb,self.sd]),True)
        self.assertEqual(purchases.targets(self.path),[])
        self.assertEqual(self.repo.headers(),[])
        with closing(self.work.connect()) as db:
            self.assertFalse(barrier.active(db,self.hb['oid']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0],0)

    def test_cancel_rolls_back_child_state_and_original_parent(self):
        before=self.repo.get(wr.today(),'T');plan=maintenance.prepare(self.path,[self.hb]);cancel=threading.Event()
        def progress(message):
            if '正在删除所选仓' in str(message):cancel.set()
        with self.assertRaises(Cancelled):maintenance.execute(self.path,plan,True,progress,cancel)
        self.assertEqual(self.repo.get(wr.today(),'T'),before)
        self.assertEqual({h['warehouse'] for h in purchases.targets(self.path)},{'河北仓','山东仓'})
        self.assertEqual(barrier.pending(self.path),[])

    def test_shared_purchase_keeps_other_warehouse_links_and_facts(self):
        row={'line_id':'line','sku':'0001','quantity':'100','base_quantity':'100','price':'2','amount':'200',
             'raw':{'note':'共同原表事实'},'links':[{'head':self.hb,'quantity':'20'},{'head':self.sd,'quantity':'30'}]}
        with closing(self.work.connect()) as db,db:
            purchases.schema(db);db.execute("INSERT INTO purchase_documents VALUES('shared','identity',1)")
            payload={'schema':purchases.SCHEMA,'rows':[row],'files':[]}
            db.execute('INSERT INTO purchase_versions VALUES(?,?,?,?,?)',('shared',1,wr.now(),purchases.fingerprint(payload),wr.dumps(payload)))
            for head,quantity in ((self.hb,'20'),(self.sd,'30')):
                db.execute('INSERT INTO purchase_link_index VALUES(?,?,?,?,?,?,?)',('shared','line',head['oid'],head['revision'],head['warehouse'],'0001',quantity))
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            payload=wr.loads(db.execute("SELECT payload FROM purchase_versions WHERE docid='shared'").fetchone()[0])
        saved=payload['rows'][0]
        self.assertEqual(saved['links'],[{'head':self.sd,'quantity':'30'}])
        self.assertEqual(saved['raw'],row['raw']);self.assertEqual(saved['amount'],'200')

    def test_late_child_write_and_package_finish_do_not_retire_sibling(self):
        barrier.register_package(self.path,self.hb['oid'],'package-hb','河北仓')
        barrier.register_package(self.path,self.sd['oid'],'package-sd','山东仓')
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            with self.assertRaises(barrier.SourceDeleted):barrier.require(db,self.hb['oid'],'河北仓')
            barrier.require(db,self.sd['oid'],'山东仓')
        operation=barrier.pending(self.path)[0];barrier.acknowledge(self.path,operation['operation_id'],operation['generation'])
        barrier.package_finished(self.path,self.sd['oid'],'package-sd','山东仓')
        self.assertEqual(barrier.pending(self.path),[])
        barrier.package_finished(self.path,self.hb['oid'],'package-hb','河北仓')
        self.assertEqual(barrier.pending(self.path)[0]['scopes'],operation['scopes'])

    def test_draft_projection_keeps_other_warehouse_and_supply_route(self):
        result=self.payload['result'];current={'results':{'T':result},'store_presets':{'T':{'supply_route':{'primary':'河北仓','secondary':'山东仓'}}},'execution_warehouses':{'T':['河北仓','山东仓']}}
        original=copy.deepcopy(current)
        projected=maintenance.strip_current_warehouses(current,[self.hb])
        self.assertEqual(projected['store_presets'],original['store_presets'])
        self.assertEqual(projected['execution_warehouses']['T'],['山东仓'])
        self.assertEqual([r['AU'] for r in projected['results']['T']['rows']],['山东仓'])
        self.assertEqual(current,original)

    def test_stale_whole_draft_cannot_hide_deleted_warehouse_behind_execution_selection(self):
        draft={'day':wr.today(),'results':{'T':self.payload['result']},'execution_warehouses':{'T':['山东仓']}}
        original_guard=barrier.basis(draft)
        self.assertEqual(set(original_guard['warehouse_generations']['T']),{'河北仓','山东仓'})
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            with self.assertRaisesRegex(wr.Conflict,'仓单'):barrier.validate(db,original_guard)
            projected=maintenance.strip_current_warehouses(draft,[self.hb])
            barrier.validate(db,barrier.basis(projected))
            self.assertEqual(barrier.epochs(db,wr.today(),['T']),{'T':0})
            self.assertEqual(barrier.warehouse_epochs(db,wr.today(),{'T':['河北仓','山东仓']}),{'T':{'河北仓':1,'山东仓':0}})

    def test_all_historical_child_data_deleted_without_altering_sibling_versions(self):
        revised=copy.deepcopy(self.payload);revised['result']['rows'][0]['D']='河北仓历史改名'
        with closing(self.work.connect()) as db,db:
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid',2,maintenance.orders.fingerprint_owned(revised['result']),wr.now(),maintenance.orders.encode(revised,db)))
            db.execute("UPDATE normal_orders SET current=2 WHERE oid='oid'")
        heads=purchases.targets(self.path);hb=next(h for h in heads if h['warehouse']=='河北仓')
        before={h['revision']:[r for r in execution.get_order(self.repo,h)['payload']['result']['rows'] if r['AU']=='山东仓'] for h in self._history('山东仓')}
        maintenance.execute(self.path,maintenance.prepare(self.path,[hb]),True)
        after={h['revision']:execution.get_order(self.repo,h)['payload']['result']['rows'] for h in self._history('山东仓')}
        self.assertEqual(before,after)
        with closing(self.work.connect()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM warehouse_execution_revisions WHERE warehouse='河北仓'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM normal_revisions').fetchone()[0],2)
            for raw, in db.execute('SELECT payload FROM normal_revisions'):
                rows=chunks.load(raw,db)['result']['rows'];self.assertEqual([r['AU'] for r in rows],['山东仓'])

    def _history(self,warehouse):
        with closing(self.work.connect()) as db:return [h for h in execution.heads(db,history=True) if h['warehouse']==warehouse]

    def test_lifecycle_versions_and_exact_mappings_keep_sibling_bytes(self):
        import order_lifecycle as lifecycle
        with closing(self.work.connect()) as db,db:
            lifecycle.schema(db)
            for head in (self.hb,self.sd):
                for stage in ('return','operations','payment','receipt'):
                    for version in (1,2):
                        payload={'rows':[{'sku':'private-'+head['warehouse'],'quantity':str(version)}],'files':[]}
                        db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',(head['oid'],1,head['warehouse'],stage,version,wr.now(),maintenance.digest(payload),wr.dumps(payload)))
                db.execute('INSERT INTO lifecycle_mappings VALUES(?,?,?)',('payment',head['warehouse'],wr.dumps({'head':head})))
            before=db.execute("SELECT * FROM lifecycle_versions WHERE warehouse='山东仓'").fetchall()
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            self.assertEqual(db.execute('SELECT * FROM lifecycle_versions').fetchall(),before)
            self.assertEqual(db.execute('SELECT name FROM lifecycle_mappings').fetchall(),[('山东仓',)])

    def test_shared_same_parent_workflow_keeps_item_identity_and_accepts_late_sibling(self):
        from aoxiang_workflows import Workflows
        workflow=Workflows(self.path.parent/'aoxiang',business_path=self.path)
        claimed=workflow.claim(workflow.prepare([self.hb,self.sd])['id'])
        hb,sd=claimed['items']
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        current=workflow.get(claimed['id'])
        self.assertEqual([item['item_id'] for item in current['items']],[sd['item_id']])
        self.assertEqual(current['execution_token'],claimed['execution_token'])
        with self.assertRaises(barrier.SourceDeleted):workflow.update(claimed['id'],item_id=hb['item_id'],oid=self.hb['oid'],execution_token=claimed['execution_token'],changes={'state':'BOUND'})
        workflow.update(claimed['id'],item_id=sd['item_id'],oid=self.sd['oid'],execution_token=claimed['execution_token'],changes={'state':'PREPARED'})
        self.assertEqual(workflow.get(claimed['id'])['items'][0]['state'],'PREPARED')

    def test_cross_database_failure_rolls_back_child_snapshots_and_outbox(self):
        from aoxiang_workflows import Workflows
        workflow=Workflows(self.path.parent/'aoxiang',business_path=self.path)
        job=workflow.prepare([self.hb])
        with closing(sqlite3.connect(workflow.path)) as db,db:
            db.execute("CREATE TRIGGER inject_failure BEFORE DELETE ON ax_workflows BEGIN SELECT RAISE(ABORT,'isolated injection'); END")
        original=self.repo.get(wr.today(),'T')
        with self.assertRaisesRegex(sqlite3.IntegrityError,'isolated injection'):maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        self.assertEqual(self.repo.get(wr.today(),'T'),original)
        self.assertEqual(workflow.get(job['id']),job)
        self.assertEqual(barrier.pending(self.path),[])
        with closing(self.work.connect()) as db:self.assertFalse(execution.has_parent(db,self.hb['oid']))

    def test_candidate_cache_removes_exact_deleted_child_and_legacy_parent_keys(self):
        import payment_matching as matching
        with closing(self.work.connect()) as db,db:execution.ensure_parent(db,self.hb['oid'])
        heads=purchases.targets(self.path);hb=next(h for h in heads if h['warehouse']=='河北仓');sd=next(h for h in heads if h['warehouse']=='山东仓')
        def key(head):return matching.checksum(['signed',head['oid'],head['revision'],head['digest'],head.get('warehouse'),head.get('snapshot_digest')])
        legacy=matching.checksum(['signed',hb['oid'],hb['revision'],hb['digest']])
        with closing(matching.Cache(self.path)) as cache:
            cache.put(key(hb),{'name':'待删除仓'});cache.put(key(sd),{'name':'保留仓'});cache.put(legacy,{'name':'旧父缓存'})
        maintenance.execute(self.path,maintenance.prepare(self.path,[hb]),True)
        with closing(matching.Cache(self.path)) as cache:
            self.assertIsNone(cache.get(key(hb)));self.assertIsNone(cache.get(legacy));self.assertEqual(cache.get(key(sd)),{'name':'保留仓'})

    def test_scoped_prepare_keeps_only_fixed_size_source_hashes(self):
        plan=maintenance.prepare(self.path,[self.hb])
        with closing(maintenance.connect(self.path)) as db,closing(sqlite3.connect(':memory:')) as scratch:
            prepared=maintenance.prepare_scoped_rewrites(db,plan,scratch,lambda _:None,None)
            self.assertTrue(prepared)
            for _,_,token,_,_ in prepared:self.assertIsInstance(token,bytes);self.assertEqual(len(token),32)
            self.assertGreater(scratch.execute('SELECT COUNT(*) FROM verified_chunks').fetchone()[0],0)

    def test_concurrent_bad_chunk_cannot_reuse_verified_receipt(self):
        self.work.save_job('2001-01-01',{'schema':1,'day':'2001-01-01','model_version':'0.3.4-rules','states':{'U':{'name':'保留','state':'待计算'}},'unknown':{'proof':'old'}},None)
        with closing(self.work.connect()) as db:
            raw=db.execute("SELECT payload FROM jobs WHERE day='2001-01-01'").fetchone()[0]
            h=next(v[1] for k,v in chunks.manifest(raw)['tree'][1] if k=='unknown' for k,v in v[1])
        prepare=maintenance.prepare_scoped_rewrites
        def change(*args):
            result=prepare(*args)
            if not args[0].in_transaction:
                with closing(self.work.connect()) as other,other:other.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(zlib.compress(b'"changed"'),h))
            return result
        plan=maintenance.prepare(self.path,[self.hb])
        with patch.object(maintenance,'prepare_scoped_rewrites',side_effect=change),self.assertRaisesRegex(ValueError,'校验失败'):
            maintenance.execute(self.path,plan,True)
        self.assertEqual(len(purchases.targets(self.path)),2);self.assertEqual(barrier.pending(self.path),[])

    def test_owned_annotations_and_carried_legacy_feedback_are_pruned(self):
        payload=copy.deepcopy(self.payload);result=payload['result']
        result['must_list_audit']=[{'sku':'0001','reason':'HB private'},{'sku':'0002','reason':'SD keep'}]
        result['new_products']={'selection':{'0001':2,'0002':3}}
        result['monthly_first_fill']={'skipped':{'0001':'HB private','0002':'SD keep'},'selected':2}
        state={'schema':1,'source':'shared-source-digest','original_summary':result['summary'],'original_budget':{'enabled':False},
               'edits':{'0001':{'warehouse':'河北仓','quantity':2,'list_revision':'hb-list'},'0002':{'warehouse':'山东仓','quantity':3,'list_revision':'sd-list'}},
               'lists':{'hb-list':{'items':[{'sku':'0001'}]},'sd-list':{'items':[{'sku':'0002'}]}},
               'risks':[{'id':'hb-risk','sku':'0001','warehouse':'河北仓'},{'id':'sd-risk','sku':'0002','warehouse':'山东仓'}],'confirmed':['hb-risk','sd-risk']}
        result['preissue']=state
        with closing(self.work.connect()) as db,db:
            db.execute('UPDATE normal_revisions SET digest=?,payload=? WHERE oid=?',(maintenance.orders.fingerprint_owned(result),maintenance.orders.encode(payload,db),'oid'))
            db.execute('CREATE TABLE merchant_feedback(oid TEXT,revision INTEGER,version INTEGER,saved TEXT,payload TEXT,PRIMARY KEY(oid,revision,version))')
            feedback={'values':{'0001':5,'0002':7},'incoming':{'0002':{'warehouse':'山东仓','quantity':7,'sources':[]}},'warehouses':{'河北仓':{},'山东仓':{}},'pending':[],'files':[],'unpriced':[]}
            db.execute('INSERT INTO merchant_feedback VALUES(?,?,?,?,?)',('oid',1,2,wr.now(),wr.dumps(feedback)))
        hb=next(h for h in purchases.targets(self.path) if h['warehouse']=='河北仓')
        maintenance.execute(self.path,maintenance.prepare(self.path,[hb]),True)
        with closing(self.work.connect()) as db:
            parent=chunks.load(db.execute('SELECT payload FROM normal_revisions').fetchone()[0],db)['result']
            saved=wr.loads(db.execute('SELECT payload FROM merchant_feedback').fetchone()[0])
        self.assertEqual(set(parent['preissue']['edits']),{'0002'});self.assertEqual(set(parent['preissue']['lists']),{'sd-list'})
        self.assertEqual(parent['preissue']['confirmed'],['sd-risk']);self.assertEqual(parent['preissue']['risks'],[state['risks'][1]])
        self.assertEqual(parent['monthly_first_fill'],{'selected':1,'skipped':{'0002':'SD keep'}})
        self.assertEqual(saved['values'],{'0002':7});self.assertEqual(saved['warehouses'],{'山东仓':{}})

    def test_draft_overlay_without_result_still_requires_deleted_child_epoch(self):
        draft={'day':wr.today(),'preissue_drafts':{'T':{'edits':{'0001':{'warehouse':'河北仓','quantity':9}}}}}
        guard=barrier.basis(draft)
        self.assertEqual(guard['warehouse_generations'],{'T':{'河北仓':0}})
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            with self.assertRaisesRegex(wr.Conflict,'仓单'):barrier.validate(db,guard)

    def test_scheduled_result_only_is_pruned_inputs_and_other_day_are_unchanged(self):
        with closing(self.work.connect()) as db,db:
            db.execute('CREATE TABLE scheduled_runs(id TEXT PRIMARY KEY,day TEXT)')
            db.execute('CREATE TABLE scheduled_values(run_id TEXT,name TEXT,digest TEXT,payload BLOB,PRIMARY KEY(run_id,name))')
            for rid,day in [('same',wr.today()),('other','2001-01-01')]:
                db.execute('INSERT INTO scheduled_runs VALUES(?,?)',(rid,day))
                for name,value in [('result:T',self.payload['result']),('loaded',{'raw':'shared-inputs-not-order-owned'}),('catalogs',{'all':'shared-warehouse-material'})]:
                    p=chunks.prepare(value);db.execute('INSERT INTO scheduled_values VALUES(?,?,?,?)',(rid,name,p.digest,chunks.put(db,p)))
            untouched=db.execute("SELECT * FROM scheduled_values WHERE run_id='other' OR name<>'result:T'").fetchall()
        maintenance.execute(self.path,maintenance.prepare(self.path,[self.hb]),True)
        with closing(self.work.connect()) as db:
            digest_value,raw=db.execute("SELECT digest,payload FROM scheduled_values WHERE run_id='same' AND name='result:T'").fetchone()
            result=chunks.load(raw,db,expected=digest_value)
            self.assertEqual([r['AU'] for r in result['rows']],['山东仓'])
            self.assertEqual(db.execute("SELECT * FROM scheduled_values WHERE run_id='other' OR name<>'result:T'").fetchall(),untouched)


if __name__=='__main__':unittest.main()
