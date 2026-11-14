"""Same-PC whole-data relocation, verified copy, retained archive and old-path junction.

Invoked only after the business window and its owned workers have closed. No
platform calls, schema upgrades, task replays or business-payload rewrites.
"""
import base64,hashlib,json,os,re,shutil,sqlite3,subprocess,sys,time,traceback,uuid,zipfile
from contextlib import ExitStack,closing,contextmanager
from pathlib import Path,PurePosixPath
import data_location as location
from stockout_common import atomic_json,now
from runtime_support import checkpoint

ACTIVE={'PREPARED','WAITING_EXIT','APPLYING'}
SKIP_DIRS={'browsers','recovery','backups','backup','binding_backups','downloads','originals','原件','备份'}

def digest(path,progress=None):
    h=hashlib.sha256()
    with Path(path).open('rb') as file:
        while block:=file.read(1024*1024):
            h.update(block)
            if progress:progress(len(block))
    return h.hexdigest()

def read_json(path):return json.loads(Path(path).read_text('utf-8'))
def config_hash():return digest(location.config_file()) if location.config_file().is_file() else None
def plan_file(nonce):return location.control()/'operations'/(nonce+'.plan.json')

def status():
    path=location.journal_file()
    from data_location_status import read
    return read(path) if path.is_file() else None

def record(plan,state,phase=None,message='',**extra):
    previous=status();previous=previous if previous and previous.get('nonce')==plan['nonce'] else {}
    value={'schema':1,'nonce':plan['nonce'],'dataset_id':plan['dataset_id'],'state':state,'phase':phase or state,
           'source':plan['source'],'target':plan['target'],'backup':plan['backup'],'archive':plan['archive'],
           'plan_file':str(plan_file(plan['nonce'])),'at':now(),'owner_pid':os.getpid(),'message':message,
           'working_directory':str(Path.cwd()),'executable':sys.executable,**extra}
    value['history']=[*previous.get('history',[]),{k:value[k] for k in ('state','phase','at')}][-64:]
    # The viewer is pinned to this operation, even after a later move is prepared.
    from data_location_status import save
    save(location.control()/'operations'/(plan['nonce']+'.status.json'),value)
    save(location.journal_file(),value);return value

class Progress:
    """Bounded telemetry outside the moving data; no Qt or business writes."""
    def __init__(self,plan,phase,message,total=0,files=0):
        self.plan=plan;self.phase=phase;self.message=message;self.total=total;self.total_files=files
        self.done=0;self.files=0;self.last=0;self.tick(force=True)
    def tick(self,amount=0,*,files=None,force=False):
        self.done+=amount
        if files is not None:self.files=files
        if not force and time.monotonic()-self.last<1:return
        record(self.plan,'APPLYING',self.phase,self.message,bytes_done=self.done,bytes_total=self.total,files=self.files,total_files=self.total_files)
        self.last=time.monotonic()

def changed_scope(message,old,new,old_dirs,new_dirs):
    added=sorted(set(new)-set(old));removed=sorted(set(old)-set(new))
    extra_dirs=sorted(set(new_dirs)-set(old_dirs));missing_dirs=sorted(set(old_dirs)-set(new_dirs))
    return ValueError(message+f'；新增文件{len(added)}、减少文件{len(removed)}、新增目录{len(extra_dirs)}、减少目录{len(missing_dirs)}。'
                      +'样例：'+json.dumps({'新增':added[:4],'减少':removed[:4],'新增目录':extra_dirs[:4],'减少目录':missing_dirs[:4]},ensure_ascii=False))

def walk_error(exc):
    # os.walk otherwise silently omits unreadable subtrees from the first list.
    raise OSError('无法完整读取资料目录，未搬迁：'+str(exc)) from exc

