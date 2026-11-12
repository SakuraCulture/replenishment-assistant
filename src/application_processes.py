"""Windows process identities. Never log command lines or terminate a process."""
import ctypes,json,os,subprocess,time
from datetime import datetime,timezone
from pathlib import PureWindowsPath,Path

HELPERS={'--catalog-validate','--storage-copy','--catalog-lifetime-helper','--multiprocessing-fork','--data-location-progress'}

def source_identity(argv):
    for arg in argv[1:]:
        if arg in ('-c','-m'):return False
        if arg.lower().endswith('.py'):
            metadata=Path(arg).resolve().parent/'version.json'
            try:
                if not metadata.is_file() or metadata.stat().st_size>65536:return False
                return json.loads(metadata.read_text('utf-8')).get('app')=='补货助手'
            except (ValueError,OSError,AttributeError):return False
    return False

def classify(argv,name):
    helper=bool(set(argv)&HELPERS)
    if name=='补货助手.exe':return {'application':True,'main':not helper}
    for index,arg in enumerate(argv[1:],1):
        if arg=='-c':return {'application':False,'main':False}
        if arg=='-m':
            matched=index+1<len(argv) and argv[index+1]=='app'
            return {'application':matched,'main':matched and not helper}
        if arg.lower().endswith('.py'):
            matched=PureWindowsPath(arg).name.casefold()=='app.py'
            return {'application':matched,'main':matched and not helper}
    return {'application':False,'main':False}

