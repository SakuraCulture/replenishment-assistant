import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {CatalogService} from '../src/service.mjs';
import {sha} from '../src/common.mjs';

async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-migration-'));
  let submits=0,recovers=0;
  const file=path.join(root,'downloads','source-batch','item','original.xlsx');
  const source={id:'sd',warehouse:'山东仓',expectedCompany:'测试公司',expectedCoid:'11',expectedUid:'22',mappingVerified:true,bindingVersion:7};
  const local={...source,bindingVersion:9,binding:{actualProfilePath:path.join(root,'local-browser','Default')},enabled:false};
  const driver={close:async()=>{},page:async()=>({}),query:async()=>({}),export:async()=>{submits++;throw Error('must not resubmit');},
    recover:async(_,o)=>{recovers++;assert.equal(o.recordId,'PLATFORM-ORIGINAL');await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,'native-file');return {file,sha256:sha(Buffer.from('native-file'))};},
    validate:async()=>({source:file,candidate:file,source_sha256:sha(Buffer.from('native-file')),candidate_sha256:sha(Buffer.from('native-file')),warehouse:'山东仓'})};
  const service=await CatalogService.open({stateRoot:root,driver});
  t.after(async()=>{await service.close();assert.equal(path.dirname(root),path.resolve(os.tmpdir()));assert(path.basename(root).startsWith('jst-migration-'));await fs.rm(root,{recursive:true,force:true});});
  await service.store.write(d=>{
    d.accounts={'山东仓':local};d.rebindRequired={'山东仓':true};d.bindingEpochs={'山东仓':9};
    d.batches['source-batch']={id:'source-batch',createdAt:'2026-11-11',purpose:'default_catalog',state:'PAUSED',items:[{
      id:'item',account:source,stage:'ACCEPTED',native:{stage:'ACCEPTED',submissions:1,nativeTaskRef:'PLATFORM-ORIGINAL',scope:{expectedRows:1,coid:'11',uid:'22'}},
      receipt:null,adoption:null,migration:{needs_binding:true}}]};
  });
  async function bind(coid='11'){
    const probe='verified-probe';await service.store.write(d=>{d.operations||={};d.operations[probe]={state:'SUCCEEDED',action:'accounts.probe',warehouse:'山东仓',bindingEpoch:9,
      result:{binding:local.binding,scope:{company:source.expectedCompany,coid,uid:'22'}},finishedAt:'2026-11-11'};});
    return service.command({action:'accounts.bind',warehouse:'山东仓',expected_version:9,probe_id:probe,mapping_verified:true});
  }
  return {service,bind,counters:()=>({submits,recovers})};
}

test('migration snapshot uses the live store and does not lose tasks beyond the status limit',async t=>{
  const f=await fixture(t);await f.service.store.write(d=>{for(let i=0;i<60;i++)d.batches['extra-'+i]={id:'extra-'+i,items:[]};});
  const before=f.service.store.read(),result=await f.service.command({action:'sync.migration_snapshot'});
  assert.equal(Object.keys(result.batches).length,61);assert.deepEqual(result,before);assert.deepEqual(f.service.store.read(),before);
  await assert.rejects(f.service.store.write(()=>{throw Error('cancelled write');}));
  assert.deepEqual(await f.service.command({action:'sync.migration_snapshot'}),before);
});

test('migration cannot resume until a verified local binding is saved',async t=>{
  const f=await fixture(t),before=f.service.store.read();
  await assert.rejects(f.service.command({action:'sync.resume',batch_id:'source-batch'}),{code:'MIGRATION_BINDING_REQUIRED'});
  assert.deepEqual(f.service.store.read(),before);assert.deepEqual(f.counters(),{submits:0,recovers:0});
});

test('different warehouse account cannot take over imported platform tasks',async t=>{
  const f=await fixture(t);await assert.rejects(f.bind('wrong-coid'),{code:'MIGRATION_ACCOUNT_MISMATCH'});
  const i=f.service.store.batch('source-batch').items[0];assert(i.migration.needs_binding);assert.equal(i.native.nativeTaskRef,'PLATFORM-ORIGINAL');
  assert.deepEqual(f.counters(),{submits:0,recovers:0});
});

test('same identity can rebind on the target and recover original task without export',async t=>{
  const f=await fixture(t);const a=await f.bind();const i=f.service.store.batch('source-batch').items[0];
  assert.equal(a.enabled,true);assert.equal(i.migration.needs_binding,false);assert.equal(i.account.binding.actualProfilePath,a.binding.actualProfilePath);
  assert.equal(f.service.store.read().rebindRequired['山东仓'],undefined);
  await f.service.command({action:'sync.resume',batch_id:'source-batch'});await f.service.active.promise;
  const completed=f.service.store.batch('source-batch');assert.equal(completed.state,'FILES_READY');
  assert.equal(completed.items[0].native.nativeTaskRef,'PLATFORM-ORIGINAL');assert.equal(completed.items[0].native.submissions,1);
  assert.deepEqual(f.counters(),{submits:0,recovers:1});
});

test('a submission of unknown outcome stays unknown after verified migration rebind',async t=>{
  const f=await fixture(t);await f.service.store.write(d=>{d.batches['source-batch'].items[0].native.stage='SUBMISSION_UNKNOWN';});
  await f.bind();await f.service.command({action:'sync.resume',batch_id:'source-batch'});await f.service.active.promise;
  const i=f.service.store.batch('source-batch').items[0];assert.equal(i.error.code,'SUBMISSION_UNKNOWN');assert.equal(i.native.submissions,1);
  assert.deepEqual(f.counters(),{submits:0,recovers:0});
});