@contextmanager
def control_gate():
    folder=location.control();folder.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(folder/'move.guard.sqlite3',timeout=.3,isolation_level=None)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS gate(id INTEGER PRIMARY KEY)')
        try:db.execute('BEGIN EXCLUSIVE')
        except sqlite3.OperationalError as exc:raise ValueError('已有数据位置操作正在执行，请等待原操作结束') from exc
        yield

def validate_base(value):
    from application_reset import validate_root
    raw=location.absolute(value)
    if raw.is_symlink() or raw.is_junction():raise ValueError('请选择普通资料文件夹，不能直接选择兼容链接')
    path=validate_root(raw)
    control=location.control().resolve()
    if path==control or path.is_relative_to(control) or control.is_relative_to(path):raise ValueError('资料目录不能包含启动控制配置')
    return path

def paths(plan):
    if not re.fullmatch('[a-f0-9]{32}',plan.get('nonce','')):raise ValueError('搬迁操作身份无效')
    source=location.absolute(plan['source']);target=location.absolute(plan['target']);n=plan['nonce']
    expected={'backup':source.with_name(source.name+'.旧位置副本-'+n),
              'stage':target.with_name('.'+target.name+'.搬迁中-'+n),
              'archive':target.with_name(target.name+'.恢复副本-'+n+'.zip')}
    for key,path in expected.items():
        if location.absolute(plan[key])!=path or path.is_symlink() or path.is_junction():raise ValueError('搬迁工作目录已变化，未操作')
    stored=read_json(plan_file(n))
    if stored!=plan:raise ValueError('搬迁计划已变化，请重新预览')
    if source==target or source.is_relative_to(target) or target.is_relative_to(source):raise ValueError('新旧资料位置不能相同或互相包含')
    return source,target,expected

def technical(relative):
    p=PurePosixPath(relative)
    return p.name.endswith('-shm') or len(p.parts)==1 and p.name.startswith('.settings.instances.sqlite3')

def files(root,*,marker=False):
    root=Path(root);result=[]
    if root.is_symlink() or root.is_junction():raise ValueError('资料快照目录不能是链接')
    for folder,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
        for name in dirs:
            p=Path(folder)/name
            if p.is_symlink() or p.is_junction():raise ValueError('资料中有目录链接，未跨越链接搬迁：'+str(p))
        for name in names:
            p=Path(folder)/name;relative=p.relative_to(root).as_posix()
            if p.is_symlink():raise ValueError('资料中有文件链接，未搬迁：'+str(p))
            if technical(relative) or relative==location.MARKER and not marker:continue
            if not p.is_file():raise ValueError('资料包含不支持的文件类型：'+str(p))
            result.append((relative,p))
    return sorted(result)

def directories(root):
    result=[]
    for folder,dirs,names in os.walk(root,followlinks=False,onerror=walk_error):
        for name in dirs:
            path=Path(folder)/name
            if path.is_symlink() or path.is_junction():raise ValueError('资料中有目录链接，未搬迁：'+str(path))
            result.append(path.relative_to(root).as_posix())
    return sorted(result)

def browser_rows():
    if os.name!='nt':return []
    script="[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); $ErrorActionPreference='Stop'; Get-CimInstance Win32_Process -Filter \"Name = 'msedge.exe' OR Name = 'chrome.exe'\" | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    p=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',script],capture_output=True,timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode:raise ValueError('未能核实采集浏览器占用，请稍后重试')
    value=json.loads(p.stdout.decode('utf-8-sig') or '[]')
    return [value] if isinstance(value,dict) else value

def require_browsers_closed(source):
    from application_processes import arguments
    busy=[]
    for row in browser_rows():
        command=row.get('CommandLine')
        if not command:busy.append(row['ProcessId']);continue
        args=arguments(command)
        for index,arg in enumerate(args):
            value=arg.split('=',1)[1] if arg.startswith('--user-data-dir=') else args[index+1] if arg=='--user-data-dir' and index+1<len(args) else None
            if value and Path(value).resolve().is_relative_to(source.resolve()):busy.append(row['ProcessId']);break
    if busy:raise ValueError('资料目录中的专用浏览器仍打开，或无法核实浏览器占用。请正常关闭相关采集浏览器后重试；未强制关闭。进程号：'+','.join(map(str,sorted(set(busy)))))

