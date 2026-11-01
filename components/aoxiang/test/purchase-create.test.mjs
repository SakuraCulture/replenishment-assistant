import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {PurchaseCreates,purchasePublic} from '../src/purchase-create-ledger.mjs';
import {MachinePurchaseCreate} from '../src/machine-purchase-create.mjs';
import {IMPORT_CODE,validatePurchaseIntent,checkCreatedPurchase,decimal,sum} from '../src/purchase-create-contract.mjs';
import {sha256} from '../src/common.mjs';
import {exportMigration} from '../src/migration.mjs';

const h='a'.repeat(64);
function intent(id='create-one'){
  const marker='AX_CREATE:'+id;
  return {schema:1,operation:'purchase.create',id,identity:{accountId:'A',merchantCode:'M'},mappingVersion:h,
    basis:{number:'BH-01',warehouse:'河北仓',store:'001',planFingerprint:h,paymentVersion:1,paymentDigest:h},
    target:{buyerWarehouseCode:'001',displayName:'合成店'},supplierCode:'2078246',marker,note:marker+' 本地单:BH-01',createExternalOrder:false,postCreationState:'待提交',
    file:{path:path.resolve('sample.xlsx'),sha256:h,bytes:300},template:{recordFile:path.resolve('template.json'),sha256:h,attemptId:'get-one'},
    rows:[{sku:'000123456789012345678',name:'合成商品甲',quantity:'2.00',amount:'0.99'},{sku:'0002',name:'合成商品乙',quantity:'3',amount:'2.01'}],totalQuantity:'5',totalAmount:'3.00'};
}
function receipt(p){return {intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:h};}
function evidence(p,r=receipt(p)){
  const orderId='PO-1';return {identity:p.identity,task:{taskId:r.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'2',failCount:'0'},candidates:[orderId],
    main:{orderId,store:p.basis.store,supplier:p.supplierCode,status:'待提交',externalOrderId:'',marker:p.note,quantity:'5.00',amount:'3',skuCount:'2'},
    rows:p.rows.map(s=>({...s,orderId,store:p.basis.store,supplier:p.supplierCode,status:'待提交',note:p.note})),
    files:['D16','D17'].map(source=>({source,sha256:h,file:path.resolve(source+'.xlsx')})),observedAt:'2026-11-02T00:00:00Z'};
}
async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-create-'));let state=await StateStore.open(root),ledger=await PurchaseCreates.open(state);
  t.after(async()=>{state.close();await fs.rm(root,{recursive:true,force:true});});
  return {get state(){return state;},get ledger(){return ledger;},root,async reopen(){state.close();state=await StateStore.open(root);ledger=await PurchaseCreates.open(state);ledger.recover();}};
}
function reserve(f,p=intent()){f.ledger.reserve(p,sha256(JSON.stringify(p)));return p;}
function sent(f,p){let r=f.ledger.get(p.id);for(const next of ['PREPARING','READY','SUBMITTING'])r=f.ledger.update(r.id,r.sequence,next,next==='SUBMITTING'?{uploadAttempted:true}:{});return r;}
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};}

