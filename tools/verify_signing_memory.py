"""Launch native packaged app on generated data, then close and reopen it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--exe',type=Path)
    p.add_argument('--small-only',action='store_true');p.add_argument('--resource-scenarios',action='store_true');a=p.parse_args();out=a.output.resolve();out.mkdir(exist_ok=False)
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    reports=[]
    phases=[('boundaries','boundaries',2,5000)]
    if not a.small_only:phases += [('full','full',17,20000),('full','reopen',17,20000)]
    else:phases += [('boundaries','reopen',2,5000)]
    if a.resource_scenarios:phases[1:1]=[('tight','full',17,20000),('tight','reopen',17,20000)]
    for name,phase,files,rows in phases:
        case=out/name;case.mkdir(exist_ok=True)
        for directory in ('settings','temp','localappdata','roaming'):(case/directory).mkdir(exist_ok=True)
        profile='tight' if name=='tight' else 'occupied' if name=='full' and a.resource_scenarios else 'ample'
        (case/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False,'files':files,'rows':rows,'extra':1000,'resource_profile':profile}),encoding='utf-8')
        env=os.environ.copy()
        for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_PORTABLE_PATHS'):env.pop(key,None)
        env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(case/'settings'),LOCALAPPDATA=str(case/'localappdata'),
            APPDATA=str(case/'roaming'),TEMP=str(case/'temp'),TMP=str(case/'temp'),QT_QPA_PLATFORM='windows')
        with (case/('process-'+phase+'.log')).open('wb') as log:
            process=subprocess.Popen([*command,'--signing-memory-smoke',str(case),phase],env=env,cwd=ROOT,
                stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
            code=process.wait(timeout=3600)
        file=case/('report-'+phase+'.json')
        report=json.loads(file.read_text('utf-8')) if file.exists() else {'passed':False,'error':'No native report. Inspect process and instance protection logs.'}
        report['exit_code']=code;reports.append(report)
        (out/'acceptance.json').write_text(json.dumps({'passed':all(r['passed'] and r['exit_code']==0 for r in reports),
            'complete':len(reports)==len(phases),'reports':reports,'live_platform':False,
            'exe':str(a.exe) if a.exe else None,
            'exe_sha256':hashlib.file_digest(a.exe.open('rb'),'sha256').hexdigest() if a.exe else None},ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'phase':phase,'passed':report['passed'],'exit_code':code,'checks':len(report.get('checks',[])),
            'error':report.get('error')},ensure_ascii=False),flush=True)
        if not report['passed'] or code:return 1
    return 0


if __name__=='__main__':sys.exit(main())
