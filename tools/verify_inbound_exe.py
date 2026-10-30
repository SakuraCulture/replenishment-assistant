"""Source/actual EXE acceptance on a new, private copy of frozen history."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);settings=out/'settings';settings.mkdir();shutil.copy2(ROOT/'qa/inbound_091/historical.sqlite3',settings/'workbench.sqlite3')
env=os.environ.copy()
for key in ['PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM']:env.pop(key,None)
env['REPLENISHMENT_DATA_DIR']=str(settings);env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
with (out/'run.log').open('w',encoding='utf-8') as log:
    proc=subprocess.Popen(cmd+['--inbound-smoke',str(out),str(ROOT/'qa/inbound_091/real')],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid);start=time.monotonic();peak=0;children=set()
    while proc.poll() is None:
        if time.monotonic()-start>300:raise TimeoutError('QA-only process '+str(proc.pid))
        try:
            tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peak=max(peak,sum(c.memory_info().rss for c in [host]+tree))
        except psutil.Error:pass
        time.sleep(.05)
r=json.loads((out/'inbound_ui_report.json').read_text(encoding='utf-8'));left=[]
for pid,created in children:
    try:
        c=psutil.Process(pid)
        if c.create_time()==created and c.is_running():left.append(pid)
    except psutil.Error:pass
r.update(exit_code=proc.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,peak_tree_mib=peak/2**20)
(out/'inbound_ui_report.json').write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(r,ensure_ascii=False));assert r['passed'] and proc.returncode==0 and not left
