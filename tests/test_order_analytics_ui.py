"""Qt projection and lifecycle controls against an isolated fixture database."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import sys,time,unittest
from pathlib import Path
from types import SimpleNamespace
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).parent)]
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QEventLoop
import test_order_analytics as fixtures
from order_analytics_ui import AnalyticsWidget


class AnalyticsUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.f=fixtures.AnalyticsTests();self.f.setUp();self.host=QWidget();self.host.closing=False
        self.host.work_repository=SimpleNamespace(path=self.f.path);self.host.repository=SimpleNamespace(folder=self.f.root)
        self.widget=AnalyticsWidget(self.host)
    def tearDown(self):
        if self.widget.worker:self.widget.cancel();self.wait()
        self.widget.deleteLater();self.host.deleteLater();self.app.processEvents();self.f.tearDown()
    def wait(self):
        deadline=time.monotonic()+15
        while self.widget.worker and time.monotonic()<deadline:
            self.app.processEvents(QEventLoop.ProcessEventsFlag.AllEvents,20);time.sleep(.005)
        self.assertIsNone(self.widget.worker)
    def test_query_drill_back_and_scope(self):
        w=self.widget;w.query();self.wait();self.assertEqual(len(w.report['rows']),2)
        self.assertEqual(w.models['store'].rowCount(),1);w.open_detail('store',0)
        self.assertEqual(w.tabs.currentIndex(),1);self.assertEqual(w.models['warehouse'].rowCount(),2)
        w.open_detail('warehouse',0);self.assertEqual(w.models['order'].rowCount(),1)
        w.open_detail('order',0);self.assertEqual(w.models['detail'].rowCount(),1)
        w.back();self.assertEqual(w.models['store'].rowCount(),1)
        w.search.setText('changed');self.assertIn('尚未查询',w.applied.text());self.assertEqual(len(w.report['rows']),2)
    def test_cancel_close_waits_for_thread(self):
        w=self.widget;w.query();self.assertFalse(w.prepare_close());self.wait();self.assertTrue(w.prepare_close())
    def test_detail_is_readonly(self):
        w=self.widget;w.show_report(self.f.report());w.tabs.setCurrentIndex(3)
        self.assertFalse(w.models['detail'].flags(w.models['detail'].index(0,0)) & __import__('PySide6.QtCore',fromlist=['Qt']).Qt.ItemFlag.ItemIsEditable)

if __name__=='__main__':unittest.main()
