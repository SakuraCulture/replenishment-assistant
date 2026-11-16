"""Small, best-effort native process/system counters; no payload inspection."""
import os
import time


def snapshot(pid=None):
    value = {'pid': os.getpid() if pid is None else pid, 'monotonic': time.monotonic()}
    if os.name != 'nt': return value
    import ctypes as c
    from ctypes import wintypes as w
    class Counters(c.Structure):
        _fields_ = [('cb', w.DWORD), ('faults', w.DWORD)] + [(n, c.c_size_t) for n in
            ('peak_working', 'working', 'peak_paged', 'paged', 'peak_nonpaged', 'nonpaged', 'pagefile', 'peak_private', 'private')]
    class Performance(c.Structure):
        _fields_ = [('cb', w.DWORD)] + [(n, c.c_size_t) for n in
            ('commit_total','commit_limit','commit_peak','physical_total','physical_available',
             'system_cache','kernel_total','kernel_paged','kernel_nonpaged','page_size')] + [
            ('handles',w.DWORD),('processes',w.DWORD),('threads',w.DWORD)]
    kernel=c.WinDLL('kernel32',use_last_error=True); api=c.WinDLL('psapi',use_last_error=True)
    kernel.GetCurrentProcess.restype=w.HANDLE
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.CloseHandle.argtypes=[w.HANDLE]
    api.GetProcessMemoryInfo.argtypes=[w.HANDLE,c.POINTER(Counters),w.DWORD]
    api.GetPerformanceInfo.argtypes=[c.POINTER(Performance),w.DWORD]
    counters=Counters(); counters.cb=c.sizeof(counters)
    handle=kernel.GetCurrentProcess() if pid is None else kernel.OpenProcess(0x1010,False,pid)
    if handle and api.GetProcessMemoryInfo(handle,c.byref(counters),c.sizeof(counters)):
        value.update(working_bytes=counters.working,peak_working_bytes=counters.peak_working,
                     private_bytes=counters.private,peak_private_bytes=counters.peak_private)
    if pid is not None and handle:kernel.CloseHandle(handle)
    perf=Performance(); perf.cb=c.sizeof(perf)
    if api.GetPerformanceInfo(c.byref(perf),c.sizeof(perf)):
        for name in ('commit_total','commit_limit','commit_peak','physical_total','physical_available'):
            value[name+'_bytes']=getattr(perf,name)*perf.page_size
    if pid is None:
        # Respect process limits imposed by a host or an isolated QA Job Object.
        class Basic(c.Structure):
            _fields_ = [('process_time',c.c_int64),('job_time',c.c_int64),('flags',w.DWORD),
                ('minimum',c.c_size_t),('maximum',c.c_size_t),('active',w.DWORD),
                ('affinity',c.c_size_t),('priority',w.DWORD),('scheduling',w.DWORD)]
        class IO(c.Structure):
            _fields_ = [(n,c.c_uint64) for n in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(c.Structure):
            _fields_ = [('basic',Basic),('io',IO),('process_memory',c.c_size_t),('job_memory',c.c_size_t),
                ('peak_process',c.c_size_t),('peak_job',c.c_size_t)]
        kernel.QueryInformationJobObject.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p]
        info=Extended()
        if kernel.QueryInformationJobObject(None,9,c.byref(info),c.sizeof(info),None) and info.basic.flags & 0x100:
            value['process_limit_bytes']=info.process_memory
    return value
