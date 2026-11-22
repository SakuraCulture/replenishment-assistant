"""Run two isolated GUI scenarios through the source app or a built Windows EXE."""

import argparse
import copy
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]


def write(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n', 'utf-8')


def markers(out):
    import export_settings
    (out/'exports').mkdir()
    export_settings.save(out/'settings'/export_settings.FILENAME, str(out/'exports'))
    write(out/'synthetic.json', {'synthetic':True, 'live_platform':False})


def payment_fixture(out):
    import openpyxl
    import lifecycle_io as io
    import normal_orders as normal
    import order_lifecycle as lifecycle
    import purchases
    import work_repository as wr
    from aoxiang_directory import Directory, atomic_json
    import test_lifecycle as tests
    from test_archive_orders import seed
    from test_jst import catalog
    from warehouse_catalog import Repository

    work, repo, payload = seed(out)
    f = tests.LifecycleTests()
    f.root, f.work, f.repo, f.payload, f.path, f.count = out, work, repo, payload, work.path, 0
    f.heads = purchases.targets(work.path)
    hb, sd = f.heads
    f.h = hb
    f.save('return', [{'门店确认数量':6}])

    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = '门店付款单'
    fields = io.fields('payment')
    sheet.append(fields)
    for head, sku, qty, amount in ((hb,'0001',6,15),(sd,'0002',1,3)):
        row = {'补货单号':head['number'],'签发修订':head['revision'],'门店编码':head['store'],
               '供货仓':head['warehouse'],'商品编码':sku,'商品名称':'合成商品','数量单位':'库存单位',
               io.QUANTITIES['payment']:qty,io.DOCNUM['payment']:'OFFLINE-'+head['warehouse'],
               io.DOCDATE['payment']:wr.today(),'实际金额':amount,'币种':'CNY'}
        sheet.append([row.get(field) for field in fields])
    book.save(out/'payment.xlsx')
    book.close()

    stores = [('T','测试店')]
    with closing(work.connect()) as db, db:
        for n in range(190):
            store=f'Q{n:03d}'
            oid='offline-paid-'+store
            frozen=copy.deepcopy(payload)
            result=frozen['result']
            result.update(store=store,store_name='合成门店 '+store)
            result['rows']=result['rows'][:1]
            stores.append((store,result['store_name']))
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',
                       (wr.today(),store,'normal_model',oid,'BH-'+store,1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       (oid,1,normal.fingerprint(result),wr.now(),normal.encode(frozen,db)))
            for stage in ('return','payment'):
                doc={'schema':1,'files':[],'rows':[{'sku':'0001','name':'合成商品',
                      'base_quantity':'4','amount':'8.00','currency':'CNY'}]}
                db.execute('INSERT INTO lifecycle_versions VALUES(?,?,?,?,?,?,?,?)',
                           (oid,1,'河北仓',stage,1,wr.now(),lifecycle.fingerprint(doc),wr.dumps(doc)))
    for warehouse in ('河北仓','山东仓'):
        Repository(work.path.parent).save(catalog(warehouse))
    folder = out/'settings'/'aoxiang'
    Directory(folder).save_suppliers({'河北仓':'2078246','山东仓':'ZXC2025'})
    markers(out)

    component = ROOT/'components'/'aoxiang'
    template = out/'offline-template.xlsx'
    shutil.copyfile(component/'test/fixtures/purchase-import-template.xlsx',template)
    targets = out/'offline-targets.json'
    write(targets,[{'buyerWarehouseCode':store,'displayName':name,'operatingStores':[],
                    'mappingSource':'offline-fixture','mappingVerifiedAt':'2026-11-02T00:00:00Z'} for store,name in stores])
    script = """
import fs from 'node:fs/promises';
import {StateStore} from './src/state.mjs';
import {freshConfig,saveConfig} from './src/config.mjs';
import {inspectPurchaseTemplate,templateContract} from './src/purchase-template.mjs';
const [root, targets, template] = process.argv.slice(1);
const state = await StateStore.open(root);
await saveConfig(root, freshConfig({accountId:'A', merchantCode:'M', displayName:'隔离合成商家'},
  JSON.parse(await fs.readFile(targets, 'utf8'))));
state.close();
console.log(JSON.stringify({...await inspectPurchaseTemplate(template), contractVersion:(await templateContract()).version}));
"""
    line = subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,
                           str(folder/'component-state'),str(targets),str(template)],cwd=component,
                          check=True,encoding='utf-8',capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
    receipt = json.loads(line.stdout)
    receipt.update(status='SUPPORTED',file=str(template),identity={'accountId':'A','merchantCode':'M'},
                   recordFile=str(out/'template-record.json'),latestFile=str(out/'template-latest.json'),
                   attemptId='offline-fixture',importSubmissions=0,uiProductLimit=None,source='isolated fixture')
    atomic_json(receipt['recordFile'],receipt)
    atomic_json(receipt['latestFile'],receipt)
    write(out/'fixture.json',{'paid':hb['number'],'bulk_selected':['BH-Q000-HB','BH-Q189-HB'],'template':receipt})


