"""Run the P1 acceptance in independent applications, then seal the portable package."""
import argparse
from contextlib import closing
from decimal import Decimal
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time


STOCK={'河北仓':100,'山东仓':145}
QTY={'河北仓':20,'山东仓':16}
PRICE={'河北仓':2,'山东仓':3}


def sha(file):
    with Path(file).open('rb') as stream:
        return file_digest(stream,'sha256').hexdigest()


def write(file, value):
    with Path(file).open('x',encoding='utf-8',newline='\n') as stream:
        json.dump(value,stream,ensure_ascii=False,indent=2)
        stream.write('\n')


def read(file):
    return json.loads(Path(file).read_text(encoding='utf-8-sig'))


def sheet(path, h, stage, quantity, amount=None):
    import lifecycle_io as io
    import openpyxl
    fields=io.fields(stage)
    row={'补货单号':h['number'],'签发修订':h['revision'],'门店编码':h['store'],'供货仓':h['warehouse'],
         '商品编码':'00000','商品名称':'合成商品','数量单位':'库存单位',io.QUANTITIES[stage]:quantity,
         io.DOCNUM[stage]:'EXE-QA-'+stage,io.DOCDATE[stage]:h['day'],'实际金额':amount,'币种':'CNY'}
    book=openpyxl.Workbook()
    book.active.append(fields)
    book.active.append([row.get(field) for field in fields])
    book.save(path)
    book.close()


