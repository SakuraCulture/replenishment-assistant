"""Storage handoff owns its lock after destroying Qt; private synthetic data only."""
import json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import data_location_pause as pause
from data_location_ui import reopen

class Timer:
    def __init__(self,active):self.active=active
    def isActive(self):return self.active
    def stop(self):self.active=False
    def start(self):self.active=True

class PauseTests(unittest.TestCase):
    def test_original_timer_and_stop_states_are_restored_on_cancel(self):
        startup=NS(paused=False,timer=Timer(True),retry_until=10)
        generator=NS(stopped=True,timer=Timer(False))
        host=NS(browser_startup=startup,jst_generator=generator)
        operation=pause.AutomaticPause(host)
        self.assertTrue(host._data_location_preparing);self.assertTrue(startup.paused);self.assertFalse(startup.timer.active)
        operation.restore();operation.restore()
        self.assertFalse(host._data_location_preparing);self.assertFalse(startup.paused);self.assertTrue(startup.timer.active)
        self.assertTrue(generator.stopped);self.assertFalse(generator.timer.active)
    def test_cancel_only_startup_checks_and_wait_for_existing_release(self):
        worker=NS(cancel=threading.Event(),released=threading.Event())
        def finish():worker.cancel.wait();worker.released.set()
        thread=threading.Thread(target=finish);thread.start()
        pause.finish_login_checks([worker]);thread.join(2)
        self.assertTrue(worker.cancel.is_set());self.assertTrue(worker.released.is_set())
    def test_failed_move_never_reopens_the_business_app(self):
        runtime=NS(location_result={'state':'FAILED'},storage_restart=True)
        with patch('subprocess.Popen') as start:reopen(runtime)
        start.assert_not_called()
    def test_shutdown_failure_does_not_enable_a_previously_stopped_queue(self):
        from migration_status import shutdown_failed
        manager=NS(stopping=True,timer=Timer(False))
        manager.resume_after_cancelled_close=lambda:setattr(manager,'stopping',False)
        host=NS(aoxiang_workflows=manager,_data_location_request={'nonce':'isolated'},
                setEnabled=lambda _:None,status=NS(setText=lambda _:None))
        operation=pause.AutomaticPause(host)
        with patch('data_relocation.record'),patch('application_runtime.current',return_value=None):
            self.assertTrue(shutdown_failed(host,RuntimeError('synthetic-close-failure')))
        self.assertTrue(manager.stopping);self.assertFalse(manager.timer.active)
        self.assertTrue(operation.restored);self.assertFalse(host._data_location_preparing)
    def test_successful_reopen_uses_a_working_directory_outside_moved_data(self):
        runtime=NS(location_result={'state':'COMPLETED'},storage_restart=True)
        with patch('subprocess.Popen') as start:reopen(runtime)
        import data_location
        self.assertEqual(start.call_args.kwargs['cwd'],data_location.control())

class NativeTeardown(unittest.TestCase):
    def test_qt_owned_directory_handle_closes_but_instance_guard_survives(self):
        with tempfile.TemporaryDirectory() as folder:
            script=r'''import os,sys,subprocess,json,ctypes
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
from ctypes import wintypes
from PySide6.QtWidgets import QApplication,QWidget
from single_instance import SingleInstance
from application_runtime import Runtime
from application_exit import finish,release
import data_location as location,data_relocation as move,test_archive_orders as fixture
root=Path(sys.argv[1]);os.environ['LOCALAPPDATA']=str(root/'local');os.environ.pop('REPLENISHMENT_DATA_DIR',None)
source=location.default_base();source.mkdir(parents=True);fixture.seed(source)
app=QApplication([]);runtime=Runtime(source/'settings',folder=location.control()/'diagnostics');runtime.application=app
primary=SingleInstance('private-storage-teardown',folder=root/'locks',parent=app);assert primary.acquire();runtime.primary=primary
lock=next((root/'locks').glob('*.lock'));win=QWidget();runtime.window=win;runtime.lease=NS(close=lambda:None)
kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE];kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
h=kernel.CreateFileW(str(source),1,3,None,3,0x02000000,None);assert h!=ctypes.c_void_p(-1).value
closed=[]
app.destroyed.connect(lambda:(kernel.CloseHandle(h),closed.append(True)))
original=move._copy
def checked_copy(*args):
 assert QApplication.instance() is None and closed==[True]
 code='from PySide6.QtCore import QLockFile;import sys;f=QLockFile(sys.argv[1]);f.setStaleLockTime(0);print(f.tryLock(0))'
 other=subprocess.run([sys.executable,'-c',code,str(lock)],capture_output=True,timeout=15)
 assert other.returncode==0 and other.stdout.strip()==b'False',(other.stdout,other.stderr)
 return original(*args)
with patch.object(move,'browser_rows',return_value=[]),patch('application_reset._check_other_processes'),patch.object(move,'_copy',checked_copy):
 win._data_location_request=move.prepare(source,root/'target');win._data_location_restart=False
 try:
  assert finish(runtime,0)==0 and runtime.location_result['phase']=='COMPLETED'
 finally:release(runtime)
assert not lock.exists();print(json.dumps({'passed':True,'qt_destroyed_before_copy':True,'guard_retained':True}))
'''
            env={**os.environ,'PYTHONPATH':os.pathsep.join((str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent))),'QT_QPA_PLATFORM':'offscreen','PYTHONUTF8':'1'}
            result=subprocess.run([sys.executable,'-c',script,folder],env=env,capture_output=True,timeout=40)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'));self.assertIn('"passed": true',result.stdout.decode())

if __name__=='__main__':unittest.main()
