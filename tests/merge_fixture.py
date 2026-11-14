"""Isolated real signed/lifecycle/allocation records for merge-import tests.

No browser or platform requests. The few SQL identity edits happen before any
downstream record exists; lifecycle and allocation facts use production APIs.
"""
import copy
import hashlib
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path

import openpyxl
import archive_orders
import deletion_barrier
import jst_batches
import jst_orders
import lifecycle_io
import normal_orders
import operations_flow
import order_lifecycle
import order_migration
import purchases
import test_archive_orders
from test_jst import catalog
import warehouse_catalog
import work_repository

DAY='2026-11-13'


def file_hashes(root):
    root=Path(root)
    return {p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file()}


def table_rows(path,table,*,where='',args=(),drop=()):
    with closing(sqlite3.connect(path)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name=? AND type='table'",(table,)).fetchone():return []
        columns=[r[1] for r in db.execute('PRAGMA table_info("'+table+'")')]
        names=[n for n in columns if n not in drop]
        return db.execute('SELECT '+','.join('"'+n+'"' for n in names)+' FROM "'+table+'"'+(' WHERE '+where if where else '')+' ORDER BY rowid',args).fetchall()


class Order:
    def __init__(self,folder,code='SOURCE',oid=None,day=DAY,number=None):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True)
        self.work,self.repo,self.payload=test_archive_orders.seed(self.folder)
        self.root=self.work.path.parent;self.path=self.work.path
        self.oid=oid or 'synthetic-oid-'+code;self.code=code;self.day=day
        self.number=number or 'BH-'+day.replace('-','')+'-'+code+'-ZC';self.count=0
        with closing(sqlite3.connect(self.path)) as db,db:
            original=db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',('frozen',)).fetchone()[0]
            work=normal_orders.decode(original,db)
            payload=copy.deepcopy(self.payload);payload['work_revision']='work-'+self.oid
            payload['result'].update(store=code,store_name='合成门店 '+code)
            for r in payload['result']['rows']:r.update(B=code,A='合成门店 '+code)
            db.execute('UPDATE normal_orders SET day=?,store=?,oid=?,number=?',(day,code,self.oid,self.number))
            db.execute('UPDATE normal_revisions SET oid=?,payload=?,digest=?',(self.oid,normal_orders.encode(payload,db),normal_orders.fingerprint(payload['result'])))
            db.execute('UPDATE normal_work_revisions SET revision=?,day=?,payload=?',('work-'+self.oid,day,normal_orders.encode(work,db)))
            self.payload=payload
        self.refresh_heads()

    def refresh_heads(self):
        self.heads=purchases.targets(self.path)
        self.h=next(h for h in self.heads if h['warehouse']=='河北仓' and h['oid']==self.oid)
        return self.h

    def save(self,stage='return',quantity=5,*,warehouse='河北仓',amount=12,number=None):
        self.count+=1
        head=next(h for h in purchases.targets(self.path) if h['oid']==self.oid and h['warehouse']==warehouse)
        sku='0001' if warehouse=='河北仓' else '0002'
        path=self.folder/f'{stage}-{self.count}.xlsx';book=openpyxl.Workbook();ws=book.active
        fields=lifecycle_io.fields(stage);ws.append(fields)
        row={'补货单号':head['number'],'签发修订':head['revision'],'门店编码':self.code,'供货仓':warehouse,
             '商品编码':sku,'商品名称':'合成商品 '+sku,'数量单位':'库存单位',lifecycle_io.QUANTITIES[stage]:quantity,
             lifecycle_io.DOCNUM[stage]:number or 'DOC-'+stage+'-'+self.oid,
             lifecycle_io.DOCDATE[stage]:self.day,'实际金额':amount,'币种':'CNY'}
        ws.append([row.get(f) for f in fields]);book.save(path);book.close()
        info=lifecycle_io.inspect([path])[0];sheet=info['sheets'][0]
        scan=lifecycle_io.scan(stage,[{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
        bindings={order_lifecycle.binding_key(r):head for r in scan['rows']}
        plan=order_lifecycle.prepare(self.path,scan,bindings)
        return order_lifecycle.commit(self.path,plan)

    def complete(self,*,stock=7):
        with closing(sqlite3.connect(self.path)) as db,db:
            operations_flow.schema(db);db.execute('INSERT INTO lifecycle_routes VALUES(?,?,2)',(self.oid,1))
        self.save('return',40);self.save('operations',12)
        jst_orders.commit_refresh(self.path,jst_orders.prepare_refresh(self.path,'河北仓',catalog=catalog(stock=stock)))
        result=jst_orders.commit(self.path,jst_orders.prepare(self.path,[self.h]))
        if result['errors']:raise AssertionError(result)
        self.gid=result['generated'][0]
        order_lifecycle.commit(self.path,jst_orders.payment_plan(self.path,self.gid))
        self.save('receipt',stock)
        return self.gid

    def signed(self):return self.repo.get(self.day,self.code)

    def view(self):return order_lifecycle.snapshot(self.path,self.h)

    def export(self,path):return order_migration.export(self.root,path)


def second_order(order,code='OTHER',*,oid=None,number=None,day=None):
    """Independent signed identity in the same source DB, before downstream use."""
    oid=oid or 'synthetic-oid-'+code;day=day or DAY;number=number or 'BH-'+day.replace('-','')+'-'+code+'-ZC'
    payload=copy.deepcopy(order.payload);payload['work_revision']='work-'+oid
    payload['result'].update(store=code,store_name='合成门店 '+code)
    for r in payload['result']['rows']:r.update(B=code,A='合成门店 '+code)
    with closing(sqlite3.connect(order.path)) as db,db:
        original=db.execute('SELECT payload FROM normal_work_revisions LIMIT 1').fetchone()[0]
        work=normal_orders.decode(original,db)
        db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,?)',(day,code,'normal_model',oid,number,1))
        db.execute('INSERT INTO normal_revisions VALUES(?,?,?,?,?)',(oid,1,normal_orders.fingerprint(payload['result']),work_repository.now(),normal_orders.encode(payload,db)))
        db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('work-'+oid,day,normal_orders.encode(work,db)))
    return next(h for h in purchases.targets(order.path) if h['oid']==oid and h['warehouse']=='河北仓')


def save_return(order,head,quantity=5):
    old=(order.h,order.oid,order.code)
    try:
        order.h=head;order.oid=head['oid'];order.code=head['store'];return order.save('return',quantity)
    finally:order.h,order.oid,order.code=old


def stage(source,target,package):
    source.export(package)
    return order_migration.prepare(target,package)


def shared_batch(order,other,stock=10):
    save_return(order,order.h,8);save_return(order,other,8)
    preview=jst_batches.preview_new(order.path,[other,order.h],{'河北仓':catalog(stock=stock)},manual_order=True)
    result=jst_batches.execute(order.path,preview)
    if result['errors']:raise AssertionError(result)
    return result


def keep_local_files(order):
    order.work.save_preset('本机参数必须保留',__import__('parameter_schema').standard())
    with closing(sqlite3.connect(order.path)) as db,db:
        db.execute('INSERT INTO stores VALUES(?,?)',('LOCAL',json.dumps({'name':'本机门店','minimum':0},ensure_ascii=False)))
    warehouse_catalog.Repository(order.root).save(catalog(stock=999))
    for name,value in [('must_sell.json','{"synthetic":"本机必卖清单"}'),('store_supply.json','{"synthetic":"本机供货设置"}'),('connection.json','{"synthetic":"本机连接"}'),('analysis/keep.bin','独立分析数据')]:
        p=order.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value,'utf-8')
    return {name:(order.root/name).read_bytes() for name in ('must_sell.json','store_supply.json','connection.json','analysis/keep.bin','warehouse_catalogs/河北仓.json')}


