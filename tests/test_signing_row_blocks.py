"""Signing row reuse must be scoped by order identity, not just block hashes."""
import hashlib
import sqlite3
import threading
import unittest
import zlib
from contextlib import closing
from unittest.mock import patch

import data_maintenance
import export_receipts
import normal_delivery
import result_semantics as sem
import storage_chunks as chunks
import storage_maintenance
import warehouse_execution
import work_repository as wr
from encoding_store import Store
from runtime_support import Cancelled
from test_preissue import result, row
import test_signing_failures as failures
from version_info import MODEL_VERSION
from work_failures import WorkFailure


def signing_fixture(layout, count=260):
    results = {}
    for store, warehouses, quantity in layout:
        rows = [row(f'{i:05}',q=quantity,step=12,price=2.25) for i in range(count)]
        for i, line in enumerate(rows):
            line.update(B=store,A=f'合成门店{store}',AU=warehouses[i%len(warehouses)])
            line['_audit'] = {'index':i,'quantity':quantity,'history':[False,0.0,-0.0]}
        results[store] = dict(result(rows),store=store,store_name=f'合成门店{store}')
    return dict(schema=1,day=wr.today(),model_version=MODEL_VERSION,input_dirty=False,
                results=results,states={s:{'name':r['store_name'],'state':'已完成'} for s,r in results.items()})


def rows_for(tree, *path):
    return chunks.node_at(tree,path+('rows',))


def replace_rows(tree, *path, node):
    children = chunks.node_at(tree,path)[1]
    i = next(i for i,(key,_) in enumerate(children) if key=='rows')
    children[i][1] = node