test('decimal facts retain precision and reject null/number/exponent/negative',()=>{
  assert.equal(sum(['0.1','0.2','9999999999999999999999.99']),'10000000000000000000000.29');
  for(const x of [null,0,'','1e2','-1','NaN'])assert.throws(()=>decimal(x));
  assert.equal(decimal('000.000'),'0');
  const p=intent();validatePurchaseIntent(p);p.rows[1].sku=p.rows[0].sku;assert.throws(()=>validatePurchaseIntent(p));
});
test('reserve idempotent; changed payment and different ID cannot duplicate an order',async t=>{
  const f=await fixture(t),p=reserve(f);assert.equal(f.ledger.reserve(p,sha256(JSON.stringify(p))).created,false);
  assert.throws(()=>f.ledger.reserve(p,'b'.repeat(64)),{code:'PURCHASE_INTENT_CHANGED'});
  const q=intent('different-id');q.basis.paymentVersion=2;assert.throws(()=>reserve(f,q),{code:'PURCHASE_ALREADY_RESERVED'});
  assert.equal(f.ledger.events(p.id).length,1);assert.equal(purchasePublic(f.ledger.get(p.id)).rows,2);
});
test('pre-upload cancel releases reservation; post-boundary cancel cannot release it',async t=>{
  const f=await fixture(t),p=reserve(f),controller=new MachinePurchaseCreate({ledger:f.ledger,adapter:{}});
  controller.cancelBeforeUpload(p.id);const q=reserve(f,intent('second'));sent(f,q);
  assert.throws(()=>controller.cancelBeforeUpload(q.id),{code:'SUBMISSION_UNKNOWN'});
  assert.throws(()=>reserve(f,intent('third')),{code:'PURCHASE_ALREADY_RESERVED'});
});
test('crash before upload pauses; crash after boundary needs review and never retries',async t=>{
  const f=await fixture(t),p=reserve(f);f.ledger.update(p.id,1,'PREPARING');await f.reopen();assert.equal(f.ledger.get(p.id).state,'PAUSED');
  sent(f,p);await f.reopen();assert.equal(f.ledger.get(p.id).state,'NEEDS_REVIEW');
  const c=new MachinePurchaseCreate({ledger:f.ledger,adapter:{upload(){assert.fail('must not retry');}}});
  assert.throws(()=>c.start(p.id),{code:'SUBMISSION_UNKNOWN'});
  f.ledger.accepted(receipt(p));await f.reopen();assert.equal(f.ledger.get(p.id).state,'ACCEPTED');
});
test('pause during preparation never uploads; retry uses original intent safely',async t=>{
  const f=await fixture(t),p=reserve(f),d=deferred();let uploads=0;
  const c=new MachinePurchaseCreate({ledger:f.ledger,adapter:{prepare:()=>d.promise,upload:async()=>{uploads++;return receipt(p);},observe:async()=>evidence(p)}});
  c.start(p.id);await new Promise(r=>setImmediate(r));c.pause(p.id);d.resolve({orderIds:[]});await c.pending;
  assert.equal(f.ledger.get(p.id).state,'PAUSED');assert.equal(uploads,0);
  c.start(p.id);await c.pending;assert.equal(uploads,1);assert.equal(f.ledger.get(p.id).state,'BOUND');
  c.start(p.id);assert.equal(uploads,1); // repeated action after success is a read
});
test('pause in upload persists returned receipt; next start only observes it',async t=>{
  const f=await fixture(t),p=reserve(f),d=deferred();let uploads=0,observes=0;
  const c=new MachinePurchaseCreate({ledger:f.ledger,adapter:{prepare:async()=>({orderIds:[]}),upload:()=>{uploads++;return d.promise;},observe:async()=>{observes++;return evidence(p);}}});
  c.start(p.id);await new Promise(r=>setImmediate(r));assert.equal(f.ledger.get(p.id).state,'SUBMITTING');
  c.pause(p.id);d.resolve(receipt(p));await c.pending;assert.equal(f.ledger.get(p.id).state,'ACCEPTED');assert.equal(observes,0);
  c.start(p.id);await c.pending;assert.equal(uploads,1);assert.equal(observes,1);assert.equal(f.ledger.get(p.id).binding.orderId,'PO-1');
});
test('timeout no receipt forbids replay; late exact receipt can recover',async t=>{
  const f=await fixture(t),p=reserve(f);let uploads=0;
  const c=new MachinePurchaseCreate({ledger:f.ledger,adapter:{prepare:async()=>({orderIds:[]}),upload:async()=>{uploads++;throw Object.assign(Error('timeout'),{code:'WORKER_RESPONSE_UNKNOWN'});},observe:async()=>evidence(p)}});
  c.start(p.id);await c.pending;assert.equal(f.ledger.get(p.id).state,'NEEDS_REVIEW');assert.throws(()=>c.start(p.id));
  c.receipt(receipt(p));c.start(p.id);await c.pending;assert.equal(uploads,1);assert.equal(f.ledger.get(p.id).state,'BOUND');
});
test('strict per-SKU proof catches equal-total swaps, wrong scope, null, split, status and external order',()=>{
  const p=intent(),r=receipt(p);assert.equal(checkCreatedPurchase(p,r,evidence(p)).orderId,'PO-1');
  for(const mutate of [e=>e.rows[0].quantity='3',e=>{[e.rows[0].amount,e.rows[1].amount]=[e.rows[1].amount,e.rows[0].amount];},e=>e.rows[0].amount=null,e=>e.rows[1].sku=e.rows[0].sku,e=>e.main.supplier='other',e=>e.candidates.push('PO-2'),e=>e.main.status='已完成',e=>e.rows[0].status='已完成',e=>e.main.externalOrderId='external',e=>e.task.failCount='1',e=>e.task.successCount='1',e=>e.files=[]]){
    const x=evidence(p);mutate(x);assert.throws(()=>checkCreatedPurchase(p,r,x));
  }
});
test('receipt identity cannot change; conflicting task retained for review',async t=>{
  const f=await fixture(t),p=reserve(f);sent(f,p);const r=receipt(p);f.ledger.accepted(r);
  assert.throws(()=>f.ledger.accepted({...r,fileSha256:'b'.repeat(64)}));
  const other={...r,taskId:r.taskId.replace('@7114','@7115')};f.ledger.accepted(other);
  assert.equal(f.ledger.get(p.id).state,'NEEDS_REVIEW');assert.equal(f.ledger.get(p.id).receipt.taskId,r.taskId);
  assert.deepEqual(f.ledger.get(p.id).additionalTaskIds,[other.taskId]);
});
test('recovered verification repeats read-only observation and protects unique platform order',async t=>{
  const f=await fixture(t),p=reserve(f);sent(f,p);f.ledger.accepted(receipt(p));let r=f.ledger.get(p.id);f.ledger.update(p.id,r.sequence,'VERIFYING');await f.reopen();
  assert.equal(f.ledger.get(p.id).state,'ACCEPTED');r=f.ledger.get(p.id);f.ledger.update(p.id,r.sequence,'VERIFYING');f.ledger.bind(p.id,evidence(p));
  const q=intent('other-order');q.basis.number='BH-02';q.note=q.marker+' 本地单:BH-02';reserve(f,q);sent(f,q);f.ledger.accepted(receipt(q));r=f.ledger.get(q.id);f.ledger.update(q.id,r.sequence,'VERIFYING');
  assert.throws(()=>f.ledger.bind(q.id,evidence(q)));assert.equal(f.ledger.get(q.id).state,'VERIFYING');
});
test('missing durable table or duplicate protection index fails closed and keeps backup',async t=>{
  const f=await fixture(t);assert.ok((await fs.readdir(path.join(f.root,'recovery'))).some(n=>n.startsWith('before-purchase-create-')));
  f.state.db.exec('DROP INDEX purchase_create_order');await assert.rejects(()=>PurchaseCreates.open(f.state),{code:'PURCHASE_CREATE_STATE_CORRUPT'});
});

test('legacy component migration cannot silently omit a reserved or submitted creation identity',async t=>{
  const f=await fixture(t);reserve(f);
  await assert.rejects(()=>exportMigration({state:f.state,config:{},output:path.join(f.root,'unsafe.zip'),sourceStopped:true}),{code:'INTEGRATED_PURCHASE_MIGRATION_REQUIRED'});
  await assert.rejects(()=>fs.stat(path.join(f.root,'unsafe.zip')),{code:'ENOENT'});
});

test('immediate close before preparation microtask pauses without upload or lost task',async t=>{
  const f=await fixture(t),p=reserve(f),c=new MachinePurchaseCreate({ledger:f.ledger,adapter:{prepare:()=>assert.fail('must not start'),upload:()=>assert.fail('must not upload')},onFatal:e=>assert.fail(e)});
  c.start(p.id);await c.close();assert.equal(f.ledger.get(p.id).state,'PAUSED');assert.equal(f.ledger.get(p.id).uploadAttempted,false);
});
