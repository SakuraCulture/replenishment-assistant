"""Isolated first-fill GUI acceptance; actual catalog imports, no platform calls."""
import argparse,copy,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/p) for p in ('src','tests','tools')]

def fixture(out):
    from test_first_fill import warehouse_fixture,base,monthly
    out.mkdir(parents=True,exist_ok=False);(out/'inputs').mkdir();(out/'settings').mkdir()
    loaded,catalogs=warehouse_fixture(out/'inputs')
    sd=base(loaded,'山东仓');hb=base(loaded,'河北仓');fallback=base(loaded,'山东仓','河北仓')
    missing=copy.deepcopy(sd);missing['rows'][0]['_meta']['first_fill']['known']=False
    invalid=copy.deepcopy(hb);invalid['rows'][0]['_meta']['first_fill']['raw']=31
    data={'synthetic':True,'live_platform':False,'monthly':monthly(),'bases':{'山东单仓':sd,'河北单仓':hb,'山东主河北从':fallback,'缺少首铺数':missing,'不合箱规':invalid}}
    (out/'fixture.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),encoding='utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('--out',required=True,type=Path);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');fixture(out)
    env={**os.environ,'PYTHONUTF8':'1','QT_QPA_PLATFORM':'windows','REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata')}
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        proc=subprocess.Popen(command+['--first-fill-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW);start=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-start>120:raise TimeoutError(f'Isolated process {proc.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'report.json').read_text('utf-8'));report['process_exit']=proc.returncode
    if a.exe:
        with a.exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    (out/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps(report,ensure_ascii=False))
    return 0 if report['passed'] and proc.returncode==0 else 1
if __name__=='__main__':sys.exit(main())