def is_database(path):
    with Path(path).open('rb') as f:return f.read(16)==b'SQLite format 3\0'

def active_database(relative,path):
    parts=PurePosixPath(relative).parts
    return parts[0]=='settings' and not any(p.casefold() in SKIP_DIRS or '备份' in p for p in parts[1:-1]) and is_database(path)

def require_space(parent,needed):
    if shutil.disk_usage(parent).free<needed:raise ValueError(f'新位置空间不足；复制和完整恢复副本至少需约{needed/1024**3:.2f} GiB，原资料未切换')

def require_directory_release(source):
    """Ask Windows for rename permission without renaming or deleting anything."""
    if os.name!='nt':return
    import ctypes
    from ctypes import wintypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.CreateFileW.argtypes=[wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype=wintypes.HANDLE;kernel.CloseHandle.argtypes=[wintypes.HANDLE]
    handle=kernel.CreateFileW(str(source),0x10000,7,None,3,0x02000000,None)
    if handle==ctypes.c_void_p(-1).value:
        error=ctypes.get_last_error()
        raise ValueError(f'Windows尚未允许移动资料目录（错误{error}）；可能有目录占用或权限限制，尚未确认占用者。未切换，原位置保持。')
    kernel.CloseHandle(handle)

def prepare(source,target,progress=lambda _:None,cancel=None):
    checkpoint(cancel);progress('核对新旧资料位置')
    source=validate_base(source);target=validate_base(target)
    if getattr(sys,'frozen',False) and (Path(sys.executable).resolve().is_relative_to(source) or Path(__file__).resolve().is_relative_to(source)):
        raise ValueError('程序文件本身位于待搬迁的资料目录内。请先从该目录之外运行完整程序，未开始搬迁')
    if os.environ.get('REPLENISHMENT_DATA_DIR') or source!=location.base().resolve():raise ValueError('搬迁来源不是当前启用的整套资料位置，未读取或搬迁其他资料')
    if not (source/'settings/workbench.sqlite3').is_file():raise ValueError('所选旧位置不包含当前订单库，未准备搬迁')
    if source==target or source.is_relative_to(target) or target.is_relative_to(source):raise ValueError('请选择与当前资料目录独立的新文件夹')
    if not target.parent.is_dir():raise ValueError('请先选择已存在的上级文件夹，新资料目录可以不存在')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):raise ValueError('新资料文件夹必须为空，不会覆盖或混合已有资料')
    require_browsers_closed(source);checkpoint(cancel)
    with control_gate():
        prior=status()
        if prior and prior['state'] in ACTIVE:raise ValueError('已有未完成的数据位置操作，请先查看原操作结果')
        count=0;size=0
        for _,file in files(source):
            checkpoint(cancel);count+=1;size+=file.stat().st_size
            if count%100==0:progress(f'已核对{count}份资料，约{size/1024**3:.2f} GiB')
        require_space(target.parent,size*2+128*1024**2)
        config=location.config();nonce=uuid.uuid4().hex
        plan={'schema':1,'nonce':nonce,'dataset_id':config['dataset_id'] if config else uuid.uuid4().hex,
              'source':str(source),'target':str(target),'backup':str(source.with_name(source.name+'.旧位置副本-'+nonce)),
              'stage':str(target.with_name('.'+target.name+'.搬迁中-'+nonce)),
              'archive':str(target.with_name(target.name+'.恢复副本-'+nonce+'.zip')),
              'anchor_base':config['anchor_base'] if config else str(source),'before_config':config,'before_config_sha256':config_hash(),
              'created':now(),'estimated_files':count,'estimated_bytes':size}
        plan_file(nonce).parent.mkdir(parents=True,exist_ok=True)
        with plan_file(nonce).open('x',encoding='utf-8') as f:json.dump(plan,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
        record(plan,'PREPARED',message='已核对位置，尚未切换')
        try:checkpoint(cancel)
        except BaseException:record(plan,'CANCELLED',message='已取消位置准备，原资料保持');raise
        return plan

def cancel(plan):paths(plan);record(plan,'CANCELLED',message='用户取消，资料位置不变')

def _copy(plan,source,stage,entries):
    stage.mkdir(exist_ok=False);manifest={};total=sum(p.stat().st_size for _,p in entries)
    progress=Progress(plan,'COPYING','复制资料并逐文件核对（包含原件与副本读取）',total*3,len(entries))
    folders=directories(source)
    for name in folders:(stage/name).mkdir(parents=True,exist_ok=True)
    for number,(name,file) in enumerate(entries,1):
        target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);h=hashlib.sha256()
        with file.open('rb') as a,target.open('xb') as b:
            while block:=a.read(1024*1024):b.write(block);h.update(block);progress.tick(len(block))
            b.flush();os.fsync(b.fileno())
        value=h.hexdigest()
        if digest(file,progress.tick)!=value or digest(target,progress.tick)!=value:raise ValueError('资料在复制期间发生变化，未切换：'+name)
        manifest[name]={'sha256':value,'bytes':target.stat().st_size}
        progress.tick(files=number,force=number==len(entries))
    fresh=files(source)
    fresh_names={n for n,_ in fresh};fresh_dirs=directories(source)
    if fresh_names!=set(manifest) or fresh_dirs!=folders:raise changed_scope('资料文件范围在复制期间变化，未切换',manifest,fresh_names,folders,fresh_dirs)
    progress=Progress(plan,'CHECKING','复核原资料内容',total,len(entries))
    for number,(name,file) in enumerate(fresh,1):
        if digest(file,progress.tick)!=manifest[name]['sha256']:raise ValueError('原资料在校验期间变化，未切换：'+name)
        progress.tick(files=number,force=number==len(fresh))
    progress=Progress(plan,'CHECKING','核对数据库完整性；此步骤无法按字节估算百分比',files=len(entries))
    from relocation_integrity import verify_database
    for number,(name,_) in enumerate(entries,1):
        file=stage/name
        if is_database(file):
            progress.message='核对数据库完整性（独立校验副本）：'+name
            progress.tick(force=True)
            verify_database(file,name,stage.parent,progress.tick)
        progress.tick(files=number,force=number==len(entries))
    progress=Progress(plan,'CHECKING','数据库检查后再次核对全部副本',total,len(entries))
    for number,(name,meta) in enumerate(manifest.items(),1):
        if digest(stage/name,progress.tick)!=meta['sha256']:raise ValueError('资料副本校验后发生变化，未切换：'+name)
        progress.tick(files=number,force=number==len(manifest))
    atomic_json(location.control()/'operations'/(plan['nonce']+'.files.json'),{'files':manifest,'directories':folders})
    atomic_json(stage/location.MARKER,{'schema':1,'dataset_id':plan['dataset_id'],'operation':plan['nonce'],'anchor_base':plan['anchor_base']})

