"""Read-only Win32 process-tree working sets; no extra runtime dependencies."""
import ctypes as C
from ctypes import wintypes as W

class Entry(C.Structure):
    _fields_=[('dwSize',W.DWORD),('cntUsage',W.DWORD),('th32ProcessID',W.DWORD),('th32DefaultHeapID',C.c_size_t),('th32ModuleID',W.DWORD),('cntThreads',W.DWORD),('th32ParentProcessID',W.DWORD),('pcPriClassBase',W.LONG),('dwFlags',W.DWORD),('szExeFile',W.WCHAR*260)]

class Memory(C.Structure):
    _fields_=[('cb',W.DWORD),('PageFaultCount',W.DWORD),('PeakWorkingSetSize',C.c_size_t),('WorkingSetSize',C.c_size_t),('QuotaPeakPagedPoolUsage',C.c_size_t),('QuotaPagedPoolUsage',C.c_size_t),('QuotaPeakNonPagedPoolUsage',C.c_size_t),('QuotaNonPagedPoolUsage',C.c_size_t),('PagefileUsage',C.c_size_t),('PeakPagefileUsage',C.c_size_t),('PrivateUsage',C.c_size_t)]

k=C.WinDLL('kernel32',use_last_error=True);ps=C.WinDLL('psapi',use_last_error=True)
k.CreateToolhelp32Snapshot.argtypes=[W.DWORD,W.DWORD];k.CreateToolhelp32Snapshot.restype=W.HANDLE
k.Process32FirstW.argtypes=[W.HANDLE,C.POINTER(Entry)];k.Process32NextW.argtypes=[W.HANDLE,C.POINTER(Entry)]
k.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];k.OpenProcess.restype=W.HANDLE
k.CloseHandle.argtypes=[W.HANDLE]
ps.GetProcessMemoryInfo.argtypes=[W.HANDLE,C.POINTER(Memory),W.DWORD]

def processes():
    h=k.CreateToolhelp32Snapshot(2,0);e=Entry();e.dwSize=C.sizeof(e);out={}
    if h==C.c_void_p(-1).value:raise C.WinError(C.get_last_error())
    try:
        ok=k.Process32FirstW(h,C.byref(e))
        while ok:
            out[int(e.th32ProcessID)]={'parent':int(e.th32ParentProcessID),'name':e.szExeFile};ok=k.Process32NextW(h,C.byref(e))
    finally:k.CloseHandle(h)
    return out

def sample_tree(pid):
    allp=processes();ids={pid}
    while True:
        new=ids|{i for i,p in allp.items() if p['parent'] in ids}
        if new==ids:break
        ids=new
    result={}
    for i in ids:
        h=k.OpenProcess(0x410,False,i)
        if not h:continue
        try:
            m=Memory();m.cb=C.sizeof(m)
            if ps.GetProcessMemoryInfo(h,C.byref(m),m.cb):result[i]={'rss':m.WorkingSetSize,'private':m.PrivateUsage,'name':allp.get(i,{}).get('name','')}
        finally:k.CloseHandle(h)
    return result
