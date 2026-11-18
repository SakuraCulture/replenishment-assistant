"""Cached native counters for this process and explicit owned process handles.

No process enumeration, command lines, WMI, psutil, or browser inspection.
CPU/IO are cumulative; callers compute deltas by creation identity.
"""
import os


class Counters:
    def __init__(self):
        self.count = os.cpu_count() or 1
        self.native = os.name == 'nt'
        if not self.native:return
        import ctypes as c
        from ctypes import wintypes as w
        self.c=c;self.w=w
        class Memory(c.Structure):
            _fields_=[('cb',w.DWORD),('faults',w.DWORD)]+[(n,c.c_size_t) for n in
                ('peak_working','working','peak_paged','paged','peak_nonpaged','nonpaged','pagefile','peak_private','private')]
        class IO(c.Structure):
            _fields_=[(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Performance(c.Structure):
            _fields_=[('cb',w.DWORD)]+[(n,c.c_size_t) for n in
                ('commit_total','commit_limit','commit_peak','physical_total','physical_available','system_cache',
                 'kernel_total','kernel_paged','kernel_nonpaged','page_size')]+[('handles',w.DWORD),('processes',w.DWORD),('threads',w.DWORD)]
        self.Memory=Memory;self.IO=IO;self.Performance=Performance
        self.kernel=c.WinDLL('kernel32',use_last_error=True);self.api=c.WinDLL('psapi',use_last_error=True)
        self.kernel.GetCurrentProcess.restype=w.HANDLE
        self.kernel.GetProcessTimes.argtypes=[w.HANDLE]+[c.POINTER(w.FILETIME)]*4
        self.kernel.GetProcessIoCounters.argtypes=[w.HANDLE,c.POINTER(IO)]
        self.kernel.GetSystemTimes.argtypes=[c.POINTER(w.FILETIME)]*3
        self.api.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.POINTER(Memory),w.DWORD]
        self.api.GetPerformanceInfo.argtypes=[c.POINTER(Performance),w.DWORD]

    @staticmethod
    def ticks(t):return (t.dwHighDateTime<<32)|t.dwLowDateTime

    def process(self,pid=None,handle=None):
        pid=os.getpid() if pid is None else pid
        result={'pid':pid}
        if not self.native:
            if pid==os.getpid():
                import time
                result['cpu_seconds']=time.process_time();result['identity']='self'
            return result
        c=self.c;w=self.w
        # A borrowed, live owned handle identifies the actual child. Do not
        # reopen arbitrary/reused PIDs or retain handles past the sample.
        handle=self.kernel.GetCurrentProcess() if pid==os.getpid() else handle
        if handle is None:return result
        created,exited,kernel,user=(w.FILETIME() for _ in range(4))
        if self.kernel.GetProcessTimes(handle,c.byref(created),c.byref(exited),c.byref(kernel),c.byref(user)):
            if self.ticks(exited):return result
            result.update(identity=self.ticks(created),cpu_seconds=(self.ticks(kernel)+self.ticks(user))/1e7)
        else:return result
        memory=self.Memory();memory.cb=c.sizeof(memory)
        if self.api.GetProcessMemoryInfo(handle,c.byref(memory),c.sizeof(memory)):
            result.update(private_bytes=memory.private,working_bytes=memory.working)
        io=self.IO()
        if self.kernel.GetProcessIoCounters(handle,c.byref(io)):
            result.update(read_bytes=io.read_bytes,write_bytes=io.write_bytes)
        return result

    def system(self):
        result={'logical_cpus':self.count}
        if not self.native:return result
        c=self.c;w=self.w
        perf=self.Performance();perf.cb=c.sizeof(perf)
        if self.api.GetPerformanceInfo(c.byref(perf),c.sizeof(perf)):
            for n in ('physical_total','physical_available','commit_total','commit_limit'):
                result[n+'_bytes']=getattr(perf,n)*perf.page_size
        idle,kernel,user=(w.FILETIME() for _ in range(3))
        if self.kernel.GetSystemTimes(c.byref(idle),c.byref(kernel),c.byref(user)):
            result.update(cpu_idle_ticks=self.ticks(idle),cpu_total_ticks=self.ticks(kernel)+self.ticks(user))
        return result