def verify_source(plan,source):
    snapshot=read_json(location.control()/'operations'/(plan['nonce']+'.files.json'))
    current=files(source)
    names={n for n,_ in current};folders=directories(source)
    if names!=set(snapshot['files']) or folders!=snapshot['directories']:raise changed_scope('原资料范围在切换前变化，未切换',snapshot['files'],names,snapshot['directories'],folders)
    progress=Progress(plan,'SWITCH_RECHECK','切换前最终核对原资料',sum(v['bytes'] for v in snapshot['files'].values()),len(current))
    for number,(name,file) in enumerate(current,1):
        if digest(file,progress.tick)!=snapshot['files'][name]['sha256']:raise ValueError('原资料在切换前变化，未切换：'+name)
        progress.tick(files=number,force=number==len(current))

def _config(plan):
    old=plan['before_config'] or {};names=[i['from'] for i in old.get('aliases',[])]+[plan['source']]
    aliases=[{'from':p,'to':plan['target']} for p in dict.fromkeys(names)]
    if len(aliases)>32:raise ValueError('旧位置兼容链已过长，请先整理旧位置记录')
    return {'schema':1,'dataset_id':plan['dataset_id'],'active_base':plan['target'],'anchor_base':plan['anchor_base'],
            'aliases':aliases,'operation':plan['nonce'],'changed':now()}

