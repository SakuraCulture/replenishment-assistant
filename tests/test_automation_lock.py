"""Real SQLite reader/writer contention and Qt callbacks; isolated stores only."""
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import QObject
from PySide6.QtWidgets import QApplication
from PySide6.QtTest import QTest
from automation_gateway import Gateway, database_busy
from automation_tasks import Journal
from automation_protocol import CommandError
from work_repository import Repository


class Host(QObject):
    def __init__(self, folder):
        super().__init__(); self.work_repository=Repository(folder); self.worker=None; self.closing=False
        self.status=type('Status',(),{'setText':lambda self,text:None})()
    def update_controls(self): pass


class LockTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.host=Host(self.temp.name)
        self.g=Gateway(self.host); self.g.journal=Journal(self.host.work_repository.path, self.g.instance)
        self.task,_=self.g.journal.create('original',{},'prepare'); self.tid=self.task['task_id']
        self.db=sqlite3.connect(self.host.work_repository.path,timeout=0)
        self.errors=[]; self.oldhook=sys.excepthook; sys.excepthook=lambda *args:self.errors.append(args)
    def tearDown(self):
        self.db.rollback(); self.db.close(); self.g.failure_timer.stop()
        self.wait(lambda:self.g.worker is None); self.host.deleteLater(); self.app.processEvents()
        sys.excepthook=self.oldhook; self.temp.cleanup()
    def wait(self,predicate):
        end=time.monotonic()+5
        while not predicate() and time.monotonic()<end: self.app.processEvents(); QTest.qWait(10)
        self.assertTrue(predicate())
    def lock(self,kind='read'):
        self.db.execute('BEGIN' if kind=='read' else 'BEGIN EXCLUSIVE')
        self.db.execute('SELECT * FROM automation_tasks').fetchall()
    def finish_failure(self,fn,finish,handler=None):
        self.g.start_job(self.task,fn,finish,handler)
        self.wait(lambda:self.g.worker is None)
        result=self.g.task_status({'task_id':self.tid,'after':17})
        self.assertEqual(result['state'],'failed'); self.assertTrue(result['status_persistence_pending'])
        self.assertEqual(result['cursor'],17); self.assertFalse(self.errors)
        with self.assertRaises(CommandError): self.g.idle()
        start=time.monotonic(); self.g.persist_failures()
        self.assertLess(time.monotonic()-start,.2)  # Timer does not wait for the lock.
        self.assertTrue(self.g.pending_failures)
        self.db.rollback(); self.g.persist_failures()
        self.assertFalse(self.g.pending_failures)
        saved=self.g.journal.get(self.tid)
        self.assertEqual(saved['state'],'failed'); self.assertFalse(saved['status_persistence_pending'])
        self.assertEqual(len(self.g.journal.events(self.tid)),1)  # Failed commits left no event.
        self.g.persist_failures(); self.assertEqual(len(self.g.journal.events(self.tid)),1)
    def test_completion_commit_busy_is_not_success_and_not_replayed(self):
        self.lock(); calls=[]
        def finish(value):
            calls.append(value); self.g.prepared[self.tid]={'report':{'ready':True}}
            self.g.journal.update(self.tid,state='prepared')
        self.finish_failure(lambda _:42,finish)
        self.assertEqual(calls,[42]); self.assertNotIn(self.tid,self.g.prepared)
        with self.assertRaises(CommandError): self.g.require_prepared(self.tid)
        fresh,_=self.g.journal.create('fresh',{},'prepare')
        self.g.start_job(fresh,lambda w:False,lambda value:self.g.journal.update(fresh['task_id'],state='prepared'))
        self.wait(lambda:self.g.worker is None)
        self.assertEqual(self.g.journal.get(fresh['task_id'])['state'],'prepared')
    def test_background_error_under_exclusive_lock_keeps_queryable_failure(self):
        self.lock('exclusive')
        def fail(_): raise ValueError('synthetic failure')
        self.finish_failure(fail,lambda _:self.fail('Cannot finish failed job'))
    def test_progress_write_failure_stops_later_callback(self):
        self.lock(); completed=[]
        self.finish_failure(lambda w:w.progress.emit('读取中'),lambda _:completed.append(True))
        self.assertEqual(completed,[])
    def test_store_event_write_failure_does_not_adopt(self):
        self.lock(); adopted=[]
        self.finish_failure(lambda w:w.store_event.emit('A','已完成',{},''),lambda _:adopted.append(True),
            lambda *args:self.g.journal.update(self.tid,message='门店已完成'))
        self.assertEqual(adopted,[])
    def test_busy_classification_does_not_treat_arbitrary_locked_text_as_database(self):
        self.assertFalse(database_busy(ValueError('locked')))
        self.assertFalse(database_busy(sqlite3.OperationalError('no such table')))


if __name__=='__main__': unittest.main()
