"""Run new UI/reset acceptance against a source app or actual EXE, then reopen."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def run(exe,out):
    out=out.resolve();out.mkdir(parents=True,exist_ok=False)
    (out/'synthetic.json').write_text('{"isolated":true,"no_live_platform":true}','utf-8')
    (out/'settings').mkdir()
    env={**os.environ,'PYTHONUTF8':'1','REPLENISHMENT_DATA_DIR':str(out/'settings')}
    env.pop('QT_QPA_PLATFORM',None)
    command=[str(exe.resolve())] if exe else [sys.executable,str(ROOT/'src/app.py')]
    for mode,file in [('', 'report.json'),('verify','restart-report.json')]:
        with (out/(mode+'process.txt')).open('w',encoding='utf-8') as log:
            process=subprocess.Popen(command+['--reset-selected-smoke',str(out)]+([mode] if mode else []),cwd=ROOT,env=env,stdout=log,stderr=log)
            try:code=process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                (out/'pending-process.json').write_text(json.dumps({'pid':process.pid,'not_terminated':True}),'utf-8')
                raise RuntimeError('App still running; not killed. PID '+str(process.pid))
        report=json.loads((out/file).read_text('utf-8'))
        report['process_exit']=code
        if exe:
            with exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
            report['source_commit']=json.loads((exe.parent/'_internal/build_info.json').read_text('utf-8'))['source_commit']
        (out/file).write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
        if code or not report['passed']:raise AssertionError(report)
        print(json.dumps({'mode':mode or 'reset','passed':True,'checks':len(report['checks'])}))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--output',type=Path,required=True);a=p.parse_args();run(a.exe,a.output)
