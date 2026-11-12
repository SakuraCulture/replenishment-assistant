"""Shutdown, orphan identities and recoverable handoff; isolated data only."""
import json,os,subprocess,sys,tempfile,unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
import application_processes as processes,migration_status as status
import order_migration as migration
import test_order_migration as fixture

class Handoff(unittest.TestCase):
    def setUp(self):self.f=fixture.MigrationTests();self.f.setUp();self.addCleanup(self.f.tmp.cleanup)
    def test_prepared_and_waiting_are_durable_and_do_not_replace(self):
        before=fixture.files(self.f.target);plan=self.f.prepare();self.assertEqual(status.latest(self.f.target)['state'],'PREPARED')
        status.record(plan,'WAITING_EXIT');value=status.latest(self.f.target)
        self.assertEqual(json.loads(Path(value['plan_file']).read_text('utf-8')),plan)
        self.assertEqual(fixture.files(self.f.target),before);self.assertIsNone(migration.recover(self.f.target))
        self.assertTrue(Path(plan['stage']).is_dir());self.assertFalse((self.f.target/'订单迁入完成.json').exists())
    def test_cancel_is_explicit_and_original_package_survives(self):
        plan=self.f.prepare();migration.discard(plan);self.assertEqual(status.latest(self.f.target)['state'],'CANCELLED')
        self.assertTrue(self.f.package.is_file());self.assertFalse(Path(plan['stage']).exists())
    def test_apply_failure_preserves_stage_and_original_and_specific_error(self):
        plan=self.f.prepare();before=fixture.files(self.f.target)
        with patch('application_reset._check_other_processes'),patch.object(migration.shutil,'copytree',side_effect=OSError('最初复制失败-证据')):
            with self.assertRaisesRegex(OSError,'最初复制失败'):migration.apply(plan)
        value=status.latest(self.f.target);self.assertEqual(value['state'],'FAILED');self.assertEqual(value['phase'],'COPYING')
        self.assertIn('最初复制失败-证据',value['error']['traceback']);self.assertEqual(fixture.files(self.f.target),before)
        self.assertTrue(Path(plan['stage']).is_dir());self.assertTrue(self.f.package.is_file())
    def test_completed_state_requires_live_root_receipt_and_keeps_backup(self):
        plan=self.f.prepare();before=fixture.files(self.f.target)
        with patch('application_reset._check_other_processes'):result=migration.apply(plan)
        self.assertEqual(status.latest(self.f.target)['state'],'COMPLETED');self.assertEqual(fixture.files(Path(result['backup'])),before)
        self.assertEqual(migration.info(self.f.target/'workbench.sqlite3')['normal_orders'],1)
        self.assertFalse(Path(plan['stage']).exists());self.assertIsNone(migration.recover(self.f.target))
    def test_actual_interruption_between_renames_rolls_back_on_reopen(self):
        plan=self.f.prepare();before=fixture.files(self.f.target);file=self.f.root/'plan.json';file.write_text(json.dumps(plan),'utf-8')
        script='''import json,os,sys
from pathlib import Path
import order_migration as m
p=json.loads(Path(sys.argv[1]).read_text('utf-8'));original=Path.rename
def rename(self,destination):
 result=original(self,destination)
 if self==Path(p['root']):os._exit(73)
 return result
Path.rename=rename
m.apply(p)
'''
        result=subprocess.run([sys.executable,'-c',script,str(file)],capture_output=True,timeout=30)
        self.assertEqual(result.returncode,73,result.stderr.decode(errors='replace'))
        self.assertFalse(self.f.target.exists());self.assertTrue(Path(plan['backup']).is_dir());self.assertTrue(Path(plan['stage']).is_dir())
        self.assertEqual(migration.recover(self.f.target)['state'],'ROLLED_BACK');self.assertEqual(fixture.files(self.f.target),before)
        self.assertEqual(status.latest(self.f.target)['state'],'FAILED');self.assertTrue(Path(plan['stage']).is_dir())

