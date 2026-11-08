import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {createArchive} from '../src/zip.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {purchaseContract,PURCHASE_CODES,dateBounds,exportScope} from '../src/purchase-contract.mjs';
import {readReceiptSource,receiptObservation} from '../src/purchase-receipts.mjs';
import {PurchaseReceipts} from '../src/purchase-receipt-ledger.mjs';
import {PurchaseReceiptService} from '../src/purchase-receipt-service.mjs';
import {MachinePurchaseReceipt} from '../src/machine-purchase-receipt.mjs';
import {PurchaseCoordinator} from '../src/purchase-coordinator.mjs';

const identity={accountId:'A',merchantCode:'M'},target={buyerWarehouseCode:'T',displayName:'合成店',operatingStores:[],mappingSource:'offline',mappingVerifiedAt:'2026-11-02T00:00:00Z'};
const p={id:'p1',identity,basis:{store:'T',warehouse:'河北仓',number:'BH-1'},supplierCode:'2078246',target,rows:[{sku:'0001234567890123456789',name:'合成商品',quantity:'7',amount:'0.99'}],totalQuantity:'7',totalAmount:'0.99'};
const binding={orderId:'PO1'},scope={orderId:'PO1',store:'T',supplier:'2078246'};
const created={id:'p1',state:'BOUND',intent:p,binding};
const request={storeCodeList:['T'],orderType:'3',merchantSubBizType:'CONSIGNEE',supplyOrderId:'PO1',pageIndex:'1',pageSize:'30',sort:'main_order_gmt_create',...dateBounds('2026-11-01','2026-11-01')};
const query={identity,warehouseCode:'T',name:target.displayName,total:1,request,orderIds:['PO1'],orderSkuCounts:{PO1:1}};
const esc=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;');
function col(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
async function fixture(file,source,{status='已完成',received='5',amount='0.70',gap=true}={}){
  const c=purchaseContract(source),row=Array(c.columns.length).fill('');
  if(source==='D16')Object.assign(row,{0:'PO1',1:status,3:'T',5:'2078246',6:'1.0',7:'7',8:'0.99',16:'1',17:received,18:amount});
  else Object.assign(row,{0:'PO1',1:status,7:'T',9:'2078246',11:'合成商品',12:p.rows[0].sku,15:'包',18:'7',21:received,26:'0.99',31:amount,40:'2026-11-01 12:00:00',41:'2026-11-02 12:00:00'});
  const rows=[{n:1,v:c.columns.map(v=>v.rawHeader)},{n:gap?5:2,v:row}],xml=rows.map(({n,v})=>`<row r="${n}">${v.map((x,k)=>`<c r="${col(k)}${n}" t="inlineStr"><is><t>${esc(x)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+xml+'</sheetData></worksheet>'}]);
  return file;
}
async function setup(t){const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-receipts-'));let state=await StateStore.open(path.join(root,'state'));t.after(async()=>{state.close();await fs.rm(root,{recursive:true,force:true});});const ledger=await PurchaseReceipts.open(state);ledger.register(created,'2026-11-01');return {root,state,ledger,config:freshConfig({...identity,displayName:"合成商家"},[target]),binding:{outputRoot:path.join(root,'out')}};}
async function evidence(ctx,options={}){
  const sources=[];for(const s of ['D16','D17']){const file=await fixture(path.join(ctx.root,s+'-'+Math.random()+'.xlsx'),s,options[s]||options);sources.push(await readReceiptSource(file,s,scope,1,await fileHash(file)));}return receiptObservation(p,binding,'run1',sources);
}
test('actual receipt uses exact decimal, native row numbers and raw blank values; completed is not equal-quantity',async t=>{
  const ctx=await setup(t),o=await evidence(ctx);assert.equal(o.eligible,true);assert.equal(o.rows[0]._sourceRow,5);assert.equal(o.rows[0].receivedQuantity,'5');assert.equal(o.platformCompletedAt,null);
  const blank=await evidence(ctx,{received:'',amount:''});assert.equal(blank.terminal,true);assert.equal(blank.eligible,false);assert.equal(blank.rows[0].receivedQuantity,'');assert.ok(blank.issues.some(i=>i.code==='RECEIPT_NUMBER_UNKNOWN'));
  const zero=await evidence(ctx,{received:'0',amount:'0'});assert.equal(zero.eligible,true);
  const partial=await evidence(ctx,{status:'待提交'});assert.equal(partial.terminal,false);assert.equal(partial.eligible,false);
});
test('voided blank receipts stop routine work without inventing zero or final receipt',async t=>{
  const ctx=await setup(t),o=await evidence(ctx,{status:'已作废',received:'',amount:''});
  // The native item-count field also has no receipt fact for this void.
  o.main.receivedSkuCount='';
  const sources=o.files.map((f,i)=>({...f,rows:i?o.rows:[o.main]}));
  const v=receiptObservation(p,binding,'run1',sources);
  assert.equal(v.terminalKind,'VOIDED');assert.equal(v.disposition,'VOIDED');assert.equal(v.eligible,false);assert.equal(v.rows[0].receivedQuantity,'');assert.equal(v.issues.length,0);
  ctx.ledger.begin(p.id);ctx.ledger.run(p.id,'run1');const r=ctx.ledger.finish(p.id,v,{file:'void.json',sha256:'a'.repeat(64)});
  assert.equal(r.state,'VOIDED');assert.equal(r.finalSync,'NOT_DUE');assert.equal(r.terminal,true);assert.equal(r.hostReceipt,undefined);
  assert.throws(()=>ctx.ledger.synced(p.id,'a'.repeat(64),{number:p.basis.number,stage:'receipt',version:1}));
});
test('voided partial receipt and completed-to-void preserve evidence and require review',async t=>{
  const ctx=await setup(t),partial=await evidence(ctx,{status:'已作废',received:'1',amount:'0.1'});
  assert.equal(partial.disposition,'VOIDED_REVIEW');assert.ok(partial.issues.some(i=>i.code==='RECEIPT_VOID_WITH_RECEIPT'));
  ctx.ledger.begin(p.id);ctx.ledger.run(p.id,'run1');let r=ctx.ledger.finish(p.id,partial,{file:'partial.json',sha256:'a'.repeat(64)});
  assert.equal(r.state,'VOIDED_REVIEW');assert.equal(r.finalSync,'NOT_DUE');
  const completed=await evidence(ctx);ctx.ledger.run(p.id,'run1');assert.throws(()=>ctx.ledger.finish(p.id,completed,{file:'complete.json',sha256:'b'.repeat(64)}),{code:'RECEIPT_TERMINAL_CONFLICT'});
  const other=await setup(t);other.ledger.run(p.id,'run1');other.ledger.finish(p.id,completed,{file:'complete.json',sha256:'b'.repeat(64)});
  other.ledger.synced(p.id,'b'.repeat(64),{number:p.basis.number,stage:'receipt',version:1});
  const sources=partial.files.map((f,i)=>({...f,rows:structuredClone(i?partial.rows:[partial.main])}));
  for(const s of sources)for(const row of s.rows){row.receivedQuantity='';row.receivedAmount='';row.receivedSkuCount='';}
  const clean=receiptObservation(p,binding,'run1',sources);other.ledger.run(p.id,'run1');r=other.ledger.finish(p.id,clean,{file:'later-void.json',sha256:'c'.repeat(64)});
  assert.equal(r.state,'VOIDED_REVIEW');assert.equal(r.hostReceipt.version,1);assert.equal(r.finalSync,'NOT_DUE');
});
test('status conflict, changed purchase, missing/duplicate SKU, unit and total mismatch never become formal receipt',async t=>{
  const ctx=await setup(t),o=await evidence(ctx),sources=o.files.map((f,i)=>({...f,rows:i?[...o.rows]:[o.main]}));
  for(const [change,code] of [
    [s=>s[1].rows[0].status='待提交','RECEIPT_STATUS_CONFLICT'],[s=>s[1].rows[0].quantity='8','RECEIPT_PURCHASE_CHANGED'],
    [s=>s[1].rows=[],'RECEIPT_COVERAGE'],[s=>s[1].rows.push({...s[1].rows[0]}),'RECEIPT_SKU_CONFLICT'],
    [s=>s[1].rows[0].unit='','RECEIPT_UNIT_UNKNOWN'],[s=>s[0].rows[0].receivedQuantity='7','RECEIPT_TOTAL_CONFLICT']]){
    const s=structuredClone(sources);change(s);const v=receiptObservation(p,binding,'r',s);assert.equal(v.eligible,false);assert.ok(v.issues.some(i=>i.code===code),code);
  }
});
test('completed latch and final pending are atomic and survive interruption/reopen, failed D17 does not restart routine monitoring',async t=>{
  const ctx=await setup(t),o=await evidence(ctx);ctx.ledger.begin(p.id);ctx.ledger.run(p.id,'run1');ctx.ledger.terminal(p.id,{...o.files[0],rows:[o.main]});
  let v=ctx.ledger.get(p.id);assert.equal(v.terminal,true);assert.equal(v.finalSync,'PENDING');
  const reopened=await PurchaseReceipts.open(ctx.state);reopened.recover();v=reopened.get(p.id);assert.equal(v.terminal,true);assert.equal(v.finalSync,'PENDING');assert.equal(v.activeRunId,'run1');assert.equal(v.state,'PAUSED');
  reopened.begin(p.id);reopened.finish(p.id,o,{file:'proof',sha256:'a'.repeat(64)});assert.equal(reopened.get(p.id).finalSync,'READY');
  assert.throws(()=>reopened.synced(p.id,'b'.repeat(64),{number:'BH-1',stage:'receipt',version:1}),{code:'RECEIPT_SYNC_CONFLICT'});
  assert.throws(()=>reopened.synced(p.id,'a'.repeat(64),{number:'OTHER',stage:'receipt',version:1}),{code:'RECEIPT_SYNC_CONFLICT'});
  reopened.synced(p.id,'a'.repeat(64),{number:'BH-1',stage:'receipt',version:1});assert.equal(reopened.get(p.id).state,'SYNCED');
});
function adapter(ctx){let fail=true,submits=0;return {stats:()=>submits,queryPurchase:async()=>structuredClone(query),preparePurchase:async()=>({taskIds:[]}),
  submitPurchase:async({intentId,sourceId})=>{submits++;const r={intentId,code:PURCHASE_CODES[sourceId],scope:exportScope(request),taskId:'GEI@'+PURCHASE_CODES[sourceId]+'@261102000000@'+submits};ctx.state.recordPurchaseReceipt(r);return r;},
  purchaseTask:async(taskId,sourceId)=>({taskId,taskCode:PURCHASE_CODES[sourceId],taskStatus:'FINISHED',successCount:1,failCount:0}),
  downloadPurchase:async({directory,taskId,sourceId})=>{if(sourceId==='D17'&&fail){fail=false;throw Object.assign(new Error('download'),{code:'DOWNLOAD_FAILED'});}await fs.mkdir(directory,{recursive:true});const file=await fixture(path.join(directory,sourceId+'.xlsx'),sourceId);return {taskId,file,sha256:await fileHash(file)};}};}
test('real coordinator hook persists completed D16 before failed D17; retry uses original tasks and fresh later poll never reuses old snapshot',async t=>{
  const ctx=await setup(t),a=adapter(ctx);ctx.ledger.begin(p.id);
  const engine=new PurchaseCoordinator({...ctx,adapter:a,onProgress:e=>{if(e.stage==='PURCHASE_RUNNING')ctx.ledger.run(p.id,e.runId);},onSourceValidated:async({run,sourceId,file})=>{const s=await readReceiptSource(file,sourceId,scope,1);if(s.rows.some(r=>r.status==='已完成'))ctx.ledger.terminal(p.id,s);}});
  await assert.rejects(()=>engine.run({target,from:'2026-11-01',to:'2026-11-01',orderId:'PO1',rerun:true}),{code:'DOWNLOAD_FAILED'});
  const mid=ctx.ledger.get(p.id);assert.equal(mid.terminal,true);assert.equal(mid.finalSync,'PENDING');assert.equal(a.stats(),2);
  const result=await engine.run({runId:mid.activeRunId});assert.equal(result.status,'SUCCEEDED');assert.equal(a.stats(),2);
  // A later polling request must explicitly request a new snapshot, as the receipt service does.
  const fresh=await new PurchaseCoordinator({...ctx,adapter:a}).run({target,from:'2026-11-01',to:'2026-11-01',orderId:'PO1',rerun:true});assert.notEqual(fresh.id,result.id);assert.equal(a.stats(),4);
});
test('receipt controller close before work, failed retry, ready idempotency and persisted stop are independent of purchase upload',async t=>{
  const ctx=await setup(t);let runs=0;const service={register:()=>ctx.ledger.get(p.id),observe:async()=>{runs++;throw new Error('offline fault');}};
  const machine=new MachinePurchaseReceipt({ledger:ctx.ledger,service});machine.start(p.id);await machine.close();assert.equal(runs,0);assert.equal(ctx.ledger.get(p.id).state,'PAUSED');
  machine.start(p.id);await machine.pending;assert.equal(runs,1);machine.start(p.id);await machine.pending;assert.equal(runs,2);
  const o=await evidence(ctx);ctx.ledger.run(p.id,'run1');ctx.ledger.finish(p.id,o,{file:'proof',sha256:'a'.repeat(64)});assert.equal(machine.start(p.id).state,'READY');assert.equal(runs,2);
});
test('missing state tables fail closed and original purchase binding cannot be replaced',async t=>{
  const ctx=await setup(t);assert.throws(()=>ctx.ledger.register({...created,binding:{orderId:'OTHER'}},'2026-11-01'),{code:'RECEIPT_BINDING_CHANGED'});
  ctx.state.db.exec('DROP TABLE purchase_receipt_events');await assert.rejects(()=>PurchaseReceipts.open(ctx.state),{code:'RECEIPT_STATE_CORRUPT'});
});
test('service freezes native proof, recovers crash after proof before ledger, and never imports again',async t=>{
  const ctx=await setup(t),a=adapter(ctx),engine=new PurchaseCoordinator({...ctx,adapter:a});
  await assert.rejects(()=>engine.run({target,from:'2026-11-01',to:'2026-11-01',orderId:'PO1'}),{code:'DOWNLOAD_FAILED'});
  const original=await engine.run({runId:ctx.state.rows('purchase_runs')[0].id});
  const originalFiles=Object.entries(original.exports).map(([source,e])=>({source,sha256:e.asset.sha256,file:path.join(ctx.binding.outputRoot,e.asset.relativePath)}));
  const full={...created,binding:{...binding,files:originalFiles}};
  const service=new PurchaseReceiptService({state:ctx.state,creates:{get:()=>full},ledger:ctx.ledger,creationService:{context:async()=>({...ctx,adapter:a})}});
  assert.equal(service.register(p.id).scope.createdDay,'2026-11-01');ctx.ledger.begin(p.id);
  const finish=ctx.ledger.finish.bind(ctx.ledger);ctx.ledger.finish=()=>{throw new Error('crash after durable proof');};
  await assert.rejects(()=>service.observe(p.id,{}),/crash after durable proof/);
  const runId=ctx.ledger.get(p.id).activeRunId;assert.notEqual(runId,original.id);assert.equal(a.stats(),4);assert.equal(ctx.ledger.get(p.id).terminal,true);
  const file=path.join(ctx.state.root,'receipt-observations',runId+'.json'),before=await fileHash(file);ctx.ledger.finish=finish;
  const done=await service.observe(p.id,{});assert.equal(a.stats(),4);assert.equal(done.state,'READY');assert.equal(done.manifest.sha256,before);assert.equal(await fileHash(file),before);
});
