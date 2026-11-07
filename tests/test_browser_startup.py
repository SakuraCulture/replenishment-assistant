"""Startup login, isolation, cancellation and stale-state regressions without platforms."""
import os,sys,time,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication,QMainWindow
from aoxiang_session import SessionOwner as AxOwner,SessionGate
from catalog_sync_session import SessionOwner as JstOwner
from browser_startup import check_ax,check_jst,error_result,yield_startup
from browser_startup_ui import StartupConnections,start_application_connections
from browser_startup_fixture import AxFixture,JstFixture
from browser_status_ui import BrowserStatus


class Checks(unittest.TestCase):
    def test_login_errors_are_actionable_and_identity_errors_are_distinct(self):
        for code in ['WAIT_LOGIN','LOGIN_REQUIRED']:
            self.assertEqual(error_result({'code':code,'message':'test'})['state'],'LOGIN_REQUIRED')
        self.assertEqual(error_result({'code':'WRONG_ACCOUNT'})['state'],'WRONG_ACCOUNT')

    def test_missing_ax_binding_does_not_start_setup_or_guess_stores(self):
        c=AxFixture();original=c.request
        c.request=lambda action,**kw:{'configured':False} if action=='configuration' else original(action,**kw)
        updates=[];check_ax(c,threading.Event(),lambda n,r:updates.append(r))
        self.assertEqual(updates[-1]['state'],'CONFIG_REQUIRED');self.assertEqual(c.calls,[])

    def test_disabled_warehouse_is_not_reenabled_and_other_warehouse_connects(self):
        c=JstFixture();c.accounts[0]['enabled']=False;updates={}
        check_jst(c,['山东仓','河北仓'],threading.Event(),lambda n,r:updates.update({n:r}))
        self.assertEqual(updates['山东仓']['state'],'DISABLED');self.assertEqual(updates['河北仓']['state'],'READY')
        self.assertEqual([v['warehouse'] for a,v in c.calls if a=='accounts.ensure'],['河北仓'])

    def test_partial_login_failure_does_not_block_the_other_warehouse(self):
        c=JstFixture();c.errors['山东仓']={'code':'LOGIN_REQUIRED','message':'login'};updates={}
        check_jst(c,['山东仓','河北仓'],threading.Event(),lambda n,r:updates.update({n:r}))
        self.assertEqual(updates['山东仓']['state'],'LOGIN_REQUIRED');self.assertEqual(updates['河北仓']['state'],'READY')

    def test_cancel_only_stops_its_own_account_operation_and_drains(self):
        c=JstFixture();cancel=threading.Event();original=c.request;polls=0
        def request(action,**values):
            nonlocal polls
            if action=='sync.status' and c.operations:
                polls+=1
                if polls==1:
                    cancel.set()
                    return {'operations':{'qa-山东仓':{'state':'RUNNING'}},'active':{'id':'qa-山东仓'}}
            return original(action,**values)
        c.request=request
        check_jst(c,['山东仓','河北仓'],cancel,lambda *_:None)
        self.assertEqual([v for a,v in c.calls if a=='sync.stop'],[{'batch_id':'qa-山东仓'}])
        self.assertEqual([v['warehouse'] for a,v in c.calls if a=='accounts.ensure'],['山东仓'])


class StartupUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.host=QMainWindow();self.host.closing=False;self.host.close_count=0
        self.host.close=lambda:setattr(self.host,'close_count',self.host.close_count+1)
        self.ax=AxFixture();self.jst=JstFixture()
        self.host.aoxiang_workflows=NS(owner=True,gate=SessionGate(),session=AxOwner(lambda:self.ax))
        self.host.catalog_sessions=NS(session=JstOwner(self.temp.name,lambda _:self.jst))
        self.host.browser_startup=self.ui=StartupConnections(self.host)
        self.bar=BrowserStatus(self.host);self.bar.timer.stop();self.app.processEvents()
    def wait(self,check,seconds=4):
        until=time.monotonic()+seconds
        while not check() and time.monotonic()<until:self.app.processEvents();time.sleep(.005)
        self.app.processEvents();self.assertTrue(check())
    def tearDown(self):
        if self.ax.gate:self.ax.gate.set()
        self.ui.prepare_close();self.bar.prepare_close();self.wait(lambda:not self.ui.workers and not self.bar.workers)
        self.host.aoxiang_workflows.session.close();self.host.catalog_sessions.session.close()
        import shiboken6
        shiboken6.delete(self.host);self.temp.cleanup()
    def test_normal_launch_starts_immediately_and_smoke_launch_remains_isolated(self):
        with patch.object(self.ui,'start') as begin:
            start_application_connections(self.host,['app.exe']);begin.assert_called_once()
            begin.reset_mock();start_application_connections(self.host,['app.exe','--stockout-smoke','qa']);begin.assert_not_called()
    def test_startup_without_manual_actions_checks_all_and_retains_shared_clients(self):
        self.ui.start();self.wait(lambda:not self.ui.workers)
        self.assertEqual([self.ui.records[n]['state'] for n in ['翱象','山东仓','河北仓']],['READY']*3)
        self.assertTrue(self.ax.running and self.jst.running)
        self.assertIsNone(self.host.catalog_sessions.session.lease);self.assertIsNone(self.host.aoxiang_workflows.gate.background)
        self.assertEqual(len([a for a,v in self.ax.calls if a=='setup.check_login']),1)
        self.assertEqual([v['warehouse'] for a,v in self.jst.calls if a=='accounts.ensure'],['山东仓','河北仓'])
        self.bar.refresh();self.wait(lambda:not self.bar.workers)
        self.assertTrue(all('登录已核验' in label.text() for label in self.bar.labels.values()))
    def test_slow_ax_does_not_block_other_connections_or_gui(self):
        self.ax.gate=threading.Event();self.ui.start()
        self.wait(lambda:'jst' not in self.ui.workers)
        self.assertIn('ax',self.ui.workers);self.assertEqual(self.ui.records['河北仓']['state'],'READY')
        self.ax.gate.set();self.wait(lambda:not self.ui.workers)
    def test_foreground_business_keeps_ownership_and_startup_defers(self):
        ticket=self.host.aoxiang_workflows.gate.request_foreground();lease=self.host.catalog_sessions.session.acquire()
        self.ui.start();self.assertFalse(self.ui.workers);self.assertFalse(self.ax.running or self.jst.running)
        self.assertEqual(len(self.ui.waiting),3)
        ticket.release();lease.close();self.ui.retry_pending();self.wait(lambda:not self.ui.workers)
        self.assertFalse(self.ui.waiting);self.assertEqual(self.ui.records['翱象']['state'],'READY')
    def test_login_retry_is_automatic_only_for_failed_login_and_keeps_a_bounded_window(self):
        self.jst.errors['山东仓']={'code':'LOGIN_REQUIRED','message':'login'}
        self.ui.start();self.wait(lambda:not self.ui.workers);self.jst.calls.clear();self.ax.calls.clear()
        deadline=self.ui.retry_until;self.jst.errors.clear();self.ui.retry_pending();self.wait(lambda:not self.ui.workers)
        self.assertEqual(self.ui.retry_until,deadline);self.assertEqual(self.ax.calls,[])
        self.assertEqual([v['warehouse'] for a,v in self.jst.calls if a=='accounts.ensure'],['山东仓'])
        self.assertEqual(self.ui.records['山东仓']['state'],'READY')
    def test_wrong_account_never_gets_green_and_no_automatic_rebinding(self):
        self.jst.errors['河北仓']={'code':'WRONG_ACCOUNT','message':'wrong'}
        self.ui.start();self.wait(lambda:not self.ui.workers);self.bar.refresh();self.wait(lambda:not self.bar.workers)
        self.assertIn('账号不符',self.bar.labels['河北仓'].text());self.jst.calls.clear()
        self.ui.retry_pending();self.assertEqual(self.jst.calls,[])
    def test_login_auto_retry_stops_at_deadline_without_losing_reason(self):
        self.jst.errors['山东仓']={'code':'LOGIN_REQUIRED','message':'login'}
        self.ui.start();self.wait(lambda:not self.ui.workers);self.jst.calls.clear()
        self.ui.retry_until=time.monotonic()-1;self.ui.retry_pending()
        self.assertEqual(self.jst.calls,[]);self.assertEqual(self.ui.records['山东仓']['state'],'LOGIN_REQUIRED')
    def test_business_can_cancel_only_startup_probe_then_acquire_same_retained_client(self):
        self.ax.gate=threading.Event();self.ui.start();self.wait(lambda:self.ax.running)
        done=threading.Event();tokens=[]
        def business():
            yield_startup(self.host,'ax')
            token=self.host.aoxiang_workflows.gate.claim_background();tokens.append(token)
            self.host.aoxiang_workflows.gate.release_background(token);done.set()
        thread=threading.Thread(target=business);thread.start()
        self.wait(lambda:self.ui.workers['ax'].cancel.is_set());self.assertFalse(done.is_set())
        self.ax.gate.set();self.wait(done.is_set);thread.join();self.wait(lambda:not self.ui.workers)
        self.assertIsNotNone(tokens[0]);self.assertTrue(self.ax.running)
    def test_old_success_does_not_mark_new_transport_or_disconnection_logged_in(self):
        self.ui.start();self.wait(lambda:not self.ui.workers)
        self.assertIsNone(self.ui.presentation('翱象','DISCONNECTED',self.ax))
        self.assertIsNone(self.ui.presentation('翱象','CONNECTED',AxFixture()))
    def test_close_cancels_startup_and_waits_for_safe_handoff(self):
        self.ax.gate=threading.Event();self.ui.start();self.wait(lambda:self.ax.running)
        self.assertFalse(self.ui.prepare_close());self.assertFalse(self.ui.timer.isActive())
        self.ax.gate.set();self.wait(lambda:not self.ui.workers)
        self.assertEqual(self.host.close_count,1);self.assertIsNone(self.host.aoxiang_workflows.gate.background)
    def test_destroying_idle_indicator_before_first_timer_callback_is_safe(self):
        import shiboken6
        host=QMainWindow();bar=BrowserStatus(host);shiboken6.delete(host)
        bar.refresh();bar.render();self.app.processEvents()


if __name__=='__main__':unittest.main()
