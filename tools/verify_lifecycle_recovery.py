"""Actual application/EXE recovery and reopen acceptance on fresh synthetic data."""
import argparse,hashlib,json,os,shutil,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);p.add_argument('--visible-source',action='store_true');a=p.parse_args()
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    import test_jst as fixture
    import purchases
    f=fixture.Service();f.setUp()
    try:
        other=f.second_store();shutil.copytree(Path(f.path).parent,out/'settings')
        data={'head':f.h,'other':other,'heads':purchases.targets(f.path)}
        (out/'fixture.json').write_text(json.dumps(data,ensure_ascii=False),'utf-8')
    finally:f.tearDown()
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')
    env=os.environ.copy()
    for name in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):env.pop(name,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if not a.exe and not a.visible_source:env['QT_QPA_PLATFORM']='offscreen'
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'runs':[],'synthetic':True,'live_platform':False,'dialogs_not_stubbed':True,
            'exe_sha256':hashlib.sha256(a.exe.read_bytes()).hexdigest() if a.exe else None}
    for mode in ('exercise','close_recovery','reopen'):
        with (out/(mode+'-process.log')).open('wb') as log:
            process=subprocess.Popen([*command,'--lifecycle-recovery-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=process.wait(240)
            except subprocess.TimeoutExpired:
                report.update(error='Owned process retained without forced exit',pid=process.pid)
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise
        file=out/(mode+'-report.json');result=json.loads(file.read_text('utf-8')) if file.exists() else {'passed':False,'error':'No application report'}
        result['exit_code']=code;report['runs'].append(result)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        assert code==0 and result['passed'],result
    report['passed']=True
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':sum(len(r['checks']) for r in report['runs']),'out':str(out)},ensure_ascii=False))


if __name__=='__main__':main()
