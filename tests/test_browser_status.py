"""Read-only indicator and thread lifetime regression; isolated fake transports."""
import os,sys,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace as NS
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from PySide6.QtWidgets import QApplication,QMainWindow
from browser_status_ui import BrowserStatus,states_for


class Client:
    def __init__(self,data):self.data=data;self.running=True;self.calls=[];self.block=None
    def request(self,action,**values):
        self.calls.append((action,values))
        if self.block:self.block.wait(2)
        if isinstance(self.data,Exception):raise self.data
        return self.data


class Indicators(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.app=QApplication.instance() or QApplication([])
    def setUp(self):
        self.host=QMainWindow();self.host.close_calls=0
        self.host.close=lambda:setattr(self.host,'close_calls',self.host.close_calls+1)
        self.ax=NS(client=None,started=True);self.jst=NS(client=None,ready=True)
        self.host.aoxiang_workflows=NS(session=self.ax);self.host.catalog_sessions=NS(session=self.jst)
        self.ui=BrowserStatus(self.host);self.ui.timer.stop();self.app.processEvents()
    def wait(self,fn):
        until=time.monotonic()+3
        while not fn() and time.monotonic()<until:self.app.processEvents();time.sleep(.005)
        self.app.processEvents();self.assertTrue(fn())
    def tearDown(self):
        for owner in (self.ax,self.jst):
            if owner.client and owner.client.block:owner.client.block.set()
        self.ui.prepare_close();self.wait(lambda:not self.ui.workers);self.host.deleteLater();self.app.processEvents()
    def poll(self):self.ui.refresh();self.wait(lambda:not self.ui.workers)
    def test_idle_does_not_create_component_or_connection(self):
        for _ in range(3):self.poll()
        self.assertIsNone(self.ax.client);self.assertIsNone(self.jst.client)
        self.assertEqual(set(self.ui.states.values()),{'NOT_CONNECTED'})
    def test_three_connections_are_independent_and_read_only(self):
        self.ax.client=Client({'state':'CONNECTED'})
        self.jst.client=Client({'connections':[{'warehouse':'山东仓','state':'CONNECTED'},{'warehouse':'河北仓','state':'DISCONNECTED'}]})
        self.poll();self.assertEqual(self.ui.states,{'翱象':'CONNECTED','山东仓':'CONNECTED','河北仓':'DISCONNECTED'})
        for owner in (self.ax,self.jst):self.assertEqual(owner.client.calls,[('session.health',{'timeout':1.5})])
    def test_failed_observation_is_unknown_and_does_not_restart_or_stop(self):
        self.ax.client=Client({'state':'CONNECTED'});self.poll();self.ax.client.data=TimeoutError('lost status');self.poll()
        self.assertEqual(self.ui.states['翱象'],'UNKNOWN');self.assertTrue(self.ax.client.running)
        self.assertEqual([x[0] for x in self.ax.client.calls],['session.health','session.health'])
    def test_dead_process_clears_green_without_request(self):
        self.ax.client=Client({'state':'CONNECTED'});self.poll();self.ax.client.running=False;self.poll()
        self.assertEqual(self.ui.states['翱象'],'DISCONNECTED');self.assertEqual(len(self.ax.client.calls),1)
    def test_old_process_reply_cannot_color_new_process_green(self):
        old=Client({'state':'CONNECTED'});old.block=threading.Event();self.ax.client=old;self.ui.refresh()
        self.ax.client=Client({'state':'DISCONNECTED'});old.block.set();self.wait(lambda:not self.ui.workers)
        self.assertNotEqual(self.ui.states['翱象'],'CONNECTED');self.poll();self.assertEqual(self.ui.states['翱象'],'DISCONNECTED')
    def test_slow_ax_does_not_block_jst_or_add_more_tasks(self):
        self.ax.client=Client({'state':'CONNECTED'});self.ax.client.block=threading.Event()
        self.jst.client=Client({'connections':[{'warehouse':'河北仓','state':'CONNECTED'}]});self.ui.refresh()
        self.wait(lambda:self.ui.states['河北仓']=='CONNECTED')
        for _ in range(5):self.ui.refresh()
        self.assertEqual(len(self.ax.client.calls),1);self.ax.client.block.set();self.wait(lambda:not self.ui.workers)
    def test_shutdown_drains_observation_without_touching_business_process(self):
        self.ax.client=Client({'state':'CONNECTED'});self.ax.client.block=threading.Event();self.ui.refresh()
        self.assertFalse(self.ui.prepare_close());self.assertFalse(self.ui.timer.isActive());self.ui.refresh()
        self.ax.client.block.set();self.wait(lambda:not self.ui.workers);self.wait(lambda:self.host.close_calls==1)
        self.assertTrue(self.ax.client.running);self.assertEqual(self.ui.states['翱象'],'NOT_CONNECTED')
        self.ui.resume();self.ui.timer.stop();self.wait(lambda:not self.ui.workers);self.assertEqual(self.ui.states['翱象'],'CONNECTED')
    def test_no_false_green_on_unknown_or_duplicate_payload(self):
        self.assertEqual(states_for('ax',{'state':'broken'}),{'翱象':'UNKNOWN'})
        self.assertEqual(states_for('jst',{'connections':[{'warehouse':'山东仓','state':'CONNECTED'}]*2})['山东仓'],'UNKNOWN')
        self.ax.client=Client(None);self.poll();self.assertEqual(self.ui.states['翱象'],'UNKNOWN')
    def test_starting_transport_does_not_claim_browser_is_connected(self):
        self.ax.client=Client({'state':'CONNECTED'});self.ax.started=False;self.poll()
        self.assertEqual(self.ui.states['翱象'],'UNKNOWN');self.assertEqual(self.ax.client.calls,[])
    def test_status_has_text_and_explains_connection_scope(self):
        self.ui.set_state('山东仓','CONNECTED')
        self.assertIn('已连接',self.ui.labels['山东仓'].text());self.assertIn('不代表网站登录',self.ui.labels['山东仓'].toolTip())

if __name__=='__main__':unittest.main()
