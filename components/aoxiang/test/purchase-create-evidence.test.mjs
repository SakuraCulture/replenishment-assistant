import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createArchive} from '../src/zip.mjs';
import {dateBounds,exportScope,purchaseContract,purchaseScopeHash,PURCHASE_CODES} from '../src/purchase-contract.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {nativeCreationEvidence,PurchaseCreateService} from '../src/purchase-create-service.mjs';
import {checkCreatedPurchase,IMPORT_CODE} from '../src/purchase-create-contract.mjs';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {PurchaseCreates} from '../src/purchase-create-ledger.mjs';
import {MachinePurchaseCreate} from '../src/machine-purchase-create.mjs';

const escape=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
function col(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
async function workbook(file,source,rows){
  const c=purchaseContract(source),data=[c.columns.map(c=>c.rawHeader),...rows];
  const xml=data.map((r,i)=>`<row r="${i+1}">${r.map((v,j)=>`<c r="${col(j)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+xml+'</sheetData></worksheet>'}]);
}
test('native main/detail XLSX values are read by contract and strictly compared per SKU',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-create-proof-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const p={id:'p1',identity:{accountId:'A',merchantCode:'M'},basis:{store:'T'},supplierCode:'2078246',note:'AX_CREATE:p1 本地单:BH-1',file:{sha256:'a'.repeat(64)},rows:[{sku:'0001234567890123456789',quantity:'7',amount:'0.99'}],totalQuantity:'7',totalAmount:'0.99'};
  const main=Array(25).fill('');Object.assign(main,{0:'PO-1',1:'待提交',3:'T',5:'2078246',6:'1',7:'7',8:'0.99'});
  const detail=Array(43).fill('');Object.assign(detail,{0:'PO-1',1:'待提交',7:'T',9:'2078246',12:p.rows[0].sku,18:'7',26:'0.99',36:p.note});
  const files={D16:path.join(root,'main.xlsx'),D17:path.join(root,'detail.xlsx')};await workbook(files.D16,'D16',[main]);await workbook(files.D17,'D17',[detail]);
  const r={intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:'b'.repeat(64)};
  const task={taskId:r.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'1',failCount:'0'};
  const run={status:'SUCCEEDED',orderId:'PO-1',query:{orderIds:['PO-1']},exports:{}};
  for(const source of ['D16','D17'])run.exports[source]={asset:{rows:1,sha256:await fileHash(files[source])}};
  const evidence=await nativeCreationEvidence(p,task,{supplyOrderId:'PO-1',buyerMessage:p.note},{sourceFile:async(_,s)=>files[s]},run);
  assert.equal(evidence.rows[0].sku,p.rows[0].sku);assert.equal(evidence.rows[0].amount,'0.99');assert.equal(checkCreatedPurchase(p,r,evidence).orderId,'PO-1');
  detail[26]='';await workbook(path.join(root,'blank.xlsx'),'D17',[detail]);
  const old=files.D17;files.D17=path.join(root,'blank.xlsx');run.exports.D17.asset.sha256=await fileHash(files.D17);
  const blank=await nativeCreationEvidence(p,task,{supplyOrderId:'PO-1',buyerMessage:p.note},{sourceFile:async(_,s)=>files[s]},run);
  assert.equal(blank.rows[0].amount,'');assert.throws(()=>checkCreatedPurchase(p,r,blank),{code:'PURCHASE_DECIMAL'});assert.ok(await fileHash(old));
});

test('known-task service reaches native void proof and refreshes only completed old export runs',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-void-proof-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const p={id:'void-p',identity:{accountId:'A',merchantCode:'M'},basis:{store:'T'},target:{buyerWarehouseCode:'T',displayName:'合成店'},
    supplierCode:'S',note:'AX_CREATE:void-p 本地单:BH-1',file:{sha256:'a'.repeat(64)},rows:[{sku:'0001',quantity:'7',amount:'0.99'}],totalQuantity:'7',totalAmount:'0.99'};
  const receipt={intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:'b'.repeat(64)};
  const task={taskId:receipt.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'1',failCount:'0'};
  const main=Array(25).fill('');Object.assign(main,{0:'PO-void',1:'已作废',3:'T',5:'S',6:'1',7:'7',8:'0.99'});
  const detail=Array(43).fill('');Object.assign(detail,{0:'PO-void',1:'已作废',7:'T',9:'S',12:'0001',18:'7',26:'0.99',36:p.note});
  const files={D16:path.join(root,'main.xlsx'),D17:path.join(root,'detail.xlsx')};
  await workbook(files.D16,'D16',[main]);await workbook(files.D17,'D17',[detail]);
  const run={status:'SUCCEEDED',orderId:'PO-void',query:{orderIds:['PO-void']},exports:{}};
  for(const source of ['D16','D17'])run.exports[source]={asset:{rows:1,sha256:await fileHash(files[source])}};
  const scope={buyerWarehouseCode:'T',from:'2026-11-01',to:'2026-11-01',dateBasis:'created',orderId:'PO-void'};
  const saved={identity:p.identity,scopeHash:purchaseScopeHash(scope),status:'SUCCEEDED'};
  const candidate={supplyOrderId:'PO-void',buyerWarehouseCode:'T',sellerMerchantCode:'S',buyerMessage:p.note,
    status:'synthetic-void-code',gmtCreate:String(Date.parse('2026-11-01T10:00:00+08:00')),outOrderId:null,executionMainOrderId:null};
  let options,prior=saved,exports=0,rows=[candidate],importResult=task;
  const service=new PurchaseCreateService({state:{rows:()=>prior?[prior]:[]},ledger:{get:()=>({baseline:{from:'2026-11-01',orderIds:[]}})},
    coordinatorFactory:()=>({sourceFile:async(_,s)=>files[s],run:async o=>{exports++;options=o;return run;}})});
  service.context=async()=>({adapter:{purchaseImportTask:async q=>{assert.equal(q.taskId,receipt.taskId);return importResult;},
    queryCreateOrders:async()=>({rows})}});
  for(const status of ['SUCCEEDED','NEEDS_ATTENTION','PAUSED',null]){
    prior=status?{...saved,status}:null;const e=await service.observe(p,receipt,{});
    assert.equal(checkCreatedPurchase(p,receipt,e).status,'已作废');assert.equal(options.orderId,'PO-void');
    assert.equal(!!options.rerun,status==='SUCCEEDED');assert.equal(options.from,'2026-11-01');
  }
  for(const changed of [{buyerWarehouseCode:'OTHER'},{sellerMerchantCode:'OTHER'},{outOrderId:'EXTERNAL'},
    {executionMainOrderId:'EXTERNAL'},{status:null},{gmtCreate:null}]){
    rows=[{...candidate,...changed}];const before=exports;await assert.rejects(()=>service.observe(p,receipt,{}));assert.equal(exports,before);
  }
  rows=[candidate,{...candidate,supplyOrderId:'PO-extra'}];await assert.rejects(()=>service.observe(p,receipt,{}),{code:'PURCHASE_NOT_UNIQUE'});
  rows=[candidate];importResult={...task,failCount:'1'};await assert.rejects(()=>service.observe(p,receipt,{}),{code:'IMPORT_TASK_PARTIAL'});
  importResult=task;detail[26]='1.99';files.D17=path.join(root,'changed-detail.xlsx');await workbook(files.D17,'D17',[detail]);
  await assert.rejects(()=>service.observe(p,receipt,{}),{code:'PURCHASE_PROOF_CHANGED'});
});