def backup_manifest(plan):return location.control()/'operations'/(plan['nonce']+'.backup.json')

def pin_backup(plan):
    _,_,p=paths(plan);entries=files(p['backup'],marker=True)
    progress=Progress(plan,'ARCHIVE_PRECHECK','记录完整恢复副本依据',sum(f.stat().st_size for _,f in entries),len(entries))
    saved={}
    for number,(name,file) in enumerate(entries,1):
        saved[name]={'sha256':digest(file,progress.tick),'bytes':file.stat().st_size};progress.tick(files=number,force=number==len(entries))
    value={'format':'replenishment-data-location/1','nonce':plan['nonce'],'source':plan['source'],
           'files':saved,
           'directories':directories(p['backup'])}
    atomic_json(backup_manifest(plan),value);return value

def archive_backup(plan,progress=lambda _:None,cancel=None):
    _,target,p=paths(plan);backup=p['backup'];archive=p['archive'];manifest_file=location.control()/'operations'/(plan['nonce']+'.backup.json')
    pinned=read_json(manifest_file);checkpoint(cancel)
    if archive.is_file():
        metadata=verify_archive(archive,plan,True);manifest=metadata['files'];folders=metadata['directories']
    else:
        if not backup.is_dir():raise ValueError('旧位置副本不存在，未执行清理')
        entries=files(backup,marker=True);folders=pinned['directories'];manifest=pinned['files']
        telemetry=Progress(plan,'ARCHIVE_PRECHECK','归档前复核完整恢复副本',sum(v['bytes'] for v in manifest.values()),len(entries))
        if {n for n,_ in entries}!=set(manifest) or directories(backup)!=folders or any(digest(file,telemetry.tick)!=manifest[name]['sha256'] for name,file in entries):raise ValueError('旧副本已变化，未归档或清理；请先核对旧副本')
        require_space(archive.parent,sum(v['bytes'] for v in manifest.values())+64*1024**2)
        temporary=archive.with_name(archive.name+'.partial')
        if temporary.exists():
            if temporary.is_symlink() or temporary.is_junction() or not temporary.is_file():raise ValueError('未完成恢复副本路径已变化，未操作')
            # Its full, pinned source is still intact; only this exact owned
            # incomplete archive can be discarded before a fresh archive attempt.
            temporary.unlink()
        telemetry=Progress(plan,'ARCHIVING','正在把完整恢复副本保存到新盘',sum(v['bytes'] for v in manifest.values()),len(entries))
        with zipfile.ZipFile(temporary,'x',zipfile.ZIP_DEFLATED,compresslevel=3,allowZip64=True) as z:
            z.writestr('manifest.json',json.dumps(pinned,ensure_ascii=False))
            for name in folders:z.writestr('data/'+name+'/',b'')
            for index,(name,file) in enumerate(entries,1):
                with file.open('rb') as source,z.open('data/'+name,'w',force_zip64=True) as destination:
                    while block:=source.read(1024*1024):checkpoint(cancel);destination.write(block);telemetry.tick(len(block))
                telemetry.tick(files=index,force=index==len(entries))
                progress(f'正在归档恢复副本 {index}/{len(entries)}份')
        with temporary.open('r+b') as saved:saved.flush();os.fsync(saved.fileno())
        verify_archive(temporary,plan,True)
        if archive.exists():raise ValueError('恢复副本目标已存在，未覆盖')
        temporary.rename(archive)
    # Destruction is limited to the recorded retired sibling after a full archive.
    # No rmtree: unknown, changed or newly added files are never recursively deleted.
    current=files(backup,marker=True) if backup.exists() else []
    telemetry=Progress(plan,'CLEANUP_CHECK','核对旧副本可清理范围',sum(f.stat().st_size for _,f in current),len(current))
    if any(name not in manifest or digest(file,telemetry.tick)!=manifest[name]['sha256'] for name,file in current) or backup.exists() and set(directories(backup))-set(folders):raise ValueError('旧副本含新增或变化文件，未清理；完整恢复副本保留')
    telemetry=Progress(plan,'CLEANING_OLD','恢复副本已校验，正在释放旧位置副本占用',sum(f.stat().st_size for _,f in current),len(current))
    for number,(name,file) in enumerate(current,1):
        checkpoint(cancel)
        if file.is_symlink() or digest(file,telemetry.tick)!=manifest[name]['sha256']:raise ValueError('旧副本在清理期间变化，已停止；完整恢复副本保留')
        file.unlink()
        telemetry.tick(files=number,force=number==len(current))
    # Instance-lease databases and SHM are technical files, not transferred facts.
    # Leave them and any new files in place rather than guessing or deleting.
    if backup.exists():
        for name in sorted(folders,key=lambda n:len(PurePosixPath(n).parts),reverse=True):
            path=backup/name
            if not path.exists():continue
            if not path.is_junction() and not path.is_symlink() and not any(path.iterdir()):path.rmdir()
        if not any(backup.iterdir()):backup.rmdir()
    # Known directories containing only excluded SHM/instance-lease files are
    # retained technical residue, not an unarchived business recovery copy.
    remains=backup.exists() and bool(files(backup,marker=True) or set(directories(backup))-set(folders))
    telemetry=Progress(plan,'CLEANUP_CHECK','记录已验证恢复归档的最终摘要',archive.stat().st_size,1)
    archive_hash=digest(archive,telemetry.tick);telemetry.tick(files=1,force=True)
    return {'archive':str(archive),'archive_sha256':archive_hash,'old_copy_remains':remains,
            'retained_technical_directory':str(backup) if backup.exists() and not remains else None}

