import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import order_migration_progress as progress
from stockout_common import atomic_json


class MigrationProgress(unittest.TestCase):
    def test_copy_progress_throttles_updates_without_deferring_terminal_state(self):
        from export_progress import RowProgress
        from migration_status import reporter
        with patch('migration_status.record') as record,patch('migration_status.time.monotonic',side_effect=[0,.2,1.01]):
            report=reporter({'nonce':'a'*32},'COPYING')
            for index in range(3):report(RowProgress('复制数据库',index,10))
        self.assertEqual([call.kwargs['progress']['current'] for call in record.call_args_list],[0,2])
        self.assertEqual(record.call_args.kwargs['progress']['total'],10)
    def test_writer_exit_distinct_from_a_failure_receipt(self):
        receipt = {'state': 'APPLYING', 'phase': 'REPLACING'}
        active = progress.presentation(receipt, 'RUNNING')
        self.assertEqual(active[:2], ('订单迁入正在进行', '当前阶段 切换正式订单库'))
        self.assertFalse(active[3])
        interrupted = progress.presentation(receipt, 'EXITED')
        self.assertEqual(interrupted[:2], ('迁入进程提前退出，结果待核对', '最后记录阶段 切换正式订单库'))
        self.assertTrue(interrupted[3])
        self.assertIn('请勿重复导入', interrupted[2])

    def test_terminal_receipts_keep_original_failure_and_actual_counts(self):
        failed = progress.presentation({
            'state': 'FAILED', 'phase': 'COPYING',
            'error': {'type': 'OSError', 'message': 'ORIGINAL-COPY-ERROR'},
        }, 'EXITED')
        self.assertEqual(failed, ('订单迁入失败', '失败阶段 复制本机资料', 'OSError\nORIGINAL-COPY-ERROR', True))
        completed = progress.presentation({
            'state': 'COMPLETED', 'phase': 'CLEANUP_PENDING',
            'result': {'mode': 'merge', 'counts': {'added': 3, 'duplicate': 1, 'conflict': 2, 'deleted': 0}},
        }, 'EXITED')
        self.assertEqual(completed[:2], ('订单迁入完成', '已写入，临时资料待清理'))
        self.assertIn('新增 3 单 · 重复 1 单 · 冲突保留本机 2 单', completed[2])
        self.assertTrue(completed[3])

    def test_viewer_uses_its_receipt_even_if_a_newer_operation_exists(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary) / 'settings'
            first, second = 'a' * 32, 'b' * 32
            same = {'nonce': first, 'root': str(root), 'state': 'FAILED'}
            file = progress.status_path(root, first)
            atomic_json(file, same)
            atomic_json(file.parent / 'latest.json', {'nonce': second})
            atomic_json(progress.status_path(root, second), {'nonce': second, 'root': str(root), 'state': 'COMPLETED'})
            self.assertEqual(progress.read_status(root, first), same)
            atomic_json(file, {**same, 'nonce': second})
            with self.assertRaisesRegex(ValueError, '与本次作业不一致'):
                progress.read_status(root, first)

    def test_failed_viewer_launch_does_not_authorize_shutdown(self):
        from order_migration_ui import _handoff
        dialog, host, plan = Mock(), Mock(), {}
        with patch('order_migration_ui.run', side_effect=[None, RuntimeError('窗口未就绪')]), \
                patch('migration_status.waiting') as waiting:
            with self.assertRaisesRegex(RuntimeError, '窗口未就绪'):
                _handoff(dialog, host, plan, '退出中')
        waiting.assert_not_called()
        dialog.accept.assert_not_called()
        host.close.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows process object')
    def test_parent_watcher_observes_the_original_process_ending(self):
        from application_processes import watch_parent
        from subprocess import CREATE_NO_WINDOW
        with subprocess.Popen([sys.executable, '-c', 'input()'], stdin=subprocess.PIPE,
                              creationflags=CREATE_NO_WINDOW) as writer:
            with watch_parent(writer.pid) as state:
                self.assertEqual(state(), 'RUNNING')
                writer.communicate(b'quit\n', timeout=10)
                self.assertEqual(state(), 'EXITED')

    @unittest.skipUnless(os.name == 'nt', 'Windows process inventory')
    def test_only_readonly_observer_is_ignored_by_the_writer_guard(self):
        from application_processes import discover
        observer = {'pid': 65432, 'parent': 65431, 'name': '补货助手.exe',
                    'argv': ['补货助手.exe', '--order-migration-progress']}
        worker = {**observer, 'pid': 65433, 'argv': ['补货助手.exe', '--catalog-validate'],
                  'created': None}
        with patch('application_processes.records', return_value=[observer, worker]), \
                patch('application_processes.main_windows', return_value={}), \
                patch('application_processes.kernel_state', return_value={'state': 'RUNNING'}):
            self.assertEqual([row['pid'] for row in discover(guarded=True)], [worker['pid']])
