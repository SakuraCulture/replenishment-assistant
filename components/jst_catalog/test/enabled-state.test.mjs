import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {CatalogService} from '../src/service.mjs';
import {sha} from '../src/common.mjs';

async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-enabled-'));
  const driver={close:async()=>{},page:async()=>{throw Error('No browser operation expected');},
    validate:async(file,o)=>({warehouse:o.warehouse,source_sha256:sha(await fs.readFile(file)),candidate:path.join(root,'candidate.json')})};
  const service=await CatalogService.open({stateRoot:root,driver});
  t.after(async()=>{await service.close();assert.equal(path.dirname(root),path.resolve(os.tmpdir()));assert(path.basename(root).startsWith('jst-enabled-'));await fs.rm(root,{recursive:true,force:true});});
  const accounts={};
  for(const [id,warehouse] of [['sd','山东仓'],['hb','河北仓']])accounts[warehouse]={id,warehouse,expectedCompany:'QA '+id,
    expectedCoid:id==='sd'?'1':'3',expectedUid:id==='sd'?'2':'4',mappingVerified:true,bindingVersion:1,
    binding:{actualProfilePath:path.join(root,id,'Default')},enabled:true};
  await service.store.write(d=>{d.accounts=accounts;d.assets={shared:'unchanged'};});
  const frozen=()=>{const a=service.store.read().accounts['山东仓'];const {enabled,enabledVersion,...original}=a;return original;};
  const toggle=(enabled,expected_enabled_version=0)=>service.command({action:'accounts.set_enabled',warehouse:'山东仓',expected_version:1,expected_enabled_version,enabled});
  return {root,driver,service,frozen,toggle};
}

test('pending exports and unadopted files do not block disabling; all records and the other warehouse stay intact',async t=>{
  for(const entry of [{native:{submissions:1,nativeTaskRef:'known',stage:'ACCEPTED'}},
                     {native:{submissions:1,stage:'SUBMISSION_UNKNOWN'}},
                     {native:{stage:'DOWNLOADED'},receipt:{source_sha256:'saved'}}]){
    const c=await fixture(t);await c.service.store.write(d=>{d.batches.original={id:'original',items:[{id:'sd',account:c.frozen(),...entry}]};});
    const before=c.service.store.read();const account=await c.toggle(false);const after=c.service.store.read();
    assert.equal(account.enabled,false);assert.equal(account.bindingVersion,1);assert.equal(account.enabledVersion,1);
    assert.deepEqual(after.batches,before.batches);assert.deepEqual(after.assets,before.assets);
    assert.deepEqual(after.accounts['河北仓'],before.accounts['河北仓']);assert.deepEqual(c.frozen(),before.batches.original.items[0].account);
  }
});

test('disable then reenable preserves the frozen account and resumes the original downloaded file',async t=>{
  const c=await fixture(t),file=path.join(c.root,'saved.xlsx');await fs.writeFile(file,'isolated saved download');
  const original={id:'sd',account:c.frozen(),stage:'DOWNLOADED',native:{stage:'DOWNLOADED',submissions:1,nativeTaskRef:'original-task',file,
    sha256:sha(await fs.readFile(file)),scope:{expectedRows:1}},receipt:null,adoption:null};
  await c.service.store.write(d=>{d.batches.original={id:'original',purpose:'default_catalog',items:[original]};});
  await c.toggle(false);await c.toggle(true,1);
  assert.deepEqual(c.service.store.batch('original').items,[original]);
  await c.service.command({action:'sync.resume',batch_id:'original',item_ids:['sd']});await c.service.active.promise;
  const item=c.service.store.batch('original').items[0];assert.equal(item.stage,'READY_TO_ADOPT');assert.equal(item.error,null);
  assert.equal(item.native.nativeTaskRef,'original-task');assert.equal(item.native.submissions,1);assert.deepEqual(item.account,original.account);
});

test('a disabled original task reports disabled rather than an account identity change',async t=>{
  const c=await fixture(t);await c.service.store.write(d=>{d.batches.original={id:'original',items:[{id:'sd',account:c.frozen(),stage:'QUEUED'}]};});
  await c.toggle(false);await c.service.command({action:'sync.resume',batch_id:'original',item_ids:['sd']});await c.service.active.promise;
  assert.equal(c.service.store.batch('original').items[0].error.code,'ACCOUNT_DISABLED');
});

test('binding protection and separate switch CAS still reject stale requests without changing data',async t=>{
  const c=await fixture(t);await c.service.store.write(d=>{d.batches.original={items:[{account:c.frozen(),native:{stage:'ACCEPTED',submissions:1}}]};
    d.operations={changed:{state:'SUCCEEDED',action:'accounts.probe',warehouse:'山东仓',result:{binding:{actualProfilePath:'new-profile'},scope:{company:'New QA',coid:'99',uid:'98'}}}};});
  await c.toggle(false);const before=c.service.store.read();
  await assert.rejects(c.toggle(true,0),{code:'CONFIG_CONFLICT'});
  await assert.rejects(c.service.command({action:'accounts.bind',warehouse:'山东仓',expected_version:1,probe_id:'changed',mapping_verified:true}),{code:'PENDING_TASK'});
  await assert.rejects(c.service.command({action:'accounts.set_enabled',warehouse:'山东仓',expected_version:0,enabled:true}),{code:'CONFIG_CONFLICT'});
  assert.deepEqual(c.service.store.read(),before);await c.toggle(true,1);
});

test('disabling during an active operation changes only future enablement and never aborts that operation',async t=>{
  const c=await fixture(t);let release;const gate=new Promise(r=>{release=r;});
  c.service.launch('active','batch',async()=>gate);const active=c.service.active;
  try{await c.toggle(false);assert.equal(c.service.active,active);assert.equal(active.controller.signal.aborted,false);}
  finally{release();await active.promise;}
  let finish;const waiting=new Promise(r=>{finish=r;});c.service.launch('checking-account','account',async()=>waiting);
  try{await assert.rejects(c.toggle(true,1),{code:'BUSY'});assert.equal(c.service.store.accounts()[0].enabled,false);}
  finally{finish();await c.service.active?.promise;}
});

test('disabled setting persists across service restart; startup does not turn it on or launch a browser',async t=>{
  const c=await fixture(t);await c.toggle(false);const before=c.service.store.read();await c.service.close();
  const reopened=await CatalogService.open({stateRoot:c.root,driver:c.driver});
  try{
    assert.deepEqual(reopened.store.read(),before);
    const ack=await reopened.command({action:'accounts.ensure',warehouse:'山东仓',expected_version:1});await reopened.active.promise;
    assert.equal(reopened.status().operations[ack.operation_id].error.code,'ACCOUNT_DISABLED');assert.equal(reopened.store.accounts()[0].enabled,false);
  }finally{await reopened.close();}
});

test('disabled warehouse cannot start a new automatic export and invalid flag inputs do not mutate it',async t=>{
  const c=await fixture(t);await c.toggle(false);const before=c.service.store.read();
  await assert.rejects(c.service.command({action:'sync.start',operation_id:'isolated-new-operation',purpose:'default_catalog',items:[{warehouse:'山东仓'}]}),/尚未启用/);
  await assert.rejects(c.service.command({action:'accounts.set_enabled',warehouse:'山东仓',expected_version:1,enabled:'false'}),/启用状态/);
  assert.deepEqual(c.service.store.read(),before);
});