def arguments(command):
    count=ctypes.c_int();shell=ctypes.windll.shell32
    shell.CommandLineToArgvW.argtypes=[ctypes.c_wchar_p,ctypes.POINTER(ctypes.c_int)];shell.CommandLineToArgvW.restype=ctypes.POINTER(ctypes.c_wchar_p)
    pointer=shell.CommandLineToArgvW(command,ctypes.byref(count))
    if not pointer:raise ValueError('不能识别运行中应用的启动参数')
    try:return [pointer[i] for i in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree.argtypes=[ctypes.c_void_p]
        ctypes.windll.kernel32.LocalFree(ctypes.cast(pointer,ctypes.c_void_p))

def main_windows(ids):
    if os.name!='nt':return {}
    user=ctypes.windll.user32;found={};callback_type=ctypes.WINFUNCTYPE(ctypes.c_bool,ctypes.c_void_p,ctypes.c_void_p)
    def visit(handle,_):
        pid=ctypes.c_ulong();user.GetWindowThreadProcessId(ctypes.c_void_p(handle),ctypes.byref(pid))
        if pid.value not in ids:return True
        title=ctypes.create_unicode_buffer(512);user.GetWindowTextW(ctypes.c_void_p(handle),title,512)
        if title.value.startswith('补货助手') and 'AI启动器' not in title.value:found[pid.value]=handle
        return True
    callback=callback_type(visit);user.EnumWindows(callback,0);return found

def wrapper_ids(records,pid,windows):
    """Windows venv/PyInstaller launchers wait for the actual GUI child.

    Only same-command ancestors without an application window are launchers.
    An independently launched process or a real parent GUI remains protected.
    """
    rows={r['pid']:r for r in records};own=rows.get(pid);result=set()
    if not own:return result
    cursor=own
    for _ in range(8):
        parent=rows.get(cursor.get('parent'))
        if not parent or parent['pid'] in windows or not own['argv'] or parent['argv'][1:]!=own['argv'][1:]:break
        result.add(parent['pid']);cursor=parent
    return result

def process_snapshot():
    """Fresh Toolhelp identities, including thread counts; never opens writers."""
    from ctypes import wintypes as w
    class Entry(ctypes.Structure):
        _fields_=[('size',w.DWORD),('usage',w.DWORD),('pid',w.DWORD),('heap',ctypes.c_size_t),('module',w.DWORD),('threads',w.DWORD),('parent',w.DWORD),('priority',w.LONG),('flags',w.DWORD),('name',w.WCHAR*260)]
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes=[w.DWORD,w.DWORD];kernel.CreateToolhelp32Snapshot.restype=w.HANDLE
    kernel.Process32FirstW.argtypes=[w.HANDLE,ctypes.POINTER(Entry)];kernel.Process32NextW.argtypes=kernel.Process32FirstW.argtypes;kernel.CloseHandle.argtypes=[w.HANDLE]
    handle=kernel.CreateToolhelp32Snapshot(2,0)
    if handle in (None,ctypes.c_void_p(-1).value):raise OSError(ctypes.get_last_error(),'不能核验进程快照')
    result={};entry=Entry();entry.size=ctypes.sizeof(entry)
    try:
        ok=kernel.Process32FirstW(handle,ctypes.byref(entry))
        while ok:
            result[entry.pid]={'parent':entry.parent,'threads':entry.threads,'name':entry.name}
            ok=kernel.Process32NextW(handle,ctypes.byref(entry))
    finally:kernel.CloseHandle(handle)
    return result

def kernel_state(pid):
    """A signaled process is exited even if its exit code happens to be 259."""
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE;kernel.CloseHandle.argtypes=[w.HANDLE]
    kernel.WaitForSingleObject.argtypes=[w.HANDLE,w.DWORD];kernel.WaitForSingleObject.restype=w.DWORD
    handle=kernel.OpenProcess(0x101000,False,pid)
    if not handle:
        error=ctypes.get_last_error()
        return {'state':'EXITED' if error==87 else 'UNKNOWN','error':error}
    try:
        wait=kernel.WaitForSingleObject(handle,0)
        return {'state':'EXITED' if wait==0 else 'RUNNING' if wait==258 else 'UNKNOWN','wait':wait}
    finally:kernel.CloseHandle(handle)

def records():
    if os.name!='nt':return []
    script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -Filter \"Name = '补货助手.exe' OR Name LIKE 'python%.exe'\" | Select-Object ProcessId,ParentProcessId,Name,CommandLine,ExecutablePath,ThreadCount,HandleCount,@{N='CreatedAt';E={$_.CreationDate.ToUniversalTime().ToString('o')}} | ConvertTo-Json -Compress"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError('未能核实其他补货助手进程，请稍后重试')
    raw=result.stdout.decode('utf-8-sig').strip();rows=json.loads(raw) if raw else []
    if isinstance(rows,dict):rows=[rows]
    return [{'pid':r['ProcessId'],'parent':r['ParentProcessId'],'name':r['Name'],'argv':arguments(r['CommandLine']) if r.get('CommandLine') else [],
             'created':r.get('CreatedAt'),'threads':r.get('ThreadCount'),'handles':r.get('HandleCount'),'image':r.get('ExecutablePath')} for r in rows]

def abandoned(row,second,first_native,second_native,windows):
    """Only an old, stable, threadless child with a gone creator can qualify.

    This is NOT an access-denied bypass. Callers may ignore this distinct
    abandoned-create state only while holding the instance or data-area guard.
    Unknown/protected, suspended, fresh and parented processes remain blocking.
    """
    if not second or row.get('created')!=second.get('created') or row.get('parent')!=second.get('parent'):return False
    try:age=(datetime.now(timezone.utc)-datetime.fromisoformat(row['created'].replace('Z','+00:00'))).total_seconds()
    except (ValueError,KeyError,TypeError):return False
    if age<30 or row['pid'] in windows or row.get('image') or second.get('image') or row['argv'] or second['argv']:return False
    if any(r.get('threads')!=0 or r.get('handles')!=0 for r in (row,second)):return False
    for snapshot in (first_native,second_native):
        item=snapshot.get(row['pid'])
        if not item or item['threads']!=0 or item['parent']!=row['parent'] or item['name']!=row['name'] or row['parent'] in snapshot:return False
    return True

def discover(*,guarded=False):
    if os.name!='nt':return []
    current=records();windows=main_windows({r['pid'] for r in current});wrappers=wrapper_ids(current,os.getpid(),windows)
    first_native=None;second_native=None;again=None
    matches=[]
    for row in current:
        if row['pid']==os.getpid() or row['pid'] in wrappers:continue
        value=classify(row['argv'],row['name'])
        identified=row['name']=='补货助手.exe' or row['pid'] in windows or source_identity(row['argv'])
        if not value['application'] or not identified:continue
        # This exact early-dispatch entrypoint can only read a status JSON and
        # show a window. It never opens a repository, lease, browser or writer.
        if '--data-location-progress' in row['argv']:continue
        state=kernel_state(row['pid'])
        if state['state']=='EXITED':continue
        if state['state']=='UNKNOWN' and row.get('threads')==0 and row.get('handles')==0:
            if again is None:
                first_native=process_snapshot();time.sleep(.15);again={r['pid']:r for r in records()};second_native=process_snapshot()
            if abandoned(row,again.get(row['pid']),first_native,second_native,windows):
                state['state']='ABANDONED_CHILD'
                if guarded:continue
        remnant=state['state']=='ABANDONED_CHILD'
        matches.append({'pid':row['pid'],'name':row['name'],'main':value['main'] and not remnant,'state':state['state'],'created':row.get('created'),
                        'role':'abandoned_child' if remnant else 'main' if value['main'] else 'worker','probe':state})
    return matches

def foreground_legacy():
    """Activate an older GUI which predates our local activation pipe."""
    if os.name!='nt':return False
    # SingleInstance invokes this only after acquiring its user-global lock.
    candidates=[p for p in discover(guarded=True) if p['main']]
    if not candidates:return False
    ids={p['pid'] for p in candidates};user=ctypes.windll.user32;found=list(main_windows(ids).values())
    if not found:raise ValueError('已有补货助手主进程正在启动或等待响应，未打开第二个窗口。进程号：'+','.join(str(p) for p in sorted(ids)))
    handle=ctypes.c_void_p(found[0]);user.ShowWindow(handle,9);user.BringWindowToTop(handle);user.SetForegroundWindow(handle)
    return True
