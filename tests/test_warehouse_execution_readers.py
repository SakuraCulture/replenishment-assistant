"""Independent warehouse pointers in readers and merge; only generated databases."""
import copy
import hashlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import test_archive_orders as fixture
import history_read
import lifecycle_home
import normal_orders
import order_analytics
import order_merge
import purchases
import warehouse_execution as execution
import work_repository as wr
from unittest.mock import patch


class WarehouseExecutionReaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.work,self.repo,self.payload=fixture.seed(self.root)
        self.path=self.work.path;self.cancel=threading.Event()

    def current(self):
        with closing(self.work.connect()) as db:return execution.heads(db)

    def convert(self):
        with closing(self.work.connect()) as db,db:execution.ensure_parent(db,'oid')

    def update_hb_only(self,mutate=None):
        value=copy.deepcopy(self.payload)
        value['result']['rows'][0]['AS']=48
        if mutate:mutate(value)
        from result_semantics import summarize
        value['result']['summary']=summarize(value['result']['rows'])
        digest=normal_orders.fingerprint_owned(value['result'],value.get('new_focus'))
        with closing(self.work.connect()) as db,db:
            blob=normal_orders.encode(value,db)
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',('oid',2,digest,wr.now(),blob))
            db.execute('UPDATE normal_orders SET current=2 WHERE oid=?',('oid',))
            execution.publish(db,'oid',2,digest,wr.now(),value,['河北仓'])

    def listing(self):
        return history_read.execute(self.path,self.root/'history.sqlite3',{'kind':'list'},self.cancel,lambda _:None)

    def test_legacy_targets_keep_original_shape_and_database_bytes(self):
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        heads=purchases.targets(self.path)
        self.assertEqual([h['warehouse'] for h in heads],['河北仓','山东仓'])
        self.assertFalse(any('child_revision' in h for h in heads))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)

    def test_unselected_warehouse_is_absent_from_every_current_reader(self):
        self.convert()
        with closing(self.work.connect()) as db,db:
            db.execute("UPDATE warehouse_execution_heads SET state='not_participating' WHERE warehouse='山东仓'")
        self.assertEqual([h['warehouse'] for h in purchases.targets(self.path)],['河北仓'])
        self.assertEqual([h['warehouse'] for h in self.listing()['records']],['河北仓'])
        home=lifecycle_home.read(self.path,self.root/'home.sqlite3',{'all_records':True})
        self.assertEqual([h['warehouse'] for h in home['records']],['河北仓'])
        report=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today()})
        self.assertEqual([h['warehouse'] for h in report['heads']],['河北仓'])

    def test_other_warehouse_remains_valid_when_parent_current_advances(self):
        self.convert();before=next(h for h in purchases.targets(self.path) if h['warehouse']=='山东仓')
        self.update_hb_only()
        heads=purchases.targets(self.path);current={h['warehouse']:h for h in heads if h['revision']==h['current_revision']}
        self.assertEqual(current['河北仓']['revision'],2)
        self.assertEqual(current['山东仓']['revision'],1)
        self.assertEqual(current['山东仓'],before)
        with closing(self.work.connect()) as db:purchases.validate_head(db,before)
        order=execution.get_order(self.repo,before)
        self.assertEqual([r['E'] for r in order['payload']['result']['rows']],['0002'])

    def test_history_cache_is_partitioned_and_reads_pinned_snapshot(self):
        self.convert();self.update_hb_only();records=self.listing()['records']
        self.assertEqual([r['warehouse'] for r in records],['河北仓','山东仓'])
        self.assertEqual([r['revision'] for r in records],[2,1])
        for record,sku in zip(records,['0001','0002']):
            detail=history_read.execute(self.path,self.root/'history.sqlite3',{'kind':'detail','head':record},self.cancel,lambda _:None)
            self.assertEqual([r[0] for r in detail['rows']],[sku])
            self.assertEqual(detail['head']['number'],record['number'])
        self.assertNotEqual(history_read.key(self.path,records[0]),history_read.key(self.path,records[1]))

    def test_home_uses_each_warehouse_current_revision(self):
        self.convert();self.update_hb_only()
        records=lifecycle_home.read(self.path,self.root/'home.sqlite3',{'all_records':True})['records']
        self.assertEqual([(r['warehouse'],r['revision'],r['current_revision']) for r in records],[('河北仓',2,2),('山东仓',1,1)])
        self.assertEqual(len({r['number'] for r in records}),2)

    def test_analytics_uses_frozen_warehouse_rows_without_parent_double_count(self):
        before=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today()})
        self.convert()
        after=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today()})
        self.assertEqual(before['metrics'],after['metrics'])
        self.assertEqual(len(before['rows']),len(after['rows']))
        self.update_hb_only()
        report=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today()})
        self.assertEqual({h['warehouse']:h['revision'] for h in report['heads']},{'河北仓':2,'山东仓':1})
        self.assertEqual(len(report['rows']),len(before['rows']))

    def test_retired_child_does_not_return_from_parent_snapshot_or_cache(self):
        self.convert();records=self.listing()['records'];gone=records[0]
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid',gone['warehouse'],'synthetic-delete')
        self.assertEqual([h['warehouse'] for h in self.listing()['records']],['山东仓'])
        self.assertEqual([h['warehouse'] for h in purchases.targets(self.path)],['山东仓'])
        with self.assertRaisesRegex(ValueError,'删除|变化'):
            history_read.cached(self.path,self.root/'history.sqlite3',gone,True)

    def test_analytics_basis_detects_child_pointer_change_without_parent_change(self):
        self.convert();report=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today()})
        with closing(self.work.connect()) as db,db:
            db.execute("UPDATE warehouse_execution_heads SET state='not_participating' WHERE warehouse='山东仓'")
        with self.assertRaisesRegex(ValueError,'更新'):
            order_analytics.validate_basis(self.path,report)

    def test_merge_owns_and_validates_warehouse_snapshots_and_retirement(self):
        self.convert()
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid','山东仓','synthetic-delete')
        graph=order_merge.Graph(self.path.parent,lambda _:None,None)
        self.addCleanup(graph.close)
        self.assertEqual(graph.active_warehouses['oid'],{'河北仓'})
        self.assertIn(('oid','山东仓'),graph.deleted_warehouses)
        owned={r.table for r in graph.by_order['oid']}
        self.assertTrue({'warehouse_execution_heads','warehouse_execution_revisions','warehouse_execution_retirements'}<=owned)
        self.assertFalse(any('暂不支持合并' in error or '分仓' in error for error in graph.bad.get('oid',[])),graph.bad)

    def test_inventory_round_combines_pinned_children_and_shared_local_once(self):
        import inventory_structure as inventory
        p=self.payload
        p['result']['rows'][0].update(K=10,L=0,V=2,G='包')
        p['result']['rows'][1].update(K=5,L=0,V=4,G='包')
        local=copy.deepcopy(p['result']['rows'][0]);local.update(E='local',AU='地采',_source='地采商品',K=4,V=5,
            _inventory_analysis_price=5,_inventory_analysis_unit='包',_inventory_analysis_source='合成明确单价')
        p['result']['rows'].append(local)
        with closing(self.work.connect()) as db,db:
            db.execute('UPDATE normal_revisions SET digest=?,payload=?',(normal_orders.fingerprint_owned(p['result'],p.get('new_focus')),normal_orders.encode(p,db)))
        spec={'from':wr.today(),'to':wr.today()}
        before=inventory.collect(self.path,spec);old_detail=inventory.detail(self.path,before,'T',wr.today())
        self.convert();converted=inventory.collect(self.path,spec)
        self.assertEqual(converted['overview']['total'],before['overview']['total'])
        self.assertEqual([r['sku'] for r in inventory.detail(self.path,converted,'T',wr.today())],[r['sku'] for r in old_detail])
        def changed(value):
            value['result']['rows'][0]['K']=30
            value['result']['rows'][1]['K']=99
        self.update_hb_only(changed)
        after=inventory.collect(self.path,spec);detail=inventory.detail(self.path,after,'T',wr.today())
        self.assertEqual(len(after['rounds']),1)
        self.assertEqual({r['sku']:r['stock'] for r in detail},{'0001':30,'0002':5,'local':4})
        self.assertEqual(after['overview']['total']['known_value'],100)
        self.assertEqual({h['warehouse']:h['revision'] for h in after['rounds'][0]['execution_sources']},{'河北仓':2,'山东仓':1})
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid','山东仓','synthetic-delete')
        with self.assertRaisesRegex(ValueError,'变化'):inventory.validate_basis(self.path,after)
        report=inventory.collect(self.path,spec)
        self.assertEqual([r['sku'] for r in inventory.detail(self.path,report,'T',wr.today())],['0001','local'])
        self.assertTrue(report['rounds'][0]['partial'])

    def test_historical_explanation_uses_warehouse_frozen_revision(self):
        import automation_jobs
        self.convert();sd=next(h for h in self.current() if h['warehouse']=='山东仓')
        self.update_hb_only(lambda p:p['result']['rows'][1].update(E='new-sd-sku'))
        answer=automation_jobs.historical_explanation(self.path,sd,{'sku':'0002','warehouse':'山东仓'},None)
        self.assertEqual(answer['sku'],'0002');self.assertEqual(answer['order']['revision'],1)
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid','山东仓','synthetic-delete')
        with self.assertRaisesRegex(ValueError,'删除|参与'):automation_jobs.historical_explanation(self.path,sd,{'sku':'0002','warehouse':'山东仓'},None)

    def test_calculation_captures_both_warehouse_deletion_fences(self):
        import automation_jobs
        import deletion_barrier
        with closing(self.work.connect()) as db,db:
            deletion_barrier.schema(db)
            db.execute('INSERT INTO warehouse_deletion_generations VALUES(?,?,?,?)',(wr.today(),'T','山东仓',3))
        prepared={'dbpath':self.path,'day':wr.today(),'jobs':{'T':{}},'loaded':{},'profile':{},'workers':2}
        with patch.object(automation_jobs.batch_service,'run',return_value='synthetic'):
            self.assertEqual(automation_jobs.calculate(prepared,['T'],lambda _:None,None),'synthetic')
        self.assertEqual(prepared['warehouse_deletion_generations']['T'],{'河北仓':0,'山东仓':3})

    def test_logs_filter_before_pagination_and_names_use_pinned_child(self):
        import business_log_views as logs
        import daily_batch
        self.convert();self.update_hb_only(lambda p:p['result'].update(store_name='新的河北店名'))
        with closing(self.work.connect()) as db,db:
            db.execute('CREATE TABLE lifecycle_events(id INTEGER PRIMARY KEY,at TEXT,oid TEXT,revision INTEGER,warehouse TEXT,action TEXT,detail TEXT)')
            for i in range(61):
                db.execute('INSERT INTO lifecycle_events VALUES(?,?,?,?,?,?,?)',(i,wr.now(),'oid',1,'山东仓','保留山东','{}'))
                db.execute('INSERT INTO lifecycle_events VALUES(?,?,?,?,?,?,?)',(i+1000,wr.now(),'oid',2,'河北仓','更新河北','{}'))
            db.execute('INSERT INTO normal_events(at,oid,revision,action,detail) VALUES(?,?,?,?,?)',(wr.now(),'oid',2,'确认归档','{}'))
        records=[{'oid':'oid','revision':1,'warehouse':'山东仓','store':'T'}, {'oid':'oid','revision':2,'warehouse':'河北仓','store':'T'}]
        logs.attach_names(self.path,records)
        self.assertEqual([r['display_name'] for r in records],['测试店','新的河北店名'])
        journal=logs.events(self.path,'oid')
        self.assertEqual([r['warehouse'] for r in journal['records']],['河北仓'])
        with closing(self.work.connect()) as db,db:execution.retire(db,'oid','河北仓','synthetic-delete')
        page=daily_batch.log_page(self.path,wr.today(),wr.today(),'保留山东 更新河北',0)
        self.assertEqual(page['count'],0)
        page=logs.log_page(self.path,wr.today(),wr.today(),'保留山东',0)
        self.assertEqual(page['count'],61);self.assertEqual(len(page['rows']),50)
        self.assertTrue(all(r['warehouse']=='山东仓' and r['display_name']=='测试店' for r in page['rows']))
        last=logs.log_page(self.path,wr.today(),wr.today(),'保留山东',1)
        self.assertEqual(last['count'],61);self.assertEqual(len(last['rows']),11)
        self.assertFalse(logs.events(self.path,'oid')['records'])

    def test_asof_analytics_does_not_depend_on_pruned_shared_parent_revision(self):
        self.convert()
        with closing(self.work.connect()) as db,db:
            db.execute("UPDATE normal_revisions SET saved='2026-11-01T10:00:00+08:00'")
            db.execute("UPDATE warehouse_execution_revisions SET saved='2026-11-01T10:00:00+08:00'")
        self.update_hb_only()
        with closing(self.work.connect()) as db,db:
            db.execute("UPDATE normal_revisions SET saved='2026-11-03T10:00:00+08:00' WHERE revision=2")
            db.execute("UPDATE warehouse_execution_revisions SET saved='2026-11-03T10:00:00+08:00' WHERE source_revision=2")
            db.execute('DELETE FROM normal_revisions WHERE revision=1')
        report=order_analytics.collect(self.path,{'from':wr.today(),'to':wr.today(),'as_of':'2026-11-02T10:00:00+08:00'})
        self.assertEqual({h['warehouse']:h['revision'] for h in report['heads']},{'河北仓':1,'山东仓':1})

    def test_merged_new_parent_keeps_child_state_and_repeat_is_noop(self):
        import merge_fixture as fixture
        import order_migration
        import deletion_barrier
        source=fixture.Order(self.root/'source','SOURCE');target=fixture.Order(self.root/'target','LOCAL')
        with closing(sqlite3.connect(source.path)) as db,db:
            execution.ensure_parent(db,source.oid)
            retired=next(h for h in execution.heads(db,[{'oid':source.oid}]) if h['warehouse']=='山东仓')
            operation=deletion_barrier.mark_warehouses(db,[retired]);execution.retire(db,source.oid,'山东仓',operation)
        component=source.root/'aoxiang/component-state/state.sqlite';component.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(component)) as db,db:
            db.executescript('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE retired_host_warehouses(oid TEXT,warehouse TEXT,operation_id TEXT,PRIMARY KEY(oid,warehouse));CREATE TABLE warehouse_owned_files(oid TEXT,warehouse TEXT,path TEXT,PRIMARY KEY(oid,warehouse,path));')
            db.execute('INSERT INTO metadata VALUES(?,?)',('schemaVersion','2'))
            db.execute('INSERT INTO metadata VALUES(?,?)',('warehouseRetirementProtocol','1'))
            db.execute('INSERT INTO retired_host_warehouses VALUES(?,?,?)',(source.oid,'山东仓',operation))
        plan=fixture.stage(source,target.root,self.root/'package.zip')
        self.addCleanup(lambda:order_migration.discard(plan) if Path(plan['stage']).exists() else None)
        report=order_merge.inspect(plan['stage'],target.root)
        self.assertEqual(report['counts']['added'],1,report)
        with patch('storage_chunks.backup_before_write'):
            order_merge.merge(plan['stage'],target.root,report)
        with closing(sqlite3.connect(target.path)) as db:
            children=execution.heads(db,[{'oid':source.oid}])
            self.assertEqual([h['warehouse'] for h in children],['河北仓'])
            self.assertEqual(execution.state(db,source.oid,'山东仓')['state'],'deleted')
            self.assertFalse(deletion_barrier.active(db,source.oid,'山东仓'))
            self.assertTrue(db.execute('SELECT 1 FROM deleted_warehouse_tokens WHERE oid=? AND warehouse=?',(source.oid,'山东仓')).fetchone())
        with closing(sqlite3.connect(target.root/'aoxiang/component-state/state.sqlite')) as db:
            self.assertEqual(db.execute('SELECT oid,warehouse FROM retired_host_warehouses').fetchall(),[(source.oid,'山东仓')])
            self.assertEqual(db.execute('SELECT value FROM metadata WHERE key=?',('warehouseRetirementProtocol',)).fetchone(),('1',))
        again=order_merge.inspect(plan['stage'],target.root)
        self.assertEqual(again['counts']['duplicate'],1,again)

    def test_followup_uses_child_pinned_revision_and_hides_unpublished_sibling(self):
        import followup
        self.convert();self.update_hb_only()
        records=followup.read(self.path,self.root/'followup.sqlite3',{})['records']
        self.assertEqual([(r['warehouse'],r['revision']) for r in records],[('河北仓',2),('山东仓',1)])
        self.assertEqual(len({r['number'] for r in records}),2)
        with closing(self.work.connect()) as db,db:
            db.execute("UPDATE warehouse_execution_heads SET state='not_participating' WHERE warehouse='山东仓'")
        records=followup.read(self.path,self.root/'followup.sqlite3',{})['records']
        self.assertEqual([r['warehouse'] for r in records],['河北仓'])


if __name__=='__main__':unittest.main()
