"""Isolated accepted-task recovery and automatic configuration UI; no live platform."""
import argparse,json,os,sqlite3,subprocess,sys
from contextlib import closing
from pathlib import Path
from prepare_collection_maintenance import seed,ROOT
from aoxiang_jobs import Jobs

def main():
    p=argparse.ArgumentParser();p.add_argument('--exe',type=Path);p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    out=a.out.resolve();out.relative_to(ROOT/'qa');info=seed(out);jobs=Jobs(out/'settings/aoxiang/jobs.sqlite3')
    with closing(sqlite3.connect(Path(info['state_root'])/'state.sqlite')) as db,db:
        old=json.loads(db.execute('SELECT payload FROM machine_commands WHERE id=?',('old-failed',)).fetchone()[0])
        for raw, in db.execute('SELECT payload FROM attempts').fetchall():
            attempt=json.loads(raw);attempt.update(stage='ACCEPTED',downloaded=None,taskSummary=None)
            db.execute('UPDATE attempts SET stage=?,payload=? WHERE id=?',('ACCEPTED',json.dumps(attempt,ensure_ascii=False),attempt['id']))
        current=json.loads(db.execute('SELECT payload FROM machine_commands WHERE id=?',('blocked',)).fetchone()[0])
        current.update(state='INTERRUPTED',sequence=current['sequence']+1,error={'code':'TASK_NOT_FOUND','message':'离线模拟：原任务暂未找到'})
        current['items']=[dict(i,state='INTERRUPTED' if n==0 else 'RUNNING',task_id='task-'+i['warehouse_code'],error=current['error'] if n==0 else None) for n,i in enumerate(old['items'])]
        db.execute('UPDATE machine_commands SET state=?,sequence=?,payload=? WHERE id=?',(current['state'],current['sequence'],json.dumps(current,ensure_ascii=False),current['id']))
    jobs.observe(current['id'],{k:v for k,v in current.items() if k!='config_snapshot'})
    info.update(recovery=True,original_runs={i['warehouse_code']:i['run_id'] for i in current['items']})
    (out/'fixture.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),'utf-8')
    env={**os.environ,'REPLENISHMENT_DATA_DIR':str(out/'settings'),'PYTHONUTF8':'1'}
    env.pop('QT_QPA_PLATFORM',None)
    cmd=[str(a.exe.resolve())] if a.exe else [sys.executable,str(ROOT/'src/app.py')]
    with (out/'process.log').open('w',encoding='utf-8') as log:
        r=subprocess.run(cmd+['--collection-maintenance-smoke',str(out)],env=env,stdout=log,stderr=log,timeout=120,creationflags=subprocess.CREATE_NO_WINDOW)
    report=json.loads((out/'exe-report.json').read_text('utf-8'));report['process_exit_code']=r.returncode
    report['accepted']=report.get('passed',False) and r.returncode==0 and report.get('shared_component_closed',False)
    (out/'process-acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
    if not report['accepted']:raise SystemExit(1)
if __name__=='__main__':main()