for(const scenario of ['missing receipt','late receipt','changed amount','different owner']){
  test('D16 verification recovery from a saved import: '+scenario,async t=>{
    const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-d16-proof-'));
    let state=await StateStore.open(path.join(root,'state'));
    t.after(()=>state.close());
    let ledger=await PurchaseCreates.open(state);
    const hash='a'.repeat(64), day='2026-11-18', orderId='PO-D16-SYNTHETIC', id='synthetic-d16-purchase';
    const identity={accountId:'A',merchantCode:'M'},target={buyerWarehouseCode:'T',displayName:'合成门店'};
    const rows=Array.from({length:scenario==='missing receipt'?384:2},(_,n)=>({sku:'SKU-'+n,name:'合成商品',quantity:'1',amount:'1.00'}));
    const basis={oid:'SYNTHETIC-OID',number:'BH-D16-SYNTHETIC',warehouse:'河北仓',store:'T',planFingerprint:hash,paymentVersion:1,paymentDigest:hash};
    const p={schema:1,operation:'purchase.create',id,identity,mappingVersion:hash,basis,target,supplierCode:'S',marker:'AX_CREATE:'+id,
      note:'AX_CREATE:'+id+' 本地单:'+basis.number,createExternalOrder:false,postCreationState:'待提交',
      file:{path:path.join(root,'original.xlsx'),sha256:hash,bytes:300},
      template:{recordFile:path.join(root,'template.json'),sha256:hash,attemptId:'synthetic'},rows,
      totalQuantity:String(rows.length),totalAmount:String(rows.length)};
    const receipt={intentId:id,taskId:'GEI@'+IMPORT_CODE+'@261118155740@9999',code:IMPORT_CODE,fileSha256:hash,
      exactSourceBytesPresent:true,importRequests:1,requestSha256:hash};
    const task={taskId:receipt.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:rows.length,failCount:0};
    let record=ledger.reserve(p,hash).record;
    record=ledger.update(id,record.sequence,'PREPARING');
    record=ledger.update(id,record.sequence,'READY',{baseline:{from:day,orderIds:[]}});
    ledger.update(id,record.sequence,'SUBMITTING',{uploadAttempted:true});
    record=ledger.accepted(receipt);
    const runId='SYNTHETIC-D16-RUN',oldIntent='SYNTHETIC-D16-OLD';
    ledger.update(id,record.sequence,'NEEDS_REVIEW',{error:{code:'SUBMISSION_UNKNOWN',message:'旧版D16回执未知',
      details:{runId,sourceId:'D16',taskId:null}}});
    await state.enablePurchases();
    const binding={outputRoot:path.join(root,'export')};
    const scope={buyerWarehouseCode:'T',from:day,to:day,dateBasis:'created',orderId};
    const query={identity,total:1,orderIds:[orderId],orderSkuCounts:{[orderId]:rows.length},
      request:{storeCodeList:['T'],orderType:'3',merchantSubBizType:'CONSIGNEE',supplyOrderId:orderId,...dateBounds(day,day)}};
    const oldMain={sourceId:'D16',stage:'SUBMISSION_UNKNOWN',intentId:oldIntent,query,errorCode:'WORKER_RESPONSE_UNKNOWN'};
    const makeReceipt=(source,intentId,num)=>({intentId,code:PURCHASE_CODES[source],scope:exportScope(query.request),
      taskId:'GEI@'+PURCHASE_CODES[source]+'@261118155800@'+num});
    const late=makeReceipt('D16',oldIntent,100);
    const detailRows=rows.map(item=>{
      const r=Array(43).fill('');
      return Object.assign(r,{0:orderId,1:'待提交',7:'T',9:'S',12:item.sku,18:item.quantity,26:item.amount,
        36:p.note,40:day+' 15:58:00',41:day+' 15:58:00'});
    });
    const detail=path.join(binding.outputRoot,'original-detail.xlsx');
    await fs.mkdir(binding.outputRoot);
    await workbook(detail,'D17',detailRows);
    const detailEvidence={sourceId:'D17',stage:'SUCCEEDED',intentId:'SYNTHETIC-D17-EXISTING',
      taskId:makeReceipt('D17','SYNTHETIC-D17-EXISTING',102).taskId,taskSummary:{successCount:rows.length},
      asset:{relativePath:'original-detail.xlsx',sha256:await fileHash(detail),rows:rows.length}};
    const missingDetail=scenario==='missing receipt';
    const owner={hostOrderId:basis.oid,hostWarehouse:basis.warehouse,hostPurchaseId:id};
    const runOwner=scenario==='different owner'?{...owner,hostPurchaseId:'OTHER-PURCHASE'}:owner;
    state.putPurchase({id:runId,identity,target,...scope,...runOwner,scopeHash:purchaseScopeHash(scope),query,createdAt:day,
      status:'NEEDS_ATTENTION',exports:{D16:oldMain,D17:missingDetail?{sourceId:'D17',stage:'PLANNED'}:detailEvidence}});
    state.close();
    state=await StateStore.open(path.join(root,'state'));
    ledger=await PurchaseCreates.open(state);
    ledger.recover();
    const submitted=[];
    const adapter={
      purchaseImportTask:async request=>{assert.equal(request.taskId,receipt.taskId);return task;},
      queryCreateOrders:async()=>({rows:[{supplyOrderId:orderId,buyerWarehouseCode:'T',sellerMerchantCode:'S',buyerMessage:p.note,
        status:'0',gmtCreate:String(Date.parse(day+'T15:58:00+08:00'))}]}),
      queryPurchase:async request=>{assert.equal(request.orderId,orderId);return query;},
      preparePurchase:async(_,source)=>{
        assert.ok(source==='D16'||missingDetail&&source==='D17');
        if(scenario==='late receipt')assert.equal(state.recordPurchaseReceipt(late),true);
        return {taskIds:[]};
      },
      submitPurchase:async({sourceId,intentId})=>{
        assert.notEqual(intentId,oldIntent);
        submitted.push(sourceId);
        // A late ack for the former attempt must remain history, not attach
        // to this verification request.
        if(sourceId==='D16')assert.equal(state.recordPurchaseReceipt(late),true);
        return makeReceipt(sourceId,intentId,sourceId==='D16'?101:103);
      },
      purchaseTask:async(taskId,source)=>({taskId,taskCode:PURCHASE_CODES[source],taskStatus:'FINISHED',
        successCount:source==='D16'?1:rows.length,failCount:0}),
      downloadPurchase:async({directory,taskId,sourceId})=>{
        const file=path.join(directory,sourceId+'.xlsx');
        await fs.mkdir(directory,{recursive:true});
        if(sourceId==='D16'){
          const row=Object.assign(Array(25).fill(''),{0:orderId,1:'待提交',3:'T',5:'S',6:String(rows.length),
            7:p.totalQuantity,8:scenario==='changed amount'?'999':p.totalAmount});
          await workbook(file,'D16',[row]);
        }else{
          assert.equal(sourceId,'D17');
          assert.ok(missingDetail);
          await fs.copyFile(detail,file);
        }
        return {file,taskId,sha256:await fileHash(file)};
      }
    };
    const service=new PurchaseCreateService({state,ledger});
    service.context=async(_,signal)=>({config:freshConfig(identity,[target]),binding,adapter,signal,...owner});
    service.upload=()=>assert.fail('The accepted import must never be sent again');
    const machine=new MachinePurchaseCreate({ledger,adapter:service});
    machine.start(id);
    await machine.pending;
    const after=ledger.get(id), verified=state.getPurchase(runId);
    assert.deepEqual(after.receipt,receipt);
    assert.equal(state.rows('purchase_runs').length,1);
    if(!missingDetail)assert.equal(verified.exports.D17.taskId,detailEvidence.taskId);
    assert.equal(verified.exports.D17.asset.sha256,detailEvidence.asset.sha256);
    assert.equal(ledger.events(id).filter(e=>e.kind==='SUBMITTING').length,1);
    if(scenario==='different owner'){
      assert.equal(after.state,'NEEDS_REVIEW');
      assert.equal(after.error.code,'PURCHASE_VERIFICATION_SCOPE');
      assert.equal(after.binding,null);
      assert.deepEqual(submitted,[]);
      assert.equal(verified.hostPurchaseId,runOwner.hostPurchaseId);
      assert.deepEqual(verified.exports.D16,oldMain);
      return;
    }
    if(scenario==='changed amount'){
      assert.equal(after.state,'NEEDS_REVIEW');
      assert.equal(after.error.code,'PURCHASE_CREATED_MISMATCH');
      assert.equal(after.binding,null);
    }else{
      assert.equal(after.state,'BOUND');
      assert.equal(after.binding.orderId,orderId);
    }
    if(scenario==='late receipt'){
      assert.deepEqual(submitted,[]);
      assert.equal(verified.exports.D16.taskId,late.taskId);
    }else{
      assert.deepEqual(submitted,missingDetail?['D16','D17']:['D16']);
      assert.notEqual(verified.exports.D16.taskId,late.taskId);
      const [previous]=verified.exports.D16.verificationHistory;
      assert.equal(previous.intentId,oldIntent);
      assert.equal(previous.stage,'SUBMISSION_UNKNOWN');
      assert.deepEqual(previous.lateReceipts,[{receipt:late,scopeVerified:true}]);
    }
  });
}
