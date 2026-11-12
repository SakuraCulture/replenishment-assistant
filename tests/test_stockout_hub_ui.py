"""Real Qt navigation and spawned workers on isolated empty settings."""
import os,sys,tempfile,time,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication,QMainWindow,QWidget,QVBoxLayout,QHBoxLayout,QStackedWidget
from PySide6.QtTest import QTest


class HubUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_one_navigation_lazy_subpages_and_safe_switch(self):
        from stockout_hub import install
        with tempfile.TemporaryDirectory() as temp:
            host=QMainWindow();host.repository=SimpleNamespace(folder=Path(temp));host.worker=None;host.closing=False
            center=QWidget();host.setCentralWidget(center);v=QVBoxLayout(center);nav=QHBoxLayout();v.addLayout(nav)
            host.sections=QStackedWidget();host.sections.addWidget(QWidget());v.addWidget(host.sections);host.show_section=host.sections.setCurrentIndex
            install(host);hub=host.stockout_hub
            self.assertEqual(nav.count(),1);self.assertEqual(hub.nav.text(),'缺货率');self.assertIsNone(hub.page)
            self.assertFalse((Path(temp)/'analysis').exists())
            host.warehouse_stockout.enter()
            def wait():
                end=time.monotonic()+25
                while any(e.worker or e.queued for e in [hub,*hub.children.values()]) and time.monotonic()<end:QTest.qWait(10)
                self.assertFalse(any(e.worker or e.queued for e in [hub,*hub.children.values()]),hub.message)
            wait();self.assertIsNotNone(host.warehouse_stockout.page);self.assertIsNone(host.stockout.page)
            import threading
            event=threading.Event();host.warehouse_stockout.worker=SimpleNamespace(cancel=event,cancel_reason='')
            hub.page.controls();self.assertTrue(hub.page.stop.isEnabled());hub.page.stop.click();self.assertTrue(event.is_set())
            host.warehouse_stockout.worker=None;hub.page.controls()
            self.assertFalse((Path(temp)/'analysis/stockout/stockout.sqlite3').exists())
            hub.page.tabs.setCurrentIndex(0);wait();self.assertIsNotNone(host.stockout.page);self.assertIsNone(host.warehouse_stockout.page)
            host.sections.setCurrentIndex(0);QTest.qWait(30);wait();self.assertIsNone(hub.page);self.assertTrue(hub.prepare_close())
            host.deleteLater();self.app.processEvents()


if __name__=='__main__':unittest.main()
