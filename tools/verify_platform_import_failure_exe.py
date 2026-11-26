"""Exercise the rc62 Windows package in two isolated same-store warehouse cases."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

from verify_jst_one_click_exe import ROOT, contents, seed as seed_two_warehouses, sha, write


def seed(case, affected):
    import jst_auto_order
    import jst_batches
    import jst_order_store
    import purchases
    import work_repository as wr
    from aoxiang_workflows import Workflows

    seed_two_warehouses(case)
    path=case/'settings/workbench.sqlite3'
    heads=[h for h in purchases.targets(path) if h['day']==wr.today()]
    assert len(heads)==2 and len({h['oid'] for h in heads})==1
    heads.sort(key=lambda h:h['warehouse']!=affected)
    batch=jst_batches.execute(path,jst_batches.preview_new(path,heads))
    assert not batch['errors'] and len(batch['generated'])==2
    with closing(wr.read_connection(path)) as db:
        prepared=jst_auto_order.prepare(path,[{'head':h,'gid':jst_order_store.active(db,h)} for h in heads])
    assert not prepared['failed'] and len(prepared['ready'])==2

    queue=Workflows(case/'settings/aoxiang')
    samples=heads+[{**heads[1],'number':'BH-QA-COMPARE-'+str(i)} for i in range(2,16)]
    preview=queue.prepare(samples)
    for index in range(16):
        queue.update(preview['id'],position=index,changes={
            'state':'PREPARED','package':{'id':f'controlled-{index}'},
            'summary':{'sku_count':601 if index==0 else 1,'amount':'15285.03' if index==0 else '80'}})
    preview=queue.update(preview['id'],state='READY')
    job=queue.upload(preview['id'],list(range(16)),preview['sequence'])
    code='SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-MULTI-UNIT-IMPORT-V2'
    task=f'GEI@{code}@261125201517@{601 if affected=="山东仓" else 602}'
    queue.update(job['id'],position=0,changes={'state':'ACCEPTED','submission_started':True,
        'purchase_observation':{'taskId':task,'uploadAttempted':True}})
    for index in range(1,16):
        queue.update(job['id'],position=index,changes={'state':'BOUND',
            'result':{'binding':{'orderId':f'PO-QA-COMPARE-{index}','status':'待入库'}}})
    write(case/'import-failure-fixture.json',{'synthetic':True,'live_platform':False,'affected':affected,
        'ax_batch':job['id'],'task':task,'jst_keys':prepared['ready']})


def run(exe,case,phase):
    env=os.environ.copy()
    for key in ('QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME','PYTHONPATH','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key,None)
    env.update(LOCALAPPDATA=str(case/'local'),APPDATA=str(case/'roaming'),
               REPLENISHMENT_DATA_DIR=str(case/'settings'),PYTHONUTF8='1')
    command=[str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')]
    with (case/(phase+'-process.log')).open('xb') as log:
        process=subprocess.Popen([*command,'--platform-import-failure-smoke',str(case),phase],
                                 env=env,cwd=case,stdout=log,stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        code=process.wait(timeout=120)
    report_path=case/(phase+'-report.json')
    report=json.loads(report_path.read_text('utf-8')) if report_path.is_file() else {}
    assert code==0 and report.get('passed') and report['frozen_exe'] is bool(exe),(code,report)
    return report


def seed_pending(case, outcomes):
    import jst_auto_order as automatic
    import jst_auto_order_store as store
    from platform_import_manual_fixture import JstManual
    seed(case,'山东仓')
    path=case/'settings/workbench.sqlite3'
    keys=json.loads((case/'import-failure-fixture.json').read_text('utf-8'))['jst_keys']
    portal=JstManual(case/'fixture')
    previews=automatic.run_browser(path,keys,portal,'preview')
    assert all(r['ok'] and r['stage']=='PREVIEWED' for r in previews)
    ready=[]
    allowed=[]
    hashes={}
    with closing(store.connection(path)) as db,db:
        for key,outcome in zip(keys,outcomes,strict=True):
            row=store.read(db,key,workbook=True)
            proof=dict(row['proof'])
            hashes[key]=row['file_sha']
            at='2026-11-25T12:15:17Z'
            if outcome in ('rejected','accepted'):
                success=int(outcome=='accepted')
                proof['import_receipt']=dict(online=key,success_count=success,fail_count=1-success,
                    text=f'成功条数：{success}，失败条数：{1-success}',observed_at=at)
            if outcome=='not_sent':
                proof['import_not_sent']=dict(online=key,file_sha=row['file_sha'],binding=proof['binding'],
                    reason='最终导入页面未就绪',observed_at=at)
            if outcome in ('rejected','not_sent'):ready.append(key)
            if outcome!='accepted':allowed.append(key)
            store.update(db,row,'IMPORT_UNKNOWN',proof=proof,reason='已核对本仓企业、店铺和导入入口')
    write(case/'pending-preview.json',dict(ready=ready,preview=allowed,file_sha=hashes,outcomes=outcomes))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--preview',action='store_true',help='验证原单重新预览和确认范围')
    args=parser.parse_args()
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    exe=files=app=None
    if args.zip:
        with zipfile.ZipFile(args.zip) as archive:
            assert archive.testzip() is None
            archive.extractall(out/'unpacked')
        app=out/'unpacked/补货助手'
        files=contents(app)
        exe=app/'补货助手.exe'
        assert {'补货助手.exe','_internal/build_info.json',
                '_internal/components/aoxiang/integrity.manifest.json',
                '_internal/components/jst_catalog/integrity.manifest.json'} <= files.keys()
    cases={}
    if args.preview:
        for name,outcomes in [('旧单均无回执',['unknown','unknown']),('明确失败与无回执',['rejected','unknown']),
                              ('未发送与已受理',['not_sent','accepted'])]:
            case=out/name
            seed_pending(case,outcomes)
            cases[name]=run(exe,case,'preview')
        if exe:assert contents(app)==files
        write(out/'report.json',dict(passed=True,synthetic=True,live_platform=False,cases=cases,
              zip_sha256=sha(args.zip) if exe else None,exe_sha256=sha(exe) if exe else None))
        print(out/'report.json')
        return
    for warehouse in ('山东仓','河北仓'):
        case=out/warehouse
        seed(case,warehouse)
        first,reopen=(run(exe,case,phase) for phase in ('exercise','reopen'))
        assert first['aoxiang']['row']['package']==reopen['aoxiang']['row']['package']
        assert first['jst']['rejected']==reopen['jst']['rejected']
        assert first['jst']['failed_file_sha']==reopen['jst']['failed_file_sha']
        assert first['jst']['stock_reservations']==reopen['jst']['stock_reservations']
        assert first['jst']['other_warehouse_internal']==reopen['jst']['other_warehouse_internal']
        cases[warehouse]=[first,reopen]
    if exe:
        assert contents(app)==files
    write(out/'report.json',{'passed':True,'synthetic':True,'cases':cases,'exe_sha256':sha(exe) if exe else None,
                             'zip_sha256':sha(args.zip) if args.zip else None,'files':len(files) if files else None})
    print(out/'report.json')


if __name__=='__main__':
    main()
