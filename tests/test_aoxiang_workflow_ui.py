"""Local Qt queue/handoff tests. Clients are synthetic; no browser or platform calls."""
import copy
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QWidget, QMessageBox
from PySide6.QtTest import QTest
from aoxiang_workflow_ui import Manager, QueueDialog, ReceiptSelection
from aoxiang_ui_common import show_modal
from runtime_support import Cancelled
from test_aoxiang_workflows import head, record


class Client:
    instances = []
    def __init__(self, *args):
        self.running = False;self.calls = [];self.closed = threading.Event();self.allow_close = threading.Event();self.allow_close.set()
        self.instances.append(self)
    def start(self): self.running = True
    def close(self, timeout=.5):
        self.closed.set()
        if not self.allow_close.is_set(): return False
        self.running = False;return True
    def request(self, action, **kw):
        self.calls.append((action, kw))
        if action == 'purchase.list':
            return {'total': 2, 'items': [{**record(n), 'state': 'BOUND', 'target': {'displayName': '合成店' + str(n)}} for n in (1, 2)]}
        if action == 'receipt.list': return {'total': 0, 'items': []}
        raise AssertionError(action)


class WorkflowUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): cls.app = QApplication.instance() or QApplication([])
    def wait(self, predicate, seconds=5):
        end = time.monotonic() + seconds
        while not predicate() and time.monotonic() < end:
            self.app.processEvents();QTest.qWait(10)
        self.assertTrue(predicate())
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.root = Path(self.tmp.name)
        # This suite exercises UI/gate behavior with minimal fake identities.
        # Real local payment/warehouse validity is covered in ReceiptScopeTests.
        self.receipt_scope=patch('aoxiang_receipt_scope.assess',side_effect=lambda path,items,**kw:{r.get('key',r.get('id')):{'eligible':True,'reason':'','head':r.get('basis')} for r in items})
        self.receipt_scope.start()
        Client.instances = []
        self.selections = []
        self.host = QWidget();self.host.repository = SimpleNamespace(folder=self.root)
        self.host.work_repository = SimpleNamespace(path=self.root / 'not-used.db');self.host.closing = False
        import sqlite3
        with sqlite3.connect(self.host.work_repository.path) as db:
            db.execute('CREATE TABLE normal_orders(oid TEXT PRIMARY KEY,day TEXT,store TEXT)')
            db.executemany('INSERT INTO normal_orders VALUES(?,?,?)',[(str(n),head(n)['day'],str(n)) for n in range(1,101)])
        db.close()
        self.m = Manager(self.host, client_factory=Client);self.m.timer.stop();self.host.aoxiang_workflows = self.m
        self.warnings = []
        self.warning = patch('aoxiang_workflow_ui.QMessageBox.warning', side_effect=lambda *a:self.warnings.append(a[-1]));self.warning.start()
    def tearDown(self):
        for d in self.selections:self.close_selection(d)
        self.m.prepare_close()
        for c in Client.instances:c.allow_close.set()
        self.wait(lambda:self.m.worker is None)
        self.m.shutdown();self.warning.stop();self.receipt_scope.stop()
        import shiboken6
        shiboken6.delete(self.host);self.tmp.cleanup()
    def start_waiting(self):
        job = self.m.store.prepare([head(1)])
        started = threading.Event()
        def run(runner, id):
            runner.store.update(id, state='RUNNING');started.set()
            if runner.cancel.wait(5): raise Cancelled('停止')
            raise AssertionError('cancel never arrived')
        patched = patch('aoxiang_workflow_ui.Runner.run', run);patched.start();self.addCleanup(patched.stop)
        self.m.tick();self.wait(started.is_set)
        return job, Client.instances[-1]

    def test_second_instance_cannot_dispatch_and_lock_released_on_shutdown(self):
        other = Manager(self.host, client_factory=Client);other.timer.stop()
        self.assertIsNone(other.owner)
        with self.assertRaisesRegex(ValueError, '调度权'):other.ensure_owner()
        other.shutdown();self.m.shutdown()
        third = Manager(self.host, client_factory=Client);third.timer.stop()
        self.assertIsNotNone(third.owner);third.shutdown()

    def test_idle_host_direct_destruction_releases_directory_lock(self):
        import shiboken6
        host = QWidget();host.repository = SimpleNamespace(folder=self.root/'another')
        host.work_repository = self.host.work_repository
        manager = Manager(host, client_factory=Client);manager.timer.stop()
        self.assertIsNotNone(manager.owner);shiboken6.delete(host)
        self.assertIsNone(manager.owner)
        self.assertFalse((self.root/'another'/'aoxiang'/'scheduler.lock.sqlite3-journal').exists())

    def test_foreground_waits_for_actual_child_exit_and_ui_heartbeat_continues(self):
        job, client = self.start_waiting();client.allow_close.clear()
        ticks = [];timer = QTimer();timer.setInterval(5);timer.timeout.connect(lambda:ticks.append(1));timer.start()
        ticket = self.m.gate.request_foreground();passed = threading.Event()
        thread = threading.Thread(target=lambda:(ticket.wait(), passed.set()));thread.start()
        self.wait(client.closed.is_set);QTest.qWait(80)
        self.assertFalse(passed.is_set());self.assertGreater(len(ticks), 1)
        self.assertEqual(self.m.store.get(job['id'])['state'], 'YIELDED')
        client.allow_close.set();self.wait(lambda:self.m.worker is None);self.assertTrue(passed.wait(1));thread.join()
        self.assertIsNone(self.m.gate.claim_background());ticket.release();timer.stop()

    def test_stop_target_close_wait_and_cancelled_host_close_can_resume(self):
        job, client = self.start_waiting();client.allow_close.clear()
        self.m.stop('another-job');self.assertFalse(self.m.token.is_set())
        self.assertFalse(self.m.prepare_close());self.wait(client.closed.is_set)
        self.assertIsNotNone(self.m.worker)
        client.allow_close.set();self.wait(lambda:self.m.worker is None)
        self.assertEqual(self.m.store.get(job['id'])['state'], 'PAUSED')
        self.m.resume_after_cancelled_close();self.assertFalse(self.m.stopping)
        self.assertFalse(self.m.close_requested)

    def test_client_constructor_failure_keeps_task_and_releases_gate(self):
        job = self.m.store.prepare([head(1)])
        def broken(*args):raise OSError('组件缺失')
        self.m.client_factory = broken;self.m.tick();self.wait(lambda:self.m.worker is None)
        self.assertEqual(self.m.store.get(job['id'])['state'], 'PAUSED')
        token = self.m.gate.claim_background();self.assertIsNotNone(token);self.m.gate.release_background(token)

    def prepared(self, n):
        j = self.m.store.prepare([head(n)])
        self.m.store.update(j['id'], position=0, changes={'state':'PREPARED','package':{'id':'p'+str(n)},'summary':{'amount':'12.00','sku_count':1}})
        return self.m.store.update(j['id'], state='READY')

    def test_batch_upload_default_cancel_exact_selection_and_no_checkbox_leak(self):
        a, b = self.prepared(1), self.prepared(2)
        d = QueueDialog(self.host, self.m, a['id']);d.timer.stop()
        d.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        with patch.object(self.m, 'tick'), patch('aoxiang_workflow_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as ask:
            d.upload();self.assertEqual(ask.call_args.args[-1], QMessageBox.StandardButton.No)
            self.assertEqual(len(self.m.store.list()), 2)
        d.jobs.setCurrentIndex(d.jobs.findData(b['id']))
        self.assertEqual(d.table.item(0, 0).checkState(), Qt.CheckState.Unchecked)
        d.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        with patch.object(self.m, 'tick'), patch('aoxiang_workflow_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):d.upload()
        created = self.m.store.get(d.id)
        self.assertEqual(created['kind'], 'upload');self.assertEqual(created['items'][0]['head']['number'], 'BH-2')
        d.accept();d.deleteLater()

    def test_batch_confirmation_rejects_concurrent_preview_change(self):
        j = self.prepared(1);d = QueueDialog(self.host, self.m, j['id']);d.timer.stop()
        d.table.item(0, 0).setCheckState(Qt.CheckState.Checked)
        def question(*args):
            self.m.store.update(j['id'], message='另一窗口更新');d.refresh();return QMessageBox.StandardButton.Yes
        with patch('aoxiang_workflow_ui.QMessageBox.question', side_effect=question):d.upload()
        self.assertIn('预览已变化', self.warnings[-1]);self.assertEqual(len(self.m.store.list()), 1)
        d.accept();d.deleteLater()

    def selection(self):
        d = ReceiptSelection(self.host, self.host, [head(2), head(1)], client_factory=Client)
        d.show();self.wait(lambda:len(d.records)==2 and d.worker is None)
        self.selections.append(d);return d
    def close_selection(self, d):
        d.close();self.wait(lambda:d._can_close and d.worker is None);d.deleteLater()

    def test_receipt_selection_local_only_order_default_off_and_confirmation(self):
        d = self.selection()
        self.assertEqual([r['id'] for r in d.records], ['p2','p1'])
        self.assertEqual(self.m.store.schedules(), [])
        self.assertEqual([a for a,_ in self.m.session.client.calls], ['purchase.list','receipt.list'])
        d.select_visible()
        with patch('aoxiang_workflow_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.No) as ask:
            d.configure(True);self.assertEqual(ask.call_args.args[-1], QMessageBox.StandardButton.No)
        self.assertEqual(self.m.store.schedules(), [])
        with patch('aoxiang_workflow_ui.QMessageBox.question', return_value=QMessageBox.StandardButton.Yes):d.configure(True)
        self.assertEqual([s['id'] for s in self.m.store.schedules()], ['p2','p1'])
        self.assertTrue(all(s['enabled'] for s in self.m.store.schedules()))

    def test_local_failure_has_reason_not_fake_preview_and_can_end_selected(self):
        j=self.m.store.prepare([head(1),head(2)])
        for pos in (0,1):j=self.m.store.update(j['id'],position=pos,changes={'state':'REVIEW','error':{'code':'BUSINESS_REVIEW','message':'付款单尚未确认'}})
        j=self.m.store.update(j['id'],state='COMPLETED_WITH_ERRORS')
        d=QueueDialog(self.host,self.m,j['id']);d.timer.stop();d.table.setCurrentCell(0,1)
        self.assertFalse(d.preview_btn.isEnabled());self.assertTrue(d.reason_btn.isEnabled())
        self.assertIn('预览就绪0单',d.summary.text());self.assertIn('待处理2单',d.summary.text())
        with patch('aoxiang_workflow_ui.QMessageBox.information') as info:
            d.details();self.assertIn('未进入上传',info.call_args.args[-1]);self.assertNotIn('翱象收货页',info.call_args.args[-1])
        d.table.item(0,0).setCheckState(Qt.CheckState.Checked)
        with patch('aoxiang_workflow_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.No) as ask:
            d.end_local();self.assertEqual(ask.call_args.args[-1],QMessageBox.StandardButton.No)
        self.assertEqual(self.m.store.get(j['id']),j)
        with patch('aoxiang_workflow_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):d.end_local()
        kept=self.m.store.get(j['id']);self.assertEqual(kept['items'][0]['state'],'SKIPPED');self.assertEqual(kept['items'][1],j['items'][1])
        self.assertEqual(Client.instances,[]);d.accept();d.deleteLater()

    def test_refresh_keeps_checks_by_order_identity_not_old_row_number(self):
        j=self.m.store.prepare([head(1),head(2),head(3)])
        for n in range(3):self.m.store.update(j['id'],position=n,changes={'state':'PREPARED','package':{'id':'p'+str(n)}})
        j=self.m.store.update(j['id'],state='READY');d=QueueDialog(self.host,self.m,j['id']);d.timer.stop()
        d.table.item(1,0).setCheckState(Qt.CheckState.Checked)
        self.m.store.update(j['id'],items=j['items'][1:]);d.refresh()
        self.assertEqual(d.table.item(0,0).checkState(),Qt.CheckState.Checked)
        self.assertEqual(d.table.item(1,0).checkState(),Qt.CheckState.Unchecked)
        d.accept();d.deleteLater()

    def test_selected_button_and_local_delete_apply_exact_checkbox_scope(self):
        j=self.m.store.prepare([head(1),head(2)]);j=self.m.store.update(j['id'],state='PAUSED')
        d=QueueDialog(self.host,self.m,j['id']);d.timer.stop()
        d.table.item(1,0).setCheckState(Qt.CheckState.Checked)
        with patch.object(self.m,'tick'),patch('aoxiang_workflow_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):
            d.selected_btn.click()
        out=self.m.store.get(j['id']);self.assertEqual(out['dispatch_keys'],['BH-2'])
        self.assertEqual([i['state'] for i in out['items']],['PENDING','PENDING'])
        with patch('aoxiang_workflow_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.No):d.delete_local_btn.click()
        self.assertEqual(len(self.m.store.get(j['id'])['items']),2)
        with patch('aoxiang_workflow_ui.QMessageBox.question',return_value=QMessageBox.StandardButton.Yes):d.delete_local_btn.click()
        self.assertEqual([i['key'] for i in self.m.store.get(j['id'])['items']],['BH-1'])
        self.assertEqual(Client.instances,[]);d.accept();d.deleteLater()

    def test_idle_queue_message_finishes_and_delete_navigation_releases_maintenance(self):
        self.m.message='正在重新检查后台及原任务状态…';self.m.tick()
        self.assertIn('没有正在执行',self.m.message)
        import data_maintenance_ui as ui
        self.host.worker=None
        with patch.object(ui,'_remove',return_value=('open_task','test-id')),patch.object(self.m,'open_queue') as opened:
            opened.side_effect=lambda _:self.assertEqual(self.m.maintenance_depth,0)
            ui.remove(self.host,self.host,[]);opened.assert_called_once_with('test-id')

    def test_show_modal_assigns_gate_before_queued_local_load(self):
        job, client = self.start_waiting();client.allow_close.clear()
        d = ReceiptSelection(self.host, self.host, client_factory=Client)
        observations = []
        def release():
            observations.append(d.client.running);client.allow_close.set()
        def finish_when_loaded():
            if len(d.records)==2 and d.worker is None:d.close()
            else:QTimer.singleShot(15, finish_when_loaded)
        QTimer.singleShot(100, release);QTimer.singleShot(120, finish_when_loaded)
        self.assertTrue(show_modal(self.host, d))
        # Ownership is now reserved before the dialog's initial worker can start.
        self.assertEqual(observations, [True]);self.assertFalse(self.m.gate.yielding)
        self.assertEqual(self.m.store.get(job['id'])['state'], 'YIELDED')


if __name__ == '__main__':unittest.main()
