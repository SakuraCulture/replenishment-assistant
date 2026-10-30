"""Two launches verify actual packaged flow and automatic default restoration."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'))
import psutil

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--exe',type=Path);ap.add_argument('--fixture',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(key,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    results=[]
    for phase in ('exercise','reopen'):
        command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            p=subprocess.Popen(command+['--warehouse-delivery-smoke',str(a.fixture.resolve()),str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(p.pid);start=time.monotonic();peak=0;children=set()
            while p.poll() is None:
                if time.monotonic()-start>240:raise TimeoutError('QA owned process timed out; inspect PID '+str(p.pid))
                try:
                    tree=host.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peak=max(peak,sum(c.memory_info().rss for c in [host]+tree))
                except psutil.Error:pass
                time.sleep(.05)
        report=json.loads((out/('ui-'+phase+'.json')).read_text(encoding='utf-8'));left=[]
        for pid,created in children:
            try:
                c=psutil.Process(pid)
                if c.create_time()==created and c.is_running():left.append(pid)
            except psutil.Error:pass
        report.update(exit_code=p.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,peak_tree_mib=peak/2**20,sample_ms=50,elapsed_seconds=time.monotonic()-start);(out/('ui-'+phase+'.json')).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');results.append(report)
        if not report['passed'] or p.returncode or left:raise AssertionError(report)
    (out/'report.json').write_text(json.dumps({'passed':True,'runs':results},ensure_ascii=False,indent=2),encoding='utf-8');print('passed: actual workflow and restart' if a.exe else 'passed: source workflow and restart')
if __name__=='__main__':main()
