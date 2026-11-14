"""Private synthetic folder, actual source/EXE UI and bundled component acceptance."""
import argparse,hashlib,json,os,shutil,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--fixture',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--timeout',type=float,default=180)
    a=p.parse_args();fixture=a.fixture.resolve();assert json.loads((fixture/'fixture.json').read_text('utf-8'))['synthetic']
    out=a.out.resolve();assert not out.exists();shutil.copytree(fixture,out,copy_function=shutil.copy2)
    shutil.copy2(ROOT/'tools/verify_memory_response.cjs',out/'verify_memory_response.cjs')
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM','REPLENISHMENT_PORTABLE_PATHS','REPLENISHMENT_DATA_DIR'):env.pop(key,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command+['--memory-load-smoke',str(out)],cwd=out,env=env,stdout=log,stderr=log,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        start=time.monotonic()
        try:code=process.wait(a.timeout)
        except subprocess.TimeoutExpired:
            (out/'timeout.json').write_text(json.dumps({'pid':process.pid,'terminated':False,'reason':'Unknown phase, no forced termination'}),'utf-8');raise
    report=json.loads((out/'ui-report.json').read_text('utf-8'));report.update(exit_code=code,seconds=time.monotonic()-start,
        exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    assert report['passed'] and code==0,report
    print(json.dumps({'passed':True,'groups':len(report['checks']),'frozen_exe':report['frozen_exe'],'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
