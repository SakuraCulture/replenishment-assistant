import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {CatalogService} from '../src/service.mjs';
import {canonical,sha} from '../src/common.mjs';
const uid=()=>crypto.randomBytes(16).toString('hex');
async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-rebind-'));
  const driver={close:async()=>{},automaticBinding:async wh=>({actualProfilePath:path.join(root,wh,'Default')}),
    inspect:async()=>({company:'QA sd',coid:'1',uid:'2',expectedRows:1})};
  let service=await CatalogService.open({stateRoot:root,driver});
  t.after(async()=>{await service.close();assert.equal(path.dirname(root),path.resolve(os.tmpdir()));assert(path.basename(root).startsWith('jst-rebind-'));await fs.rm(root,{recursive:true,force:true});});
  const accounts={};for(const [id,wh,coid,operator] of [['sd','山东仓','1','2'],['hb','河北仓','3','4']])
    accounts[wh]={id,warehouse:wh,expectedCompany:'QA '+id,expectedCoid:coid,expectedUid:operator,mappingVerified:true,bindingVersion:1,
      binding:{actualProfilePath:path.join(root,id,'Default')},enabled:true};
  const request={operation_id:'original-export-operation',purpose:'default_catalog',items:[{warehouse:'山东仓'},{warehouse:'河北仓'}]};
  await service.store.write(d=>{d.accounts=accounts;d.operations={};d.batches.old={id:'old',createdAt:'2026-11-11',state:'STOPPED',...request,
    fingerprint:sha(canonical(request)),items:Object.values(accounts).map((a,index)=>{const {enabled,...account}=a;return {id:a.id,index,account,stage:'ACCEPTED',
      native:{stage:'ACCEPTED',submissions:1,nativeTaskRef:'task-'+a.id},receipt:null,adoption:null};})};});
  async function probe(wh,target=wh){
    const a=accounts[target],id='probe-'+uid();
    await service.store.write(d=>{d.operations[id]={id,state:'SUCCEEDED',action:'accounts.probe',warehouse:wh,
      bindingEpoch:d.accounts?.[wh]?.bindingVersion??d.bindingEpochs?.[wh]??0,
      result:{binding:a.binding,scope:{company:a.expectedCompany,coid:a.expectedCoid,uid:a.expectedUid,expectedRows:1}}};});return id;
  }
  const preview=warehouses=>service.command({action:'accounts.reset_preview',warehouses});
  const reset=(plan,id=uid())=>service.command({action:'accounts.reset_bindings',warehouses:plan.warehouses,expected_fingerprint:plan.fingerprint,operation_id:id,confirmed:true});
  const bind=async(wh,target,version)=>service.command({action:'accounts.bind',warehouse:wh,probe_id:await probe(wh,target),expected_version:version,mapping_verified:true});
  return {root,driver,accounts,request,get service(){return service;},probe,preview,reset,bind,
    reopen:async()=>{await service.close();service=await CatalogService.open({stateRoot:root,driver});}};
}