def verify_archive(archive,plan,report=False):
    with zipfile.ZipFile(archive) as z:
        raw=z.read('manifest.json');value=json.loads(raw)
        if value.get('format')!='replenishment-data-location/1' or value.get('nonce')!=plan['nonce']:raise ValueError('恢复副本身份不一致，未清理旧资料')
        if value!=read_json(backup_manifest(plan)):raise ValueError('恢复副本与搬迁时保留的清单不一致，未清理')
        manifest=value['files'];folders=value['directories']
        telemetry=Progress(plan,'ARCHIVE_VERIFY','逐文件核对恢复归档内容',sum(v['bytes'] for v in manifest.values()),len(manifest)) if report else None
        if len(folders)!=len(set(folders)):raise ValueError('恢复副本目录重复')
        for name in folders:
            path=PurePosixPath(name)
            if path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:raise ValueError('恢复副本目录无效')
        if len(z.namelist())!=len(set(z.namelist())) or set(z.namelist())!={'manifest.json',*('data/'+n for n in manifest),*('data/'+n+'/' for n in folders)}:raise ValueError('恢复副本文件范围不一致')
        for number,(name,meta) in enumerate(manifest.items(),1):
            p=PurePosixPath(name)
            if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:raise ValueError('恢复副本路径无效')
            h=hashlib.sha256();size=0
            with z.open('data/'+name) as file:
                while block:=file.read(1024*1024):
                    h.update(block);size+=len(block)
                    if telemetry:telemetry.tick(len(block))
            if h.hexdigest()!=meta['sha256'] or size!=meta['bytes']:raise ValueError('恢复副本内容校验失败，未清理旧资料')
            if telemetry:telemetry.tick(files=number,force=number==len(manifest))
    return value

