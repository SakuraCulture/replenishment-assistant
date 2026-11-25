"""Verify both manual import entries from an extracted Windows candidate."""
import argparse
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
from unittest.mock import patch

from verify_jst_one_click_exe import ROOT, contents, seed as seed_two_warehouses, sha, write


def make_template(case, contract_path):
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    from openpyxl.worksheet.datavalidation import DataValidation

    contract=json.loads(contract_path.read_text('utf-8'))
    file=case/'controlled-template.xlsx'
    workbook=openpyxl.Workbook()
    ws=workbook.active
    ws.title='sheet1'
    for cell in contract['schema']['sheets'][0]['headers']:
        ws[cell['ref']]=cell['value']
    for row in (8,9):
        for column in range(1,13):
            cell=ws.cell(row,column,'示例')
            cell.font=Font(bold=True)
            cell.fill=PatternFill('solid',fgColor='AAFFAA')
    hidden=workbook.create_sheet('hidden_sheet1_col_10')
    hidden.sheet_state='hidden'
    hidden['K3001'],hidden['K3002']='是','否'
    validation=DataValidation(type='list',formula1='hidden_sheet1_col_10')
    ws.add_data_validation(validation)
    validation.add('K2:K3001')
    workbook.save(file)
    workbook.close()
    receipt={'status':'SUPPORTED','file':str(file),'sha256':sha(file),
             'contractVersion':contract['version'],'schema':copy.deepcopy(contract['schema']),
             'identity':{'accountId':'AX-QA','merchantCode':'MERCHANT-QA'},'uiProductLimit':100}
    return {**receipt,'recordFile':str(case/'controlled-template-receipt.json'),'attemptId':'QA-NATIVE'}


def seed(case, affected):
    import openpyxl
    from aoxiang_directory import Directory, atomic_json
    from aoxiang_purchase_tasks import prepare
    from aoxiang_workflows import Workflows
    import jst_auto_order
    import jst_batches
    import jst_order_store
    import purchases
    import work_repository as wr
    from jst_one_click_fixture import SKUS
    from platform_import_manual_fixture import JstManual
    import test_archive_orders
    from test_lifecycle import LifecycleTests

    make_row=test_archive_orders.row
    with patch.object(test_archive_orders,'row',side_effect=lambda **values:make_row(G='件',**values)):
        seed_two_warehouses(case)
    path=case/'settings/workbench.sqlite3'
    heads=sorted((h for h in purchases.targets(path) if h['day']==wr.today()),
                 key=lambda h:h['warehouse']!=affected)
    assert len(heads)==2 and len({h['oid'] for h in heads})==1
    batch=jst_batches.execute(path,jst_batches.preview_new(path,heads))
    assert not batch['errors'] and len(batch['generated'])==2
    with closing(wr.read_connection(path)) as db:
        jst=jst_auto_order.prepare(path,[{'head':h,'gid':jst_order_store.active(db,h)} for h in heads])
    assert not jst['failed'] and len(jst['ready'])==2
    class Connection(JstManual):
        def order(self, action, spec, args):
            if action=='order.import' and args['warehouse']==affected:
                saved=self.permits.pop(args['permit'])
                return {'dispatched':True,'receipt':{'online':saved['online'],'binding':saved['binding'],
                    'success_count':0,'fail_count':1,'text':'成功条数：0，失败条数：1',
                    'observed_at':'2026-11-25T12:15:17Z'}}
            return super().order(action,spec,args)
    portal=Connection(case)
    assert all(r['ok'] for r in jst_auto_order.run_browser(path,jst['ready'],portal,'preview'))
    from jst_auto_order_store import list_orders
    approvals={row['online']:row['sequence'] for row in list_orders(path)}
    submissions=jst_auto_order.run_browser(path,jst['ready'],portal,'submit',approved=approvals)
    assert len(submissions)==2 and submissions[0]['stage']=='IMPORT_FAILED' and submissions[1]['internal'], submissions
    write(case/'manual-jst-state.json',portal.orders)

    sample=LifecycleTests()
    sample.root,sample.path,sample.count=case,path,0
    for head in heads:
        sample.h=head
        sample.save('payment',[{'门店编码':head['store'],'供货仓':head['warehouse'],
                                '商品编码':SKUS[head['warehouse']],'付款单商品数量':4,
                                '实际金额':12}])
    suppliers=Directory(case/'settings/aoxiang').suppliers()
    identity={'accountId':'AX-QA','merchantCode':'MERCHANT-QA'}
    configuration={'configured':True,'enabled':True,'account_binding':{'account_id':identity['accountId'],
        'merchant_code':identity['merchantCode']},'store_mapping_version':'a'*64,
        'targets':[{'buyerWarehouseCode':heads[0]['store'],'displayName':heads[0]['name']}]}
    contract=ROOT/'components/aoxiang/assets/purchase-import-template-contract.json'
    receipt=make_template(case,contract)
    write(Path(receipt['recordFile']),receipt)
    workflows=Workflows(case/'settings/aoxiang')
    batch=workflows.prepare(heads)
    records={}
    for index, head in enumerate(heads):
        package=prepare(path,head,configuration,suppliers,receipt,workflows.folder/'purchase-packages',contract)
        atomic_json(Path(package['package_file']).parent/'本地任务索引.json',package)
        p=package['intent']
        number='PO-QA-'+('HB' if head['warehouse']=='河北仓' else 'SD')
        task='GEI@SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-MULTI-UNIT-IMPORT-V2@261125201517@'+str(701+index)
        record={'id':p['id'],'basis':p['basis'],'target':p['target'],'rows':len(p['rows']),
                'totalQuantity':p['totalQuantity'],'totalAmount':p['totalAmount'],'originalFile':{
                    'file':p['file']['path'],'sha256':p['file']['sha256']},'createdAt':wr.now(),
                'state':'NEEDS_REVIEW' if not index else 'BOUND','sequence':6,'taskId':task,
                'uploadAttempted':True,'confirmedImportRequests':1,'additionalTaskIds':[],
                'binding':None if not index else {'orderId':number,'status':'待入库'}}
        if not index:
            failure=case/'controlled-failure.xlsx'
            wb=openpyxl.Workbook()
            wb.active.append(['商品编码','失败原因'])
            wb.active.append([p['rows'][0]['sku'],'受控平台拒绝'])
            wb.save(failure)
            wb.close()
            record['importResult']={'taskId':task,'kind':'failed','errorCode':'IMPORT_TASK_FAILED',
                'successCount':0,'failCount':1,'nativeStatus':'部分失败','failureFile':{'taskId':task,
                    'file':str(failure),'sha256':sha(failure)}}
        records[p['id']]=record
        workflows.update(batch['id'],position=index,changes={'state':'PREPARED','package':package})
    batch=workflows.update(batch['id'],state='READY')
    upload=workflows.upload(batch['id'],[0,1],batch['sequence'])
    for index,record in enumerate(records.values()):
        workflows.update(upload['id'],position=index,changes={
            'state':'REVIEW' if not index else 'BOUND','submission_started':True,
            'purchase_observation':{'taskId':record['taskId'],'uploadAttempted':True,
                                    'importResult':record.get('importResult')},
            'result':{'binding':record['binding']} if index else None})
    write(case/'manual-component-state.json',records)
    write(case/'manual-fixture.json',{'synthetic':True,'live_platform':False,'warehouse':affected,
        'jst_keys':jst['ready'],'jst_seed_calls':portal.calls,'ax_job':upload['id'],'configuration':configuration})


