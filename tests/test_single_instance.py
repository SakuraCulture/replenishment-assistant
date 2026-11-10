"""Real local IPC: second launch restores hidden/minimized first window."""
import json,os,subprocess,sys,tempfile,time,unittest,uuid
from pathlib import Path
from single_instance import scope

HELPER='''
import json,sys
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QTimer
from single_instance import SingleInstance
root=Path(sys.argv[1]);app=QApplication([]);lease=SingleInstance(sys.argv[2],folder=root/'locks')
if not lease.acquire():sys.exit(0)
w=QWidget();lease.bind(w);w.showMinimized();w.hide()
(root/'ready').write_text(str(__import__('os').getpid()))
def tick():
    (root/'state').write_text(json.dumps({'visible':w.isVisible(),'minimized':w.isMinimized()}))
    if (root/'stop').exists():app.quit()
t=QTimer();t.timeout.connect(tick);t.start(50);QTimer.singleShot(20000,app.quit)
app.exec();lease.close()
'''

class SingleInstanceTests(unittest.TestCase):
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
                time.sleep(.15);state=json.loads((root/'state').read_text());self.assertTrue(state['visible']);self.assertFalse(state['minimized'])
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
