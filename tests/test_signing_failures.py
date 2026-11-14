"""Signing failure evidence, atomicity, and fixed-revision export recovery."""
import copy
import gc
import json
import tempfile
import unittest
import weakref
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import normal_delivery as delivery
import normal_orders
import work_repository as wr
import export_receipts
import export_recovery
from work_failures import FailureContext, WorkFailure
from test_preissue import result, row
from version_info import MODEL_VERSION


class SigningFailures(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work = wr.Repository(self.root / 'settings')
        self.repo = normal_orders.Repository(self.work)
        self.output = self.root / 'output'; self.output.mkdir()

    def tearDown(self): self.tmp.cleanup()

    def payload(self, count=1):
        results = {}
        for n in range(count):
            store = 'S' + str(n + 1)
            value = copy.deepcopy(result([row('a'), row('b')]))
            value['store'] = store; value['store_name'] = '合成店' + store
            for d in value['rows']: d['B'] = store
            value['rows'][1]['AU'] = '山东仓'
            results[store] = value
        return dict(schema=1, day=wr.today(), model_version=MODEL_VERSION, input_dirty=False,
                    results=results, states={s:dict(state='已完成', name=r['store_name']) for s,r in results.items()})

    def prepare(self, count=1):
        payload = self.payload(count)
        return delivery.prepare_batch(self.repo, payload, list(payload['results']))

    def task(self): return export_receipts.recent(self.work.path)[0]

    def test_empty_memory_error_has_type_stack_stage_and_no_false_commit(self):
        batch = self.prepare()
        with patch.object(self.repo, 'confirm', side_effect=MemoryError()):
            with self.assertRaises(WorkFailure) as caught:
                delivery.execute_batch(self.repo, batch, self.output)
        task = self.task(); evidence = task['failure']
        self.assertEqual(evidence['exception_type'], 'MemoryError')
        self.assertEqual(evidence['commit_state'], 'not_started')
        self.assertIn('MemoryError', str(caught.exception))
        self.assertLess(str(caught.exception).index('原因：'), str(caught.exception).index('未生成：'))
        saved = json.loads(Path(evidence['diagnostic']).read_text('utf-8'))
        self.assertIn('MemoryError', saved['traceback']); self.assertEqual(saved['operation'], 'delivery_execute')
        self.assertEqual(self.repo.headers(), []); self.assertEqual(list(self.output.iterdir()), [])

    def test_empty_assertion_and_failed_diagnostic_write_retain_first_error(self):
        context = FailureContext('confirm_normal')
        context.progress('校验所选门店')
        try: raise AssertionError()
        except AssertionError as exc:
            with patch('work_failures.tempfile.mkstemp', side_effect=PermissionError('diagnostic denied')):
                evidence = context.capture(exc, self.work.path)
        self.assertEqual(evidence['exception_type'], 'AssertionError')
        self.assertIn('AssertionError', evidence['reason'])
        self.assertIn('diagnostic denied', evidence['diagnostic_error'])
        self.assertIn('raise AssertionError()', evidence['traceback'])

    def test_commit_exception_is_unknown_not_falsely_successful(self):
        from runtime_support import CommitProgress
        context = FailureContext('confirm_normal'); context.progress(CommitProgress(True))
        evidence = context.capture(OSError('commit failed'), self.work.path)
        self.assertEqual(evidence['commit_state'], 'unknown')

    def test_precommit_transaction_failure_has_exact_heads_but_cannot_recover(self):
        batch = self.prepare(2)
        with closing(self.work.connect()) as db, db:
            db.execute("CREATE TRIGGER qa_abort BEFORE INSERT ON normal_revisions WHEN NEW.oid != (SELECT oid FROM normal_orders ORDER BY rowid LIMIT 1) BEGIN SELECT RAISE(ABORT,'isolated second-store failure'); END")
        with self.assertRaises(WorkFailure) as caught:
            delivery.execute_batch(self.repo, batch, self.output)
        task = self.task()
        self.assertIn('isolated second-store failure', str(caught.exception))
        self.assertTrue(all(f['head'] for f in task['files']))
        self.assertEqual(self.repo.headers(), []); self.assertIsNone(self.work.get_job(wr.today()))
        review = export_recovery.inspect(self.work.path, task['id'])
        self.assertTrue(all(r['action']=='blocked' for r in review['items']))

    def test_postcommit_empty_error_recovers_exact_original_without_resigning(self):
        batch = self.prepare(2)
        with patch.object(self.repo, 'verify_issued', side_effect=MemoryError()):
            with self.assertRaises(WorkFailure): delivery.execute_batch(self.repo, batch, self.output)
        task = self.task(); heads = self.repo.headers()
        self.assertEqual(len(heads), 2)
        self.assertEqual(task['failure']['commit_state'], 'committed')
        self.assertEqual(list(self.output.iterdir()), [])
        review = export_recovery.inspect(self.work.path, task['id'])
        self.assertTrue(all(r['action']=='export' for r in review['items']))
        recovered = export_recovery.recover(self.work.path, review, list(range(4)), self.output)
        self.assertEqual(len(recovered['paths']), 4); self.assertEqual(self.repo.headers(), heads)
        self.assertTrue(all(h['revision']==1 for h in heads))

    def test_first_sign_reads_only_one_store_at_a_time_and_tracking_finds_all(self):
        batch = self.prepare(3)
        import warehouse_execution
        read = self.repo.get; read_child = warehouse_execution.get_order; references = []; calls = []
        class Tracked(dict): pass
        def get(repository,head,*args, **kwargs):
            gc.collect()
            self.assertTrue(all(ref() is None for ref in references))
            value = Tracked(read_child(repository,head,*args, **kwargs)); references.append(weakref.ref(value)); calls.append((head['store'],head['warehouse']))
            return value
        # Warehouse snapshots are now authority: retaining only a parent get()
        # call count would miss decoded child trees accumulating between files.
        with patch('warehouse_execution.get_order', side_effect=get):
            receipt = delivery.execute_batch(self.repo, batch, self.output)
        self.assertEqual(calls, [(s,w) for s in ('S1','S2','S3') for w in ('河北仓','山东仓')])
        self.assertEqual(len(receipt['paths']), 6); self.assertEqual(len(receipt['orders']), 3)
        self.assertTrue(all('payload' not in h for h in receipt['orders']))
        for store, value in batch['payload']['results'].items():
            self.assertEqual(read(wr.today(), store)['payload']['result'], value)
        import daily_batch
        records = daily_batch.read(self.work.path, self.root/'tracking.sqlite3', {'from':wr.today(), 'to':wr.today(), 'all_matched':True})
        self.assertEqual(records['count'], 6)
        self.assertEqual({h['store'] for h in records['records']}, {'S1','S2','S3'})

    def test_callback_failure_stops_before_any_signature_write(self):
        payload = self.payload()
        with self.assertRaisesRegex(OSError, 'cannot retain exact identities'):
            self.repo.confirm(payload, ['S1'], None, {'S1':None},
                              prepared_heads=lambda _: (_ for _ in ()).throw(OSError('cannot retain exact identities')))
        self.assertEqual(self.repo.headers(), [])

    def test_receipt_database_lock_does_not_replace_original_failure(self):
        import sqlite3
        batch = self.prepare()
        save = export_receipts.save
        def fail_updates(database, receipt):
            if receipt.get('failure'): raise sqlite3.OperationalError('isolated receipt lock')
            return save(database, receipt)
        with patch.object(self.repo, 'confirm', side_effect=MemoryError()), patch.object(export_receipts, 'save', side_effect=fail_updates):
            with self.assertRaises(WorkFailure) as caught:
                delivery.execute_batch(self.repo, batch, self.output)
        self.assertEqual(caught.exception.evidence['exception_type'], 'MemoryError')
        self.assertIn('isolated receipt lock', str(caught.exception))
        self.assertTrue(Path(caught.exception.evidence['diagnostic']).is_file())

    def test_worker_preserves_original_type_and_traceback(self):
        from ui_work_io import Session
        with Session(self.work.path) as session:
            with self.assertRaises(WorkFailure) as caught:
                session.call('confirm_normal', None, lambda _:None)
        evidence = caught.exception.evidence
        self.assertEqual(evidence['exception_type'], 'ValueError')
        self.assertEqual(evidence['operation'], 'confirm_normal')
        self.assertIn('缺少已核对的正常单', evidence['reason'])
        self.assertIn('work_io_process.py', evidence['traceback'])
        self.assertTrue(Path(evidence['diagnostic']).is_file())


if __name__ == '__main__': unittest.main()