def old_copy(root, previous):
    from aoxiang_directory import Directory
    from aoxiang_workflows import Workflows
    import normal_orders
    import purchases
    from test_aoxiang_purchase import config
    from test_order_redo_p1 import AX, Dual, SUPPLIERS
    import work_repository as wr
    source=previous/'old-isolated'
    settings=root/'settings'
    settings.mkdir()
    with closing(sqlite3.connect((source/'settings/workbench.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as old, \
         closing(sqlite3.connect(settings/'workbench.sqlite3')) as target:
        old.backup(target)
    shutil.copytree(source/'settings/warehouse_catalogs',settings/'warehouse_catalogs')
    case=Dual.__new__(Dual)
    case.root=root
    case.work=wr.Repository(settings)
    case.repo=normal_orders.Repository(case.work)
    case.path=case.work.path
    case.config=config()
    case.ax=AX(case)
    case.workflow=Workflows(settings/'aoxiang',business_path=case.path)
    Directory(case.workflow.folder).save_suppliers(SUPPLIERS)
    case.heads={h['warehouse']:h for h in purchases.targets(case.path)}
    fixture=read(source/'controlled-portal.json')
    return case,fixture['online'],fixture['orders']


def native_record(package, record):
    return {**record,'intent':package['intent'],'basis':package['intent']['basis'],
            'target':{'displayName':'隔离合成门店'},'sequence':1,
            'package_file':package['package_file']}


def seed(root, name, old_evidence, component):
    from aoxiang_purchase_tasks import validate_for_upload
    import export_settings
    import jst_auto_order_store as orders
    import jst_cancellation
    import jst_order_store as generations
    import order_lifecycle as lifecycle
    from test_order_redo_p1 import Dual, SUPPLIERS
    import work_repository as wr
    from order_redo_platform_fixture import Ax, new_receipt
    root.mkdir()
    kind,wh,variant=name
    if kind=='legacy':
        case,keys,portal=old_copy(root,old_evidence)
    else:
        case=Dual(root)
        gids=case.begin()
        keys={w:case.order(w,gid) for w,gid in gids.items()}
        for key in keys.values():
            assert case.pay(key)['version']==1
        portal=case.jst.orders
    data={'case':'order-redo-rc14','synthetic':True,'live_platform':False,'kind':kind,'warehouse':wh,
          'day':case.heads[wh]['day'],'stock':STOCK,'quantity':QTY,'price':PRICE,'suppliers':SUPPLIERS,
          'ax_config':{**case.config,'purchase_pipeline':True},'originals':{}}
    h=case.heads[wh]
    online=keys[wh]
    with closing(wr.read_connection(case.path)) as db:
        if kind=='legacy':
            assert jst_cancellation.read(db,h)['legacy']
            assert not orders.exists(db,orders.CANCELLATION_HOLDS)
        for w,key in keys.items():
            task=orders.read(db,key,workbook=True)
            history=lifecycle.history(case.path,case.heads[w],'payment')
            assert len(history)==1 and history[0]['version']==1
            data['originals'][w]={'online':key,'internal':task['internal'],'gid':task['gid'],
                                  'binding':task['proof']['binding'],'payment_digest':history[0]['digest']}
            gen,_=generations.read_generation(db,task['gid'])
            if w==wh:
                data['original_batch']=gen['batch_id']
            lines=task['payload']['lines']
            items=[{'sku':sku,'qty':qty,'price':price,'amount':str(Decimal(qty)*Decimal(price))} for sku,qty,price in lines]
            portal[key]={**portal[key],'binding':task['proof']['binding'],'online':key,'contact_ok':True,'buyer_check':'verified',
                         'lines':lines,'amount':str(sum(Decimal(i['amount']) for i in items)),
                         'items':items,'detail_count':len(items),'currency':'CNY','freight':'0'}
    if kind=='legacy':
        # Seed a genuinely pre-authorized queued intent, then restore the rc13
        # cancellation marker verbatim before the new EXE's first lookup.
        with closing(case.work.connect()) as db,db:
            prior=db.execute('SELECT stage,reason FROM jst_auto_orders WHERE online=?',(online,)).fetchone()
            db.execute("UPDATE jst_auto_orders SET stage='IMPORTED',reason='合成取消前' WHERE online=?",(online,))
    package=case.make_package(wh)
    validate_for_upload(case.path,package,case.config,SUPPLIERS)
    if kind=='legacy':
        queued=case.queue(wh,package)
        data.update(queued=queued['id'],lookup_failure=variant,phases=['exercise','reopen'])
        with closing(case.work.connect()) as db,db:
            db.execute('UPDATE jst_auto_orders SET stage=?,reason=? WHERE online=?',(*prior,online))
    template=read(root/'isolated-template.json')
    data['template']=template
    state={'records':{},'receipts':{},'uploads':[],'acknowledged':[]}
    key=package['id']
    if kind=='redo':
        qty=12 if variant=='correct' else QTY[wh]
        data.update(decision=variant,redo_quantity=qty,phases=['exercise','allocate','reopen'])
        if variant=='correct':
            sheet(root/'return-correction.xlsx',h,'return',qty)
            sheet(root/'payment-correction.xlsx',h,'payment',qty,Decimal(qty*PRICE[wh]))
    if kind in ('redo','legacy'):
        data['old_preview']=package
        portal[online].update(native_status='已取消',status='review')
        if kind=='legacy':
            state['records'][key]=native_record(package,case.ax.records[key])
    if kind=='amount':
        completed=variant=='completed'
        queued=case.queue(wh,package)
        original=case.runner().run(queued['id'])['items'][0]
        assert original['state']=='BOUND'
        record=native_record(package,case.ax.records[key])
        observe=new_receipt(root,record)
        write(root/'final-observation.json',observe)
        current=observe if completed else {**observe,'state':'IDLE','terminal':False,'terminalKind':None,
                                            'summary':{'status':'采购中'},'manifest':None}
        case.workflow.configure_schedules([record],5,True)
        receipt_job=case.workflow.receipt([record])
        case.workflow.observe_schedule(key,current)
        data.update(completed=completed,bound=key,receipt_job=receipt_job['id'],phases=['exercise','reopen'],
                    bound_index=str(Path(package['plan_file']).parent/'本地任务索引.json'))
        state.update(records={key:record},receipts={key:current},uploads=[key])
        sheet(root/'payment-correction.xlsx',h,'payment',QTY[wh],
              Decimal(package['intent']['totalAmount'])+Decimal('1.23'))
    (root/'exports').mkdir()
    export_settings.save(root/'settings'/export_settings.FILENAME,str(root/'exports'))
    write(root/'synthetic.json',data)
    write(root/'controlled-jst.json',portal)
    write(root/'controlled-ax.json',state)
    if state['records']:
        fake=Ax(root,data,component)
        for record in state['records'].values():
            fake.persist(record)
    return data


def run(command, folder, phase, build, *, headless=False):
    env=os.environ.copy()
    for key in ('QT_PLUGIN_PATH','PYTHONHOME','PYTHONPATH','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):
        env.pop(key,None)
    env.update(LOCALAPPDATA=str(folder/'test-appdata'),APPDATA=str(folder/'test-roaming'),
               REPLENISHMENT_DATA_DIR=str(folder/'settings'),PYTHONUTF8='1',PYTHONHASHSEED='0')
    if build:
        env['PATH']=os.pathsep.join((str(Path(env['WINDIR'])/'System32'),env['WINDIR']))
    if headless:
        env['QT_QPA_PLATFORM']='offscreen'
    started=time.monotonic()
    out=folder/(phase+'-report.json')
    with (folder/(phase+'-process.log')).open('xb') as log:
        p=subprocess.Popen([*command,'--order-redo-smoke',str(folder),phase],cwd=folder,env=env,
                           stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code=p.wait(135)
        except subprocess.TimeoutExpired:
            write(folder/(phase+'-timeout.json'),{'pid':p.pid,'executable':command[0]})
            # This is the exact isolated child launched above.
            p.terminate()
            p.wait(15)
            raise
    result=read(out) if out.is_file() else {}
    assert code==0 and result.get('passed'),(folder.name,phase,code,result.get('error'),str(out))
    assert result['frozen_exe']==bool(build) and result['synthetic'] is True and result['live_platform'] is False
    if build:
        assert all(result['build'][k]==build[k] for k in ('source_commit','build_id','source_archive_sha256'))
    result.update(pid=p.pid,elapsed=round(time.monotonic()-started,3),exit_code=code)
    return result


def matrix():
    return ([('legacy','河北仓',failure) for failure in ('missing','identity','timeout')]
            +[('redo',wh,choice) for wh in QTY for choice in ('reuse','correct','stop')]
            +[('amount',wh,stage) for wh in QTY for stage in ('pending','completed')])


def name_of(case):
    return '-'.join(case)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--worktree',type=Path,required=True)
    p.add_argument('--old-evidence',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--delivery',type=Path)
    p.add_argument('--source',type=Path)
    p.add_argument('--preserved',type=Path,required=True)
    p.add_argument('--case',action='append',default=[])
    a=p.parse_args()
    assert bool(a.delivery)!=bool(a.source)
    sys.path[:0]=[str(a.worktree/'src'),str(a.worktree/'tests'),
                 str(a.worktree/'tools'),str(Path(__file__).resolve().parent)]
    from verify_jst_one_click_exe import pack, contents
    folder=a.out.resolve()
    folder.mkdir(parents=True,exist_ok=False)
    before=read(a.preserved)
    assert all(sha(f)==digest for f,digest in before.items())
    metadata=None
    if a.delivery:
        pending, unpacked, expected_files=pack(a.delivery,folder)
        metadata=read(unpacked/'_internal/build_info.json')
        command=[str(unpacked/'补货助手.exe')]
        component=unpacked/'_internal/components/aoxiang'
        assert metadata['build_id']=='rc14'
    else:
        command=[sys.executable,str(a.source.resolve()/'src/app.py')]
        component=a.worktree/'components/aoxiang'
    cases=[spec for spec in matrix() if not a.case or name_of(spec) in a.case]
    reports=[]
    for spec in cases:
        case=folder/name_of(spec)
        data=seed(case,spec,a.old_evidence.resolve(),component)
        case_results=[]
        for phase in data['phases']:
            if data['kind']=='amount' and phase=='reopen':
                from aoxiang_directory import atomic_json
                controlled=read(case/'controlled-ax.json')
                controlled['receipts'][data['bound']]=read(case/'final-observation.json')
                atomic_json(case/'controlled-ax.json',controlled)
            result=run(command,case,phase,metadata,headless=not metadata)
            details={'case':name_of(spec),**result}
            case_results.append(details)
            print(name_of(spec),phase,'PASS',result['elapsed'],flush=True)
        logs=case/'.settings.diagnostics/application.jsonl'
        events=[json.loads(line)['event'] for line in logs.read_text('utf-8').splitlines()]
        assert events.count('STOPPED')==len(data['phases']),events
        assert not {'EXCEPTION','UNRAISABLE','THREAD_EXCEPTION','ERROR_DIALOG_FAILED'} & set(events),events
        reports.extend(case_results)
    assert all(sha(f)==digest for f,digest in before.items())
    summary={'passed':True,'build':metadata,'case_count':len(cases),'runs':reports,'old_sha256':before}
    if metadata:
        assert contents(unpacked)==expected_files
        name=a.delivery/'补货助手_0.54.16_rc14_订单撤回重做.zip'
        assert not name.exists()
        summary.update(exe_sha256=expected_files['补货助手.exe'][1],zip_sha256=sha(pending),
                       zip_bytes=pending.stat().st_size,files=len(expected_files))
        # Only the complete matrix seals a delivery ZIP.
        if cases==matrix():
            pending.rename(name)
            assert sha(name)==summary['zip_sha256']
            summary['zip']=str(name)
            write(folder/'文件清单_SHA256.json',expected_files)
    write(folder/'独立EXE验收结果.json',summary)


if __name__=='__main__':
    main()