def shared_purchase(order,heads):
    import purchase_io
    path=order.folder/'shared-purchase.xlsx';book=openpyxl.Workbook();ws=book.active;ws.append(purchase_io.FIELDS)
    row={'来源系统':'合成ERP','采购订单号':'SYNTHETIC-SHARED-PO','采购明细号':'LINE-001','下单日期':DAY,
         '门店编码':heads[0]['store'],'供货仓':'河北仓','商品编码':'0001','商品名称':'共同采购商品',
         '本版实际下单数量':10,'数量单位':'库存单位'}
    ws.append([row.get(f) for f in purchase_io.FIELDS]);book.save(path);book.close()
    info=purchase_io.inspect([path])[0];sheet=info['sheets'][0]
    scan=purchase_io.scan([{**info,**sheet,'mapping':{h:i for i,h in enumerate(sheet['headers'])}}])
    scan['rows'][0]['links']=[purchases.link(h,n) for h,n in zip(heads,[2,3])]
    return purchases.commit(order.path,purchases.prepare(order.path,scan))


def purchase_ledger(order,*,platform_id=None):
    """Real local Node StateStore/PurchaseCreates; synthetic response, no browser."""
    component=Path(__file__).resolve().parents[1]/'components/aoxiang'
    state=order.root/'aoxiang/component-state';assets=state/'assets';assets.mkdir(parents=True,exist_ok=True)
    upload=assets/'synthetic-upload.xlsx';book=openpyxl.Workbook();book.active.append(['商品编码','数量']);book.active.append(['0001',2]);book.save(upload);book.close()
    template=assets/'synthetic-template.json';template.write_text('{"synthetic":true}','utf-8')
    command='purchase-'+order.oid
    identity={'accountId':'SYNTHETIC-USER','merchantCode':'SYNTHETIC-MERCHANT'}
    h='a'*64;head=order.h
    intent={'schema':1,'operation':'purchase.create','id':command,'identity':identity,'mappingVersion':h,
            'basis':{'number':head['number'],'warehouse':head['warehouse'],'store':head['store'],'oid':head['oid'],
                     'revision':head['revision'],'signedDigest':head['digest'],'visibilityToken':head.get('visibility_token',0),
                     'planFingerprint':h,'paymentVersion':1,'paymentDigest':h},
            'target':{'buyerWarehouseCode':head['store'],'displayName':'合成门店'},'supplierCode':'2078246',
            'marker':'AX_CREATE:'+command,'note':'AX_CREATE:'+command+' 本地单:'+head['number'],
            'createExternalOrder':False,'postCreationState':'待提交',
            'file':{'path':str(upload),'sha256':hashlib.sha256(upload.read_bytes()).hexdigest(),'bytes':upload.stat().st_size},
            'template':{'recordFile':str(template),'sha256':h,'attemptId':'offline-synthetic'},
            'rows':[{'sku':'0001','name':'合成商品','quantity':'2','amount':'2'}],'totalQuantity':'2','totalAmount':'2'}
    intent_file=assets/'synthetic-intent.json';intent_file.write_text(json.dumps(intent,ensure_ascii=False),'utf-8')
    script="""import fs from 'node:fs/promises';
import {StateStore} from './src/state.mjs';import {PurchaseCreates} from './src/purchase-create-ledger.mjs';
import {canonical,sha256} from './src/common.mjs';import {IMPORT_CODE} from './src/purchase-create-contract.mjs';
const [root,file,platform]=process.argv.slice(1),p=JSON.parse(await fs.readFile(file,'utf8'));
const state=await StateStore.open(root),ledger=await PurchaseCreates.open(state);
try{
 let r=ledger.reserve(p,sha256(canonical(p))).record;
 for(const phase of ['PREPARING','READY','SUBMITTING'])r=ledger.update(p.id,r.sequence,phase,phase==='SUBMITTING'?{uploadAttempted:true}:{});
 if(!platform){r=ledger.update(p.id,r.sequence,'NEEDS_REVIEW',{error:{code:'SUBMISSION_UNKNOWN',message:'Synthetic unknown receipt; no platform called'}});}
 else{
  const receipt={intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261113120000@0528',code:IMPORT_CODE,fileSha256:p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:'a'.repeat(64)};
  r=ledger.update(p.id,r.sequence,'ACCEPTED',{receipt});
  r=ledger.update(p.id,r.sequence,'VERIFYING',{});
  const evidence={identity:p.identity,task:{taskId:receipt.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'1',failCount:'0'},candidates:[platform],main:{orderId:platform,store:p.basis.store,supplier:p.supplierCode,status:'待提交',externalOrderId:'',marker:p.note,quantity:'2',amount:'2',skuCount:'1'},rows:p.rows.map(x=>({...x,orderId:platform,store:p.basis.store,supplier:p.supplierCode,status:'待提交',note:p.note})),files:['D16','D17'].map(source=>({source,sha256:p.file.sha256,file:p.file.path})),observedAt:'2026-11-13T12:00:00Z'};
  r=ledger.bind(p.id,evidence);
 }
 console.log(JSON.stringify(r));
}finally{state.close();}
"""
    process=subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(state),str(intent_file),platform_id or ''],
                           cwd=component,env={**os.environ,'REPLENISHMENT_BUSINESS_DB':str(order.path)},capture_output=True,encoding='utf-8')
    if process.returncode:raise AssertionError(process.stderr)
    deletion_barrier.register_package(order.path,order.oid,command)
    return json.loads(process.stdout)