test('identical verified binding saves without invalidating or blocking a pending task',async t=>{
  const c=await fixture(t),probe=await c.probe('山东仓'),before=c.service.store.read();
  const result=await c.service.command({action:'accounts.bind',warehouse:'山东仓',probe_id:probe,expected_version:1,mapping_verified:true});
  assert.deepEqual(result,c.accounts['山东仓']);assert.deepEqual(c.service.store.read(),before);
});
test('two swapped accounts can be retired together and rebound in either order; native evidence and files remain',async t=>{
  const c=await fixture(t);const file=path.join(c.root,'original.xlsx');await fs.writeFile(file,'retained original bytes');
  const oldProbe=await c.probe('山东仓','河北仓');
  await assert.rejects(c.service.command({action:'accounts.bind',warehouse:'山东仓',expected_version:1,probe_id:oldProbe,mapping_verified:true}),{code:'PENDING_TASK'});
  const plan=await c.preview(['山东仓','河北仓']),before=c.service.store.read(),result=await c.reset(plan);
  assert.deepEqual(JSON.parse(await fs.readFile(result.backup_path,'utf8')).before,before);
  const ended=c.service.store.batch('old');assert.equal(ended.state,'LOCALLY_ENDED');assert(ended.items.every(i=>i.retirement));
  assert.deepEqual(ended.items.map(i=>i.native),before.batches.old.items.map(i=>i.native));assert.equal(await fs.readFile(file,'utf8'),'retained original bytes');
  assert.deepEqual(c.service.store.accounts(),[]);
  await assert.rejects(c.service.command({action:'accounts.bind',warehouse:'山东仓',expected_version:2,probe_id:oldProbe,mapping_verified:true}),{code:'PROBE_STALE'});
  assert.equal((await c.bind('山东仓','河北仓',2)).bindingVersion,3);assert.equal((await c.bind('河北仓','山东仓',2)).bindingVersion,3);
  assert.equal(c.service.store.read().accounts['山东仓'].expectedCoid,'3');assert.equal(c.service.store.read().accounts['河北仓'].expectedCoid,'1');
});
test('single warehouse recovery preserves the other warehouse and every adopted item',async t=>{
  const c=await fixture(t);await c.service.store.write(d=>{const adopted=structuredClone(d.batches.old.items[0]);adopted.id='adopted';adopted.adoption={saved:true};d.batches.old.items.push(adopted);});
  const before=c.service.store.read();await c.reset(await c.preview(['山东仓']));const after=c.service.store.read();
  assert.deepEqual(after.accounts['河北仓'],before.accounts['河北仓']);assert.deepEqual(after.batches.old.items[1],before.batches.old.items[1]);
  assert.deepEqual(after.batches.old.items[2],before.batches.old.items[2]);assert(after.batches.old.items[0].retirement);
  assert.equal(after.batches.old.state,before.batches.old.state);
});
test('retired tasks reject resume, retry, late observations, late receipts and cached write callbacks',async t=>{
  const c=await fixture(t);await c.reset(await c.preview(['山东仓','河北仓']));const before=c.service.store.read();
  for(const action of ['sync.resume','sync.retry_unsubmitted','sync.provide_observation','sync.record_adoption_error','sync.acknowledge_adoption'])
    await assert.rejects(c.service.command({action,batch_id:'old',item_id:'sd',item_ids:['sd'],receipt:{warehouse:'山东仓',source_sha256:'late'}}),{code:'ITEM_RETIRED'});
  await assert.rejects(c.service.patch('old','sd',{retirement:null,stage:'APPLIED'}),{code:'ITEM_RETIRED'});
  await assert.rejects(c.service.command({action:'sync.start',...c.request}),{code:'ITEM_RETIRED'});
  assert.deepEqual(c.service.store.read(),before);
});
test('preview is read-only; stale scope is rejected and replaying a completed reset cannot clear new bindings',async t=>{
  const c=await fixture(t),before=c.service.store.read(),plan=await c.preview(['山东仓']);assert.deepEqual(c.service.store.read(),before);
  await c.service.command({action:'accounts.set_enabled',warehouse:'山东仓',expected_version:1,enabled:false});
  await assert.rejects(c.reset(plan),{code:'CONFIG_CONFLICT'});
  const fresh=await c.preview(['山东仓']),id=uid(),saved=await c.reset(fresh,id);await c.bind('山东仓','山东仓',2);
  const after=c.service.store.read();assert.deepEqual(await c.reset(fresh,id),saved);assert.deepEqual(c.service.store.read(),after);
});
test('host adoption gate prevents recovery during a commit without aborting the writer',async t=>{
  const c=await fixture(t),plan=await c.preview(['山东仓']),before=c.service.store.read();
  const gate=new DatabaseSync(path.join(c.root,'binding-change.lock.sqlite'));gate.exec('BEGIN IMMEDIATE');
  try{await assert.rejects(c.reset(plan),{code:'ADOPTION_BUSY'});assert.deepEqual(c.service.store.read(),before);}
  finally{gate.exec('ROLLBACK');gate.close();}
  await c.reset(plan);
});
test('running native task must stop safely before recovery; backup failure leaves bindings intact',async t=>{
  const c=await fixture(t),plan=await c.preview(['山东仓']);let finish;const wait=new Promise(r=>{finish=r;});
  c.service.launch('old','batch',async()=>wait);const active=c.service.active;
  try{await assert.rejects(c.reset(plan),{code:'BUSY'});assert.equal(active.controller.signal.aborted,false);}
  finally{finish();await active.promise;}
  const before=c.service.store.read();await fs.writeFile(path.join(c.root,'binding_backups'),'cannot create backup directory');
  await assert.rejects(c.reset(plan));assert.deepEqual(c.service.store.read(),before);
});
test('restart holds automatic binding; explicit prepare requires mapping confirmation and versions never go backwards',async t=>{
  const c=await fixture(t);await c.reset(await c.preview(['山东仓']));await c.reopen();
  await assert.rejects(c.service.command({action:'accounts.ensure',warehouse:'山东仓',expected_version:0}),{code:'ACCOUNT_REBIND_REQUIRED'});
  const ack=await c.service.command({action:'accounts.auto',warehouse:'山东仓',expected_version:2});await c.service.active.promise;
  const op=c.service.status().operations[ack.operation_id];assert.equal(op.state,'SUCCEEDED');assert.equal(op.result.requires_confirmation,true);
  assert.equal(c.service.store.read().accounts['山东仓'],undefined);
  const saved=await c.service.command({action:'accounts.bind',warehouse:'山东仓',expected_version:2,probe_id:op.id,mapping_verified:true});assert.equal(saved.bindingVersion,3);
  assert(!c.service.status().rebind_required['山东仓']);
});
test('store failure after backup is recoverable with the same operation ID',async t=>{
  const c=await fixture(t),plan=await c.preview(['山东仓']),before=c.service.store.read(),id=uid(),write=c.service.store.write.bind(c.service.store);
  c.service.store.write=async()=>{throw Error('synthetic store failure');};
  await assert.rejects(c.reset(plan,id),/synthetic/);assert.deepEqual(c.service.store.read(),before);
  c.service.store.write=write;await c.reset(plan,id);assert(!c.service.store.read().accounts['山东仓']);
});

test('an existing incomplete backup never permits clearing a binding',async t=>{
  const c=await fixture(t),plan=await c.preview(['山东仓']),before=c.service.store.read(),id=uid();
  await fs.mkdir(path.join(c.root,'binding_backups'));
  const file=path.join(c.root,'binding_backups',id+'.json');
  await fs.writeFile(file,JSON.stringify({operation_id:id,fingerprint:plan.fingerprint,before:{storeId:before.storeId}}));
  await assert.rejects(c.reset(plan,id),/备份不完整/);assert.deepEqual(c.service.store.read(),before);
});
