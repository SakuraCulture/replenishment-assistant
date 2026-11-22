"""Controlled replies for the explicit, isolated order-redo EXE acceptance."""
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import openpyxl

from aoxiang_client import ComponentError as AxError
from aoxiang_directory import atomic_json, file_hash
from catalog_sync_client import ComponentError as JstError
import catalog_sync_validation
from jst_one_click_fixture import Component
import warehouse_catalog


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class Jst(Component):
    def __init__(self, root, data, phase):
        super().__init__(root/'jst-fixture')
        self.path = root/'controlled-jst.json'
        self.orders = load(self.path)
        self.target = data['warehouse']
        self.original = data['originals'][self.target]['online']
        self.failure = data.get('lookup_failure') if phase=='exercise' else None
        self.drop_import_response = data['kind']=='redo' and phase=='allocate'
        self.stock = data['stock']
        self.price = data['price']
        self.accounts = []
        for wh, reference in data['originals'].items():
            b = reference['binding']
            self.accounts.append({'id':wh,'warehouse':wh,'enabled':True,'mappingVerified':True,
                                  'expectedCompany':b['company'],'expectedCoid':b['coid'],
                                  'expectedUid':b['uid'],'bindingVersion':b['version']})

    def request(self, action, *, spec=None, batch_id=None, **args):
        if action != 'sync.provide_observation':
            return super().request(action,spec=spec,batch_id=batch_id,**args)
        assert self.running
        item, = self.batches[batch_id]['items']
        wh = item['warehouse']
        assert wh==self.target
        dest = self.folder/'downloads'/batch_id/item['id']
        dest.mkdir(parents=True)
        source = dest/'库存.xlsx'
        book = openpyxl.Workbook()
        book.active.append(warehouse_catalog.REQUIRED+['商品标签','商品名称'])
        # Both warehouses have SKU 00000, with independent stock and prices.
        book.active.append(['00000','隔离测试',self.price[wh],1,'启用',self.stock[wh],'','合成商品'])
        book.save(source)
        book.close()
        sha = file_hash(source)
        receipt = catalog_sync_validation.validate({
            'schema':1,'warehouse':wh,'file':str(source),'sha256':sha,
            'expected_rows':1,'candidate':str(dest/'candidate.json')})
        item.update(stage='VALIDATED',observation=args['observation'],receipt=receipt,
                    native={'sha256':sha,'nativeTaskRef':'isolated-redo','scope':{
                        'coid':item['account']['expectedCoid'],'uid':item['account']['expectedUid']}})
        return {}

    def order(self, action, spec, args):
        if action=='order.import':
            assert args['warehouse']==self.target
            try:
                return super().order(action,spec,args)
            finally:
                # Preserve the native order even when the reply is lost.
                atomic_json(self.path,self.orders)
        if action=='order.lookup' and spec['online']==self.original and self.failure:
            if self.failure=='missing':
                return {'found':False}
            if self.failure=='identity':
                result = super().order(action,spec,args)
                return {**result,'binding':{**result['binding'],'coid':'isolated-other-company','shop_id':'other-shop'}}
            if self.failure=='timeout':
                raise JstError('RESPONSE_UNKNOWN','隔离回查超时')
            raise AssertionError(self.failure)
        return super().order(action,spec,args)


def guard(component, db, intent_file, consume=True):
    script = """
import fs from 'node:fs';
import {checkHostDispatch} from './src/purchase-host-guard.mjs';
const intent=JSON.parse(fs.readFileSync(process.argv[1],'utf8'));
try{checkHostDispatch(intent,{consume:process.argv[2]==='1',requestHash:'e'.repeat(64)});console.log(JSON.stringify({ok:true}));}
catch(e){console.log(JSON.stringify({ok:false,code:e.code,message:e.message}));}
"""
    result = subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,
                             str(intent_file),str(int(consume))],cwd=component,
                            env={**os.environ,'REPLENISHMENT_BUSINESS_DB':str(db)},check=True,
                            capture_output=True,text=True,encoding='utf-8',timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout.splitlines()[-1])