def read_purchase_ledger(root,command):
    component=Path(__file__).resolve().parents[1]/'components/aoxiang'
    script="""import {StateStore} from './src/state.mjs';import {PurchaseCreates} from './src/purchase-create-ledger.mjs';
const s=await StateStore.open(process.argv[1]);try{const ledger=await PurchaseCreates.open(s);console.log(JSON.stringify(ledger.get(process.argv[2])));}finally{s.close();}
"""
    process=subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(Path(root)/'aoxiang/component-state'),command],
                           cwd=component,capture_output=True,encoding='utf-8')
    if process.returncode:raise AssertionError(process.stderr)
    return json.loads(process.stdout)


def machine_command(order):
    """Reserve a schema-valid local collection command without executing it."""
    from aoxiang_jobs import Jobs
    component=Path(__file__).resolve().parents[1]/'components/aoxiang'
    script="""import {StateStore} from './src/state.mjs';import {MachineCommands,mappingVersion} from './src/machine-commands.mjs';import {freshConfig} from './src/config.mjs';
const [root,oid,store]=process.argv.slice(1),s=await StateStore.open(root);
try{
 const commands=new MachineCommands(s),config=freshConfig({accountId:'SYNTHETIC-USER',merchantCode:'SYNTHETIC-MERCHANT',displayName:'合成商家'},[{buyerWarehouseCode:store,displayName:'合成门店',operatingStores:[],mappingSource:'isolated-synthetic',mappingVerifiedAt:'2026-11-13T00:00:00.000Z'}]);
 const request={protocol_version:1,command_id:'machine-'+oid,operation:'replenishment.export',consumer:{purpose:'replenishment',owner:oid},account_binding:{account_id:'SYNTHETIC-USER',merchant_code:'SYNTHETIC-MERCHANT'},scope:{warehouse_codes:[store]},basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'reuse'}};
 const r=(await commands.reserve(request,config)).record;
 console.log(JSON.stringify({record:r,runs:r.items.map(x=>s.getRun(x.run_id)),items:r.items.flatMap(x=>s.getItems(x.run_id)),events:commands.events(r.id)}));
}finally{s.close();}
"""
    process=subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(order.root/'aoxiang/component-state'),order.oid,order.code],
                           cwd=component,capture_output=True,encoding='utf-8')
    if process.returncode:raise AssertionError(process.stderr)
    value=json.loads(process.stdout);record=value['record'];jobs=Jobs(order.root/'aoxiang/jobs.sqlite3')
    jobs.register(record['request'],{'purpose':'replenishment','order_ref':{k:order.h[k] for k in ('oid','number','warehouse','revision','day','store')},'day':order.day,'stores':[{'buyerWarehouseCode':order.code,'displayName':'合成门店'}]})
    jobs.observe(record['id'],record)
    return value


def read_machine_and_kick(root,command):
    component=Path(__file__).resolve().parents[1]/'components/aoxiang'
    script="""import {StateStore} from './src/state.mjs';import {MachineCommands} from './src/machine-commands.mjs';import {MachineRuntime} from './src/machine-runtime.mjs';
const s=await StateStore.open(process.argv[1]);try{
 const commands=new MachineCommands(s),visits=[],runtime=new MachineRuntime({commands,executeItem:async x=>{visits.push(x.item.run_id);throw Error('Unexpected automatic execution of imported job');}});
 runtime.kick();await runtime.idle();const record=commands.get(process.argv[2]);
 console.log(JSON.stringify({record,runs:record.items.map(x=>s.getRun(x.run_id)),items:record.items.flatMap(x=>s.getItems(x.run_id)),events:commands.events(record.id),visits}));
 await runtime.shutdown();
}finally{s.close();}
"""
    process=subprocess.run([str(component/'runtime/node.exe'),'--input-type=module','-e',script,str(Path(root)/'aoxiang/component-state'),command],
                           cwd=component,capture_output=True,encoding='utf-8')
    if process.returncode:raise AssertionError(process.stderr)
    return json.loads(process.stdout)