class ProcessStates(unittest.TestCase):
    def row(self,**changes):
        return dict({'pid':65432,'parent':65431,'name':'补货助手.exe','argv':[],'created':(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat(),'threads':0,'handles':0,'image':None},**changes)
    def discover(self,row,*,guarded=False,second=None,parent=False,state='UNKNOWN'):
        native={row['pid']:{'name':row['name'],'parent':row['parent'],'threads':row['threads']}}
        if parent:native[row['parent']]={'name':'parent.exe','parent':1,'threads':1}
        with patch.object(processes,'records',side_effect=[[row],[second or row]]),patch.object(processes,'main_windows',return_value={}),patch.object(processes,'process_snapshot',return_value=native),patch.object(processes,'kernel_state',return_value={'state':state}),patch.object(processes.time,'sleep'):
            return processes.discover(guarded=guarded)
    def test_signaled_exited_process_is_not_live(self):self.assertEqual(self.discover(self.row(),state='EXITED'),[])
    def test_running_windowless_process_remains_protected(self):self.assertEqual(self.discover(self.row(threads=1),guarded=True,state='RUNNING')[0]['state'],'RUNNING')
    def test_access_denied_alone_never_allows_another_instance(self):self.assertEqual(self.discover(self.row(threads=None,handles=None),guarded=True)[0]['state'],'UNKNOWN')
    def test_abandoned_child_distinct_and_only_ignored_under_guard(self):
        row=self.row();self.assertEqual(self.discover(row)[0]['state'],'ABANDONED_CHILD');self.assertEqual(self.discover(row,guarded=True),[])
    def test_initializing_or_suspended_process_stays_protected(self):
        row=self.row(created=datetime.now(timezone.utc).isoformat());self.assertTrue(self.discover(row,guarded=True))
        self.assertTrue(self.discover(self.row(),guarded=True,parent=True))
    def test_pid_reuse_between_probes_is_unknown_and_blocking(self):
        row=self.row();second={**row,'created':datetime.now(timezone.utc).isoformat()}
        self.assertEqual(self.discover(row,second=second,guarded=True)[0]['state'],'UNKNOWN')
    def test_live_worker_cannot_be_misread_as_main_or_ignored(self):
        row=self.row(argv=['补货助手.exe','--catalog-validate'],threads=1)
        value=self.discover(row,state='RUNNING',guarded=True)[0];self.assertFalse(value['main']);self.assertEqual(value['role'],'worker')

class NativeExceptionPolicy(unittest.TestCase):
    def test_handled_health_failure_is_logged_without_failing_safe_shutdown(self):
        from application_runtime import Runtime
        with tempfile.TemporaryDirectory() as folder:
            runtime=Runtime(Path(folder)/'settings');runtime.transition('WAITING_EXIT')
            try:
                try:raise ValueError('HANDLED-HEALTH-FAILURE')
                except Exception:runtime.exception(*sys.exc_info(),event='COMPONENT_THREAD_EXCEPTION',blocking=False)
                self.assertEqual(runtime.shutdown_errors,0);self.assertIn('HANDLED-HEALTH-FAILURE',runtime.path.read_text('utf-8'))
            finally:runtime.restore()
    def test_default_root_is_the_settings_directory_not_its_parent(self):
        from application_runtime import settings_root
        with patch.dict(os.environ):
            os.environ.pop('REPLENISHMENT_DATA_DIR',None)
            self.assertEqual(settings_root(Path.cwd()/'synthetic-appdata'),Path.cwd()/'synthetic-appdata/settings')
    def test_running_error_dialog_real_and_finalized_errors_log_without_widgets(self):
        with tempfile.TemporaryDirectory() as folder:
            code='''import json,sys
from pathlib import Path
from PySide6.QtCore import QTimer,QObject
from PySide6.QtWidgets import QApplication,QMessageBox
import shiboken6
from application_runtime import Runtime,ui_allowed
from readable_ui import failure
app=QApplication([]);r=Runtime(Path(sys.argv[1])/'settings');seen=[]
timer=QTimer(app)
def dismiss():
 box=QApplication.activeModalWidget()
 if isinstance(box,QMessageBox):seen.append(box.windowTitle());box.accept()
timer.timeout.connect(dismiss);timer.start(10)
try:raise ValueError('FIRST-NATIVE-ERROR')
except Exception:r.unexpected(*sys.exc_info())
assert seen==['操作未完成']
r.transition('UI_TEARDOWN');timer.stop();shiboken6.delete(app)
assert not ui_allowed()
try:raise RuntimeError('ORIGINAL-AFTER-QT-ERROR')
except Exception:r.unexpected(*sys.exc_info())
failure(None,'late-window',ValueError('LATE-WIDGET-SUPPRESSED'))
data=r.path.read_text('utf-8');assert 'ORIGINAL-AFTER-QT-ERROR' in data and 'Traceback' in data and 'LATE-WIDGET-SUPPRESSED' in data
print(json.dumps({'passed':True,'real_dialogs':seen,'qt_destroyed':True}));r.restore()
'''
            result=subprocess.run([sys.executable,'-c',code,folder],env={**os.environ,'QT_QPA_PLATFORM':'offscreen'},capture_output=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'));self.assertIn('"passed": true',result.stdout.decode())

class NativeStartingInstance(unittest.TestCase):
    def test_second_launch_waits_for_initial_window_without_second_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);script=root/'waiting_owner.py'
            script.write_text('''import json,sys,os
from pathlib import Path
from PySide6.QtWidgets import QApplication,QWidget
from PySide6.QtCore import QTimer
from single_instance import SingleInstance
root=Path(sys.argv[1]);app=QApplication([]);s=SingleInstance('native-starting-test',folder=root/'locks')
if not s.acquire():raise SystemExit(0)
(root/'owner').write_text(str(os.getpid()))
def window():
 app.w=QWidget();s.bind(app.w);app.w.show()
QTimer.singleShot(500,window)
timer=QTimer(app);timer.timeout.connect(lambda:app.quit() if (root/'stop').exists() else None);timer.start(20)
app.exec();s.close()
''','utf-8')
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen'};args=[sys.executable,str(script),folder]
            first=subprocess.Popen(args,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            try:
                import time
                end=time.monotonic()+10
                while not (root/'owner').exists() and first.poll() is None and time.monotonic()<end:time.sleep(.02)
                self.assertTrue((root/'owner').exists());pid=(root/'owner').read_text();second=subprocess.run(args,env=env,capture_output=True,timeout=18)
                self.assertEqual(second.returncode,0,second.stderr.decode(errors='replace'));self.assertEqual((root/'owner').read_text(),pid);self.assertIsNone(first.poll())
            finally:
                (root/'stop').touch();out,err=first.communicate(timeout=10);self.assertEqual(first.returncode,0,err.decode(errors='replace'))
