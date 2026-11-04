"""Synthetic isolated collection records plus real draft/signed-order fixtures. No browser."""
import argparse,json,subprocess,sys
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]

def seed(out):
    from test_archive_orders import seed as business_seed
    from aoxiang_jobs import Jobs
    from aoxiang_acquisition import manifest,freeze_manifest
    import sqlite3
    out=Path(out).resolve()
    if out.exists():raise FileExistsError('Use a new isolated QA folder')
    out.mkdir(parents=True)
    work,_,order_payload=business_seed(out)
    import work_repository
    work.save_job(work_repository.today(),{'schema':1,'day':work_repository.today(),'model_version':'0.3.4-rules','states':{'T':{'state':'已完成','name':'测试店'}},'results':{'T':order_payload['result']},'stores':{'T':{'store':'T'}}})
    component=ROOT/'components/aoxiang'
    state=out/'settings/aoxiang/component-state'
    script="import {fixture} from './test/maintenance-fixture.mjs';const c=await fixture(process.argv[1]);console.log(JSON.stringify(c.commands.list().map(({config_snapshot,...r})=>r)));c.state.close();"
    result=subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(state)],cwd=component,capture_output=True,encoding='utf-8',check=True,creationflags=subprocess.CREATE_NO_WINDOW)
    records=json.loads(result.stdout);jobs=Jobs(out/'settings/aoxiang/jobs.sqlite3')
    for r in records:
        jobs.register(r['request'],{'day':'2026-11-04','created':r['created_at'],'stores':[{'buyerWarehouseCode':i['warehouse_code'],'displayName':{'W2':'合成琅琊店','W1':'合成乳山店','W3':'合成已采用店'}[i['warehouse_code']]} for i in r['items']]})
        jobs.observe(r['id'],r)
    saved=freeze_manifest(manifest(jobs.get('adopted'),state),out/'settings/aoxiang')
    # Read-only logical rows include BLOB contents and all draft/signed/order data.
    with closing(sqlite3.connect(work.path)) as db:
        business_dump='\n'.join(db.iterdump())
    (out/'business-before.sql').write_text(business_dump,encoding='utf-8')
    info={'synthetic':True,'live_platform':False,'business_db':str(work.path),'manifest':saved,'state_root':str(state),'command_files':[r['items'][0]['result']['file'] for r in records if r['id']=='adopted']}
    (out/'fixture.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
    return info

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);a=p.parse_args();print(json.dumps(seed(a.out),ensure_ascii=False))
