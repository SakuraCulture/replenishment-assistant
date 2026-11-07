import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createArchive} from '../src/zip.mjs';
import {purchaseContract,purchaseScopeHash} from '../src/purchase-contract.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {nativeCreationEvidence,PurchaseCreateService} from '../src/purchase-create-service.mjs';
import {checkCreatedPurchase,IMPORT_CODE} from '../src/purchase-create-contract.mjs';

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
