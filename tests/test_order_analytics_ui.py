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
    def test_updated_data_does_not_replace_visible_snapshot(self):
        w=self.widget;w.show_report(self.f.report());snapshot=w.report;self.host.show();w.show();self.app.processEvents();self.assertTrue(w.isVisible());self.f.f.save('return');w.check_update()
        deadline=time.monotonic()+10
        while w.basis_worker and time.monotonic()<deadline:self.app.processEvents();time.sleep(.005)
        self.assertIsNone(w.basis_worker);self.assertIs(w.report,snapshot);self.assertTrue(w.stale);self.assertFalse(w.export_btn.isEnabled())
    def test_identifiers_appear_in_all_summary_views_and_coverage_view(self):
        from test_analytics_identifiers import seed_purchase
        import order_lifecycle as lc
        import payment_references as refs
        self.f.f.save('return');self.f.f.save('payment');head=next(h for h in self.f.report()['heads'] if h['number']==self.f.f.h['number'])
        refs.save(self.f.path,refs.read(self.f.path,head),'000JST1')
        seed_purchase(self.f.path,head,lc.snapshot(self.f.path,head)['documents']['payment'])
        w=self.widget;w.show_report(self.f.report())
        for index,key in enumerate(('store','warehouse','order')):
            w.tabs.setCurrentIndex(index)
            self.assertEqual(w.models[key].headers[2:5],['签发单号','聚水潭内部订单号','翱象采购单号'])
            self.assertTrue(any('000JST1' in str(r[3]) and 'PO00001' in str(r[4]) for r in w.models[key].rows))
        w.metric_choice.setCurrentIndex(3)
        self.assertEqual(w.models['order'].headers[2:5],['签发单号','聚水潭内部订单号','翱象采购单号'])
        self.assertTrue(any('PO00001' in str(r[4]) for r in w.models['order'].rows))
    def test_number_maintenance_marks_existing_report_stale(self):
        import payment_references as refs
        self.f.f.save('return');self.f.f.save('payment');w=self.widget;w.show_report(self.f.report());snapshot=w.report
        head=next(h for h in snapshot['heads'] if h['number']==self.f.f.h['number'])
        refs.save(self.f.path,refs.read(self.f.path,head),'NEW-JST')
        self.host.show();w.show();self.app.processEvents();w.check_update()
        deadline=time.monotonic()+10
        while w.basis_worker and time.monotonic()<deadline:self.app.processEvents();time.sleep(.005)
        self.assertIs(w.report,snapshot);self.assertTrue(w.stale);self.assertFalse(w.export_btn.isEnabled())

if __name__=='__main__':unittest.main()
