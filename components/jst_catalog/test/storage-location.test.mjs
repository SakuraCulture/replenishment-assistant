import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import os from 'node:os';
import fs from 'node:fs/promises';
import {sameBinding,relocatedBinding} from '../src/storage-paths.mjs';
import {AccountSessions} from '../src/accounts.mjs';
import {CatalogService} from '../src/service.mjs';
import {canonical} from '../src/common.mjs';

const old=path.resolve('fixture-old-storage'),current=path.resolve('fixture-new-storage');
const aliases=[{from:old,to:current}];
const binding=root=>({userDataRoot:path.join(root,'settings','jst_catalog_sync','browsers','sd','User Data'),
  actualProfilePath:path.join(root,'settings','jst_catalog_sync','browsers','sd','User Data','Default'),
  edgeExecutable:'fixed-edge.exe',profileDirectory:'Default',backend:'jst_dedicated'});

test('only an explicitly approved whole-data move makes old/new profile bindings equivalent',()=>{
  assert.equal(sameBinding(binding(old),binding(current)),false);
  assert.equal(sameBinding(binding(old),binding(current),aliases),true);
  assert.equal(sameBinding(binding(old),binding(path.resolve('different-browser')),aliases),false);
  assert.equal(sameBinding(binding(old),{...binding(current),edgeExecutable:'other.exe'},aliases),false);
  assert.equal(sameBinding(binding(old),{...binding(current),profileDirectory:'Profile 2'},aliases),false);
  assert.equal(relocatedBinding(binding(old),aliases).actualProfilePath,binding(current).actualProfilePath);
});

test('similar name prefixes and unrelated paths never gain equivalence',()=>{
  assert.equal(sameBinding(binding(old+'-other'),binding(current),aliases),false);
  assert.equal(sameBinding(binding(old),binding(current),[{from:path.parse(old).root,to:current}]),false);
});

test('a moved retained browser reuses its frozen key and remains visible in status',async()=>{
  const sessions=new AccountSessions({stateRoot:current,bridgeRoot:'unused',lockRoot:'unused',storageAliases:aliases});
  let opened=0;
  class Bridge{
    constructor(){opened++;this.page={isClosed:()=>false};this.browser={isConnected:()=>true};}
    async bind(value){assert.deepEqual(value,binding(current));}
    async close(){}
  }
  sessions.components={EdgeBridge:Bridge,ExclusiveLock:{acquire:async()=>({release(){}})}};
  sessions.binding=async()=>binding(current);sessions.ensureBrowser=async()=> 'isolated-endpoint';
  const account={warehouse:'山东仓',binding:binding(old)};
  const first=await sessions.connect(account);assert.equal(await sessions.connect(account),first);assert.equal(opened,1);
  assert.equal(sessions.status([account])[0].state,'CONNECTED');await sessions.disconnect(account.binding);
  assert.equal(sessions.status([account])[0].state,'DISCONNECTED');await sessions.close();
});

test('a changed physical binding without a location receipt remains blocked',async()=>{
  const sessions=new AccountSessions({stateRoot:current,bridgeRoot:'unused',lockRoot:'unused'});
  sessions.components={};sessions.binding=async()=>binding(current);
  await assert.rejects(sessions.connect({binding:binding(old)}),{code:'BINDING_CHANGED'});
});

test('explicitly rechecking a moved binding does not invalidate pending tasks or account versions',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-storage-location-'));
  const service=await CatalogService.open({stateRoot:root,storageAliases:aliases,driver:{close:async()=>{}}});
  t.after(async()=>{await service.close();assert(path.basename(root).startsWith('jst-storage-location-'));assert.equal(path.dirname(root),path.resolve(os.tmpdir()));await fs.rm(root,{recursive:true,force:true});});
  const account={id:'sd',warehouse:'山东仓',expectedCompany:'合成公司',expectedCoid:'1',expectedUid:'2',
    binding:binding(old),bindingVersion:3,mappingVerified:true,enabled:true};
  await service.store.write(d=>{d.accounts={'山东仓':account};d.batches={pending:{items:[{account,native:{submissions:1,nativeTaskRef:'ORIGINAL-ID',stage:'ACCEPTED'}}]}};});
  const before=service.store.read(),verified={state:'SUCCEEDED',action:'accounts.probe',warehouse:'山东仓',
    result:{scope:{company:'合成公司',coid:'1',uid:'2'},binding:binding(current)},finishedAt:'fixed'};
  const result=await service.configure({action:'accounts.bind',warehouse:'山东仓',expected_version:3,mapping_verified:true},verified);
  assert.deepEqual(result,account);assert.deepEqual(service.store.read(),before);
  const changed={...verified,result:{...verified.result,binding:binding(path.resolve('really-different'))}};
  await assert.rejects(service.configure({action:'accounts.bind',warehouse:'山东仓',expected_version:3,mapping_verified:true},changed),{code:'PENDING_TASK'});
  assert.equal(canonical(service.store.read().batches),canonical(before.batches));
});
