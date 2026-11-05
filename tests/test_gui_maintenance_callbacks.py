"""Reproduce actual modal-layout rebuild and SQLite contention, without user data."""
import gc,os,sys,sqlite3,tempfile,time,unittest,weakref
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtCore import QObject,QTimer,QCoreApplication,QEvent
from PySide6.QtWidgets import QApplication,QMessageBox,QLabel,QDialogButtonBox
from PySide6.QtTest import QTest
import shiboken6
import readable_ui,scheduled_ui,scheduled_replenishment as sr,data_maintenance_ui as maintenance

class Callbacks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.errors=[];p=patch.object(sys,'excepthook',lambda t,v,tb:self.errors.append(str(v)));p.start();self.addCleanup(p.stop)
    def wait(self,predicate,seconds=5):
        end=time.monotonic()+seconds
        while not predicate() and time.monotonic()<end:QTest.qWait(10)
        self.assertTrue(predicate())
    def box(self):
        b=QMessageBox();b.setText('确认签发');self.addCleanup(lambda:shiboken6.delete(b) if shiboken6.isValid(b) else None)
        readable_ui.review_table(b,['商品','原量','新量'],[['0001',5,0],['0002',0,6]])
        return b
    def assert_layout(self,b):
        layout=b.layout();self.assertGreaterEqual(layout.indexOf(b.business_table),0)
        self.assertGreaterEqual(layout.indexOf(b.business_search),0)
        buttons=b.findChild(QDialogButtonBox)
        self.assertGreater(layout.getItemPosition(layout.indexOf(buttons))[0],layout.getItemPosition(layout.indexOf(b.business_table))[0])
        self.assertEqual(self.errors,[])
    def test_signature_sets_information_after_table_without_deleted_layout(self):
        b=self.box();b.setInformativeText('先加差异表，再设置签发说明')
        cancel=b.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=b.addButton('确认签发',QMessageBox.ButtonRole.AcceptRole)
        b.setDefaultButton(cancel);b.setEscapeButton(cancel)
        QTimer.singleShot(50,cancel.click);b.exec()
        self.assertIs(b.clickedButton(),cancel);self.assertIs(b.defaultButton(),cancel);self.assertIs(b.escapeButton(),cancel)
        self.assert_layout(b);self.assertEqual(b.business_model.rows,[['0001',5,0],['0002',0,6]])
    def test_diff_export_reopen_keeps_table_and_explicit_confirm_semantics(self):
        b=self.box();export=b.addButton('导出完整差异',QMessageBox.ButtonRole.ActionRole);cancel=b.addButton('取消',QMessageBox.ButtonRole.RejectRole);accept=b.addButton('确认签发',QMessageBox.ButtonRole.AcceptRole);b.setDefaultButton(cancel)
        for choice in (export,cancel,accept):
            b.setInformativeText('再次核对 '+choice.text());QTimer.singleShot(50,choice.click);b.exec()
            self.assertIs(b.clickedButton(),choice);self.assert_layout(b)
        b.business_search.setText('0002');self.assertEqual(b.business_model.indices,[1])
    def test_visible_text_change_and_deleted_box_never_reuses_cxx_layout(self):
        b=self.box();b.addButton('取消',QMessageBox.ButtonRole.RejectRole);b.show();QTest.qWait(30)
        b.setInformativeText('界面显示后说明变化');QTest.qWait(30);self.assert_layout(b)
        b.business_layout.schedule();shiboken6.delete(b);QTest.qWait(30);self.assertEqual(self.errors,[])
    def test_layout_observer_does_not_keep_private_layout_or_dialog_cycle(self):
        b=QMessageBox();readable_ui.review_table(b,['商品'],[['A']]);b.setInformativeText('核对');b.show();QTest.qWait(30)
        observer=b.business_layout
        self.assertNotIn('box',observer.__dict__);self.assertNotIn('installed',observer.__dict__)
        ref=weakref.ref(b);b.close();del b;gc.collect();QTest.qWait(30)
        self.assertIsNone(ref());self.assertFalse(shiboken6.isValid(observer));self.assertEqual(self.errors,[])
    def manager(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);path=Path(tmp.name)/'work.sqlite3'
        sqlite3.connect(path).close();repo=sr.Repository(path);repo.ensure()
        m=scheduled_ui.Manager.__new__(scheduled_ui.Manager);QObject.__init__(m)
        m.host=SimpleNamespace(_large_io_dialog=None,aoxiang_workflows=SimpleNamespace(maintenance_depth=0))
        m.repo=repo;m.reader=None;m.worker=None;m.publisher=None;m.owner=False;m.closing=False;m.status=QLabel();m.recent=[];m.schedules=[]
        def cleanup():
            if m.reader:m.reader.wait();self.app.processEvents()
            shiboken6.delete(m.status);shiboken6.delete(m)
        self.addCleanup(cleanup);return m,path
    def test_maintenance_poll_performs_no_database_access(self):
        m,path=self.manager();c=sqlite3.connect(path);c.execute('BEGIN EXCLUSIVE')
        try:
            for modal,depth in ((SimpleNamespace(_completed=False),0),(None,1)):
                m.host._large_io_dialog=modal;m.host.aoxiang_workflows.maintenance_depth=depth
                with patch.object(m.repo,'exists',side_effect=AssertionError('UI touched database')):
                    m.poll();self.assertIsNone(m.reader)
                self.assertIn('等待',m.status.text())
        finally:c.rollback();c.close()
        self.assertEqual(self.errors,[])
    def test_sqlite_exclusive_lock_does_not_block_gui_or_raise_modal_and_recovers(self):
        m,path=self.manager();c=sqlite3.connect(path);c.execute('BEGIN EXCLUSIVE');beats=[]
        timer=QTimer();timer.setInterval(10);timer.timeout.connect(lambda:beats.append(1));timer.start()
        try:
            started=time.monotonic();m.poll();self.assertLess(time.monotonic()-started,.3)
            self.wait(lambda:m.reader is None);self.assertGreater(len(beats),10)
            self.assertIn('等待数据库释放',m.status.text());self.assertEqual(self.errors,[])
        finally:c.rollback();c.close();timer.stop()
        m.poll();self.wait(lambda:m.reader is None);self.assertIn('未启用安排',m.status.text())
        with closing(sqlite3.connect(path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM scheduled_runs').fetchone()[0],0)
    def test_maintenance_rejects_running_scheduler_without_mutation(self):
        for worker,publisher in ((object(),None),(None,object())):
            host=SimpleNamespace(scheduled=SimpleNamespace(worker=worker,publisher=publisher))
            with patch.object(maintenance.QMessageBox,'information') as note:
                operation=unittest.mock.Mock();maintenance.guarded(None,host,operation);operation.assert_not_called();note.assert_called_once()
    def test_read_started_before_maintenance_cannot_launch_queued_run(self):
        m,path=self.manager();m.owner=True;m.host.aoxiang_workflows.maintenance_depth=1
        # The read has already finished: exercise its GUI completion callback.
        m.host.catalog_sessions=SimpleNamespace(session=SimpleNamespace(lease=None))
        m.host.jst_generator=SimpleNamespace(worker=None)
        with patch.object(m,'start') as start,patch.object(m.repo,'exists',return_value=True),patch.object(m.repo,'due'),patch.object(m.repo,'schedules',return_value=[]),patch.object(m.repo,'list',return_value={'items':[]}),patch.object(m.repo,'next_queued',return_value={'id':'queued'}):
            m.host.aoxiang_workflows.maintenance_depth=0;m.poll()
            m.host.aoxiang_workflows.maintenance_depth=1
            self.wait(lambda:m.reader is None);start.assert_not_called()

if __name__=='__main__':unittest.main()
