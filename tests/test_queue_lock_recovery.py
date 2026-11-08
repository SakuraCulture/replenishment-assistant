import os,sys,sqlite3,tempfile,unittest,time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src')]
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtTest import QTest
from aoxiang_workflow_ui import Manager
from aoxiang_workflows import Workflows
import shiboken6

class QueueLockRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.host=QWidget();self.host.repository=SimpleNamespace(folder=self.root/'settings')
        self.host.work_repository=SimpleNamespace(path=self.root/'settings/workbench.sqlite3');self.host.closing=False
        self.m=Manager(self.host,client_factory=lambda *a:(_ for _ in ()).throw(AssertionError('No platform client')))
        self.m.timer.stop()
    def tearDown(self):
        self.m.shutdown();shiboken6.delete(self.host);self.tmp.cleanup()
    def test_live_timer_busy_recovery_and_maintenance(self):
        from queue_lock_probe import exercise
        with patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(self.root/'settings')):
            self.m.timer.start();exercise(self.app,self.m,self.root)
    def test_idle_poll_under_read_lock_never_needs_write(self):
        db=sqlite3.connect(self.m.store.path);db.execute('BEGIN');db.execute('SELECT * FROM ax_schedules').fetchall()
        try:
            start=time.perf_counter();self.m.tick()
            self.assertLess(time.perf_counter()-start,.5);self.assertFalse(self.m.busy);self.assertFalse(self.m.stopping)
        finally:db.rollback();db.close()
    def test_fatal_error_reported_but_owned_maintenance_possible(self):
        with patch.object(self.m.store,'enqueue_due',side_effect=ValueError('合成结构错误')):self.m.tick()
        self.assertTrue(self.m.stopping)
        with self.assertRaisesRegex(ValueError,'合成结构错误'):self.m.ensure_owner()
        with self.assertRaisesRegex(RuntimeError,'cancel'):
            with self.m.maintenance():raise RuntimeError('cancel')
        self.assertEqual(self.m.maintenance_depth,0);self.assertTrue(self.m.stopping)
        self.m.retry_queue();self.assertFalse(self.m.stopping);self.m.ensure_owner()
    def test_other_owner_cannot_maintain_then_explicit_reacquire(self):
        other=Manager(self.host);other.timer.stop()
        try:
            with self.assertRaisesRegex(ValueError,'另一实例'):
                with other.maintenance():self.fail('must not enter')
            other.retry_queue();self.assertIsNone(other.owner)
            self.m.shutdown();other.retry_queue();other.ensure_owner();self.assertFalse(other.stopping)
        finally:other.shutdown()
    def test_closing_and_active_worker_are_not_bypassed(self):
        self.m.worker=object()
        try:
            with self.assertRaisesRegex(ValueError,'尚未安全退出'):
                with self.m.maintenance():pass
        finally:self.m.worker=None
        with self.m.maintenance():self.m.prepare_close()
        self.assertTrue(self.m.stopping);self.assertTrue(self.m.close_requested)
        self.m.retry_queue()
        with self.assertRaisesRegex(ValueError,'正在安全关闭'):self.m.ensure_owner(for_maintenance=True)
    def test_due_schedule_rechecked_after_read_phase(self):
        from test_aoxiang_workflows import record
        # A due-schedule race needs a valid original order/payment now. Keep
        # the new production guard active while exercising the same lock race.
        from test_lifecycle import LifecycleTests
        fixture=LifecycleTests();fixture.setUp();previous=self.m.store.business_path
        try:
            fixture.save('return');fixture.save('payment');payment=fixture.view()['documents']['payment']
            value=record(1);value['basis']={k:fixture.h[k] for k in ('oid','revision','number','store','warehouse')};value['basis'].update(paymentVersion=payment['version'],paymentDigest=payment['digest'])
            self.m.store.business_path=fixture.path;self.m.store.configure_schedules([value],5,True)
            original=self.m.store._due;calls=[]
            def changed(db):
                due=original(db);calls.append(1)
                if len(calls)==1:self.m.store.configure_schedules([value],5,False)
                return due
            with patch.object(self.m.store,'_due',side_effect=changed):self.assertIsNone(self.m.store.enqueue_due(timeout=0))
            self.assertEqual(len(calls),2);self.assertEqual(self.m.store.list(),[])
        finally:self.m.store.business_path=previous;fixture.tearDown()

if __name__=='__main__':unittest.main()
