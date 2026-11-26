"""Real local IPC: second launch restores hidden/minimized first window."""
import json,os,sqlite3,subprocess,sys,tempfile,time,unittest,uuid
from contextlib import closing,contextmanager
from pathlib import Path
from unittest.mock import Mock,patch
from application_reset import InstanceLease,lease_is_idle
from single_instance import SingleInstance,process_endpoint,scope

HELPER='''
import json,sys
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QTimer
from single_instance import SingleInstance
root=Path(sys.argv[1])

class ObservedInstance(SingleInstance):
    def activate(self):
        super().activate()
        # The production handler replies OK after activate returns.
        w=self.window
        (root/'state').write_text(json.dumps({'visible':w.isVisible(),'minimized':w.isMinimized()}))

app=QApplication([]);lease=ObservedInstance(sys.argv[2],folder=root/'locks')
if not lease.acquire():sys.exit(0)
w=QWidget();lease.bind(w);w.showMinimized();w.hide()
ready=root/'ready.tmp';ready.write_text(str(__import__('os').getpid()));ready.replace(root/'ready')
def tick():
    if (root/'stop').exists():app.quit()
t=QTimer();t.timeout.connect(tick);t.start(50);QTimer.singleShot(20000,app.quit)
app.exec();lease.close()
'''

class SingleInstanceTests(unittest.TestCase):
    def test_offline_marked_profile_without_test_arguments_has_its_own_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)/'settings'
            (root.parent/'synthetic.json').write_text('{"synthetic":true,"live_platform":false}')
            with patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(root)):
                self.assertTrue(scope([],root).startswith('qa:'))
                self.assertEqual(scope([],root.parent/'other'),'main')
            (root.parent/'synthetic.json').write_text('{"synthetic":true,"live_platform":true}')
            with patch.dict(os.environ,REPLENISHMENT_DATA_DIR=str(root)):
                self.assertEqual(scope([],root),'main')

    def test_skip_only_an_established_guarded_workbench_on_other_data(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);business=folder/'business/settings';other=folder/'isolated/settings'
            business.mkdir(parents=True);other.mkdir(parents=True)
            image=folder/'release/补货助手.exe';(image.parent/'_internal').mkdir(parents=True)
            (image.parent/'_internal/version.json').write_text(json.dumps(
                dict(app='补货助手',entry=image.name,version='0.54.16')),'utf-8')
            main=SingleInstance(folder=folder/'locks',data_root=business)
            qa=InstanceLease(other)
            try:
                self.assertTrue(lease_is_idle(business))
                self.assertFalse((business.parent/'.settings.instances.sqlite3').exists())
                self.assertFalse(lease_is_idle(other))
                self.assertEqual(main.independent_windows({1234:str(image)}),{1234})
                actual=InstanceLease(business)
                try:self.assertEqual(main.independent_windows({1234:str(image)}),set())
                finally:actual.close()
                self.assertTrue(lease_is_idle(business))
                # WAL readers coexist with BEGIN EXCLUSIVE and cannot prove an idle data root.
                with closing(sqlite3.connect(business.parent/'.settings.instances.sqlite3')) as db:
                    self.assertEqual(db.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                self.assertFalse(lease_is_idle(business))
            finally:qa.close()

    def test_second_click_sends_focus_only_to_the_file_lock_owner(self):
        with tempfile.TemporaryDirectory() as temp:
            lease=SingleInstance('private-test',folder=Path(temp))
            lease.lock=Mock()
            lease.lock.getLockInfo.return_value=(5678,'local','workbench')
            responses=iter([None,1234,5678]);addresses=[]
            @contextmanager
            def hello(name,**kwargs):
                addresses.append(name)
                yield object(),next(responses)
            with patch('single_instance.greeting',hello),patch.object(lease,'focus',return_value=b'OK') as focus:
                lease.activate_existing()
            self.assertEqual(addresses,[process_endpoint(5678),lease.name,process_endpoint(5678)])
            self.assertEqual([call.args[1] for call in focus.call_args_list],[5678])

    def test_main_is_user_global_and_smoke_isolated(self):
        self.assertEqual(scope([],Path('a')),scope([],Path('b')))
        self.assertEqual(scope(['--unmarked-smoke'],Path('a')),'main')
        with tempfile.TemporaryDirectory() as temp:
            roots=[Path(temp)/name/'settings' for name in ('a','b')]
            for root in roots:root.parent.mkdir();(root.parent/'synthetic.json').write_text('{"synthetic":true}')
            self.assertNotEqual(scope(['--stockout-smoke'],roots[0]),scope(['--stockout-smoke'],roots[1]))
        self.assertEqual(scope(['--user-note'],Path('a')),'main')

    def test_second_launch_and_crash_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);script=root/'helper.py';script.write_text(HELPER,encoding='utf-8');key='test:'+uuid.uuid4().hex
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen'}
            args=[sys.executable,str(script),str(root),key]
            first=subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                deadline=time.monotonic()+10
                while not (root/'ready').exists() and first.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                self.assertTrue((root/'ready').exists());pid=(root/'ready').read_text()
                second=subprocess.run(args,env=env,capture_output=True,timeout=18)
                self.assertEqual(second.returncode,0,second.stderr.decode(errors='replace'));self.assertIsNone(first.poll());self.assertEqual((root/'ready').read_text(),pid)
                state=json.loads((root/'state').read_text());self.assertTrue(state['visible']);self.assertFalse(state['minimized'])
                # Only this synthetic GUI process is killed to emulate a crash;
                # it has no database, workers, commits or platform connection.
                first.kill();first.wait(timeout=5);(root/'ready').unlink();(root/'state').unlink(missing_ok=True)
                recovered=subprocess.Popen(args,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                try:
                    deadline=time.monotonic()+10
                    while not (root/'ready').exists() and recovered.poll() is None and time.monotonic()<deadline:time.sleep(.05)
                    self.assertTrue((root/'ready').exists());self.assertNotEqual((root/'ready').read_text(),pid)
                finally:(root/'stop').touch();recovered.wait(timeout=5)
            finally:
                (root/'stop').touch()
                if first.poll() is None:first.wait(timeout=5)


if __name__=='__main__':unittest.main()
