"""Real two-process app test with isolated preferences and generated archive data."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'qa/export_071/qa-deps')]
import psutil
from test_archive_orders import seed

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--exe',type=Path);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);seed(out);env=os.environ.copy()
    for k in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM'):env.pop(k,None)
    env['REPLENISHMENT_DATA_DIR']=str(out/'settings');env['PATH']=env['WINDIR']+';'+str(Path(env['WINDIR'])/'System32')
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    runs=[]
    for phase in ('exercise','reopen'):
        cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
        with (out/(phase+'.log')).open('w',encoding='utf-8') as log:
            p=subprocess.Popen(cmd+['--export-location-smoke',str(out),phase],cwd=out,env=env,stdout=log,stderr=log);host=psutil.Process(p.pid);start=time.monotonic();children=set()
            while p.poll() is None:
                if time.monotonic()-start>120:
                    for child in reversed(host.children(recursive=True)):child.terminate()
                    host.terminate();p.wait(10);raise TimeoutError('QA app timed out; owned tree stopped')
                try:children.update((c.pid,c.create_time()) for c in host.children(recursive=True))
                except psutil.Error:pass
                time.sleep(.05)
        r=json.loads((out/('ui-'+phase+'.json')).read_text(encoding='utf-8'));left=[]
        for pid,created in children:
            try:
                c=psutil.Process(pid)
                if c.create_time()==created and c.is_running():left.append(pid)
            except psutil.Error:pass
        r.update(exit_code=p.returncode,remaining_children=left,development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None,elapsed_seconds=time.monotonic()-start);runs.append(r)
        (out/('ui-'+phase+'.json')).write_text(json.dumps(r,ensure_ascii=False,indent=2),encoding='utf-8')
        if not r['passed'] or p.returncode or left:raise AssertionError(r)
    (out/'report.json').write_text(json.dumps({'passed':True,'runs':runs},ensure_ascii=False,indent=2),encoding='utf-8');print('PASS actual EXE' if a.exe else 'PASS source UI')
if __name__=='__main__':main()
