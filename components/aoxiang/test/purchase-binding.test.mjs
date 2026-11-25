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
import {recordOriginalExport,originalExportSource,takeOverPurchase} from '../src/purchase-manual.mjs';
import {MachinePurchaseCreate} from '../src/machine-purchase-create.mjs';
import {importResult} from '../src/purchase-import-result.mjs';

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
async function fixture(t,{status='待提交',unknown=false,manual=false}={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-binding-')),state=await StateStore.open(root),creates=await PurchaseCreates.open(state),p=intent();
  if(manual){
    const file=path.join(root,'source.xlsx'),bytes=Buffer.from('controlled original file bytes'.repeat(10));
    await fs.writeFile(file,bytes);
    p.file={path:file,sha256:sha256(bytes),bytes:bytes.length};
    for(const [i,row] of p.rows.entries())row.unitPrice=i?'0.67':'0.50';
  }
  t.after(async()=>{state.close();await fs.rm(root,{recursive:true,force:true});});creates.reserve(p,h);
  if(unknown){let r=creates.get(p.id);for(const next of ['PREPARING','READY','SUBMITTING','NEEDS_REVIEW'])r=creates.update(p.id,r.sequence,next,next==='READY'?{baseline:{from:'2026-11-01',orderIds:[]}}:next==='SUBMITTING'?{uploadAttempted:true}:{});}
  const main=Array(25).fill('');Object.assign(main,{0:'PO1',1:status,3:'T',5:p.supplierCode,6:'2',7:'5',8:'3.00'});
  const rows=p.rows.map(r=>{const a=Array(43).fill('');Object.assign(a,{0:'PO1',1:status,7:'T',9:p.supplierCode,12:r.sku,15:manual?'件':'',18:r.quantity,22:r.unitPrice||'',26:r.amount,36:unknown||manual?p.note:''});return a;});
  const files={D16:path.join(root,'main.xlsx'),D17:path.join(root,'detail.xlsx')};await workbook(files.D16,'D16',[main]);await workbook(files.D17,'D17',rows);
  const run={id:'run-original',identity:p.identity,from:'2026-11-01',to:'2026-11-01',dateBasis:'created',status:'SUCCEEDED',orderId:'PO1',query:{orderIds:['PO1']},exports:{}};
  for(const s of ['D16','D17'])run.exports[s]={asset:{rows:s==='D16'?1:2,sha256:await fileHash(files[s])}};
  const marks=[{supplyOrderId:'PO1',buyerWarehouseCode:'T',buyerMessage:p.note}],calls=[];
  const tasks=[];
  const creation={context:async()=>({adapter:{queryCreateOrders:async()=>{calls.push('query-markers');return {query:{identity:p.identity,warehouseCode:'T',orderIds:marks.map(r=>r.supplyOrderId),total:marks.length},rows:marks};},
    purchaseImportTask:async({taskId})=>{calls.push('query-task');return tasks.find(r=>r.taskId===taskId);}}}),preflight:async()=>{calls.push('preflight');}};
  const service=new PurchaseBindingService({state,creates,creationService:creation,coordinatorFactory:options=>({
    run:async args=>{calls.push(args);options.onProgress({stage:'PURCHASE_RUNNING',runId:run.id});return run;},sourceFile:async(_,s)=>files[s]})});
  const machine=new MachinePurchaseBinding({creates,service});service.progress=stage=>machine.progress(stage);
  return {root,state,creates,p,files,run,marks,calls,creation,service,machine,main,rows,tasks};
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

function accepted(f,counts){
  const receipt={intentId:f.p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,
    fileSha256:f.p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:h};
  f.creates.accepted(receipt);
  const observed={taskId:receipt.taskId,code:IMPORT_CODE,status:'部分失败',...counts};
  f.tasks.push(observed);
  const r=f.creates.get(f.p.id);
  f.creates.update(r.id,r.sequence,'NEEDS_REVIEW',{importResult:importResult(observed,receipt.taskId,f.p.rows.length)});
  return receipt;
}
const operator={operator:'隔离经办',reason:'按原任务核实',host_facts:h};
const manualRequest=f=>({...request,...operator,original_units:Object.fromEntries(f.p.rows.map(r=>[r.sku,'件']))});
async function refreshSources(f){
  for(const source of ['D16','D17'])f.files[source]=path.join(f.root,source+'-updated.xlsx');
  const files=f.files;
  await workbook(files.D16,'D16',[f.main]);await workbook(files.D17,'D17',f.rows);
  for(const s of ['D16','D17'])f.run.exports[s]={asset:{rows:s==='D16'?1:f.rows.length,sha256:await fileHash(files[s])}};
}

test('exact 0-success recheck and no original PO allow original export; autonomous upload stays blocked',async t=>{
  const f=await fixture(t,{manual:true,unknown:true}),p=f.p;
  const unknown=f.creates.get(p.id);
  await assert.rejects(takeOverPurchase(f.creates,f.creation,p.id,{...operator,sequence:unknown.sequence}),{code:'MANUAL_IMPORT_PENDING'});
  const receipt=accepted(f,{successCount:0,failCount:2});
  const failed=f.creates.get(p.id);
  await assert.rejects(takeOverPurchase(f.creates,f.creation,p.id,{...operator,sequence:failed.sequence}),{code:'MANUAL_IMPORT_ORDER_EXISTS'});
  f.marks.length=0;f.tasks[0].status='RUNNING';
  await assert.rejects(takeOverPurchase(f.creates,f.creation,p.id,{...operator,sequence:failed.sequence}),{code:'MANUAL_IMPORT_PENDING'});
  f.tasks[0].status='部分失败';
  const handed=await takeOverPurchase(f.creates,f.creation,p.id,{...operator,sequence:failed.sequence});
  assert.equal(handed.state,'MANUAL_HANDOFF');assert.deepEqual(handed.receipt,receipt);
  assert.equal((await takeOverPurchase(f.creates,f.creation,p.id,{...operator,sequence:failed.sequence})).sequence,handed.sequence);
  const machine=new MachinePurchaseCreate({ledger:f.creates});
  assert.throws(()=>machine.start(p.id,{submitOnly:true}),{code:'PURCHASE_MANUAL_HANDOFF'});
  const source=await originalExportSource(f.creates,p.id,{usage:'manual',host_facts:h});
  const output=path.join(f.root,'manual-export.xlsx');
  await fs.copyFile(source.file,output);
  const args={output,usage:'manual',export_sha256:source.sha256,host_facts:h};
  assert.deepEqual(await recordOriginalExport(f.creates,p.id,args),await recordOriginalExport(f.creates,p.id,args));
  await fs.appendFile(output,'changed');
  await assert.rejects(recordOriginalExport(f.creates,p.id,args),{code:'PURCHASE_FILE_CHANGED'});
  assert.equal(f.creates.events(p.id).filter(e=>e.kind==='ORIGINAL_FILE_EXPORTED').length,1);
});

test('manual binding reads exact marked original completed PO and confirms persistent changed rows from a partial import',async t=>{
  const f=await fixture(t,{manual:true,unknown:true,status:'部分入库'}),p=f.p;
  const original=accepted(f,{successCount:1,failCount:1});
  await assert.rejects(takeOverPurchase(f.creates,f.creation,p.id,
    {...operator,sequence:f.creates.get(p.id).sequence}),{code:'MANUAL_IMPORT_PENDING'});
  f.rows[0][18]='1';f.rows[0][26]='0.50';f.rows[1][12]='@00003';
  f.main[7]='4';f.main[8]='2.51';await refreshSources(f);
  const request=manualRequest(f);
  f.machine.start(p.id,request);await f.machine.pending;
  const preview=f.creates.get(p.id);
  assert.equal(preview.bindingProposal.state,'READY',JSON.stringify(preview.bindingProposal.error));
  assert.equal(preview.bindingProposal.summary.differences,3);
  f.marks.push({supplyOrderId:'PO-EXTRA',buyerWarehouseCode:'T',buyerMessage:p.note});
  await assert.rejects(f.service.confirm(p.id,preview.bindingProposal.id,preview.sequence,preview.bindingProposal.manifest.sha256,h),{code:'PURCHASE_NOT_UNIQUE'});
  assert.equal(f.creates.get(p.id).bindingProposal.state,'STALE');
  f.marks.pop();
  f.machine.start(p.id,request);await f.machine.pending;
  const ready=f.creates.get(p.id),m=ready.bindingProposal;
  assert.equal(m.state,'READY',JSON.stringify(m.error));assert.notEqual(m.id,preview.bindingProposal.id);
  const bound=await f.service.confirm(p.id,m.id,ready.sequence,m.manifest.sha256,h);
  assert.equal(bound.binding.orderId,'PO1');assert.equal(bound.binding.status,'部分入库');
  assert.equal(bound.binding.taskId,original.taskId);assert.deepEqual(bound.intent.rows,p.rows);
  assert.deepEqual(bound.binding.manualRevision.rows.map(x=>x.sku),[p.rows[0].sku,'@00003']);
  assert.equal((await f.service.confirm(p.id,m.id,ready.sequence,m.manifest.sha256,h)).sequence,bound.sequence);
  assert.equal(f.creates.events(p.id).filter(e=>e.kind==='OPERATOR_CONFIRMED_NATIVE_BINDING').length,1);
});

test('manual binding adopts valid platform units for retained SKUs',async t=>{
  for(const originalUnit of ['件',null]){
    await t.test(originalUnit?'changed original unit':'unrecorded original unit',async t=>{
      const f=await fixture(t,{manual:true,unknown:true}),p=f.p;
      const original=accepted(f,{successCount:0,failCount:2});
      const input=manualRequest(f),sku=p.rows[0].sku;
      if(!originalUnit)delete input.original_units[sku];
      f.rows[0][15]='箱';
      await refreshSources(f);

      f.machine.start(p.id,input);await f.machine.pending;
      const ready=f.creates.get(p.id),proposal=ready.bindingProposal;
      assert.equal(proposal.state,'READY',JSON.stringify(proposal.error));
      assert.equal(proposal.summary.differences,originalUnit?1:0);
      assert.equal(proposal.summary.unverifiedUnits,originalUnit?0:1);
      const proof=JSON.parse(await fs.readFile(proposal.manifest.file,'utf8'));
      for(const [code,mutate] of [
        ['PURCHASE_DETAIL_MISMATCH',e=>{e.rows[0].unit='';}],
        ['PURCHASE_DETAIL_MISMATCH',e=>{e.rows[1].sku=sku;}],
        ['PURCHASE_CREATED_MISMATCH',e=>{e.main.supplier='other-warehouse';}],
        ['PURCHASE_CREATED_MISMATCH',e=>{e.main.amount='999';}],
      ]){
        const wrong=structuredClone(proof);mutate(wrong.evidence);
        assert.throws(()=>checkedBinding(ready,wrong),{code});
      }

      const bound=await f.service.confirm(p.id,proposal.id,ready.sequence,proposal.manifest.sha256,h);
      assert.equal(bound.binding.manualRevision.rows[0].unit,'箱');
      assert.equal(bound.binding.manualRevision.rows[0].quantity,p.rows[0].quantity);
      assert.equal(bound.binding.taskId,original.taskId);
      assert.deepEqual(bound.intent,p);
      assert.equal(f.creates.events(p.id).filter(e=>e.kind==='OPERATOR_CONFIRMED_NATIVE_BINDING').length,1);
    });
  }
});
