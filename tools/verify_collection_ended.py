"""Upgrade a synthetic pre-0.50 ENDED fixture in a real GUI, without a browser."""
import argparse,hashlib,json,os,sqlite3,subprocess,sys,time
from contextlib import closing
from pathlib import Path
from prepare_collection_maintenance import seed
ROOT=Path(__file__).resolve().parents[1]

def fixture(out):
    info=seed(out)
    from aoxiang_jobs import Jobs
    jobs=Jobs(out/'settings/aoxiang/jobs.sqlite3')
    with closing(sqlite3.connect(Path(info['state_root'])/'state.sqlite')) as db,db:
        for id,raw in db.execute('SELECT id,payload FROM machine_commands').fetchall():
            c=json.loads(raw)
            if id not in {'old-failed','blocked'}:continue
            c.update(state='ENDED',sequence=c['sequence']+1)
            for item in c['items']:
                item['state']='ENDED';rid=item['run_id']
                run=json.loads(db.execute('SELECT payload FROM runs WHERE id=?',(rid,)).fetchone()[0])
                run.update(status='ENDED',endedAt='2026-11-01T00:00:00Z',resumePending=True)
                db.execute('UPDATE runs SET payload=? WHERE id=?',(json.dumps(run),rid))
            db.execute('UPDATE machine_commands SET payload=? WHERE id=?',(json.dumps(c),id))
            jobs.observe(id,{k:v for k,v in c.items() if k!='config_snapshot'})
        attempts=[]
        for id,raw in db.execute('SELECT id,payload FROM attempts').fetchall():
            attempt=json.loads(raw);attempt['stage']='ACCEPTED';attempts.append({'id':id,'taskId':attempt['taskId'],'runId':attempt['runId']})
            db.execute('UPDATE attempts SET stage=?,payload=? WHERE id=?',('ACCEPTED',json.dumps(attempt),id))
    info['legacy_attempts']=attempts
    (out/'fixture.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
    (out/'synthetic.json').write_text(json.dumps({'synthetic':True,'live_platform':False}),'utf-8')

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--out',type=Path,required=True);p.add_argument('--exe',type=Path);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');fixture(out)
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'LOCALAPPDATA':str(out/'localappdata'),'PYTHONUTF8':'1'}
    for key in ['QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME']:env.pop(key,None)
    command=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('wb') as log:
        proc=subprocess.Popen(command+['--collection-ended-smoke',str(out)],cwd=ROOT,env=env,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
        start=time.monotonic()
        while proc.poll() is None:
            if time.monotonic()-start>120:raise TimeoutError(f'Isolated process {proc.pid} retained; no forced termination')
            time.sleep(.2)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit_code']=proc.returncode
    if a.exe:
        with a.exe.open('rb') as f:report['exe_sha256']=hashlib.file_digest(f,'sha256').hexdigest()
    report['accepted']=report['passed'] and proc.returncode==0 and report.get('shared_component_closed',False)
    (out/'process-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8');print(json.dumps(report,ensure_ascii=False))
    return 0 if report['accepted'] else 1
if __name__=='__main__':sys.exit(main())