def launch(exe, case, phase):
    env=os.environ.copy()
    for key in ('QT_QPA_PLATFORM','QT_PLUGIN_PATH','PYTHONHOME','PYTHONPATH','REPLENISHMENT_PORTABLE_PATHS'):
        env.pop(key,None)
    env.update(LOCALAPPDATA=str(case/'local'),APPDATA=str(case/'roaming'),
               REPLENISHMENT_DATA_DIR=str(case/'settings'),PYTHONUTF8='1')
    command=[str(exe)] if exe else [sys.executable,str(ROOT/'src/app.py')]
    with (case/(phase+'-app.log')).open('xb') as log:
        process=subprocess.Popen([*command,'--platform-import-manual-smoke',str(case),phase],
                                 env=env,cwd=case,stdout=log,stderr=subprocess.STDOUT,
                                 creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            code=process.wait(timeout=150)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=10)
            raise
    file=case/(phase+'-report.json')
    result=json.loads(file.read_text('utf-8')) if file.is_file() else {}
    assert code==0 and result.get('passed') and result['frozen_exe'] is bool(exe),(code,result,str(file))
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--zip',type=Path)
    parser.add_argument('--out',type=Path,required=True)
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
    cases={}
    for warehouse in ('山东仓','河北仓'):
        case=out/warehouse
        seed(case,warehouse)
        exercise,reopened=(launch(exe,case,phase) for phase in ('exercise','reopen'))
        for platform in ('jst','aoxiang'):
            assert exercise[platform]['binding']==reopened[platform]['binding']
        assert exercise['protected_facts']==reopened['protected_facts']
        cases[warehouse]={'exercise':exercise,'reopen':reopened}
    if exe:
        assert contents(app)==files
    write(out/'report.json',{'passed':True,'frozen_exe':bool(exe),'synthetic':True,'cases':cases,
                            'exe_sha256':sha(exe) if exe else None,'zip_sha256':sha(args.zip) if exe else None})
    print(out/'report.json')


if __name__=='__main__':
    main()
