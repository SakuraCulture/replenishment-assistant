"""UI-free Qt/native diagnostics retained until QApplication is destroyed."""
import faulthandler
import os
import sys
import time
import traceback
from pathlib import Path


class Diagnostics:
    def __init__(self, runtime, folder):
        self.runtime=runtime;self.closed=False;self.file=None;self.owns_fault_handler=False
        from PySide6.QtCore import qInstallMessageHandler
        self.previous=qInstallMessageHandler(self.qt_message)
        try:
            # Keep the descriptor outside movable business data. The file is
            # normally empty; fault output contains Python stacks, not a dump
            # of business payloads. Never replace a caller's existing handler.
            if not faulthandler.is_enabled():
                folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
                self.path=folder/f'native-{os.getpid()}-{time.time_ns()}.log'
                self.file=self.path.open('xb',buffering=0)
                faulthandler.enable(file=self.file,all_threads=True)
                self.owns_fault_handler=True
                runtime.emit('NATIVE_DIAGNOSTICS',path=str(self.path))
        except Exception:
            runtime.emit('NATIVE_DIAGNOSTICS_FAILED',traceback=traceback.format_exc())
            if self.file is not None:self.file.close();self.file=None

    def qt_message(self,kind,context,message):
        # A Qt warning/fatal handler can run on any thread and during teardown.
        # It must never create, inspect or call a widget.
        try:
            if not self.closed and getattr(kind,'name',str(kind)) in ('QtWarningMsg','QtCriticalMsg','QtFatalMsg'):
                self.runtime.emit('QT_MESSAGE',kind=getattr(kind,'name',str(kind)),
                    message=str(message)[:16000],file=getattr(context,'file',None),
                    line=getattr(context,'line',None),function=getattr(context,'function',None),
                    python_stack=''.join(traceback.format_stack(limit=24)))
        except BaseException:pass
        try:
            if self.previous is not None:self.previous(kind,context,message)
            elif sys.__stderr__ is not None:sys.__stderr__.write(str(message)+'\n')
        except BaseException:pass

    def close(self):
        if self.closed:return
        self.closed=True
        from PySide6.QtCore import qInstallMessageHandler
        qInstallMessageHandler(self.previous)
        if self.owns_fault_handler:
            faulthandler.disable();self.owns_fault_handler=False
        if self.file is not None:self.file.close();self.file=None