class SigningRowBlocks(unittest.TestCase):
    setUp = failures.SigningFailures.setUp
    tearDown = failures.SigningFailures.tearDown

    def set_rows(self,quantity):
        return signing_fixture([('A',('河北仓',),quantity)],count=1)

    def confirm(self,payload,heads=(),**kwargs):
        job = normal_delivery.job_head(self.work,payload['day'])
        return self.repo.confirm(payload,list(payload['results']),job['revision'] if job else None,
                                 {h['store']:h['revision'] for h in heads},headers_only=True,**kwargs)

    def business_rows(self):
        tables = ('jobs','normal_orders','normal_revisions','normal_work_revisions','normal_events',
                  'warehouse_execution_heads','warehouse_execution_revisions','snapshot_chunks')
        with closing(self.work.connect()) as db:
            return {table: list(db.execute(f'SELECT * FROM {table} ORDER BY rowid'))
                    if db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone() else []
                    for table in tables}

    def manifests(self):
        with closing(self.work.connect()) as db:
            return [chunks.require_manifest(db.execute(f'SELECT payload FROM {table}').fetchone()[0])
                    for table in ('jobs','normal_revisions','warehouse_execution_revisions')]

    def test_scheduled_signing_reads_pinned_results_through_the_write_connection(self):
        from scheduled_replenishment import Repository
        from scheduled_snapshots import Reference, Results, projection

        payload = signing_fixture([('A', ('河北仓', '山东仓'), 12), ('B', ('河北仓', '山东仓'), 24)])
        scheduled = Repository(self.work.path)
        schedule = {'name': '签发写锁', 'plan_id': 'test-plan', 'kind': 'weekly',
                    'weekdays': list(range(7)), 'time': '06:30', 'workers': 1, 'enabled': False}
        run = scheduled.claim(scheduled.manual(schedule)['id'])
        expected = payload['results']
        for code, value in expected.items():
            scheduled.save_value(run['id'], 'result:' + code, value, run['epoch'])
        digests = scheduled.digests(run['id'])
        payload['results'] = Results(
            {code: Reference(self.work.path, run['id'], 'result:' + code, digests['result:' + code])
             for code in expected},
            {code: projection(value, None) for code, value in expected.items()},
        )

        class ExclusiveConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                # Windows reproduced this stronger lock after dirty pages spilled.
                if sql == 'BEGIN IMMEDIATE':
                    sql = 'BEGIN EXCLUSIVE'
                return super().execute(sql, parameters)

        def signing_connection():
            return sqlite3.connect(self.work.path, factory=ExclusiveConnection)

        with patch.object(self.work, 'connect', signing_connection):
            heads = self.confirm(payload)
        self.assertEqual(len(heads), 2)
        for head in heads:
            self.assertEqual(len(head['warehouse_heads']), 2)
            self.assertEqual(self.repo.get(payload['day'], head['store'])['payload']['result'],
                             expected[head['store']])

    def test_single_warehouse_skips_reencoding_both_targets_and_verifies_once(self):
        payload = signing_fixture([('A',('山东仓',),12)])
        verify = Store.verify_row_block
        verified = []
        def observe(spool,db,key,size,required,cancel):
            self.assertTrue(db.in_transaction)
            verified.append(key)
            return verify(spool,db,key,size,required,cancel)
        encode = Store.encode
        encoded_rows = []
        def record(spool,value,feed,fragment=False):
            if isinstance(value,list) and value and isinstance(value[0],dict) and 'AS' in value[0]:
                encoded_rows.append(len(value))
            return encode(spool,value,feed,fragment)
        batch = normal_delivery.prepare_owned_batch(self.repo,payload,['A'])
        with patch.object(Store,'verify_row_block',observe),patch.object(Store,'encode',record):
            outcome = normal_delivery.execute_batch(self.repo,batch,self.output)
        self.assertEqual(encoded_rows,[256,4])
        self.assertEqual(len(verified),2)
        self.assertEqual(len(set(verified)),2)
        self.assertEqual(len(outcome['paths']),1)
        draft,parent,child = self.manifests()
        self.assertEqual(len({m['digest'] for m in (draft,parent)}),2)
        self.assertEqual(rows_for(draft['tree'],'results','A'),rows_for(parent['tree'],'result'))
        self.assertEqual(rows_for(parent['tree'],'result'),rows_for(child['tree'],'result'))
        self.assertIsNotNone(parent['binding'])
        self.assertIsNotNone(child['binding'])
        self.assertEqual(self.work.get_job(payload['day'])['payload']['results']['A'],payload['results']['A'])
        head, = outcome['orders'][0]['warehouse_heads']
        self.assertEqual(warehouse_execution.get_order(self.repo,head)['payload']['result']['rows'],payload['results']['A']['rows'])

    def test_valid_other_store_rows_rejected_before_any_issue(self):
        payload = signing_fixture([('A',('河北仓',),12),('B',('河北仓',),24)],count=1)
        original = wr.prepare_job
        def swap(*args,**kwargs):
            info,pack,digest = original(*args,**kwargs)
            a,b = (rows_for(pack.tree,'results',s) for s in ('A','B'))
            for node in a,b:
                self.assertEqual(hashlib.sha256(b''.join(pack.chunks.store.raw(node[1]))).hexdigest(),node[1])
            replace_rows(pack.tree,'results','A',node=b)
            replace_rows(pack.tree,'results','B',node=a)
            return info,pack,digest
        before = self.business_rows()
        with patch.object(wr,'prepare_job',swap),self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 1.*内容'):
            self.confirm(payload)
        self.assertEqual(self.business_rows(),before)

    def test_valid_old_rows_rejected_before_new_revision(self):
        v1 = self.set_rows(12)
        before_heads = self.confirm(v1)
        before = self.business_rows()
        original = wr.prepare_job
        def stale(*args,**kwargs):
            info,pack,digest = original(*args,**kwargs)
            key,size = pack.chunks.store.encode(v1['results']['A']['rows'],lambda _:None)
            self.assertEqual(hashlib.sha256(b''.join(pack.chunks.store.raw(key))).hexdigest(),key)
            replace_rows(pack.tree,'results','A',node=['ref',key,size])
            return info,pack,digest
        with patch.object(wr,'prepare_job',stale),self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 2.*内容'):
            self.confirm(self.set_rows(24),before_heads)
        self.assertEqual(self.business_rows(),before)

    def test_swapped_prepared_target_at_commit_does_not_publish(self):
        payload = signing_fixture([('A',('河北仓',),12),('B',('河北仓',),24)],count=1)
        prepare, targets = chunks.prepare, []
        def observe(*args,**kwargs):
            pack = prepare(*args,**kwargs)
            if kwargs.get('rows_from') is not None:targets.append(pack)
            return pack
        def tamper(heads):
            a,b = targets[:2]
            replace_rows(a.tree,'result',node=rows_for(b.tree,'result'))
        before = self.business_rows()
        with patch.object(chunks,'prepare',observe),self.assertRaisesRegex(wr.Conflict,'门店 A.*签发修订 1.*待签发清单'):
            self.confirm(payload,prepared_heads=tamper)
        self.assertEqual(self.business_rows(),before)

    def test_selected_warehouse_does_not_qualify_mixed_complete_results(self):
        payload = signing_fixture([('A',('河北仓','山东仓'),12)])
        with patch.object(Store,'verify_row_block',side_effect=AssertionError('mixed result reused')):
            head, = self.confirm(payload,warehouses={'A':['山东仓']})
        child, = head['warehouse_heads']
        self.assertEqual(child['warehouse'],'山东仓')
        original = payload['results']['A']['rows']
        with closing(self.work.connect()) as db:
            self.assertEqual([h['warehouse'] for h in warehouse_execution.heads(db)],['山东仓'])
        self.assertEqual(warehouse_execution.get_order(self.repo,child)['payload']['result']['rows'],original[1::2])
        self.assertEqual(self.repo.get(payload['day'],'A')['payload']['result']['rows'],original)

    def test_reissue_retains_original_and_repeat_does_not_change_versions(self):
        v1 = self.set_rows(12);a = self.confirm(v1)
        v2 = self.set_rows(24);b = self.confirm(v2,a)
        self.assertEqual(b,self.confirm(v2,b))
        self.assertEqual(a[0]['number'],b[0]['number'])
        for heads,quantity,revision in [(a,12,1),(b,24,2)]:
            child, = heads[0]['warehouse_heads']
            self.assertEqual((child['source_parent_revision'],child['child_revision']),(revision,revision))
            self.assertEqual(warehouse_execution.get_order(self.repo,child)['payload']['result']['rows'][0]['AS'],quantity)
            self.assertEqual(self.repo.get(v1['day'],'A',revision)['payload']['result']['rows'][0]['AS'],quantity)

    def test_cancel_during_shared_writes_rolls_back_and_preserves_receipt(self):
        payload = signing_fixture([('A',('河北仓',),12),('B',('山东仓',),24)],count=1)
        batch = normal_delivery.prepare_owned_batch(self.repo,payload,['A','B'])
        cancel = threading.Event()
        before = self.business_rows()
        def on_progress(value):
            if '事务内容准备完成' in str(value):cancel.set()
        with self.assertRaises(WorkFailure) as failure:
            normal_delivery.execute_batch(self.repo,batch,self.output,progress=on_progress,cancel=cancel)
        self.assertIsInstance(failure.exception.__cause__,Cancelled)
        self.assertEqual(self.business_rows(),before)
        receipt, = export_receipts.recent(self.work.path)
        self.assertEqual(receipt['failure']['commit_state'],'not_started')
        self.assertEqual(len(receipt['files']),2)
        self.assertEqual(list(self.output.iterdir()),[])

    def test_other_window_winning_draft_cas_keeps_its_own_records(self):
        payload = self.set_rows(12)
        concurrent = self.set_rows(24)
        saved = []
        def winner(_):
            self.work.save_job(concurrent['day'],concurrent)
            saved.append(self.business_rows())
        with self.assertRaisesRegex(wr.Conflict,'另一窗口'):
            self.confirm(payload,prepared_heads=winner)
        self.assertEqual(self.business_rows(),saved[0])
        self.assertEqual(self.work.get_job(concurrent['day'])['payload'],concurrent)

    def test_private_damage_bypasses_cache_and_blocks_the_entire_batch(self):
        payload = self.set_rows(12)
        original = wr.prepare_job
        packs = []
        def observe(*args,**kwargs):
            info,pack,digest = original(*args,**kwargs)
            packs.append(pack)
            return info,pack,digest
        for mode in ('missing','tampered'):
            with self.subTest(mode=mode):
                def break_block(_):
                    pack = packs[-1]
                    _,key,_ = rows_for(pack.tree,'results','A')
                    spool = pack.chunks.store
                    if mode=='missing':del spool.index[key]
                    else:
                        _,offset,length,_ = spool.index[key]
                        spool.file.seek(offset+length-1)
                        final = spool.file.read(1)
                        spool.file.seek(offset+length-1)
                        spool.file.write(bytes([final[0]^1]))
                before = self.business_rows()
                with patch.object(wr,'prepare_job',observe),self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 1'):
                    self.confirm(payload,prepared_heads=break_block)
                self.assertEqual(self.business_rows(),before)

    def test_live_source_change_at_commit_is_rejected_by_independent_warehouse_digest(self):
        payload = self.set_rows(12)
        def change(_):
            original = payload['results']['A']
            original['rows'][0].update(AS=24,BV=54)
            original['summary'] = sem.summarize(original['rows'])
        before = self.business_rows()
        with self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 1.*预编码仓单'):
            self.confirm(payload,prepared_heads=change)
        self.assertEqual(self.business_rows(),before)

    def test_existing_block_missing_or_same_size_wrong_content_fails_atomically(self):
        payload = self.set_rows(12)
        self.work.save_job(payload['day'],payload)
        with closing(self.work.connect()) as db:
            blob, = db.execute('SELECT payload FROM jobs').fetchone()
            _,key,size = rows_for(chunks.require_manifest(blob)['tree'],'results','A')
            packed, = db.execute('SELECT data FROM snapshot_chunks WHERE hash=?',(key,)).fetchone()
        saved = []
        for mode in ('missing','tampered'):
            with self.subTest(mode=mode):
                def break_block(_):
                    with closing(self.work.connect()) as db,db:
                        if mode=='missing':db.execute('DELETE FROM snapshot_chunks WHERE hash=?',(key,))
                        else:
                            raw = zlib.decompress(packed)
                            different = raw.replace(b'"AS": 12',b'"AS": 24')
                            self.assertNotEqual(raw,different)
                            self.assertEqual(len(different),size)
                            db.execute('UPDATE snapshot_chunks SET data=? WHERE hash=?',(zlib.compress(different),key))
                    saved.append(self.business_rows())
                with self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 1.*(缺少|哈希)'):
                    self.confirm(payload,prepared_heads=break_block)
                self.assertEqual(self.business_rows(),saved[-1])
                with closing(self.work.connect()) as db,db:
                    db.execute('INSERT OR REPLACE INTO snapshot_chunks VALUES(?,?,?)',(key,size,packed))
        self.assertEqual(len(self.confirm(payload)),1)

    def test_already_missing_referenced_block_cannot_be_rebuilt_by_a_changed_draft(self):
        payload = self.set_rows(12)
        self.work.save_job(payload['day'],payload)
        with closing(self.work.connect()) as db,db:
            blob, = db.execute('SELECT payload FROM jobs').fetchone()
            _,key,_ = rows_for(chunks.require_manifest(blob)['tree'],'results','A')
            db.execute('DELETE FROM snapshot_chunks WHERE hash=?',(key,))
        before = self.business_rows()
        payload['export_log'] = ['独立改变草稿元数据']
        with self.assertRaisesRegex(wr.Conflict,'门店 A.*河北仓.*签发修订 1.*缺少'):
            self.confirm(payload)
        self.assertEqual(self.business_rows(),before)

    def test_backup_and_deleting_other_references_preserve_the_shared_block(self):
        payload = self.set_rows(12)
        head, = self.confirm(payload)
        child, = head['warehouse_heads']
        saved = self.work.get_job(payload['day'])
        backup = self.root/'restored.sqlite3'
        storage_maintenance.backup(self.work.path,backup)
        with closing(sqlite3.connect(backup)) as db:
            blob, = db.execute('SELECT payload FROM warehouse_execution_revisions').fetchone()
            self.assertEqual(chunks.load(blob,db)['result']['rows'],payload['results']['A']['rows'])
        data_maintenance.delete_job(self.work.path,payload['day'],saved['revision'])
        self.assertIsNone(self.work.get_job(payload['day']))
        self.assertEqual(warehouse_execution.get_order(self.repo,child)['payload']['result']['rows'],payload['results']['A']['rows'])


if __name__=='__main__':
    unittest.main()
