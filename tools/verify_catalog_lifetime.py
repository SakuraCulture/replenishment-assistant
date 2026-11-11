"""New-directory-only desktop crash/reopen acceptance, zero platform actions."""
import argparse,hashlib,json,os,subprocess,sys,time,uuid,ctypes
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2),'utf-8')


def wait_for_exit(pid,seconds):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_bool,ctypes.c_ulong];kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong];kernel.WaitForSingleObject.restype=ctypes.c_ulong
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=kernel.OpenProcess(0x100000,False,pid)
    if not handle:
        if ctypes.get_last_error()==87:return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if kernel.WaitForSingleObject(handle,int(seconds*1000))!=0:raise TimeoutError('Owned Node did not drain; not terminated')
    finally:kernel.CloseHandle(handle)


def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');out.mkdir(parents=True,exist_ok=False)
    folder=out/'settings/jst_catalog_sync';folder.mkdir(parents=True)
    store_id=uuid.uuid4().hex;original='SYNTHETIC-ORIGINAL-TASK'
    data={'schemaVersion':1,'storeId':store_id,'assets':{},'accounts':{},'batches':{original:{'id':original,'state':'NEEDS_ATTENTION','createdAt':'2026-11-10T08:00:00Z','purpose':'default_catalog','items':[{'id':'SYNTHETIC-ITEM','account':{'warehouse':'山东仓','expectedCompany':'隔离合成仓'},'stage':'ACCEPTED','native':{'nativeTaskRef':'SYNTHETIC-EXACT-RECEIPT','submissions':1,'stage':'ACCEPTED'},'receipt':None,'adoption':None,'error':None}]}}}
    write(folder/'jobs.json',data);write(folder/'store.guard.json',{'schemaVersion':1,'storeId':store_id})
    sha=hashlib.sha256((folder/'jobs.json').read_bytes()).hexdigest()
    write(out/'fixture.json',{'synthetic':True,'live_platform':False,'original_id':original,'jobs_sha256':sha})
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1'}
    for key in ('PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(key,None)
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'helper.log').open('w',encoding='utf-8') as log:
        helper=subprocess.run(command+['--catalog-lifetime-helper',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW,timeout=30)
    assert helper.returncode==23,'Expected an explicit synthetic idle-parent crash'
    ids=json.loads((out/'parent-started.json').read_text('utf-8'));wait_for_exit(ids['child_pid'],10)
    assert not (folder/'service-owner.json').exists()
    assert hashlib.sha256((folder/'jobs.json').read_bytes()).hexdigest()==sha
    write(out/'parent-crash-recovery.json',{'passed':True,'simulated_parent_exit':23,'frozen':ids['frozen'],'owned_child_exited':True,'jobs_unchanged':True,'pids':ids,'live_platform':False})
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command+['--catalog-lifetime-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        began=time.monotonic()
        while process.poll() is None:
            if time.monotonic()-began>180:
                write(out/'still-running.json',{'pid':process.pid,'not_terminated':True});raise TimeoutError('Owned UI still active; not terminated')
            time.sleep(.2)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report.update(process_exit=process.returncode,parent_crash_recovered=True)
    if a.exe:
        with a.exe.open('rb') as stream:report['exe_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    report['accepted']=report['passed'] and process.returncode==0
    write(out/'process-acceptance.json',report);print(json.dumps(report,ensure_ascii=False));return 0 if report['accepted'] else 1


if __name__=='__main__':sys.exit(main())
