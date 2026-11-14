"""Run the actual Qt/Session EXE against a frozen baseline synthetic fixture."""
import argparse,hashlib,json,os,shutil,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True);p.add_argument('--fixture',type=Path,required=True)
    p.add_argument('--expected',type=Path,required=True);p.add_argument('--exe',type=Path)
    p.add_argument('--timeout',type=float,default=180);a=p.parse_args()
    fixture=a.fixture.resolve();assert json.loads((fixture/'fixture.json').read_text('utf-8'))['synthetic']
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False);(out/'settings').mkdir()
    with closing(sqlite3.connect((fixture/'settings/workbench.sqlite3').as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(out/'settings/workbench.sqlite3')) as dst:src.backup(dst)
    shutil.copy2(fixture/'payload.json',out/'payload.json');shutil.copy2(a.expected,out/'expected-typed.json')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','QT_QPA_PLATFORM','REPLENISHMENT_PORTABLE_PATHS','REPLENISHMENT_DATA_DIR'):env.pop(key,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    command+=['--perf-core-smoke',str(out/'payload.json'),str(out)];start=time.monotonic()
    with (out/'process.log').open('w',encoding='utf-8') as log:
        process=subprocess.Popen(command,cwd=out,env=env,stdout=log,stderr=log)
        try:code=process.wait(a.timeout)
        except subprocess.TimeoutExpired:
            (out/'timeout.json').write_text(json.dumps({'pid':process.pid,'terminated':False,'reason':'No forced termination in an unknown commit phase'}),'utf-8');raise
    report=json.loads((out/'ui-report.json').read_text('utf-8'))
    report.update(exit_code=code,elapsed_seconds=time.monotonic()-start,expected_sha256=hashlib.sha256(a.expected.read_bytes()).hexdigest(),
        exe_sha256=hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    assert code==0 and report['passed'],report
    print(json.dumps({'passed':True,'frozen_exe':report['frozen_exe'],'checks':len(report['checks']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
