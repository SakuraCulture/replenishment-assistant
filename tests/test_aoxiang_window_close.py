"""An accepted host close must stop old and new background timer consumers."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))


class WindowCloseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from PySide6.QtWidgets import QApplication
        cls.app = QApplication.instance() or QApplication([])

    def test_accepted_close_blocks_queued_old_generator_and_new_queue_ticks(self):
        import shiboken6
        from application_window import ApplicationWindow
        from PySide6.QtCore import QThread
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, REPLENISHMENT_DATA_DIR=folder):
            with patch('history_tasks.HistoryReader.request'):
                host = ApplicationWindow()
            try:
                host.job_dirty = False
                host.close()
                # Explicitly simulate a callback already queued before timer.stop.
                host.jst_generator.tick()
                host.aoxiang_workflows.tick()
                self.assertTrue(host.closing)
                self.assertFalse(host.jst_generator.timer.isActive())
                self.assertFalse(host.aoxiang_workflows.timer.isActive())
                self.assertIsNone(host.jst_generator.worker)
                self.assertIsNone(host.aoxiang_workflows.worker)
                self.assertFalse(any(t.isRunning() for t in host.findChildren(QThread)))
            finally:
                host.closing = True
                host.jst_generator.timer.stop()
                if host.jst_generator.worker:
                    host.jst_generator.cancel()
                    host.jst_generator.worker.wait()
                host.aoxiang_workflows.shutdown()
                shiboken6.delete(host)

    def test_declined_close_keeps_normal_workflow_available(self):
        import shiboken6
        from application_window import ApplicationWindow
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, REPLENISHMENT_DATA_DIR=folder):
            with patch('history_tasks.HistoryReader.request'):
                host = ApplicationWindow()
            try:
                with patch.object(host, 'leave_current', return_value=False):
                    host.close()
                self.assertFalse(host.closing)
                self.assertTrue(host.jst_generator.timer.isActive())
                self.assertFalse(host.aoxiang_workflows.stopping)
            finally:
                host.job_dirty = False
                host.close()
                shiboken6.delete(host)


if __name__ == '__main__':
    unittest.main()
