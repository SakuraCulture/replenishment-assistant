"""Windows process identities. Never log command lines or terminate a process."""
import ctypes,json,os,subprocess
from pathlib import PureWindowsPath,Path

HELPERS={'--catalog-validate','--storage-copy','--catalog-lifetime-helper','--multiprocessing-fork'}

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

def discover():
    if os.name!='nt':return []
    script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -Filter \"Name = '补货助手.exe' OR Name LIKE 'python%.exe'\" | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise ValueError('未能核实其他补货助手进程，请稍后重试')
    raw=result.stdout.decode('utf-8-sig').strip();rows=json.loads(raw) if raw else []
    if isinstance(rows,dict):rows=[rows]
    records=[{'pid':r['ProcessId'],'parent':r['ParentProcessId'],'name':r['Name'],'argv':arguments(r['CommandLine']) if r.get('CommandLine') else []} for r in rows]
    windows=main_windows({r['pid'] for r in records});wrappers=wrapper_ids(records,os.getpid(),windows)
    matches=[]
    for row in records:
        if row['pid']==os.getpid() or row['pid'] in wrappers:continue
        value=classify(row['argv'],row['name'])
        identified=row['name']=='补货助手.exe' or row['pid'] in windows or source_identity(row['argv'])
        if value['application'] and identified:matches.append({'pid':row['pid'],'name':row['name'],'main':value['main']})
    return matches

def foreground_legacy():
    """Activate an older GUI which predates our local activation pipe."""
    if os.name!='nt':return False
    candidates=[p for p in discover() if p['main']]
    if not candidates:return False
    ids={p['pid'] for p in candidates};user=ctypes.windll.user32;found=list(main_windows(ids).values())
    if not found:raise ValueError('已有补货助手主进程正在启动或等待响应，未打开第二个窗口。进程号：'+','.join(str(p) for p in sorted(ids)))
    handle=ctypes.c_void_p(found[0]);user.ShowWindow(handle,9);user.BringWindowToTop(handle);user.SetForegroundWindow(handle)
    return True