def _committed(plan):
    value=location.config()
    return bool(value and value.get('operation')==plan['nonce'] and value.get('active_base')==plan['target'])

def _rollback(plan):
    source,target,p=paths(plan)
    if _committed(plan):return
    if source.is_junction():
        from directory_links import remove
        remove(source,target)
    if p['backup'].exists():
        if source.exists():raise ValueError('原资料位置出现其他内容，未覆盖；旧副本与新副本均保留')
        p['backup'].rename(source)
    if not source.is_dir():raise ValueError('原资料位置未能恢复，请保留全部副本并查看搬迁记录')

def apply(plan):
    source,target,p=paths(plan)
    try:current=Path.cwd().resolve()
    except FileNotFoundError:current=source
    if any(current==root or current.is_relative_to(root) for root in (source,target,p['stage'],p['backup'])):
        safe=location.control();safe.mkdir(parents=True,exist_ok=True);os.chdir(safe)
    from application_reset import _exclusive,_check_other_processes
    try:
        with control_gate():
            if config_hash()!=plan['before_config_sha256']:raise ValueError('数据位置配置已变化，未切换')
            if os.environ.get('REPLENISHMENT_DATA_DIR') or source!=location.base().resolve():raise ValueError('当前启用的资料位置与搬迁计划不同，未执行')
            validate_base(source);validate_base(target)
            if target.exists() and any(target.iterdir()) or p['stage'].exists() or p['backup'].exists():raise ValueError('搬迁目录已变化，未覆盖已有资料')
            require_browsers_closed(source);require_directory_release(source);record(plan,'APPLYING','WAITING_LOCKS','核对当前资料占用')
            with _exclusive(source/'settings'),ExitStack() as stack:
                _check_other_processes();entries=files(source)
                initial_names={name for name,_ in entries};initial_folders=directories(source);guard_files=set()
                require_space(target.parent,sum(file.stat().st_size for _,file in entries)*2+128*1024**2)
                for relative,file in entries:
                    if active_database(relative,file):
                        wal=Path(str(file)+'-wal');had_wal=wal.exists()
                        db=stack.enter_context(closing(sqlite3.connect(file.resolve().as_uri()+'?mode=rw',uri=True,timeout=.3,isolation_level=None)))
                        # A read guard must not checkpoint away the original WAL
                        # when it closes, before the verified directory rename.
                        db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE,True)
                        try:db.execute('BEGIN EXCLUSIVE')
                        except sqlite3.OperationalError as exc:raise ValueError('资料库仍被原任务或其他窗口占用，未搬迁：'+relative) from exc
                        if not had_wal and wal.is_file():guard_files.add(relative+'-wal')
                # Opening a clean WAL-mode DB creates its WAL even without a
                # business write. Freeze after acquiring guards; only these
                # known sidecars may extend the first census. Other changes
                # still fail, and every WAL byte receives normal validation.
                guarded=files(source);guarded_names={name for name,_ in guarded};guarded_folders=directories(source)
                if guarded_names!=initial_names|guard_files or guarded_folders!=initial_folders:
                    raise changed_scope('核对占用时资料范围发生外部变化，未切换',initial_names|guard_files,guarded_names,initial_folders,guarded_folders)
                entries=guarded
                require_space(target.parent,sum(file.stat().st_size for _,file in entries)*2+128*1024**2)
                _copy(plan,source,p['stage'],entries)
                require_browsers_closed(source);_check_other_processes()
                verify_source(plan,source)
            if config_hash()!=plan['before_config_sha256']:raise ValueError('数据位置配置已变化，未切换')
            require_directory_release(source)
            if target.exists():target.rmdir()  # Exact empty destination only.
            record(plan,'APPLYING','TARGET_READY','副本校验完成，准备切换资料位置')
            p['stage'].rename(target)
            record(plan,'APPLYING','SOURCE_RENAME','保留旧位置完整副本')
            try:source.rename(p['backup'])
            except PermissionError as exc:raise ValueError('Windows拒绝改名旧资料目录，可能仍有本程序资源或其他程序占用，尚未确认占用者。未切换，原位置保持；请保留诊断后正常关闭占用程序再重试。') from exc
            # SQLite locks must be released for a Windows directory rename.
            # Detect a writer that committed and closed in that short interval;
            # never activate the older copy over its newly committed facts.
            verify_source(plan,p['backup'])
            pin_backup(plan)
            from directory_links import create
            record(plan,'APPLYING','LINKING','保留历史文件路径兼容')
            create(source,target)
            atomic_json(location.config_file(),_config(plan))
            record(plan,'APPLYING','CONFIG_COMMITTED','新位置已启用，正在保留恢复副本')
            try:
                result=archive_backup(plan)
                phase='CLEANUP_PENDING' if result['old_copy_remains'] else 'COMPLETED'
                return record(plan,'COMPLETED',phase,'数据已搬迁，完整恢复副本保留',result=result)
            except BaseException as exc:
                return record(plan,'COMPLETED','CLEANUP_PENDING','新位置已启用；旧位置副本尚待归档或清理',error={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()})
    except BaseException as exc:
        try:_rollback(plan)
        except BaseException as rollback:
            record(plan,'FAILED','RECOVERY_REQUIRED','未能自动恢复原位置，请保留全部副本',error={'message':str(exc),'traceback':traceback.format_exc()},rollback_error=str(rollback));raise
        record(plan,'FAILED','ROLLED_BACK','未切换，继续使用原位置；迁移材料保留',error={'type':type(exc).__name__,'message':str(exc),'traceback':''.join(traceback.format_exception(type(exc),exc,exc.__traceback__))})
        raise

