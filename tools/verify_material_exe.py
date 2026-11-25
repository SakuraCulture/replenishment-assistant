"""Reopen a controlled rc60 database copy in the source app or an extracted Windows ZIP."""
import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import zipfile
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'tests')]


def write(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def seed(root,old_db,affected):
    import openpyxl
    import jst_auto_order
    import material_requests
    import payment_shop_names
    import work_repository as wr
    from test_jst_auto_order import CONTACT
    from test_material_orders import catalog, SKU, HB, SD

    root.mkdir()
    write(root/'synthetic.json',{'synthetic':True,'live_platform':False,'case':'material-orders'})
    settings = root/'settings'
    settings.mkdir()
    path = settings/'workbench.sqlite3'
    with closing(wr.read_connection(old_db)) as before, closing(sqlite3.connect(path)) as after:
        assert not before.execute("SELECT 1 FROM sqlite_master WHERE name='material_requests'").fetchone()
        before.backup(after)
    parents = None
    names = {'MAT-A':'合成甲店','MAT-B':'合成乙店'}
    with closing(sqlite3.connect(path)) as db, db:
        parents = db.execute('SELECT day,store,kind,oid,number,current FROM normal_orders ORDER BY oid').fetchall()
        db.executemany('INSERT INTO stores VALUES(?,?)',[(key,wr.dumps({'name':name})) for key,name in names.items()])
    catalog(root,SD,100)
    catalog(root,HB,145)
    other = HB if affected==SD else SD
    demand = (70,16,50) if affected==SD else (115,20,50)
    quantities = [str(demand[0]),str(demand[1]),'30']
    specs = [('MAT-A',affected,demand[0]),('MAT-A',other,demand[1]),('MAT-B',affected,demand[2])]
    for store,wh,_ in specs:
        change=payment_shop_names.prepare(dict(names=names,records={}),[dict(store=store,warehouse=wh,value=names[store]+'·'+wh)])
        payment_shop_names.save(path,change)
        jst_auto_order.save_profile(path,store,wh,CONTACT,None)
    book=openpyxl.Workbook()
    page=book.active
    page.title='物料需求'
    page.append(material_requests.HEADERS)
    for store, wh, qty in specs:
        page.append((store,wh,SKU,qty))
    book.save(root/'物料需求.xlsx')
    book.close()
    contract_path=ROOT/'components/aoxiang/assets/purchase-import-template-contract.json'
    contract=json.loads(contract_path.read_text(encoding='utf-8'))
    template=root/'受控翱象采购模板.xlsx'
    book=openpyxl.Workbook()
    page=book.active
    page.title='sheet1'
    for definition in contract['schema']['sheets'][0]['headers']:
        page[definition['ref']]=definition['value']
    page['K8']='否'
    page['K9']='是'
    hidden=book.create_sheet('hidden_sheet1_col_10')
    hidden.sheet_state='hidden'
    hidden['K3001'],hidden['K3002']='是','否'
    book.save(template)
    book.close()
    receipt=dict(status='SUPPORTED',file=str(template),sha256=digest(template),contractVersion=contract['version'],
                 schema=contract['schema'],identity={'accountId':'MAT','merchantCode':'MAT-EXERCISE'},uiProductLimit=100,
                 recordFile=str(root/'template-receipt.json'),attemptId='material-offline-template')
    write(root/'template-receipt.json',receipt)
    config=dict(configured=True,enabled=True,account_binding=dict(account_id='MAT',merchant_code='MAT-EXERCISE'),
                store_mapping_version='material-offline',targets=[dict(buyerWarehouseCode=code,displayName=name) for code,name in names.items()])
    write(root/'material-fixture.json',dict(synthetic=True,live_platform=False,affected=affected,sku=SKU,quantities=quantities,
          template_contract=str(contract_path),template_receipt=receipt,ax_config=config,suppliers={HB:'HB-TEST',SD:'SD-TEST'}))
    return parents


def invoke(command, case, phase):
    env = os.environ.copy()
    env.update(REPLENISHMENT_DATA_DIR=str(case/'settings'),LOCALAPPDATA=str(case/'test-appdata'),
               APPDATA=str(case/'test-appdata'),QT_QPA_PLATFORM='offscreen',PYTHONUTF8='1')
    if phase=='entry':
        env.pop('QT_QPA_PLATFORM',None)
    started=time.perf_counter()
    with (case/(phase+'.log')).open('wb') as output:
        process = subprocess.run([*command,'--material-smoke',str(case),phase],cwd=case,env=env,stdout=output,
                                 stderr=subprocess.STDOUT,timeout=140)
    result=json.loads((case/(phase+'-report.json')).read_text(encoding='utf-8'))
    if process.returncode or 'traceback' in result:
        raise RuntimeError(result.get('traceback',f'{phase} exit {process.returncode}'))
    result['wall_seconds']=round(time.perf_counter()-started,3)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--old-db',type=Path,required=True)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--warehouse',choices=('山东仓','河北仓'))
    parser.add_argument('--entry-only',action='store_true',help='从工作台验证手工录入与多选登记')
    args=parser.parse_args()
    root=args.out.resolve()
    root.mkdir(parents=True)
    old_sha=digest(args.old_db)
    if args.zip:
        unpacked=root/'空目录解包'
        with zipfile.ZipFile(args.zip) as book:
            assert book.testzip() is None
            book.extractall(unpacked)
        exe,=unpacked.rglob('补货助手.exe')
        command=[str(exe)]
    else:
        command=[sys.executable,str(ROOT/'src/app.py')]
    from test_material_orders import catalog
    reports=[]
    for affected in (args.warehouse,) if args.warehouse else ('山东仓','河北仓'):
        case=root/affected
        parents=seed(case,args.old_db,affected)
        before=invoke(command,case,'entry' if args.entry_only else 'register')
        assert before['frozen_exe'] is bool(args.zip)
        if args.entry_only:
            assert not before['calls']
            reports.append(dict(exception_warehouse=affected,registered=before))
        else:
            catalog(case,affected,60 if affected=='山东仓' else 90,postfix='-新')
            after=invoke(command,case,'resume')
            assert before['batch']==after['batch'] and before['rows']==after['rows']
            reports.append(dict(exception_warehouse=affected,registered=before,resumed=after))
        with closing(sqlite3.connect(case/'settings/workbench.sqlite3')) as db:
            unchanged=db.execute("SELECT day,store,kind,oid,number,current FROM normal_orders WHERE kind<>'material' ORDER BY oid").fetchall()
            assert unchanged==parents
    assert digest(args.old_db)==old_sha
    write(root/'逐仓验收.json',dict(reports=reports,old_source_sha256=old_sha,
          exe_sha256=digest(exe) if args.zip else None,zip_sha256=digest(args.zip) if args.zip else None))


if __name__=='__main__':
    main()
