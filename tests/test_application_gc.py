"""Real Qt wrappers, natural worker allocation, and deterministic GUI teardown."""
import os
import subprocess
import sys
import tempfile
import unittest


class GuiGarbageCollection(unittest.TestCase):
    def execute(self,code):
        with tempfile.TemporaryDirectory() as folder:
            result=subprocess.run([sys.executable,'-c',code,folder],
                env={**os.environ,'QT_QPA_PLATFORM':'offscreen','LOCALAPPDATA':folder,'PYTHONUTF8':'1'},
                capture_output=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
            self.assertIn('PASSED',result.stdout.decode())

    def test_natural_worker_allocation_never_collects_gui_cycles_in_worker(self):
        self.execute(r'''
import gc,sys,threading,weakref
from pathlib import Path
from PySide6.QtCore import QEventLoop,QTimer
from PySide6.QtWidgets import QApplication,QWidget
import shiboken6
from application_runtime import Runtime
app=QApplication([]);app.setQuitOnLastWindowClosed(False);r=Runtime(Path(sys.argv[1])/'settings');r.bind_qt(app)
guard=r.gui_gc;owner=threading.get_ident();finalized=[];refs=[];thresholds=gc.get_threshold()
class Widget(QWidget):
 def __del__(self):finalized.append(threading.get_ident())
for i in range(200):
 widget=Widget();widget.cycle=[widget];refs.append(weakref.ref(widget))
del widget
def pressure():
 for i in range(20000):
  cycle=[];cycle.append(cycle)
thread=threading.Thread(target=pressure);thread.start();thread.join()
assert not finalized and not gc.isenabled()
loop=QEventLoop();timer=QTimer();timer.timeout.connect(lambda:loop.quit() if len(finalized)==200 else None);timer.start(20)
QTimer.singleShot(2000,loop.quit);loop.exec();timer.stop()
assert len(finalized)==200 and set(finalized)=={owner},(len(finalized),set(finalized))
assert guard.off_thread_collections==0 and sum(guard.collections)>0
assert gc.get_threshold()==thresholds
r.transition('UI_TEARDOWN');guard.drain();shiboken6.delete(app);r.restore()
assert gc.isenabled() and guard.closed and guard.audit not in gc.callbacks
print('PASSED')
''')

    def test_original_disabled_gc_and_reentrant_callbacks_are_preserved(self):
        self.execute(r'''
import gc,sys
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
import shiboken6
from application_runtime import Runtime
gc.disable();app=QApplication([]);r=Runtime(Path(sys.argv[1])/'settings');r.bind_qt(app);g=r.gui_gc
assert not g.timer.isActive() and not g.enabled_before
g.collect_due();assert g.collections==[0,0,0]
def reenter(phase,info):
 if phase=='start':g.collect(0)
gc.callbacks.append(reenter)
try:g.drain()
finally:gc.callbacks.remove(reenter)
assert g.collections==[0,0,1] and not gc.isenabled()
shiboken6.delete(app);g.drain();r.restore();g.restore();assert not gc.isenabled()
gc.enable();print('PASSED')
''')

    def test_worker_cannot_install_collect_or_restore_global_gui_policy(self):
        self.execute(r'''
import gc,sys,threading
from pathlib import Path
from PySide6.QtWidgets import QApplication
import shiboken6
from application_gc import GuiCollector
from application_runtime import Runtime
app=QApplication([]);r=Runtime(Path(sys.argv[1])/'settings');failures=[]
def try_call(fn):
 try:fn()
 except RuntimeError as exc:failures.append(str(exc))
thread=threading.Thread(target=lambda:try_call(lambda:GuiCollector(app,r)));thread.start();thread.join()
assert len(failures)==1 and gc.isenabled() and not r.__dict__.get('gui_gc')
r.bind_qt(app);g=r.gui_gc;r.bind_qt(app);assert r.gui_gc is g
thread=threading.Thread(target=lambda:[try_call(fn) for fn in (lambda:g.collect(0),g.drain,g.restore)])
thread.start();thread.join();assert len(failures)==4 and not g.closed and not gc.isenabled()
g.drain();shiboken6.delete(app);r.restore();assert gc.isenabled();print('PASSED')
''')

    def test_explicit_worker_collection_is_reported_without_hiding_it(self):
        self.execute(r'''
import gc,json,sys,threading
from pathlib import Path
from PySide6.QtWidgets import QApplication
import shiboken6
from application_runtime import Runtime
app=QApplication([]);r=Runtime(Path(sys.argv[1])/'settings');r.bind_qt(app);g=r.gui_gc
thread=threading.Thread(target=lambda:gc.collect(0));thread.start();thread.join()
assert g.off_thread_collections==1
events=[json.loads(line) for line in r.path.read_text('utf-8').splitlines()]
assert any(x['event']=='GUI_GC_OFF_THREAD' for x in events)
g.drain();shiboken6.delete(app);r.restore();print('PASSED')
''')


if __name__=='__main__':unittest.main()
