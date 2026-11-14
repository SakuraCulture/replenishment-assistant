"""Qt/native reporting must survive worker messages and QApplication teardown."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class NativeDiagnostics(unittest.TestCase):
    def execute(self,code):
        with tempfile.TemporaryDirectory() as folder:
            env={**os.environ,'QT_QPA_PLATFORM':'offscreen','LOCALAPPDATA':folder,'PYTHONUTF8':'1'}
            env.pop('PYTHONFAULTHANDLER',None)
            result=subprocess.run([sys.executable,'-c',code,folder],env=env,capture_output=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
            self.assertIn('PASSED',result.stdout.decode())

    def test_qt_messages_and_native_stacks_have_durable_non_widget_evidence(self):
        self.execute(r'''
import faulthandler,json,sys,threading
from pathlib import Path
from PySide6.QtCore import qWarning
from PySide6.QtWidgets import QApplication
import shiboken6
from application_runtime import Runtime
root=Path(sys.argv[1]);app=QApplication([]);r=Runtime(root/'settings');r.bind_qt(app)
native=r.native_diagnostics
assert faulthandler.is_enabled() and native.owns_fault_handler
assert native.path.is_relative_to(root/'YoutuanTools'/'data-location'/'diagnostics')
qWarning('SYNTHETIC-GUI-QT-WARNING')
thread=threading.Thread(target=lambda:qWarning('SYNTHETIC-WORKER-QT-WARNING'));thread.start();thread.join()
faulthandler.dump_traceback(file=native.file,all_threads=True)
assert 'Current thread' in native.path.read_text('utf-8')
r.transition('UI_TEARDOWN');shiboken6.delete(app)
qWarning('SYNTHETIC-AFTER-QT-WARNING')
events=[json.loads(line) for line in r.path.read_text('utf-8').splitlines()]
messages=[x for x in events if x['event']=='QT_MESSAGE']
assert len(messages)==3 and all(x['python_stack'] for x in messages)
assert messages[-1]['phase']=='UI_TEARDOWN'
r.restore();assert not faulthandler.is_enabled() and native.file is None
print('PASSED')
''')

    def test_preexisting_native_and_qt_handlers_remain_usable(self):
        self.execute(r'''
import faulthandler,sys
from pathlib import Path
from PySide6.QtCore import qWarning,qInstallMessageHandler
from PySide6.QtWidgets import QApplication
import shiboken6
from application_runtime import Runtime
root=Path(sys.argv[1]);old_file=(root/'caller-faults.log').open('wb')
faulthandler.enable(file=old_file,all_threads=True);seen=[]
previous=qInstallMessageHandler(lambda kind,context,text:seen.append(text))
app=QApplication([]);r=Runtime(root/'settings');r.bind_qt(app)
assert not r.native_diagnostics.owns_fault_handler and r.native_diagnostics.file is None
qWarning('during');r.transition('UI_TEARDOWN');shiboken6.delete(app);r.restore();qWarning('after')
assert seen==['during','after'] and faulthandler.is_enabled()
faulthandler.disable();old_file.close();qInstallMessageHandler(previous)
print('PASSED')
''')

    def test_native_log_setup_failure_preserves_original_error_and_qt_handler(self):
        self.execute(r'''
import faulthandler,sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import qWarning
from PySide6.QtWidgets import QApplication
import shiboken6
from application_runtime import Runtime
root=Path(sys.argv[1]);app=QApplication([]);r=Runtime(root/'settings')
with patch('faulthandler.enable',side_effect=OSError('SYNTHETIC-FAULT-LOG-FAILURE')):r.bind_qt(app)
assert not faulthandler.is_enabled() and r.native_diagnostics.file is None
qWarning('logging-still-works');text=r.path.read_text('utf-8')
assert 'SYNTHETIC-FAULT-LOG-FAILURE' in text and 'logging-still-works' in text
shiboken6.delete(app);r.restore();print('PASSED')
''')


if __name__=='__main__':unittest.main()
