"""Independent graph-preservation tests for additive order-package imports.

Only fresh synthetic folders/SQLite/XLSX; no platform or real user settings.
"""
import copy
import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from decimal import Decimal
from unittest.mock import patch

import merge_fixture as f
import order_merge
import normal_orders
import order_lifecycle
import order_migration
import work_repository
import deletion_barrier
from runtime_support import Cancelled


class OrderMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=f.Order(self.root/'source','SOURCE');self.target=f.Order(self.root/'target','LOCAL')
        self.backup=patch('storage_chunks.backup_before_write');self.backup.start();self.addCleanup(self.backup.stop)
        self.counter=0

    def prepare(self):
        self.counter+=1;plan=f.stage(self.source,self.target.root,self.root/f'package-{self.counter}.zip')
        self.addCleanup(lambda:order_migration.discard(plan) if Path(plan['stage']).exists() else None)
        return Path(plan['stage'])

    def inspect(self,source=None):return order_merge.inspect(source or self.prepare(),self.target.root)

    def merge(self,source=None):
        source=source or self.prepare();report=order_merge.inspect(source,self.target.root)
        return order_merge.merge(source,self.target.root,report)

    def count(self):return len(f.table_rows(self.target.path,'normal_orders'))

    def test_new_order_preserves_local_order_parameters_catalogs_and_original_source(self):
        local=self.target.signed();kept=f.keep_local_files(self.target)
        source=self.prepare();before=f.file_hashes(source);expected=self.source.signed()
        stores=f.table_rows(self.target.path,'stores');presets=f.table_rows(self.target.path,'presets');jobs=f.table_rows(self.target.path,'jobs')
        report=order_merge.inspect(source,self.target.root)
        self.assertEqual(report['counts']['added'],1);self.assertEqual(report['counts']['conflict'],0)
        self.assertEqual(self.count(),1,'Preview must not insert orders')
        order_merge.merge(source,self.target.root,report)
        self.assertEqual(self.count(),2);self.assertEqual(self.target.signed(),local)
        self.assertEqual(normal_orders.Repository(work_repository.Repository(self.target.root)).get(f.DAY,'SOURCE'),expected)
        self.assertEqual(f.table_rows(self.target.path,'stores'),stores);self.assertEqual(f.table_rows(self.target.path,'presets'),presets);self.assertEqual(f.table_rows(self.target.path,'jobs'),jobs)
        for name,raw in kept.items():self.assertEqual((self.target.root/name).read_bytes(),raw,name)
        self.assertEqual(f.file_hashes(source),before)

    def test_complete_operations_allocation_payment_receipt_chain_survives(self):
        self.source.complete();expected=self.source.view();signed=self.source.signed()
        tables=('lifecycle_routes','lifecycle_versions','lifecycle_events','jst_generations','jst_active','jst_reservations','jst_baselines')
        frozen={t:f.table_rows(self.source.path,t,drop=('id',) if t.endswith('_events') else ()) for t in tables}
        local=self.target.signed();self.merge()
        self.assertEqual(normal_orders.Repository(work_repository.Repository(self.target.root)).get(f.DAY,'SOURCE'),signed)
        actual=order_lifecycle.snapshot(self.target.path,self.source.h)
        for key in ('documents','rows','health','next','status','flow_version'):self.assertEqual(actual[key],expected[key],key)
        self.assertEqual(self.target.signed(),local)
        for table,rows in frozen.items():self.assertEqual(f.table_rows(self.target.path,table,drop=('id',) if table.endswith('_events') else ()),rows,table)

    def test_repeat_identical_prepared_package_is_noop(self):
        source=self.prepare();self.merge(source)
        tables=('normal_orders','normal_revisions','normal_work_revisions','source_files','snapshot_chunks')
        before={t:f.table_rows(self.target.path,t) for t in tables}
        report=order_merge.inspect(source,self.target.root)
        self.assertEqual(report['counts']['added'],0);self.assertEqual(report['counts']['duplicate'],1)
        order_merge.merge(source,self.target.root,report)
        self.assertEqual(self.count(),2)
        self.assertEqual({t:f.table_rows(self.target.path,t) for t in tables},before)

    def test_reexported_same_business_content_is_duplicate_not_new(self):
        self.source.complete();self.merge();before=f.table_rows(self.target.path,'normal_orders');events=f.table_rows(self.target.path,'lifecycle_events')
        # Fresh archive id/created time/staging paths are not a new order identity.
        source=self.prepare();r=order_merge.inspect(source,self.target.root)
        self.assertEqual(r['counts']['duplicate'],1);self.assertEqual(r['counts']['added'],0)
        order_merge.merge(source,self.target.root,r)
        self.assertEqual(f.table_rows(self.target.path,'normal_orders'),before);self.assertEqual(f.table_rows(self.target.path,'lifecycle_events'),events)

    def test_same_signed_order_but_different_downstream_is_conflict_not_duplicate(self):
        self.merge();self.source.save('payment',7)
        local=f.file_hashes(self.target.root);r=self.inspect()
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['duplicate'],0)
        self.assertEqual(f.file_hashes(self.target.root),local)

    def test_same_business_day_store_different_oid_keeps_local_and_imports_independent(self):
        f.second_order(self.source,'LOCAL',oid='another-LOCAL')
        before=self.target.signed();self.merge()
        self.assertEqual(self.count(),2);self.assertEqual(self.target.signed(),before)
        self.assertIsNotNone(normal_orders.Repository(work_repository.Repository(self.target.root)).get(f.DAY,'SOURCE'))
        with closing(sqlite3.connect(self.target.path)) as db:self.assertFalse(db.execute('SELECT 1 FROM normal_orders WHERE oid=?',('another-LOCAL',)).fetchone())

    def test_same_number_different_order_is_conflict(self):
        f.second_order(self.source,'NUMBER-COLLISION',number=self.target.number)
        r=self.inspect();self.assertEqual(r['counts']['added'],1);self.assertEqual(r['counts']['conflict'],1)

    def test_same_oid_different_header_is_conflict(self):
        f.second_order(self.source,'MOVED',oid=self.target.oid)
        r=self.inspect();self.assertEqual(r['counts']['added'],1);self.assertEqual(r['counts']['conflict'],1)

    def test_deleted_target_order_is_not_resurrected(self):
        with closing(sqlite3.connect(self.target.path)) as db,db:
            deletion_barrier.schema(db);db.execute('INSERT INTO deleted_order_tokens VALUES(?,?)',(self.source.oid,'synthetic-delete'))
        r=self.inspect();self.assertEqual(r['counts']['deleted'],1);self.assertEqual(r['counts']['added'],0)
        self.merge();self.assertEqual(self.count(),1)
        self.assertEqual(f.table_rows(self.target.path,'deleted_order_tokens'),[(self.source.oid,'synthetic-delete')])

    def test_cancel_preview_and_merge_do_not_write_target(self):
        source=self.prepare();event=threading.Event();event.set();before=f.file_hashes(self.target.root)
        with self.assertRaises(Cancelled):order_merge.inspect(source,self.target.root,cancel=event)
        r=order_merge.inspect(source,self.target.root)
        with self.assertRaises(Cancelled):order_merge.merge(source,self.target.root,r,cancel=event)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_source_changed_after_preview_rejected(self):
        source=self.prepare();r=order_merge.inspect(source,self.target.root);before=f.file_hashes(self.target.root)
        with closing(sqlite3.connect(source/'workbench.sqlite3')) as db,db:db.execute("UPDATE normal_orders SET number=number||'-changed'")
        with self.assertRaises((ValueError,work_repository.Conflict)):order_merge.merge(source,self.target.root,r)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_target_changed_after_preview_rejected(self):
        source=self.prepare();r=order_merge.inspect(source,self.target.root)
        # A newly conflicting local identity must invalidate the preview. An
        # unrelated order edit is intentionally not a reason to block imports.
        with closing(sqlite3.connect(self.target.path)) as db,db:db.execute('UPDATE normal_orders SET store=?,number=?',('SOURCE',self.source.number))
        before=f.file_hashes(self.target.root)
        with self.assertRaises((ValueError,work_repository.Conflict)):order_merge.merge(source,self.target.root,r)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_corrupt_content_chunk_never_imported(self):
        source=self.prepare();before=f.file_hashes(self.target.root)
        import storage_chunks
        def references(node):
            if node[0]=='ref':return [node[1]]
            if node[0]=='dict':return [h for _,child in node[1] for h in references(child)]
            return [h for child in node[1] for h in references(child)]
        with closing(sqlite3.connect(source/'workbench.sqlite3')) as db,db:
            raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=?',(self.source.oid,)).fetchone()[0]
            digest=references(storage_chunks.require_manifest(raw)['tree'])[0]
            db.execute("UPDATE snapshot_chunks SET data=X'00' WHERE hash=?",(digest,))
        report=order_merge.inspect(source,self.target.root)
        self.assertEqual(report['counts']['added'],0);self.assertEqual(report['counts']['conflict'],1)
        order_merge.merge(source,self.target.root,report);self.assertEqual(self.count(),1)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_corrupt_original_file_never_imported(self):
        source=self.prepare();before=f.file_hashes(self.target.root)
        with closing(sqlite3.connect(source/'workbench.sqlite3')) as db,db:db.execute("UPDATE source_files SET content=X'00'")
        report=order_merge.inspect(source,self.target.root)
        self.assertEqual(report['counts']['added'],0);self.assertEqual(report['counts']['conflict'],1)
        order_merge.merge(source,self.target.root,report);self.assertEqual(self.count(),1)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_unknown_order_owned_table_is_isolated_not_lost(self):
        f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            db.execute('CREATE TABLE future_order_fact(oid TEXT PRIMARY KEY,critical TEXT)')
            db.execute('INSERT INTO future_order_fact VALUES(?,?)',(self.source.oid,'must-not-disappear'))
        r=self.inspect();self.assertEqual(r['counts']['added'],1);self.assertEqual(r['counts']['conflict'],1)

    def test_shared_stock_batch_preserves_order_snapshot_and_current_local_pointer(self):
        other=f.second_order(self.source,'SHARED');done=f.shared_batch(self.source,other)
        from test_jst import catalog
        import jst_orders,jst_batches
        jst_orders.commit_refresh(self.target.path,jst_orders.prepare_refresh(self.target.path,'河北仓',catalog=catalog(stock=999)))
        current=f.table_rows(self.target.path,'jst_warehouses');batch=done['batch_id']
        with closing(sqlite3.connect(self.source.path)) as db:
            before=jst_batches.context(db,batch);reserved=jst_batches.reservations(db,batch,'河北仓')
        self.merge();self.assertEqual(self.count(),3);self.assertEqual(f.table_rows(self.target.path,'jst_warehouses'),current)
        with closing(sqlite3.connect(self.target.path)) as db:
            self.assertEqual(jst_batches.context(db,batch),before);self.assertEqual(jst_batches.reservations(db,batch,'河北仓'),reserved)

    def test_shared_stock_conflict_blocks_related_group_only(self):
        other=f.second_order(self.source,'LOCAL',oid='source-local-conflict');f.shared_batch(self.source,other)
        f.second_order(self.source,'INDEPENDENT')
        before=self.target.signed();r=self.inspect()
        self.assertEqual(r['counts']['added'],1);self.assertEqual(r['counts']['conflict'],2)
        self.merge();self.assertEqual(self.count(),2);self.assertEqual(self.target.signed(),before)
        with closing(sqlite3.connect(self.target.path)) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM normal_orders WHERE oid=?',(self.source.oid,)).fetchone())
            self.assertIsNotNone(db.execute('SELECT 1 FROM normal_orders WHERE store=?',('INDEPENDENT',)).fetchone())

    def test_shared_purchase_preserves_whole_original_row_and_both_links(self):
        import purchases
        other=f.second_order(self.source,'SOURCE',oid='source-yesterday',day='2026-11-12')
        f.shared_purchase(self.source,[self.source.h,other])
        expected=purchases.documents(self.source.path)
        self.merge();self.assertEqual(self.count(),3)
        self.assertEqual(purchases.documents(self.target.path),expected)
        links=f.table_rows(self.target.path,'purchase_link_index')
        self.assertEqual({r[2] for r in links},{self.source.oid,'source-yesterday'})
        self.assertEqual([r[-1] for r in links],['2','3'])
        self.assertEqual(Decimal(expected[0]['payload']['rows'][0]['base_quantity']),Decimal(10))

    def test_shared_purchase_conflict_does_not_import_dangling_half(self):
        other=f.second_order(self.source,'SOURCE',oid='source-yesterday',day='2026-11-12')
        f.shared_purchase(self.source,[self.source.h,other]);f.second_order(self.source,'SAFE')
        f.second_order(self.target,'SOURCE',oid='local-yesterday',day='2026-11-12')
        r=self.inspect();self.assertEqual(r['counts']['conflict'],2);self.assertEqual(r['counts']['added'],1)
        self.merge();self.assertEqual(self.count(),3)
        self.assertEqual(f.table_rows(self.target.path,'purchase_documents'),[])

    def test_imported_workflow_pauses_only_source_and_repeat_deduplicates(self):
        from aoxiang_workflows import Workflows
        source=Workflows(self.source.root/'aoxiang').prepare(self.source.heads)
        local=Workflows(self.target.root/'aoxiang').prepare(self.target.heads)
        before=copy.deepcopy(Workflows(self.target.root/'aoxiang').get(local['id']))
        stage=self.prepare();self.merge(stage)
        store=Workflows(self.target.root/'aoxiang')
        self.assertEqual(store.get(local['id']),before)
        imported=store.get(source['id']);self.assertEqual(imported['state'],'PAUSED')
        self.assertEqual([i['key'] for i in imported['items']],[i['key'] for i in source['items']])
        self.assertEqual([i['head'] for i in imported['items']],[i['head'] for i in source['items']])
        self.assertEqual(store.next_queued()['id'],local['id'])
        r=order_merge.inspect(stage,self.target.root)
        self.assertEqual(r['counts']['duplicate'],1);self.assertEqual(r['counts']['conflict'],0)

    def test_private_copy_sql_failure_never_changes_original_or_source(self):
        source=self.prepare();original=f.file_hashes(self.target.root);source_before=f.file_hashes(source)
        private=self.root/'private-target';shutil.copytree(self.target.root,private)
        with closing(sqlite3.connect(private/'workbench.sqlite3')) as db,db:
            db.execute("CREATE TRIGGER fail_merge_insert BEFORE INSERT ON normal_revisions BEGIN SELECT RAISE(ABORT,'synthetic-import-failure'); END")
        report=order_merge.inspect(source,private)
        with self.assertRaisesRegex((sqlite3.DatabaseError,ValueError),'synthetic-import-failure'):
            order_merge.merge(source,private,report)
        self.assertEqual(f.file_hashes(self.target.root),original);self.assertEqual(f.file_hashes(source),source_before)

    def test_unique_platform_binding_and_actual_node_ledger_survive(self):
        record=f.purchase_ledger(self.source,platform_id='PO-SYNTHETIC-0528')
        events=f.table_rows(self.source.root/'aoxiang/component-state/state.sqlite','purchase_create_events')
        self.merge()
        path=self.target.root/'aoxiang/component-state/state.sqlite'
        with closing(sqlite3.connect(path)) as db:
            saved=json.loads(db.execute('SELECT payload FROM purchase_creates WHERE id=?',(record['id'],)).fetchone()[0])
            self.assertEqual(saved['binding'],record['binding']);self.assertEqual(saved['intent'],record['intent'])
            self.assertTrue(saved['uploadAttempted']);self.assertEqual(saved['receipt'],record['receipt'])
            self.assertEqual(db.execute('SELECT platform_order FROM purchase_creates WHERE id=?',(record['id'],)).fetchone()[0],'PO-SYNTHETIC-0528')
        self.assertEqual(f.table_rows(path,'purchase_create_events'),events)
        self.assertEqual(f.read_purchase_ledger(self.target.root,record['id'])['binding'],record['binding'])
        r=self.inspect();self.assertEqual(r['counts']['duplicate'],1);self.assertEqual(r['counts']['conflict'],0)

    def test_same_platform_order_on_other_local_order_conflicts_without_changing_target(self):
        f.purchase_ledger(self.source,platform_id='PO-SYNTHETIC-COLLISION')
        f.purchase_ledger(self.target,platform_id='PO-SYNTHETIC-COLLISION')
        before=f.file_hashes(self.target.root);r=self.inspect()
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],0)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_unknown_submission_is_preserved_not_treated_as_never_uploaded(self):
        record=f.purchase_ledger(self.source)
        self.merge()
        path=self.target.root/'aoxiang/component-state/state.sqlite'
        with closing(sqlite3.connect(path)) as db:
            saved=json.loads(db.execute('SELECT payload FROM purchase_creates WHERE id=?',(record['id'],)).fetchone()[0])
        self.assertTrue(saved['uploadAttempted']);self.assertIsNone(saved['receipt'])
        self.assertEqual(saved['error']['code'],'SUBMISSION_UNKNOWN');self.assertEqual(saved['state'],'NEEDS_REVIEW')
        self.assertEqual(saved['intent'],record['intent'])
        reopened=f.read_purchase_ledger(self.target.root,record['id'])
        self.assertTrue(reopened['uploadAttempted']);self.assertEqual(reopened['state'],'NEEDS_REVIEW')

    def test_component_retirement_fence_blocks_old_package_without_main_tombstone(self):
        f.purchase_ledger(self.target)
        with closing(sqlite3.connect(self.target.root/'aoxiang/component-state/state.sqlite')) as db,db:
            db.execute('INSERT INTO retired_host_orders VALUES(?,?)',(self.source.oid,'synthetic-retirement'))
        r=self.inspect();self.assertEqual(r['counts']['deleted'],1);self.assertEqual(r['counts']['added'],0)
        self.merge();self.assertEqual(self.count(),1)

    def test_external_path_collision_isolated_without_overwriting_target_file(self):
        source=self.prepare()
        mapping=json.loads((source/'迁入原文件映射.json').read_text('utf-8'))['paths']
        signed=self.source.signed()
        with closing(sqlite3.connect(self.source.path)) as db:
            payload=normal_orders.decode(db.execute('SELECT payload FROM normal_work_revisions').fetchone()[0],db)
        original=Path(payload['catalogs']['河北仓']['source_path'])
        self.assertIn(str(original),mapping)
        original.write_bytes(b'DIFFERENT EXISTING LOCAL FILE')
        r=order_merge.inspect(source,self.target.root)
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],0)
        self.assertEqual(original.read_bytes(),b'DIFFERENT EXISTING LOCAL FILE')
        self.assertEqual(self.source.signed(),signed)

    def test_missing_current_signed_revision_isolated_without_blocking_independent_order(self):
        f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            db.execute('UPDATE normal_orders SET current=99 WHERE oid=?',(self.source.oid,))
        r=self.inspect();self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],1)
        self.merge();self.assertEqual(self.count(),2)
        self.assertIsNone(normal_orders.Repository(work_repository.Repository(self.target.root)).get(f.DAY,'SOURCE'))
        self.assertIsNotNone(normal_orders.Repository(work_repository.Repository(self.target.root)).get(f.DAY,'SAFE'))

    def test_missing_required_work_revision_field_isolated(self):
        f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=?',(self.source.oid,)).fetchone()[0]
            payload=normal_orders.decode(raw,db);payload.pop('work_revision')
            db.execute('UPDATE normal_revisions SET payload=? WHERE oid=?',(normal_orders.encode(payload,db),self.source.oid))
        r=self.inspect();self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],1)

    def test_missing_frozen_work_revision_record_isolated(self):
        f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            db.execute('DELETE FROM normal_work_revisions WHERE revision=?',('work-'+self.source.oid,))
        r=self.inspect();self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],1)

    def test_missing_active_generation_isolated_without_blocking_independent_order(self):
        import jst_orders
        f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            jst_orders.schema(db);db.execute('INSERT INTO jst_active VALUES(?,?,?)',(self.source.oid,'河北仓','missing-generation'))
        r=self.inspect();self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],1)
        self.merge();self.assertEqual(self.count(),2)
        self.assertEqual(f.table_rows(self.target.path,'jst_active'),[])

    def test_existing_same_sha_bad_original_is_not_reused_or_overwritten(self):
        with closing(sqlite3.connect(self.source.path)) as db:
            sha,name,saved,_=db.execute('SELECT sha,name,saved,content FROM source_files LIMIT 1').fetchone()
        with closing(sqlite3.connect(self.target.path)) as db,db:
            db.execute('INSERT OR REPLACE INTO source_files VALUES(?,?,?,?)',(sha,name,saved,b'corrupt-existing-local-original'))
        before=f.file_hashes(self.target.root);r=self.inspect()
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],0)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_existing_same_hash_bad_chunk_is_not_reused_or_overwritten(self):
        import storage_chunks
        def reference(node):
            if node[0]=='ref':return node[1]
            if node[0]=='dict':return reference(node[1][0][1])
            return reference(node[1][0])
        with closing(sqlite3.connect(self.source.path)) as db:
            raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=?',(self.source.oid,)).fetchone()[0]
            key=reference(storage_chunks.require_manifest(raw)['tree'])
            size=db.execute('SELECT size FROM snapshot_chunks WHERE hash=?',(key,)).fetchone()[0]
        with closing(sqlite3.connect(self.target.path)) as db,db:
            db.execute('INSERT OR REPLACE INTO snapshot_chunks VALUES(?,?,?)',(key,size,b'corrupt-existing-local-chunk'))
        before=f.file_hashes(self.target.root);r=self.inspect()
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],0)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_real_machine_command_and_host_record_pause_without_runtime_dispatch(self):
        from aoxiang_jobs import Jobs
        before=f.machine_command(self.source);record=before['record']
        self.assertEqual(record['state'],'QUEUED')
        source=self.prepare();r=order_merge.inspect(source,self.target.root)
        self.assertEqual(r['counts']['added'],1);self.assertEqual(r['counts']['conflict'],0)
        order_merge.merge(source,self.target.root,r)
        local=Jobs(self.target.root/'aoxiang/jobs.sqlite3').get(record['id'])
        self.assertEqual(local['state'],'PAUSED');self.assertEqual(local['record']['state'],'PAUSED')
        self.assertEqual(local['command'],record['request'])
        self.assertEqual(local['context']['order_ref']['oid'],self.source.oid)
        reopened=f.read_machine_and_kick(self.target.root,record['id'])
        self.assertEqual(reopened['visits'],[])
        self.assertEqual(reopened['record']['state'],'PAUSED')
        self.assertEqual(reopened['record']['sequence'],record['sequence']+1)
        self.assertEqual(reopened['record']['request'],record['request'])
        self.assertEqual(local['record'],reopened['record'],'Host and component must agree at the same receipt sequence')
        self.assertEqual(local['sequence'],reopened['record']['sequence'])
        self.assertEqual(reopened['runs'],before['runs']);self.assertEqual(reopened['items'],before['items'])
        self.assertEqual(reopened['events'][:-1],before['events'])
        self.assertEqual(reopened['events'][-1]['kind'],'MIGRATION_PAUSED')
        # An ordinary status refresh is not a new business version. It must not
        # make a repeated archive conflict just because host observed_at changed.
        Jobs(self.target.root/'aoxiang/jobs.sqlite3').observe(record['id'],reopened['record'])
        repeated=order_merge.inspect(source,self.target.root)
        self.assertEqual(repeated['counts']['duplicate'],1);self.assertEqual(repeated['counts']['added'],0)

    def incompatible_target_schema(self,sql):
        with closing(sqlite3.connect(self.target.path)) as db,db:
            rows=db.execute('SELECT * FROM normal_orders').fetchall()
            db.execute('DROP TABLE normal_orders');db.execute(sql)
            db.executemany('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',rows)
        before=f.file_hashes(self.target.root);r=self.inspect()
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],0)
        self.assertEqual(f.file_hashes(self.target.root),before)

    def test_same_column_names_different_affinity_is_not_accepted(self):
        self.incompatible_target_schema('CREATE TABLE normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,oid BLOB NOT NULL UNIQUE,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,PRIMARY KEY(day,store,kind))')

    def test_same_column_names_different_primary_key_is_not_accepted(self):
        self.incompatible_target_schema('CREATE TABLE normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,oid TEXT NOT NULL UNIQUE,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,PRIMARY KEY(day,store))')

    def test_missing_order_identity_unique_constraint_is_not_accepted(self):
        self.incompatible_target_schema('CREATE TABLE normal_orders(day TEXT NOT NULL,store TEXT NOT NULL,kind TEXT NOT NULL,oid TEXT NOT NULL,number TEXT NOT NULL UNIQUE,current INTEGER NOT NULL,PRIMARY KEY(day,store,kind))')

    def declared_original_source(self,*,retain_external):
        self.source.save('return',5);f.second_order(self.source,'SAFE')
        with closing(sqlite3.connect(self.source.path)) as db,db:
            payload=json.loads(db.execute('SELECT payload FROM lifecycle_versions WHERE oid=? AND stage=?',(self.source.oid,'return')).fetchone()[0])
            declared=payload['files'][0]
            original=db.execute('SELECT content FROM lifecycle_files WHERE sha=?',(declared['sha'],)).fetchone()[0]
            db.execute('DELETE FROM lifecycle_files WHERE sha=?',(declared['sha'],))
        if not retain_external:
            path=Path(declared['path']);self.assertTrue(path.resolve().is_relative_to(self.root.resolve()));path.unlink()
        stage=self.prepare()
        with closing(sqlite3.connect(stage/'workbench.sqlite3')) as db:
            self.assertIsNone(db.execute('SELECT 1 FROM lifecycle_files WHERE sha=?',(declared['sha'],)).fetchone())
        mapping=json.loads((stage/'迁入原文件映射.json').read_text('utf-8'))['paths']
        retained={hashlib.sha256((stage/rel).read_bytes()).hexdigest() for rel in mapping.values()}
        self.assertEqual(declared['sha'] in retained,retain_external)
        # Normal signed snapshots never had a files field: do not invent a new
        # requirement for those legacy records while checking explicit claims.
        safe=normal_orders.Repository(work_repository.Repository(self.source.root)).get(f.DAY,'SAFE')
        self.assertNotIn('files',safe['payload'])
        return stage,declared,original

    def test_declared_original_missing_database_and_package_isolates_only_related_order(self):
        stage,declared,original=self.declared_original_source(retain_external=False)
        r=order_merge.inspect(stage,self.target.root)
        self.assertEqual(r['counts']['conflict'],1);self.assertEqual(r['counts']['added'],1)
        order_merge.merge(stage,self.target.root,r)
        self.assertEqual(self.count(),2)
        repo=normal_orders.Repository(work_repository.Repository(self.target.root))
        self.assertIsNone(repo.get(f.DAY,'SOURCE'));self.assertIsNotNone(repo.get(f.DAY,'SAFE'))
        self.assertEqual(f.table_rows(self.target.path,'lifecycle_versions'),[])

    def test_declared_original_from_package_restores_actual_attachment_access(self):
        stage,declared,original=self.declared_original_source(retain_external=True)
        r=order_merge.inspect(stage,self.target.root)
        self.assertEqual(r['counts']['added'],2);self.assertEqual(r['counts']['conflict'],0)
        order_merge.merge(stage,self.target.root,r)
        self.assertEqual(self.count(),3)
        copied=self.root/'restored-return-original.xlsx'
        order_lifecycle.attachment(self.target.path,self.source.h,'return',1,declared['sha'],copied)
        self.assertEqual(copied.read_bytes(),original)
        repeated=order_merge.inspect(stage,self.target.root)
        self.assertEqual(repeated['counts']['duplicate'],2);self.assertEqual(repeated['counts']['added'],0)

    def test_source_parent_with_own_deleted_or_retired_fence_is_conflict_not_resurrected(self):
        for fence in ('main','component'):
            with self.subTest(fence=fence):
                self.source=f.Order(self.root/('source-'+fence),'SOURCE')
                self.target=f.Order(self.root/('target-'+fence),'LOCAL')
                f.second_order(self.source,'SAFE')
                if fence=='main':
                    with closing(sqlite3.connect(self.source.path)) as db,db:
                        deletion_barrier.schema(db)
                        db.execute('INSERT INTO deleted_order_tokens VALUES(?,?)',(self.source.oid,'source-contradiction'))
                else:
                    # Construct the complete supported Node protocol via the
                    # actual local runtime before introducing the contradiction.
                    f.purchase_ledger(self.source)
                    with closing(sqlite3.connect(self.source.root/'aoxiang/component-state/state.sqlite')) as db,db:
                        db.execute('INSERT INTO retired_host_orders VALUES(?,?)',(self.source.oid,'source-contradiction'))
                source_before=f.file_hashes(self.source.root);local=self.target.signed()
                stage=self.prepare();stage_before=f.file_hashes(stage)
                r=order_merge.inspect(stage,self.target.root)
                states={o['oid']:o['status'] for o in r['orders']}
                self.assertEqual(states[self.source.oid],'CONFLICT')
                self.assertEqual(states['synthetic-oid-SAFE'],'ADD')
                self.assertEqual(r['counts'],{'added':1,'duplicate':0,'conflict':1,'deleted':0})
                order_merge.merge(stage,self.target.root,r)
                self.assertEqual(self.count(),2);self.assertEqual(self.target.signed(),local)
                repo=normal_orders.Repository(work_repository.Repository(self.target.root))
                self.assertIsNone(repo.get(f.DAY,'SOURCE'));self.assertIsNotNone(repo.get(f.DAY,'SAFE'))
                self.assertEqual(f.file_hashes(stage),stage_before)
                self.assertEqual(f.file_hashes(self.source.root),source_before)


if __name__=='__main__':unittest.main()
