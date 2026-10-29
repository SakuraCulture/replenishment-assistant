"""Run frozen history acceptance on a private source copy; sample process-tree RSS."""
import argparse,subprocess,os,sys,shutil,time,json,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'));import psutil
p=argparse.ArgumentParser();p.add_argument('--exe',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--snapshot',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);settings=out/'settings';settings.mkdir();shutil.copy2(a.snapshot,settings/'workbench.sqlite3')
env=os.environ.copy()
for k in ['PYTHONPATH','PYTHONHOME','QT_QPA_PLATFORM','QT_PLUGIN_PATH']:env.pop(k,None)
env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(settings)
run=subprocess.Popen([str(a.exe.resolve()),'--history-smoke',str(out)],cwd=out,env=env);proc=psutil.Process(run.pid);samples=[];children=set();started=time.monotonic()
while run.poll() is None:
    if time.monotonic()-started>280:
        for c in proc.children(recursive=True):c.kill()
        proc.kill();raise TimeoutError('history smoke')
    try:
        tree=proc.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);samples.append(sum(q.memory_info().rss for q in [proc]+tree))
    except psutil.Error:pass
    time.sleep(.05)
assert run.returncode==0,run.returncode
report=json.loads((out/'history_ui_report.json').read_text(encoding='utf-8'));assert report['passed'],report
left=[]
for pid,created in children:
    try:
        c=psutil.Process(pid)
        if c.create_time()==created and c.is_running():left.append(pid)
    except psutil.Error:pass
assert not left,left
report.update(exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest(),source_commit=report['version']['source_commit'],app_version=report['version']['version'],sampled_peak_tree_mib=max(samples)/1024**2,sample_ms=50,remaining_children=left,development=False)
(out/'history_ui_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
