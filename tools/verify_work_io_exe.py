"""Supervise large work-I/O source/EXE acceptance with isolated private data."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'qa/export_071/qa-deps'))
import psutil

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);settings=out/'settings';settings.mkdir();shutil.copy2(a.snapshot,settings/'workbench.sqlite3')
    env=os.environ.copy()
    for key in ['PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM']:env.pop(key,None)
    env['PATH']=str(Path(env['WINDIR'])/'System32')+';'+env['WINDIR'];env['REPLENISHMENT_DATA_DIR']=str(settings)
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'run.log').open('w',encoding='utf-8') as log:
        run=subprocess.Popen(command+['--work-io-smoke',str(out)],cwd=out,env=env,stdout=log,stderr=log);proc=psutil.Process(run.pid);peaks={};children=set();start=time.monotonic()
        while run.poll() is None:
            if time.monotonic()-start>900:
                for c in proc.children(recursive=True):c.kill()
                proc.kill();raise TimeoutError('isolated full work I/O')
            try:
                phase=(out/'phase.txt').read_text(encoding='ascii') if (out/'phase.txt').exists() else 'startup';tree=proc.children(recursive=True);children.update((c.pid,c.create_time()) for c in tree);peaks[phase]=max(peaks.get(phase,0),sum(c.memory_info().rss for c in [proc]+tree))
            except (psutil.Error,OSError):pass
            time.sleep(.05)
    report=json.loads((out/'work_io_report.json').read_text(encoding='utf-8'));left=[]
    for pid,created in children:
        try:
            c=psutil.Process(pid)
            if c.create_time()==created and c.is_running():left.append(pid)
        except psutil.Error:pass
    report.update(exit_code=run.returncode,remaining_children=left,peak_tree_mib={k:v/2**20 for k,v in peaks.items()},memory_method='50ms external sum of main and owned child RSS; shared pages counted in each process',development=not bool(a.exe),exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'work_io_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False));assert run.returncode==0 and report['passed'] and not left

if __name__=='__main__':main()
