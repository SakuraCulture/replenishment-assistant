"""Launch each isolated Windows Qt case and retain working set, user-visible result and source."""
import argparse
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from benchmark_export_exe import working_set_tree

def read(f):
    return json.loads(f.read_text('utf-8-sig'))

def write(f,v):
    f.write_text(json.dumps(v,ensure_ascii=False,indent=2),'utf8')

def free_ram():
    class Status(ctypes.Structure):
        _fields_=[('length',ctypes.c_uint32),('load',ctypes.c_uint32)]+[(k,ctypes.c_ulonglong) for k in
             ('total','free','page','page_free','virtual','virtual_free','extended')]
    v=Status()
    v.length=ctypes.sizeof(v)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(v)):raise ctypes.WinError()
    return v.free

def exercise(exe,case,root,fixture,template,*,reopen=False):
    if not reopen:
        root.mkdir(parents=True,exist_ok=False)
        shutil.copytree(fixture/'settings',root/'settings')
        shutil.copytree(fixture/'inputs',root/'inputs')
        shutil.copyfile(template,root/'inputs/controlled-template.xlsx')
        shutil.copyfile(fixture/'synthetic.json',root/'synthetic.json')
        ax=root/'settings/aoxiang'
        ax.mkdir(exist_ok=True)
        write(ax/'suppliers.json',{'山东仓':'SD-SUP','河北仓':'HB-SUP'})
        if case in ('race','opening'):
            from special_fixture import contention
            contention(root)
        if case=='original-2000':
            from special_fixture import ordinary_2000
            ordinary_2000(root)
        if case=='short':
            for flag in ('short-receipt','lose-ax-response','lose-receipt-response'):(root/flag).touch()
    if read(root/'synthetic.json')!={'synthetic':True,'live_platform':False}:raise ValueError('Isolation required')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(root/'settings'),'LOCALAPPDATA':str(root/'local'),
         'APPDATA':str(root/'roaming'),'TMP':str(root/'temp'),'TEMP':str(root/'temp'),'PYTHONUTF8':'1'}
    for k in ('QT_QPA_PLATFORM','PYTHONPATH','PYTHONHOME','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(k,None)
    (root/'temp').mkdir(exist_ok=True)
    argv=([str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')])+['--special-orders-smoke',str(root),case]
    start=time.perf_counter()
    peak=0
    avail=free_ram()
    floor=avail
    with (root/(case+'-process.log')).open('wb') as log,(root/(case+'-memory.jsonl')).open('w') as samples:
        proc=subprocess.Popen(argv,cwd=root,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        while proc.poll() is None:
            tree=working_set_tree(proc.pid)
            free=free_ram()
            peak=max(peak,sum(tree.values()))
            floor=min(floor,free)
            samples.write(json.dumps(dict(t=time.perf_counter()-start,working_set=tree,available=free))+'\n')
            samples.flush()
            if time.perf_counter()-start>145:
                # Terminate only the specific isolated acceptance process created here.
                proc.terminate()
                proc.wait(timeout=30)
                break
            time.sleep(.2)
    result=root/(case+'-ui.json')
    ui=read(result) if result.exists() else {'passed':False,'error':'未正常返回桌面证据'}
    v={'case':case,'exe':str(exe) if exe else None,'exit':proc.returncode,'elapsed':time.perf_counter()-start,
       'peak_tree_bytes':peak,'free_at_start':avail,'min_free':floor,'ui':ui,'path':str(root)}
    if exe:
        with exe.open('rb') as f:v['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
        v['build']=read(exe.parent/'版本信息.json')
    write(root/(case+'-record.json'),v)
    return v

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--exe',type=Path)
    parser.add_argument('--fixture',required=True,type=Path)
    parser.add_argument('--template',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('cases',nargs='+')
    args=parser.parse_args()
    for entry in args.cases:
        case,_,label=entry.partition(':')
        case_root=args.out/(label or case)
        result=exercise(args.exe,case,case_root,args.fixture,args.template,reopen=case in ('reopen','resume-transaction','migration-export','migration-check','file-resume'))
        print(case,result['ui'].get('passed'),str(result['ui'].get('error',''))[-950:],flush=True)
