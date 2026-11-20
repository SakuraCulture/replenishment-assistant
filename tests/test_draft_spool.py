"""The signing handoff uses bounded local files and keeps the original failure."""
import hashlib
import tempfile
import time
import unittest
from datetime import date, datetime
from pathlib import Path

import draft_spool
import normal_delivery
import normal_orders
import storage_chunks
import work_repository
from encoding_store import Store
from runtime_support import Cancelled
from signing_memory_fixture import fixture
from work_failures import WorkFailure
from work_transfer import send_tree


def fail_expansion(connection, database, cancel):
    from work_io_process import serve

    index_tree = draft_spool.Reader.index_tree
    opens = 0

    def read(reader):
        nonlocal opens
        opens += 1
        if opens == 2:
            reader.stream.read(8)
            raise MemoryError('QA_DRAFT_EXPAND_RECEIVER')
        return index_tree(reader)

    draft_spool.Reader.index_tree = read
    serve(connection, database, cancel)


class DraftSpoolTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = work_repository.Repository(Path(directory.name) / 'settings')
        self.orders = normal_orders.Repository(self.work)

    def private_files(self):
        return list(self.work.path.parent.glob('.encoding-cache/*.tmp'))

    def test_staged_nested_values_match_canonical_snapshot_and_manifest(self):
        # Force the sender to subdivide a single large record as well as
        # receiving ordinary array and map pages.
        notes = [str(i) + '汉字' * 168 for i in range(1100)]
        original = {
            'array': [{'nested': notes}] + list(range(560)),
            'map': {f'商品{i:04d}': i for i in range(560, -1, -1)},
            'deep': {'a': {'b': {'c': {'d': (notes, None, False, 0, -0.0)}}}},
            'dates': (date(2026, 11, 19), datetime(2026, 11, 19, 11, 33)),
        }
        with draft_spool.create(self.work.path) as writer:
            send_tree(writer, original)
            writer.stream.flush()
            path = writer.folder / writer.name
            with draft_spool.open_draft(self.work.path, writer.name, None, lambda _: None) as staged:
                original_pack = storage_chunks.prepare(original, binding='source')
                with Store(self.work.path) as store:
                    staged_pack = storage_chunks.prepare(staged, binding='source', store=store)
                    self.assertEqual(staged_pack.blob(), original_pack.blob())
                    self.assertEqual(dict(staged_pack.chunks.items()), original_pack.chunks)
                self.assertEqual(list(staged['map']), list(original['map']))
                self.assertEqual(staged['array'][61:71], original['array'][61:71])
                self.assertEqual(draft_spool.native(staged), original)
        self.assertFalse(path.exists())

    def test_truncated_input_is_rejected_and_cleaned(self):
        with draft_spool.create(self.work.path) as writer:
            send_tree(writer, {'rows': list(range(300))})
            writer.stream.truncate(writer.stream.tell() - 1)
            writer.stream.flush()
            with self.assertRaisesRegex(ValueError, '临时文件不完整'):
                with draft_spool.open_draft(self.work.path, writer.name, None, lambda _: None):
                    pass
        self.assertFalse(self.private_files())

    def test_real_child_reports_expansion_error_without_transport_timeout(self):
        from ui_work_io import Session

        payload, codes = fixture(320, 2, 13)
        chosen = codes[:1]
        stages = []
        with Session(self.work.path, worker=fail_expansion, stall_seconds=4) as session:
            preview = session.call('delivery_probe', [normal_delivery.probe_payload(payload, chosen), chosen], stages.append)
            self.assertFalse(preview['plans'][0]['same'])
            tail = {**payload, 'results': {key: value for key, value in payload['results'].items() if key not in chosen}}
            started = time.monotonic()
            with self.assertRaises(WorkFailure) as failure:
                session.call('delivery_expand', [tail, list(payload['results'])], stages.append)
            self.assertLess(time.monotonic() - started, 4)
            info = failure.exception.evidence
            self.assertEqual(info['exception_type'], 'MemoryError')
            self.assertEqual(info['operation'], 'delivery_expand')
            self.assertEqual(info['commit_state'], 'not_started')
            self.assertEqual(info['memory']['pid'], session.process.pid)
            self.assertIn('QA_DRAFT_EXPAND_RECEIVER', info['traceback'])
            self.assertIn('读取签发草稿', info['stage'])
            with self.assertRaisesRegex(WorkFailure, '缺少已核对的导出批次'):
                session.call('delivery_execute', str(self.work.path.parent), stages.append)
        self.assertFalse(self.private_files())
        self.assertFalse(self.orders.headers())
        self.assertIsNone(self.work.get_job(payload['day']))

    def test_cancellation_while_staging_leaves_no_files_or_business_rows(self):
        from ui_work_io import Session

        payload, codes = fixture(320, 2, 13)
        with Session(self.work.path) as session:
            def cancel_at_write(message):
                if '已写' in str(message):
                    session.cancel.set()

            with self.assertRaises(Cancelled):
                session.call('prepare_normal', [payload, codes], cancel_at_write)
        self.assertFalse(self.private_files())
        self.assertFalse(self.orders.headers())
        self.assertIsNone(self.work.get_job(payload['day']))

    def test_complete_unselected_sources_saved_without_reordering_export(self):
        from ui_work_io import Session

        payload, codes = fixture(320, 3, 270)
        chosen = [codes[2], codes[0]]
        out = self.work.path.parent / 'exports'
        out.mkdir()
        original = work_repository.dumps(payload)
        with Session(self.work.path) as session:
            prepare = lambda command, args: session.call(command, args, lambda _: None)
            plans, signed = normal_delivery.prepare_export(payload, chosen, None, prepare, lambda: True)
            self.assertTrue(signed)
            receipt = prepare('delivery_execute', str(out))
        self.assertEqual(work_repository.dumps(payload), original)
        self.assertEqual([order['store'] for order in receipt['orders']], chosen)
        self.assertEqual([Path(file).name for file in receipt['paths']],
                         [file['filename'] for plan in plans['plans'] for file in plan['files']])
        expected = {**payload, 'execution_warehouses': {
            plan['store']: [f['warehouse'] for f in plan['files']] for plan in plans['plans']}}
        saved = self.work.get_job(payload['day'])
        self.assertEqual(saved['digest'], hashlib.sha256(work_repository.dumps(expected).encode()).hexdigest())
        self.assertEqual(work_repository.dumps(saved['payload']), work_repository.dumps(expected))
        self.assertFalse(self.private_files())