def recover():
    value=status()
    if not value or value['state'] not in ACTIVE:return value
    plan=read_json(plan_file(value['nonce']));source,target,p=paths(plan)
    with control_gate():
        if _committed(plan):
            location.base()  # Missing configured drive must not fall back or create data.
            if not source.exists():
                from directory_links import create
                create(source,target)
            if not source.is_junction() or source.resolve()!=target.resolve():raise ValueError('旧位置兼容链接已变化，请保留现场；未打开业务资料')
            return record(plan,'COMPLETED','CLEANUP_PENDING','搬迁已生效；旧位置副本保留，请核对后继续归档清理')
        _rollback(plan)
        return record(plan,'FAILED','ROLLED_BACK','上次搬迁未完成，已保留并恢复原位置；不会自动重放复制',error={'message':'进程在切换完成前中断；原位置已恢复，副本保留'})

def retry_cleanup(progress=lambda _:None,cancel=None):
    value=status()
    if not value or value['state']!='COMPLETED':raise ValueError('没有已完成搬迁的旧位置副本可清理')
    plan=read_json(plan_file(value['nonce']));paths(plan)
    if not _committed(plan):raise ValueError('当前资料位置已变化，未清理旧副本')
    with control_gate():
        try:
            result=archive_backup(plan,progress,cancel)
            return record(plan,'COMPLETED','CLEANUP_PENDING' if result['old_copy_remains'] else 'COMPLETED','完整恢复副本保留，旧位置清理已处理',result=result)
        except BaseException as exc:
            record(plan,'COMPLETED','CLEANUP_PENDING','旧副本归档或清理未完成；新位置与已有恢复副本保持',error={'message':str(exc),'traceback':traceback.format_exc()});raise
