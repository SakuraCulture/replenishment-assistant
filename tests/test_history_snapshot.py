"""Cold signed-list projections keep the old proofs and archive details."""
import copy
import hashlib
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from test_archive_orders import seed

import archive_orders
import history_read
import lifecycle_home
import new_product_focus
import normal_orders
import result_semantics
import storage_chunks
import warehouse_execution
import work_repository


class ColdHistorySnapshots(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        work, self.repo, self.payload = seed(self.root)
        self.path = work.path
        self.cancel = threading.Event()
        self.payload['audit_only'] = '外层证据，仅测试完整性'
        self.payload['new_focus'] = new_product_focus.empty(work_repository.today()[:7])
        base, sd = self.payload['result']['rows']
        rows = []
        for index in range(storage_chunks.BLOCK_ROWS + 7):
            row = copy.deepcopy(base)
            row.update(E=f'QA-{index:04}', D=f'合成商品{index}', evidence=[date(2026, 1, 1), datetime(2026, 1, 1, 8)])
            rows.append(row)
        rows.append(sd)
        self.payload['result'].update(rows=rows, summary=result_semantics.summarize(rows))
        self.save_parent()

    def save_parent(self):
        result, focus = self.payload['result'], self.payload['new_focus']
        digest = normal_orders.fingerprint_owned(result, focus)
        with closing(self.repo.work.connect()) as db, db:
            blob = normal_orders.encode(self.payload, db)
            db.execute('UPDATE normal_revisions SET digest=?,payload=?', (digest, blob))

    def manage(self):
        with closing(self.repo.work.connect()) as db, db:
            warehouse_execution.ensure_parent(db, 'oid')
            return warehouse_execution.heads(db)[0]

    def archive(self, name, **filters):
        return history_read.execute(
            self.path, self.root / (name + '.sqlite3'),
            {'kind': 'list', **filters}, self.cancel, lambda _: None,
        )

    def lifecycle(self, name):
        return lifecycle_home.read(self.path, self.root / (name + '.sqlite3'), {'all_records': True}, self.cancel)

    def test_legacy_and_pinned_history_create_detail_only_on_open(self):
        for managed in (False, True):
            with self.subTest(managed=managed):
                if managed:
                    self.manage()
                before = hashlib.sha256(self.path.read_bytes()).digest()
                cache = self.root / f'archive-{managed}.sqlite3'
                with patch.object(history_read, 'project', side_effect=AssertionError('List restored display rows')):
                    archive = self.archive(cache.stem, search='测试店 QA-0256')
                    self.assertEqual(archive['matches'], [True, False])
                    home = self.lifecycle(f'flow-{managed}')
                self.assertEqual([item['warehouse'] for item in home['records']], ['河北仓', '山东仓'])
                self.assertTrue(all(item['status'] == '待回传' for item in home['records']))
                head = archive['records'][0]
                issued = warehouse_execution.get_order(self.repo, archive['heads'][0], view='export')
                expected = [row['E'] for row in issued['payload']['result']['rows'] if archive_orders.warehouse(row) == '河北仓']
                self.assertEqual(head['summary']['rows'], len(expected))
                with closing(sqlite3.connect(cache)) as db:
                    self.assertEqual({row[0] for row in db.execute('SELECT projection FROM views')}, {'null'})
                request = {'kind': 'detail', 'head': head}
                detail = history_read.execute(self.path, cache, request, self.cancel, lambda _: None)
                self.assertEqual([row[0] for row in detail['rows']], expected)
                self.assertEqual(detail, history_read.execute(self.path, cache, request, self.cancel, lambda _: None))
                self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_rejected_block_outside_list_fields_is_never_cached(self):
        head = self.manage()
        with closing(self.repo.work.connect()) as db, db:
            _, _, _, blob = warehouse_execution.active_snapshot_source(db, head)
            node = dict(storage_chunks.require_manifest(blob)['tree'][1])['audit_only']
            db.execute('UPDATE snapshot_chunks SET size=size+1 WHERE hash=?', (node[1],))
        self.assert_cold_rejected('快照内容块校验失败')

    def test_intact_blocks_still_need_the_full_and_business_digests(self):
        head = self.manage()
        snapshot = warehouse_execution.get_order(self.repo, head)['payload']
        snapshot['result']['sources']['test'] = '另一份合法编码但不属于该签发的资料'
        with closing(self.repo.work.connect()) as db, db:
            prepared = storage_chunks.prepare(snapshot, binding=head['snapshot_digest'])
            blob = storage_chunks.put(db, prepared)
            db.execute('UPDATE warehouse_execution_revisions SET payload=? WHERE warehouse=?', (blob, '河北仓'))
        self.assert_cold_rejected('仓单业务快照校验失败', suffix='business')
        with closing(self.repo.work.connect()) as db, db:
            prepared.digest = '0' * 64
            db.execute('UPDATE warehouse_execution_revisions SET payload=? WHERE warehouse=?', (prepared.blob(), '河北仓'))
        self.assert_cold_rejected('完整快照校验失败', suffix='full')

    def assert_cold_rejected(self, reason, suffix='block'):
        before = hashlib.sha256(self.path.read_bytes()).digest()
        with self.assertRaisesRegex(ValueError, reason):
            self.archive('bad-archive-' + suffix)
        with self.assertRaisesRegex(ValueError, reason):
            self.lifecycle('bad-home-' + suffix)
        with closing(sqlite3.connect(self.root / ('bad-archive-' + suffix + '.sqlite3'))) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM views').fetchone()[0], 0)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).digest())

    def test_old_map_layout_uses_existing_verified_restore(self):
        self.payload['result'].update({f'qa_audit_{i:03d}': i for i in range(storage_chunks.BLOCK_ROWS)})
        self.save_parent()
        legacy = self.archive('legacy-map')['records']
        self.assertEqual([r['warehouse'] for r in legacy], ['河北仓', '山东仓'])
        self.manage()
        with patch.object(warehouse_execution, 'restore_snapshot', wraps=warehouse_execution.restore_snapshot) as restore:
            history = self.archive('managed-map')['records']
            self.assertEqual(restore.call_count, 2)
        self.assertEqual([r['summary'] for r in history], [r['summary'] for r in legacy])
