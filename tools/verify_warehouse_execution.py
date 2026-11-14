"""Synthetic native UI/spawn/sign/export/delete acceptance, then actual reopen."""
import argparse,hashlib,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT/'tools')]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--exe',type=Path)
    parser.add_argument('--timeout',type=float,default=180);args=parser.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    from verify_signing_feedback import fixture
    payload=fixture();codes=list(payload['results'])[:2]
    payload['results']={s:payload['results'][s] for s in codes};payload['states']={s:payload['states'][s] for s in codes}
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False},indent=2),'utf-8')
    (out/'payload.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),'utf-8')
    (out/'exports').mkdir();(out/'survivor-export').mkdir()
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_PORTABLE_PATHS','REPLENISHMENT_DATA_DIR','QT_QPA_PLATFORM'):env.pop(key,None)
    env['PYTHONUTF8']='1';env['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    env['LOCALAPPDATA']=str(out/'localappdata');env['APPDATA']=str(out/'roaming')
    if not args.exe:env['QT_QPA_PLATFORM']='offscreen'
    base=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    report={'passed':False,'live_platform':False,'synthetic_input_adapter':True,'runs':[],
            'exe_sha256':hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None}
    started=time.monotonic()
    for mode in ('exercise','reopen'):
        with (out/(mode+'-process.log')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(base+['--warehouse-execution-smoke',str(out),mode],cwd=out,env=env,stdout=log,stderr=log)
            try:code=process.wait(args.timeout)
            except subprocess.TimeoutExpired:
                report.update(timeout=True,pid=process.pid,message='QA process still running; not force-terminated in an unknown commit phase')
                (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');raise
        file=out/(mode+'-report.json')
        run=json.loads(file.read_text('utf-8')) if file.is_file() else {'passed':False,'error':'Native report was not produced'}
        run['exit_code']=code;report['runs'].append(run)
        (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if code or not run['passed']:raise AssertionError(run.get('error') or run)
    report.update(passed=True,elapsed_seconds=time.monotonic()-started)
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps({'passed':True,'checks':sum(len(run['checks']) for run in report['runs']),
                      'frozen_exe':all(run['frozen_exe'] for run in report['runs']),'report':str(out/'report.json')},ensure_ascii=False))


if __name__=='__main__':main()
