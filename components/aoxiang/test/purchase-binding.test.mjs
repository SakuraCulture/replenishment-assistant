import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {PurchaseCreates} from '../src/purchase-create-ledger.mjs';
import {PurchaseBindingService,MachinePurchaseBinding} from '../src/purchase-binding.mjs';
import {bindingRequest,checkedBinding,IMPORT_CODE} from '../src/purchase-create-contract.mjs';
import {createArchive} from '../src/zip.mjs';
import {purchaseContract} from '../src/purchase-contract.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {sha256} from '../src/common.mjs';
import {PurchaseReceipts} from '../src/purchase-receipt-ledger.mjs';
import {PurchaseReceiptService} from '../src/purchase-receipt-service.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';

const h='a'.repeat(64),request={order_id:'PO1',created_day:'2026-11-01',reason:'核对已有采购单'};
const escape=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;');
function col(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
async function workbook(file,source,rows){
  const c=purchaseContract(source),data=[c.columns.map(c=>c.rawHeader),...rows];
  const xml=data.map((r,i)=>`<row r="${i+1}">${r.map((v,j)=>`<c r="${col(j)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+xml+'</sheetData></worksheet>'}]);
}
function intent(){
  const id='manual-one',marker='AX_CREATE:'+id;
  return {schema:1,operation:'purchase.create',id,identity:{accountId:'A',merchantCode:'M'},mappingVersion:h,
    basis:{number:'BH-1',store:'T',warehouse:'河北仓',planFingerprint:h,paymentVersion:1,paymentDigest:h},
    target:{buyerWarehouseCode:'T',displayName:'合成店'},supplierCode:'2078246',marker,note:marker+' 本地单:BH-1',createExternalOrder:false,postCreationState:'待提交',
    rows:[{sku:'000123456789012345678',name:'甲',quantity:'2',amount:'0.99'},{sku:'0002',name:'乙',quantity:'3',amount:'2.01'}],totalQuantity:'5',totalAmount:'3.00',
    file:{path:path.resolve('sample.xlsx'),sha256:h,bytes:300},template:{recordFile:path.resolve('template.json'),sha256:h,attemptId:'sample'}};
}
async function fixture(t,{status='待提交',unknown=false}={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-binding-')),state=await StateStore.open(root),creates=await PurchaseCreates.open(state),p=intent();
  t.after(async()=>{state.close();await fs.rm(root,{recursive:true,force:true});});creates.reserve(p,h);
  if(unknown){let r=creates.get(p.id);for(const next of ['PREPARING','READY','SUBMITTING','NEEDS_REVIEW'])r=creates.update(p.id,r.sequence,next,next==='READY'?{baseline:{from:'2026-11-01',orderIds:[]}}:next==='SUBMITTING'?{uploadAttempted:true}:{});}
  const main=Array(25).fill('');Object.assign(main,{0:'PO1',1:status,3:'T',5:p.supplierCode,6:'2',7:'5',8:'3.00'});
  const rows=p.rows.map(r=>{const a=Array(43).fill('');Object.assign(a,{0:'PO1',1:status,7:'T',9:p.supplierCode,12:r.sku,18:r.quantity,26:r.amount,36:unknown?p.note:''});return a;});
  const files={D16:path.join(root,'main.xlsx'),D17:path.join(root,'detail.xlsx')};await workbook(files.D16,'D16',[main]);await workbook(files.D17,'D17',rows);
  const run={id:'run-original',identity:p.identity,from:'2026-11-01',to:'2026-11-01',dateBasis:'created',status:'SUCCEEDED',orderId:'PO1',query:{orderIds:['PO1']},exports:{}};
  for(const s of ['D16','D17'])run.exports[s]={asset:{rows:s==='D16'?1:2,sha256:await fileHash(files[s])}};
  const marks=[{supplyOrderId:'PO1',buyerMessage:p.note}],calls=[];
  const creation={context:async()=>({adapter:{queryCreateOrders:async()=>{calls.push('query-markers');return {query:{identity:p.identity},rows:marks};}}}),preflight:async()=>{calls.push('preflight');}};
  const service=new PurchaseBindingService({state,creates,creationService:creation,coordinatorFactory:options=>({
    run:async args=>{calls.push(args);options.onProgress({stage:'PURCHASE_RUNNING',runId:run.id});return run;},sourceFile:async(_,s)=>files[s]})});
  const machine=new MachinePurchaseBinding({creates,service});service.progress=stage=>machine.progress(stage);
  return {root,state,creates,p,files,run,marks,calls,creation,service,machine};
}
async function ready(f){f.machine.start(f.p.id,request);await f.machine.pending;const r=f.creates.get(f.p.id);assert.equal(r.bindingProposal.state,'READY',JSON.stringify(r.bindingProposal.error));return r;}
async function proof(f){const r=await ready(f);return JSON.parse(await fs.readFile(r.bindingProposal.manifest.file,'utf8'));}
async function confirm(f,r){return f.service.confirm(f.p.id,r.bindingProposal.id,r.sequence,r.bindingProposal.manifest.sha256);}

test('native exact PO preview does not bind; explicit confirm is idempotent and never claims upload',async t=>{
  const f=await fixture(t),r=await ready(f);assert.equal(r.state,'RESERVED');assert.equal(r.binding,null);assert.equal(r.uploadAttempted,false);
  const bound=await confirm(f,r);assert.equal(bound.state,'BOUND');assert.equal(bound.binding.orderId,'PO1');assert.equal(bound.receipt,null);assert.equal(bound.uploadAttempted,false);
  assert.equal(bound.binding.method,'operator_confirmed_exact_native_rows');assert.equal((await confirm(f,r)).sequence,bound.sequence);
  assert.equal(f.creates.events(f.p.id).at(-1).kind,'OPERATOR_CONFIRMED_NATIVE_BINDING');assert.equal(f.calls.filter(x=>x==='query-markers').length,0);
  assert.throws(()=>f.machine.start(f.p.id,request),{code:'BINDING_NOT_ELIGIBLE'});
});
test('unknown upload requires unique original marker and exact native rows; never retries upload',async t=>{
  const f=await fixture(t,{unknown:true});f.marks.push({supplyOrderId:'PO2',buyerMessage:f.p.note});f.machine.start(f.p.id,request);await f.machine.pending;
  assert.equal(f.creates.get(f.p.id).bindingProposal.error.code,'PURCHASE_NOT_UNIQUE');assert.equal(f.creates.get(f.p.id).state,'NEEDS_REVIEW');
  f.marks.pop();const r=await ready(f),bound=await confirm(f,r);assert.equal(bound.uploadAttempted,true);assert.equal(bound.receipt,null);
  const receipt={intentId:f.p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:f.p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:h};
  const late=f.creates.accepted(receipt);assert.equal(late.state,'BOUND');assert.equal(late.binding.orderId,'PO1');assert.equal(late.receipt.taskId,receipt.taskId);
  const other={...receipt,taskId:receipt.taskId.replace('@7114','@7115')};f.creates.accepted(other);assert.equal(f.creates.get(f.p.id).state,'NEEDS_REVIEW');
  assert.throws(()=>f.machine.start(f.p.id,request),{code:'BINDING_NOT_ELIGIBLE'});
});
test('per SKU swaps, blanks, wrong identity/supplier/status, split and stale basis fail closed',async t=>{
  const f=await fixture(t),e=await proof(f),r=f.creates.get(f.p.id);
  for(const mutate of [x=>{[x.evidence.rows[0].amount,x.evidence.rows[1].amount]=[x.evidence.rows[1].amount,x.evidence.rows[0].amount];},
    x=>x.evidence.rows[0].quantity='',x=>x.evidence.rows[1].sku=x.evidence.rows[0].sku,x=>x.evidence.main.supplier='other',x=>x.evidence.main.status='',
    x=>x.evidence.rows[0].status='已完成',x=>x.evidence.identity.accountId='other',x=>x.basis.paymentVersion=2,x=>x.evidence.candidates.push('PO2'),x=>x.evidence.files=[]]){
    const changed=structuredClone(e);mutate(changed);assert.throws(()=>checkedBinding(r,changed));
  }
  assert.equal(f.creates.get(f.p.id).binding,null);
});
test('confirmation binds precise sequence and rejects source/proof tamper',async t=>{
  const f=await fixture(t),r=await ready(f);
  f.creates.putBindingProposal(f.p.id,r.sequence,r.bindingProposal,'OTHER_WINDOW_REFRESH');await assert.rejects(confirm(f,r),{code:'BINDING_PREVIEW_STALE'});
  const next=f.creates.get(f.p.id);await fs.appendFile(f.files.D17,'changed');await assert.rejects(confirm(f,next),{code:'BINDING_PROOF_CHANGED'});
  assert.equal(f.creates.get(f.p.id).binding,null);
});
test('confirmation catches state change during awaited preflight and cannot overwrite it',async t=>{
  const f=await fixture(t),r=await ready(f);
  f.creation.preflight=async()=>{const current=f.creates.get(f.p.id);f.creates.putBindingProposal(f.p.id,current.sequence,current.bindingProposal,'CONCURRENT_CHANGE');};
  await assert.rejects(confirm(f,r),{code:'BINDING_PREVIEW_STALE'});assert.equal(f.creates.get(f.p.id).state,'RESERVED');
});
test('platform PO uniqueness is shared with automatic creates and rollback keeps preview',async t=>{
  const f=await fixture(t),r=await ready(f);await confirm(f,r);
  const q={...structuredClone(f.p),id:'other',marker:'AX_CREATE:other',note:'AX_CREATE:other 本地单:BH-2',basis:{...f.p.basis,number:'BH-2'}};
  f.creates.reserve(q,h);const original=f.creates.get(f.p.id);const e=JSON.parse(await fs.readFile(original.bindingProposal.manifest.file,'utf8'));e.purchaseId=q.id;e.basis=q.basis;
  const next=f.creates.putBindingProposal(q.id,1,{...original.bindingProposal,id:'second',state:'READY'},'PREVIEW');
  assert.throws(()=>f.creates.confirmBinding(q.id,next.sequence,e,'second',next.bindingProposal.manifest.sha256),/UNIQUE/);
  assert.equal(f.creates.get(q.id).binding,null);assert.equal(f.creates.get(q.id).state,'RESERVED');
});
test('pause/restart keeps original proof run and never auto-confirms',async t=>{
  const f=await fixture(t);let release;const wait=new Promise(r=>release=r);
  f.service.observe=async(id,proposal,{signal})=>{const r=f.creates.get(id);f.creates.putBindingProposal(id,r.sequence,{...proposal,runId:'original-run'},'DOWNLOAD');await wait;if(signal.aborted)throw Error('stopped');};
  f.machine.start(f.p.id,request);await new Promise(r=>setImmediate(r));f.machine.pause(f.p.id);release();await f.machine.pending;
  let r=f.creates.get(f.p.id);assert.equal(r.bindingProposal.state,'PAUSED');assert.equal(r.bindingProposal.runId,'original-run');assert.equal(r.binding,null);
  r=f.creates.putBindingProposal(r.id,r.sequence,{...r.bindingProposal,state:'RUNNING'},'INTERRUPTED');f.creates.recover();r=f.creates.get(r.id);
  assert.equal(r.bindingProposal.state,'PAUSED');assert.equal(r.bindingProposal.runId,'original-run');
});
test('known completed manual PO immediately latches final-sync pending on receipt registration',async t=>{
  const f=await fixture(t,{status:'已完成'}),r=await ready(f);await confirm(f,r);
  const ledger=await PurchaseReceipts.open(f.state),service=new PurchaseReceiptService({state:{rows:()=>[f.run]},creates:f.creates,ledger});
  const registered=service.register(f.p.id);assert.equal(registered.terminal,true);assert.equal(registered.finalSync,'PENDING');
  assert.equal(service.register(f.p.id).terminalEvidence.length,1);
});
test('binding protocol rejects missing reason, fuzzy IDs, invalid/future day and unknown fields',()=>{
  for(const bad of [{...request,order_id:'PO1,PO2'},{...request,created_day:'2026-02-30'},{...request,created_day:'2999-01-01'},{...request,reason:''},{...request,extra:true}])assert.throws(()=>bindingRequest(bad));
  const frame={protocol_version:1,request_id:'q',action:'binding.start',purchase_id:'p',binding_request:request};validateRequest(frame);
  assert.throws(()=>validateRequest({...frame,upload:true}));
  assert.throws(()=>validateRequest({protocol_version:1,request_id:'q',action:'binding.confirm',purchase_id:'p',proposal_id:'b',sequence:0,manifest_sha256:h}));
});

test('late receipt during proof retires only the preview and preserves the original recovery path',async t=>{
  const f=await fixture(t,{unknown:true}),fatal=[];
  f.machine.onFatal=e=>fatal.push(e);
  const receipt={intentId:f.p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:f.p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:h};
  f.creation.preflight=async()=>{f.creates.accepted(receipt);};
  f.machine.start(f.p.id,request);await f.machine.pending;
  const r=f.creates.get(f.p.id);
  assert.deepEqual(fatal,[]);assert.equal(r.state,'ACCEPTED');assert.deepEqual(r.receipt,receipt);
  assert.equal(r.uploadAttempted,true);assert.equal(r.binding,null);assert.equal(r.bindingProposal.state,'SUPERSEDED');
  assert.equal(r.bindingProposal.error.code,'BINDING_SUPERSEDED');assert.equal(f.machine.busy,false);
  f.creates.recover();assert.equal(f.creates.get(f.p.id).sequence,r.sequence);
  assert.equal(f.creates.events(f.p.id).at(-1).proposalId,r.bindingProposal.id);
  assert.throws(()=>f.machine.start(f.p.id,request),{code:'BINDING_NOT_ELIGIBLE'});
});

test('restart retires an interrupted preview after receipt without allowing proof replacement',async t=>{
  const f=await fixture(t,{unknown:true}),r=f.creates.get(f.p.id);
  const proposal={id:'proof-one',request,state:'RUNNING',runId:'original-download',manifest:null,summary:null,error:null,createdAt:'original'};
  f.creates.putBindingProposal(r.id,r.sequence,proposal,'BINDING_CHECK_STARTED');
  f.creates.accepted({intentId:f.p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:f.p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:h});
  const accepted=f.creates.get(r.id);
  assert.throws(()=>f.creates.putBindingProposal(r.id,accepted.sequence,{...proposal,state:'SUPERSEDED',runId:'other'},'FORGED_RETIRE'),{code:'BINDING_NOT_ELIGIBLE'});
  assert.throws(()=>f.creates.putBindingProposal(r.id,accepted.sequence,{...proposal,state:'READY'},'FORGED_READY'),{code:'BINDING_NOT_ELIGIBLE'});
  f.creates.recover();const recovered=f.creates.get(r.id);
  assert.equal(recovered.state,'ACCEPTED');assert.deepEqual(recovered.receipt,accepted.receipt);
  assert.equal(recovered.bindingProposal.state,'SUPERSEDED');assert.equal(recovered.bindingProposal.runId,'original-download');
  assert.equal(recovered.binding,null);assert.equal(recovered.sequence,accepted.sequence+1);
  f.creates.recover();assert.equal(f.creates.get(r.id).sequence,recovered.sequence);
});
