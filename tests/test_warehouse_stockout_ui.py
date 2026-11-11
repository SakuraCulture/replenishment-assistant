"""Real Qt widgets and worker process against isolated synthetic analysis data."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import json,sys,subprocess,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QEventLoop,QTimer
import test_warehouse_stockout as fixtures
from warehouse_stockout_worker import execute
import warehouse_stockout as service

class WarehouseUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def test_page_and_custom_dimensions_and_filters(self):
        from warehouse_stockout_ui import Page,Combinations,Filters
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);repo=service.Repository(root);repo.initialize();source=fixtures.workbook(root/'source.xlsx')
            execute(repo,'ingest',{'path':str(source),'warehouse':'山东仓'},threading.Event(),lambda _:None,lambda *_:None)
            host=QWidget();host.repository=SimpleNamespace(folder=root)
            class Entry:
                worker=None;background=False
                def start(self,op,args,callback=None,**kw):
                    result=execute(repo,op,args,threading.Event(),lambda _:None,lambda *_:None)
                    if callback:callback(result)
                def pause(self):pass
            page=Page(host,Entry());page.refresh_metadata();self.assertIn('40.00%',page.total.text());self.assertEqual(page.tables[0].rowCount(),1)
            page.tabs.setCurrentIndex(2);self.assertEqual(page.tables[2].rowCount(),8);page.mode.setCurrentIndex(2);self.assertEqual(page.tables[2].rowCount(),3)
            dialog=Combinations(page,page.views);dialog.name.setText('自定义上游与分类');dialog.accept();self.assertEqual(dialog.config['combinations'][0]['name'],'自定义上游与分类')
            filter_dialog=Filters(page);self.assertEqual(filter_dialog.key,'warehouse');self.assertEqual(filter_dialog.list.count(),1);filter_dialog.set_checks(True);filter_dialog.accept();self.assertEqual(filter_dialog.filters['warehouse'],['山东仓'])
            page.deleteLater();host.deleteLater();self.app.processEvents()
    def test_real_child_worker_imports_no_qt_and_releases(self):
        from stockout_task import Task
        from warehouse_stockout_worker import serve
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);host=QWidget();host.repository=SimpleNamespace(folder=root);source=fixtures.workbook(root/'source.xlsx');results=[];errors=[];loop=QEventLoop()
            task=Task(host,'ingest',{'path':str(source),'warehouse':'山东仓'},worker_target=serve);task.succeeded.connect(results.append);task.failed.connect(errors.append);task.finished.connect(loop.quit)
            timer=QTimer();timer.setSingleShot(True);timer.timeout.connect(lambda:(task.cancel.set(),loop.quit()));timer.start(30000);task.start();loop.exec();task.wait(10000);timer.stop()
            self.assertFalse(task.isRunning());self.assertFalse(errors,errors);self.assertEqual(len(results),1);self.assertEqual(service.query(service.Repository(root),{'start':fixtures.today(),'end':fixtures.today()})['rate'],.4)
            self.assertFalse(task.diagnostic['qt_loaded'])
            task.deleteLater();host.deleteLater();self.app.processEvents()
    def test_entry_import_has_no_analysis_io_or_backend(self):
        with tempfile.TemporaryDirectory() as temp:
            code="import sys,json,pathlib; import warehouse_stockout_entry; print(json.dumps({'heavy':'warehouse_stockout' in sys.modules,'files':list(pathlib.Path(sys.argv[1]).iterdir())}))"
            result=subprocess.run([sys.executable,'-c',code,temp],capture_output=True,text=True,timeout=10,check=True)
            self.assertEqual(json.loads(result.stdout),{'heavy':False,'files':[]})


if __name__=='__main__':unittest.main()