class Ax:
    """All protocol calls terminate in the isolated fixture. Final dispatch uses bundled Node."""
    def __init__(self, root, data, component):
        self.root, self.data, self.component = root,data,component
        self.path = root/'controlled-ax.json'
        self.state = load(self.path)
        self.calls = []
        self.db = root/'settings/workbench.sqlite3'
        self.ledger = root/'settings/aoxiang/component-state/state.sqlite'
        self.ledger.parent.mkdir(parents=True,exist_ok=True)
        self.running = False

    def start(self):
        self.running = True

    def close(self, timeout=.5):
        self.running = False
        return True

    def persist(self, record=None):
        atomic_json(self.path,self.state)
        if record:
            with closing(sqlite3.connect(self.ledger)) as db,db:
                db.execute('CREATE TABLE IF NOT EXISTS purchase_creates(id TEXT PRIMARY KEY,state TEXT,platform_order TEXT,payload TEXT)')
                db.execute('INSERT OR REPLACE INTO purchase_creates VALUES(?,?,?,?)',
                           (record['id'],record['state'],(record.get('binding') or {}).get('orderId'),
                            json.dumps(record,ensure_ascii=False)))

    def request(self, action, **values):
        assert self.running, action
        key = values.get('purchase_id')
        self.calls.append([action,key])
        if action=='session.release':
            return {'idle':True}
        if action=='configuration':
            return self.data['ax_config']
        if action=='setup.check_login':
            return {'id':'isolated-login','state':'VERIFIED'}
        if action=='template.start':
            return {'id':'isolated-template','state':'READY','receipt':self.data['template']}
        if action=='template.status':
            return {'id':'isolated-template','state':'READY','receipt':self.data['template']}
        records, received = self.state['records'], self.state['receipts']
        if action in ('purchase.list','receipt.list'):
            rows = list((records if action=='purchase.list' else received).values())
            offset, limit = values.get('offset',0),values.get('limit',100)
            return {'items':rows[offset:offset+limit],'total':len(rows)}
        if action=='purchase.reserve':
            file = Path(values['package_file']).resolve()
            assert file.is_relative_to(self.root/'settings/aoxiang/purchase-packages')
            assert file_hash(file)==values['package_sha256']
            intent=load(file)
            key=intent['id']
            if key not in records:
                b = intent['basis']
                assert not any(r['state']=='BOUND' and r['basis']['number']==b['number'] for r in records.values())
                r = {'id':key,'intent':intent,'basis':b,'target':{'displayName':'隔离合成门店'},
                     'state':'RESERVED','sequence':1,'binding':None,'taskId':None,
                     'uploadAttempted':False,'active':False,'package_file':str(file)}
                records[key]=r
                self.persist(r)
            return {'record':dict(records[key])}
        if action=='purchase.status':
            return dict(records[key])
        if action=='purchase.submit':
            r = records[key]
            assert not r['uploadAttempted'] and r['state']=='RESERVED',r['state']
            outcome=guard(self.component,self.db,r['package_file'])
            if not outcome['ok']:
                raise AxError(outcome['code'],outcome['message'])
            self.state['uploads'].append(key)
            n=len(self.state['uploads'])
            r.update(state='ACCEPTED',uploadAttempted=True,taskId=f'EXE-TASK-{n}',sequence=2)
            self.persist(r)
            return dict(r)
        if action=='purchase.start':
            r=records[key]
            assert r['state'] in ('ACCEPTED','BOUND')
            if r['state']=='ACCEPTED':
                number=self.state['uploads'].index(key)+1
                r.update(state='BOUND',binding={'orderId':f'PO-EXE-{number:03d}','status':'待提交'},sequence=3)
                received[key]=new_receipt(self.root,r)
                self.persist(r)
            return dict(r)
        if action=='receipt.status':
            return dict(received[key])
        if action=='receipt.start':
            # A missing final marker remains pending until the inter-process fixture
            # publishes the retained original receipt during the next launch.
            return dict(received[key])
        if action=='receipt.synced':
            r=received[key]
            assert r['manifest']['sha256']==values['manifest_sha256']
            assert r['basis']['number']==values['receipt']['number']
            r.update(state='SYNCED',hostReceipt=values['receipt'],sequence=r['sequence']+1)
            self.state['acknowledged'].append(key)
            self.persist()
            return dict(r)
        raise AssertionError(f'隔离环境未开放请求 {action}')


def new_receipt(root, record):
    intent = record['intent']
    key = record['id']
    assert key.startswith('purchase-') and key[9:].isalnum()
    folder = root/'controlled-receipts'/key
    folder.mkdir(parents=True,exist_ok=False)
    po = record['binding']['orderId']
    common = {'orderId':po,'store':intent['basis']['store'],'supplier':intent['supplierCode'],'status':'已完成'}
    rows = [{**common,'sku':r['sku'],'name':r['name'],'unit':'库存单位','_sourceRow':i,
             'quantity':r['quantity'],'amount':r['amount'],
             'receivedQuantity':r['quantity'],'receivedAmount':r['amount']} for i,r in enumerate(intent['rows'],2)]
    main = {**common,'skuCount':len(rows),'quantity':intent['totalQuantity'],'amount':intent['totalAmount'],
            'receivedQuantity':intent['totalQuantity'],'receivedAmount':intent['totalAmount']}
    files = []
    for source, values in (('D16',[main]),('D17',rows)):
        file=folder/(source+'.xlsx')
        book=openpyxl.Workbook()
        book.active.append(list(values[0]))
        for row in values:book.active.append(list(row.values()))
        book.save(file)
        book.close()
        files.append({'source':source,'file':str(file),'sha256':file_hash(file)})
    manifest=folder/'实收观察.json'
    stamped='2026-11-22T04:00:00Z'
    atomic_json(manifest,{'schema':1,'operation':'purchase.receipt.observation',
        'purchaseId':key,'runId':key,'identity':intent['identity'],'basis':intent['basis'],'orderId':po,
        'supplierCode':intent['supplierCode'],'observedAt':stamped,'terminal':True,'eligible':True,
        'main':main,'rows':rows,'issues':[],'files':files,'quantityBasis':'native_actual_inventory_quantity_no_conversion'})
    return {'id':key,'state':'READY','active':False,'sequence':1,'terminal':True,'terminalKind':'COMPLETED',
            'scope':{'orderId':po},'basis':intent['basis'],'summary':{'status':'已完成','observedAt':stamped},
            'manifest':{'file':str(manifest),'sha256':file_hash(manifest)}}
