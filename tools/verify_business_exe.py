"""Run business forms/return UI in source or packaged app, isolated data only."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'))
import psutil
def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--real-result',type=Path);p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
    for k in ['PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM']:env.pop(k,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'run.log').open('w',encoding='utf-8') as log:
        proc=subprocess.Popen(command+(['--fixes-smoke',str(a.fixture.resolve()),str(a.real_result.resolve()),str(out)] if a.real_result else ['--business-smoke',str(a.fixture.resolve()),str(out)]),cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(proc.pid);start=time.monotonic();peak=0;children=set()
        while proc.poll() is None:
            if time.monotonic()-start>240:
                # QA-only process; do not force kill an active write. Report timeout.
                raise TimeoutError('private business UI test; inspect owned PID '+str(proc.pid))
            try:
                tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peak=max(peak,sum(c.memory_info().rss for c in [host]+tree))
            except psutil.Error:pass
            time.sleep(.05)
    report_name='fixes_ui_report.json' if a.real_result else 'business_ui_report.json'
    r=json.loads((out/report_name).read_text(encoding='utf-8'));left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    r.update(exit_code=proc.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,peak_tree_mib=peak/2**20,sample_ms=50);(out/report_name).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps(r,ensure_ascii=False));assert r['passed'] and proc.returncode==0 and not left
if __name__=='__main__':main()
