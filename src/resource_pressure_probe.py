"""Explicit synthetic EXE acceptance: limit only its own multiprocessing child.

This is never loaded by a normal business session. No process is terminated and
no host-wide limit is changed. The retained spawn process handle owns identity.
"""
import ctypes as c
from ctypes import wintypes as w
import time
import re

from work_memory import snapshot


class Probe:
    def __init__(self,profile):
        if profile not in ('ample','tight','occupied'):raise ValueError('Unknown synthetic resource profile')
        self.profile=profile;self.job=None;self.events=[];self.started=None;self.down=False;self.up=False;self.exported=False;self.export_at=None
        self.kernel=c.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[c.c_void_p,w.LPCWSTR];self.kernel.CreateJobObjectW.restype=w.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes=[w.HANDLE,w.HANDLE]
        self.kernel.SetInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD]
        self.kernel.CloseHandle.argtypes=[w.HANDLE]

    def limit(self,process,mib,stage):
        if not process.is_alive():raise RuntimeError('Synthetic worker already exited')
        class Basic(c.Structure):
            _fields_=[('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
                ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(c.Structure):
            _fields_=[(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_=[('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        if self.job is None:
            self.job=self.kernel.CreateJobObjectW(None,None)
            if not self.job or not self.kernel.AssignProcessToJobObject(self.job,process.sentinel):raise c.WinError(c.get_last_error())
        info=Extended();info.basic.flags=0x100;info.process_memory=mib*1024**2
        if not self.kernel.SetInformationJobObject(self.job,9,c.byref(info),c.sizeof(info)):raise c.WinError(c.get_last_error())
        self.events.append({'stage':stage,'pid':process.pid,'at':time.monotonic(),'limit_mib':mib,**snapshot(process.pid)})

    def tick(self,dialog):
        if dialog.operation!='delivery_execute':return
        text=dialog.note.text();process=dialog.session.process;now=time.monotonic()
        encoding='编码' in text and '完成' not in text
        if encoding and self.started is None:
            self.started=now
            if self.profile=='tight':
                self.limit(process,snapshot(process.pid)['private_bytes']//1024**2+128,'encoding-tight');self.down=True
        if self.profile!='occupied' or self.started is None:return
        if encoding and not self.down and now-self.started>2:
            self.limit(process,snapshot(process.pid)['private_bytes']//1024**2+128,'encoding-pressure');self.down=True
        elif self.down and not self.up and now-self.started>12:
            self.limit(process,6000,'encoding-resources-released');self.up=True
        row_progress=re.search(r'已写\s+([\d,]+)\s+/',text)
        if self.up and not self.exported and row_progress and int(row_progress[1].replace(',',''))>=500:
            self.limit(process,snapshot(process.pid)['private_bytes']//1024**2+128,'export-pressure');self.exported=True;self.export_at=now
        elif self.export_at and now-self.export_at>8:
            self.limit(process,6000,'export-resources-released');self.export_at=None

    def close(self):
        if self.job:self.kernel.CloseHandle(self.job);self.job=None
