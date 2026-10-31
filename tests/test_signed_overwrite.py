"""Same-name signed exports replace only after a complete successful write."""
import copy
import sqlite3
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import closing

from test_archive_orders import seed
import normal_exports as exports
import normal_delivery as delivery
import lifecycle_batch
import purchases
import work_repository as wr
from runtime_support import Cancelled
import openpyxl


def snapshot(path):
    book = openpyxl.load_workbook(path)
    try:
        return [(ws.title, [(cell.coordinate, cell.value, cell.data_type,
                            str(cell._style), cell.comment.text if cell.comment else None)
                           for row in ws for cell in row]) for ws in book]
    finally:
        book.close()


class SignedOverwriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.work, self.repo, _ = seed(self.root)
        self.order = self.repo.get(wr.today(), 'T')
        self.before = copy.deepcopy(self.order)
        self.target = self.root / 'signed.xlsx'
        self.target.write_bytes(b'previous complete export')
        self.old = self.target.read_bytes()

    def tearDown(self):
        self.assertEqual(self.order, self.before)
        self.assertEqual(list(self.root.glob('.normal-*')), [])
        self.tmp.cleanup()

    def write(self, **kwargs):
        return exports.export(self.order, self.target, warehouse='河北仓', **kwargs)

    def test_overwrite_has_exactly_same_cells_types_styles_and_order_as_new_file(self):
        fresh = self.root / 'fresh.xlsx'
        exports.export(self.order, fresh, warehouse='河北仓')
        self.write()
        self.assertEqual(snapshot(self.target), snapshot(fresh))
        self.write()
        self.assertEqual(snapshot(self.target), snapshot(fresh))

    def test_cancel_during_rows_keeps_previous_file(self):
        event = threading.Event()
        with self.assertRaises(Cancelled):
            self.write(cancel=event, progress=lambda _: event.set())
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_cancel_after_compression_before_replace_keeps_previous_file(self):
        event = threading.Event()
        save = exports.Workbook.save
        def finish_then_cancel(book, path):
            save(book, path)
            event.set()
        with patch.object(exports.Workbook, 'save', finish_then_cancel):
            with self.assertRaises(Cancelled): self.write(cancel=event)
        self.assertEqual(self.target.read_bytes(), self.old)
        event.clear()
        self.write(cancel=event)

    def test_partial_save_failure_keeps_previous_file(self):
        def broken_save(book, path):
            Path(path).write_bytes(b'incomplete zip')
            raise OSError('injected disk failure')
        with patch.object(exports.Workbook, 'save', broken_save):
            with self.assertRaisesRegex(OSError, 'disk failure'): self.write()
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_replace_failure_is_actionable_and_retry_succeeds(self):
        with patch.object(exports.os, 'replace', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(PermissionError, '关闭.*Excel/WPS'): self.write()
        self.assertEqual(self.target.read_bytes(), self.old)
        self.write()

    @unittest.skipUnless(os.name == 'nt', 'Windows sharing semantics')
    def test_real_windows_open_file_lock_retains_old_then_retry(self):
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.target), 0x80000000, 0, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
        try:
            with self.assertRaisesRegex(PermissionError, '关闭.*Excel/WPS'): self.write()
        finally:
            kernel.CloseHandle(handle)
        self.assertEqual(self.target.read_bytes(), self.old)
        self.write()

    def test_batch_and_history_reexport_preserve_order_and_archive(self):
        folder = self.root / 'exports'
        folder.mkdir()
        whs = ['河北仓', '山东仓']
        files = [{'warehouse': w, 'filename': exports.filename(self.order, w)} for w in whs]
        paths = [folder / f['filename'] for f in files]
        for p in paths: p.write_bytes(b'previous')
        batch = {'plans': [{'same': True, 'store': 'T', 'old': self.order, 'files': files}],
                 'payload': {'day': wr.today()}, 'head': None}
        receipt = delivery.execute_batch(self.repo, batch, folder)
        self.assertEqual(receipt['paths'], [str(p) for p in paths])
        self.assertFalse(receipt['signed'])
        values = [snapshot(p) for p in paths]
        self.assertEqual(delivery.export_signed(self.repo, self.order, folder, whs), [str(p) for p in paths])
        heads = purchases.targets(self.work.path)
        self.assertEqual(lifecycle_batch.export_signed(self.work.path, heads, folder), [str(p) for p in paths])
        self.assertEqual([snapshot(p) for p in paths], values)
        self.assertEqual(self.repo.get(wr.today(), 'T'), self.before)
        with closing(sqlite3.connect(self.work.path)) as db:
            logs = db.execute('SELECT oid,revision,action,detail FROM normal_events ORDER BY id').fetchall()
        self.assertEqual(len(logs), 6)
        self.assertEqual([wr.loads(r[3])['warehouse'] for r in logs], whs * 3)
        self.assertTrue(all(r[:3] == ('oid', 1, '带号导出') for r in logs))

    def test_intra_batch_name_collision_still_rejected_before_replacement(self):
        files = [{'warehouse': w, 'filename': self.target.name} for w in ['河北仓', '山东仓']]
        batch = {'plans': [{'same': True, 'store': 'T', 'old': self.order, 'files': files}],
                 'payload': {'day': wr.today()}, 'head': None}
        with self.assertRaisesRegex(ValueError, '文件名重复'):
            delivery.execute_batch(self.repo, batch, self.root)
        self.assertEqual(self.target.read_bytes(), self.old)

    def test_tracking_repeated_target_does_not_replace_first_output_again(self):
        head = purchases.targets(self.work.path)[0]
        with self.assertRaisesRegex(RuntimeError, '本批次门店文件名重复'):
            lifecycle_batch.export_signed(self.work.path, [head, head], self.root)
        with closing(sqlite3.connect(self.work.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM normal_events').fetchone()[0], 1)

    def test_second_file_cancel_keeps_first_completed_and_second_previous(self):
        folder = self.root / 'partial'
        folder.mkdir()
        paths = [folder / exports.filename(self.order, w) for w in ['河北仓', '山东仓']]
        for p in paths: p.write_bytes(self.old)
        event = threading.Event()
        def progress(text):
            if '文件 2/2' in str(text): event.set()
        with self.assertRaisesRegex(RuntimeError, '未生成'):
            delivery.export_signed(self.repo, self.order, folder, ['河北仓', '山东仓'], progress, event)
        self.assertNotEqual(paths[0].read_bytes(), self.old)
        self.assertEqual(paths[1].read_bytes(), self.old)
        event.clear()
        delivery.export_signed(self.repo, self.order, folder, ['河北仓', '山东仓'], cancel=event)


if __name__ == '__main__': unittest.main()