def allocation_fixture(out):
    import jst_batches as batches
    import jst_orders
    import normal_orders as normal
    import purchases
    import work_repository as wr
    from test_jst import catalog
    import test_order_analytics as fixtures

    sample = fixtures.AnalyticsTests()
    sample.setUp()
    try:
        day = sample.spec['from']
        def at(clock):
            return day+'T'+clock+':00+08:00'

        path = sample.path
        f = sample.f
        hb, sd = f.heads
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('UPDATE normal_revisions SET saved=?',(at('08:00'),))
            missing=copy.deepcopy(sample.p)
            missing['result'].update(store='U',store_name='合成历史未留存')
            missing['result']['rows']=missing['result']['rows'][:1]
            db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,'U','normal_model','unallocated','BH-U-ZC',1))
            db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',
                       ('unallocated',1,normal.fingerprint(missing['result']),at('08:00'),normal.encode(missing,db)))
        missing = next(h for h in purchases.targets(path) if h['store']=='U')
        with patch.object(wr,'now',return_value=at('09:00')):
            f.h = hb
            f.save('return', [{'门店确认数量':20}])
            f.h = sd
            f.save('return', [{'商品编码':'0002','供货仓':'山东仓','门店确认数量':5}])
        from warehouse_catalog import revision
        zero_catalog = catalog('山东仓',stock=0)
        zero_catalog['items'] = {'0002':{**zero_catalog['items']['0001'],'商品编码':'0002'}}
        zero_catalog['revision'] = revision(zero_catalog['items'])
        with patch.object(wr,'now',return_value=at('10:00')):
            first = batches.execute(path,batches.preview_new(path,[hb,sd],
                      {'河北仓':catalog(stock=100),'山东仓':zero_catalog}))
        assert not first['errors'] and len(first['generated'])==len(first['zero'])==1, first
        batch = jst_orders.get(path,first['generated'][0])['batch_id']
        f.h = hb
        with patch.object(wr,'now',return_value=at('11:00')):
            f.save('operations', [{'门店确认数量':15}])
        with patch.object(wr,'now',return_value=at('12:00')):
            current = batches.execute(path,batches.preview_existing(path,batch,[hb],correction=True))
        assert not current['errors'] and len(current['generated'])==1, current
        with patch.object(wr,'now',return_value=at('13:00')):
            f.save('operations', [{'门店确认数量':12}])
        shutil.copytree(path.parent,out/'settings')
        missing_cells=['未留存']*4
        stale=missing_cells[:3]+['待重新分配']
        by_cutoff=[]
        for cutoff, cells in ((None,stale),(at('10:30'),[100,100,20,'按回传分配']),
                               (at('11:30'),stale),(at('12:30'),[100,100,15,'按回传分配'])):
            by_cutoff.append({'as_of':cutoff,'rows':[
                [hb['number'],'0001',cells],[sd['number'],'0002',[0,0,0,'无可用库存']],
                [missing['number'],'0001',missing_cells]]})
        write(out/'fixture.json',{'day':day,'expected_orders':3,'cutoffs':by_cutoff})
    finally:
        sample.tearDown()
    markers(out)


def launch(out, command, flag, mode, timeout):
    env=os.environ.copy()
    for key in ('PYTHONPATH','PYTHONHOME','QT_PLUGIN_PATH','REPLENISHMENT_DATA_DIR','REPLENISHMENT_PORTABLE_PATHS','QT_QPA_PLATFORM'):
        env.pop(key,None)
    env.update(PYTHONUTF8='1',REPLENISHMENT_DATA_DIR=str(out/'settings'),
               LOCALAPPDATA=str(out/'localappdata'),APPDATA=str(out/'roaming'))
    if command[0]==sys.executable:
        env['QT_QPA_PLATFORM']='offscreen'
    with (out/(mode+'-process.log')).open('xb') as log:
        process = subprocess.Popen([*command,flag,str(out),mode],cwd=out,env=env,
                                   stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code=process.wait(timeout)
        except subprocess.TimeoutExpired:
            write(out/(mode+'-timeout.json'),{'pid':process.pid,'synthetic':True})
            raise
    report=json.loads((out/(mode+'-report.json')).read_text('utf-8'))
    report['exit_code']=code
    if code or not report['passed']:
        raise AssertionError(report.get('error') or report)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--exe',type=Path)
    parser.add_argument('--timeout',type=int,default=160)
    args=parser.parse_args()
    out=args.out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    pay, allocation=out/'payment',out/'allocation'
    pay.mkdir()
    allocation.mkdir()
    payment_fixture(pay)
    allocation_fixture(allocation)
    command=[str(args.exe.resolve())] if args.exe else [sys.executable,str(ROOT/'src/app.py')]
    result={'passed':False,'synthetic':True,'live_platform':False,
            'exe_sha256':hashlib.sha256(args.exe.read_bytes()).hexdigest() if args.exe else None,'runs':[]}
    try:
        for case, flag, mode in ((pay,'--payment-preview-smoke','exercise'),(pay,'--payment-preview-smoke','reopen'),
                                 (allocation,'--analytics-templates-smoke','allocation-revisions')):
            result['runs'].append(launch(case,command,flag,mode,args.timeout))
        result['passed']=True
    finally:
        write(out/'验收报告.json',result)
    print(json.dumps({'passed':True,'checks':sum(len(run['checks']) for run in result['runs']),
                      'report':str(out/'验收报告.json')},ensure_ascii=False))


if __name__=='__main__':
    main()
