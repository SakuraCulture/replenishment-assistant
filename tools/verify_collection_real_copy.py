"""Read-only backup of AX metadata; maintenance executes exclusively on QA copies."""
import argparse,hashlib,json,sqlite3,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
from aoxiang_jobs import Jobs
from aoxiang_client import ProcessClient
from aoxiang_acquisition import prepare

def digest(p):
    with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as db:
        return hashlib.sha256('\n'.join(db.iterdump()).encode()).hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True);parser.add_argument('--out',type=Path,required=True);a=parser.parse_args()
    source=a.source.resolve();out=a.out.resolve()
    if out.exists() or source==out or source in out.parents:raise ValueError('Use a new QA directory outside source settings')
    out.mkdir(parents=True);(out/'component-state').mkdir()
    before={}
    for name in ['jobs.sqlite3','component-state/state.sqlite']:
        p=source/name;before[name]=digest(p)
        with closing(sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)) as src,closing(sqlite3.connect(out/name)) as target:src.backup(target)
    jobs=Jobs(out/'jobs.sqlite3');blocked=next(j for j in jobs.recent() if j['state']=='NEEDS_REVIEW' and (j.get('record') or {}).get('error',{}).get('code')=='UNRESOLVED_TASK')
    original=jobs.get(blocked['id']);configuration={'configured':True,'enabled':True,'account_binding':original['command']['account_binding'],'store_mapping_version':original['command']['basis_versions']['store_mapping'],'targets':original['context']['stores']}
    codes=original['command']['scope']['warehouse_codes'];client=ProcessClient(ROOT/'components/aoxiang',out/'component-state')
    report={'baseline':'real local metadata copied read-only','live_platform':False,'passed':False}
    try:
        client.start();jobs.synchronize(client)
        plan=jobs.maintenance_preview(client,blocked['id'],'end')
        if plan['blocked']:raise AssertionError(plan['blocked'])
        jobs.maintenance_apply(client,plan)
        created=prepare(jobs,configuration,codes,'refresh',day='2026-11-04')
        jobs.maintenance_apply(client,jobs.maintenance_preview(client,blocked['id'],'delete'))
        with closing(sqlite3.connect(out/'component-state/state.sqlite')) as db:
            still_pending=[(r[0],r[1]) for r in db.execute("SELECT warehouse_code,stage FROM attempts WHERE stage NOT IN ('SUCCEEDED','ABANDONED','FAILED_CONFIRMED','CANCELLED_BEFORE_SUBMIT','REUSED')")]
        report.update(passed=True,ended_stores=[s['name'] for s in plan['stores']],related_records=len(plan['related']),new_task_prepared=True,deleted_record_absent=all(j['id']!=blocked['id'] for j in jobs.recent()),unrelated_pending=still_pending,source_logical_hash_before=before,source_logical_hash_after={n:digest(source/n) for n in before})
        report['original_metadata_unchanged']=report['source_logical_hash_before']==report['source_logical_hash_after']
        if not report['original_metadata_unchanged']:raise AssertionError('Source changed externally during read-only verification')
    finally:client.close()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
