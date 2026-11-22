import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {PurchaseCreates} from '../src/purchase-create-ledger.mjs';
import {PurchaseReceipts} from '../src/purchase-receipt-ledger.mjs';
import {MachinePurchaseCreate} from '../src/machine-purchase-create.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';
import {IMPORT_CODE,orderKey} from '../src/purchase-create-contract.mjs';
import {sha256,canonical} from '../src/common.mjs';
const h='a'.repeat(64),op='1'.repeat(32);
function intent(id='old'){
  return {schema:1,operation:'purchase.create',id,identity:{accountId:'A',merchantCode:'M'},mappingVersion:h,
    basis:{oid:'source-oid',number:'BH-1',warehouse:'河北仓',store:'S',planFingerprint:h,paymentVersion:1,paymentDigest:h},
    target:{buyerWarehouseCode:'S',displayName:'合成店'},supplierCode:'SUPPLIER',marker:'AX_CREATE:'+id,note:'AX_CREATE:'+id+' 本地单:BH-1',
    createExternalOrder:false,postCreationState:'待提交',file:{path:path.resolve('synthetic.xlsx'),sha256:h,bytes:300},
    template:{recordFile:path.resolve('synthetic.json'),sha256:h,attemptId:'tpl'},
    rows:[{sku:'SKU',name:'合成商品',quantity:'2',amount:'4'}],totalQuantity:'2',totalAmount:'4'};
}
const hash=p=>sha256(JSON.stringify(p));
const receipt=p=>({intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261110120000@7114',code:IMPORT_CODE,fileSha256:h,exactSourceBytesPresent:true,importRequests:1,requestSha256:h});
async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-rebuild-'));let state=await StateStore.open(root),ledger=await PurchaseCreates.open(state);
  t.after(async()=>{state.close();await fs.rm(root,{recursive:true,force:true});});
  return {get ledger(){return ledger;},get state(){return state;},async reopen(){state.close();state=await StateStore.open(root);ledger=await PurchaseCreates.open(state);ledger.recover();}};
}
function reserve(f,p=intent()){f.ledger.reserve(p,hash(p));return p;}
function sent(f,p){let r=f.ledger.get(p.id);for(const s of ['PREPARING','READY','SUBMITTING'])r=f.ledger.update(p.id,r.sequence,s,s==='SUBMITTING'?{uploadAttempted:true}:{});return r;}
function rebuild(f,p,q=intent('new'),operation=op){return f.ledger.rebuild(p.id,f.ledger.get(p.id).sequence,q,hash(q),operation,'业务确认重建，保留旧平台单');}

test('unknown upload owns its original reservation through restart',async t=>{
  const f=await fixture(t),p=reserve(f);const s=sent(f,p);f.ledger.update(p.id,s.sequence,'NEEDS_REVIEW',{error:{code:'SUBMISSION_UNKNOWN'}});
  const q=intent('new'),before=f.ledger.get(p.id);
  assert.throws(()=>rebuild(f,p,q),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  await f.reopen();
  assert.throws(()=>rebuild(f,p,q),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  assert.deepEqual(f.ledger.get(p.id),before);
  assert.equal(f.ledger.list().length,1);
  assert.throws(()=>f.ledger.reserve(intent('third'),h),{code:'PURCHASE_ALREADY_RESERVED'});
});
test('late original receipts after a refused rebuild remain on the original task',async t=>{
  const f=await fixture(t),p=reserve(f);let r=sent(f,p);f.ledger.update(p.id,r.sequence,'NEEDS_REVIEW',{error:{code:'SUBMISSION_UNKNOWN'}});
  assert.throws(()=>rebuild(f,p),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  f.ledger.accepted(receipt(p));
  const old=f.ledger.get(p.id);assert.equal(old.state,'ACCEPTED');assert.equal(old.receipt.taskId,receipt(p).taskId);
  assert.equal(f.ledger.accepted(receipt(p)).sequence,old.sequence);
  assert.equal(f.ledger.list().length,1);
  assert.throws(()=>rebuild(f,p),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
});
test('one component transaction rolls back predecessor release if successor cannot reserve',async t=>{
  const f=await fixture(t),p=reserve(f),q=intent('new');q.identity.accountId='SECOND';
  // A historical extra reservation with another operator must not be hidden by a failed rebuild.
  const before=f.ledger.get(p.id);const run=f.ledger.reserveWithin.bind(f.ledger);
  f.ledger.reserveWithin=()=>{throw Error('isolated disk/insert failure');};
  assert.throws(()=>rebuild(f,p,q),/isolated/);f.ledger.reserveWithin=run;
  assert.deepEqual(f.ledger.get(p.id),before);assert.equal(f.ledger.list().length,1);
});
test('rebuild validates source identity, current sequence and safe action boundary',async t=>{
  const f=await fixture(t),p=reserve(f);const q=intent('new');q.basis.oid='different';
  assert.throws(()=>rebuild(f,p,q),{code:'PURCHASE_REBUILD_SCOPE'});
  assert.throws(()=>f.ledger.rebuild(p.id,99,intent('new'),h,op,'reason'),{code:'PURCHASE_STATE_CONFLICT'});
  sent(f,p);assert.throws(()=>rebuild(f,p),{code:'PURCHASE_CREATE_BUSY'});
});
test('missing predecessor cannot establish that the original was never uploaded',async t=>{
  const f=await fixture(t),q=intent('new');
  assert.throws(()=>f.ledger.rebuild('missing',1,q,hash(q),op,'旧记录缺失，业务确认重建'),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  assert.equal(f.ledger.list().length,0);
});
test('explicit never-uploaded rebuild releases original reservation, unrelated order remains identical',async t=>{
  const f=await fixture(t),p=reserve(f),other=intent('other');other.basis.number='BH-2';other.note=other.marker+' 本地单:BH-2';reserve(f,other);
  const before=f.ledger.get(other.id);rebuild(f,p);assert.deepEqual(f.ledger.get(other.id),before);
  const controller=new MachinePurchaseCreate({ledger:f.ledger,adapter:{}});
  assert.throws(()=>controller.start(p.id),{code:'PURCHASE_SUPERSEDED'});
  assert.throws(()=>f.ledger.accepted(receipt(p)),{code:'UNEXPECTED_IMPORT_RECEIPT'});
});

test('only the exact voided purchase with no receiving conflict can be rebuilt',async t=>{
  const f=await fixture(t),p=reserve(f);sent(f,p);let r=f.ledger.accepted(receipt(p));
  r=f.ledger.update(p.id,r.sequence,'VERIFYING');r=f.ledger.update(p.id,r.sequence,'BOUND',{binding:{orderId:'PO-OLD',status:'待提交'}});
  assert.throws(()=>rebuild(f,p),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  const receipts=await PurchaseReceipts.open(f.state);
  const original=receipts.register(r,'2026-11-10');
  const voided={...original,state:'VOIDED',terminal:true,terminalKind:'VOIDED',finalSync:'NOT_DUE',
    summary:{status:'已作废',issueCount:0}};
  f.state.write(()=>receipts.save({...voided,state:'VOIDED_REVIEW',hostConflict:'已有实收'},'CONTROLLED_RECEIPT_CONFLICT'));
  assert.throws(()=>rebuild(f,p),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  const stored=f.state.write(()=>receipts.save({...voided,sequence:receipts.get(p.id).sequence},'CONTROLLED_NATIVE_VOID'));
  const before=structuredClone(r),newer=rebuild(f,p).record,old=f.ledger.get(p.id);
  assert.equal(old.state,'SUPERSEDED');assert.deepEqual(old.binding,before.binding);assert.deepEqual(old.receipt,before.receipt);
  assert.equal(newer.previousOrderId,'PO-OLD');assert.equal(newer.previousTaskId,before.receipt.taskId);
  assert.deepEqual(receipts.get(p.id),stored);
  await f.reopen();
  assert.equal(rebuild(f,p).record.id,newer.id);
  assert.throws(()=>f.ledger.update(p.id,old.sequence,'BOUND',{binding:{orderId:'PO-NEW'}}),{code:'PURCHASE_STATE_TRANSITION'});
});

function legacyExtra(f,p,oid=p.basis.oid){
  const q=intent('legacy-extra');q.identity.accountId='SECOND';q.basis.oid=oid;
  const r={...f.ledger.get(p.id),id:q.id,intent:q,requestHash:hash(q)};
  f.state.write(()=>{
    f.state.db.prepare('INSERT INTO purchase_creates VALUES(?,?,?,?,?,?,?,?)').run(r.id,orderKey(q),sha256(canonical(q.identity)),r.requestHash,r.state,r.sequence,null,JSON.stringify(r));
    f.ledger.event(r,'LEGACY_FIXTURE');
  });return r;
}
test('explicit rebuild retires related legacy operator reservations of the exact oid only',async t=>{
  const f=await fixture(t),p=reserve(f),extra=legacyExtra(f,p),newer=rebuild(f,p).record;
  assert.deepEqual(newer.rebuildAlso,[extra.id]);assert.equal(f.ledger.get(extra.id).successorId,newer.id);
  assert.equal(f.ledger.list().filter(r=>r.state==='RESERVED').length,1);
});
test('a different oid with the same visible number remains protected and release is rolled back',async t=>{
  const f=await fixture(t),p=reserve(f),extra=legacyExtra(f,p,'different-source'),before=f.ledger.get(p.id);
  assert.throws(()=>rebuild(f,p),{code:'PURCHASE_ALREADY_RESERVED'});
  assert.deepEqual(f.ledger.get(p.id),before);assert.deepEqual(f.ledger.get(extra.id),extra);
});
test('missing predecessor does not release another known reservation for the same original oid',async t=>{
  const f=await fixture(t),p=reserve(f),q=intent('new');
  assert.throws(()=>f.ledger.rebuild('lost-predecessor',1,q,hash(q),op,'业务明确重建原仓单'),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  assert.equal(f.ledger.get(p.id).state,'RESERVED');
});
test('an unsafe replacement reserved by the old build still cannot release a native request',async t=>{
  const f=await fixture(t),p=reserve(f);
  const original=sent(f,p),q=legacyExtra(f,p);
  f.state.write(()=>{
    const old={...original,state:'SUPERSEDED',successorId:q.id};
    f.state.db.prepare('UPDATE purchase_creates SET state=?,payload=? WHERE id=?').run(old.state,JSON.stringify(old),old.id);
    const next={...q,state:'RESERVED',uploadAttempted:false,rebuildOf:p.id};
    f.state.db.prepare('UPDATE purchase_creates SET state=?,payload=? WHERE id=?').run(next.state,JSON.stringify(next),next.id);
  });
  await f.reopen();
  let r=f.ledger.get(q.id);
  r=f.ledger.update(q.id,r.sequence,'PREPARING');
  r=f.ledger.update(q.id,r.sequence,'READY');
  assert.throws(()=>f.ledger.update(q.id,r.sequence,'SUBMITTING',{uploadAttempted:true}),{code:'PURCHASE_ORIGINAL_UNRESOLVED'});
  assert.equal(f.ledger.get(q.id).uploadAttempted,false);
  const late=f.ledger.accepted(receipt(p));
  assert.equal(late.receipt.taskId,receipt(p).taskId);
  assert.equal(f.ledger.get(q.id).receipt,null);
});
test('submit-only saves receipt and frees controller before any import observation',async t=>{
  const f=await fixture(t),p=reserve(f);let uploads=0,observes=0;
  const controller=new MachinePurchaseCreate({ledger:f.ledger,adapter:{prepare:async()=>({orderIds:[]}),upload:async()=>{uploads++;return receipt(p);},observe:async()=>{observes++;throw Object.assign(Error('pending'),{code:'TASK_PENDING'});}}});
  controller.start(p.id,{submitOnly:true});await controller.pending;
  assert.equal(f.ledger.get(p.id).state,'ACCEPTED');assert.equal(uploads,1);assert.equal(observes,0);assert.equal(controller.busy,false);
  controller.start(p.id,{submitOnly:true});await controller.pending;assert.equal(uploads,1);
  controller.start(p.id);await controller.pending;assert.equal(observes,1);assert.equal(uploads,1);
});
test('rebuild confirmation is required in the protocol but cannot waive replacement checks',()=>{
  const p={protocol_version:1,request_id:'r',action:'purchase.rebuild',purchase_id:'old',sequence:1,
    package_file:'C:/isolated/new.json',package_sha256:h,operation_id:op,reason:'业务重建',acknowledge_uncertainty:true};
  assert.equal(validateRequest(p),p);
  for(const change of [{acknowledge_uncertainty:false},{sequence:0},{operation_id:'bad'},{extra:'x'}])assert.throws(()=>validateRequest({...p,...change}),{code:'REQUEST_SCHEMA'});
  validateRequest({protocol_version:1,request_id:'r',action:'purchase.submit',purchase_id:'old'});
});
