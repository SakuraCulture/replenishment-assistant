import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
const component=process.env.AX_GUARD_FIXTURE_COMPONENT||path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const load=name=>import(pathToFileURL(path.join(component,'src',name)).href);
const [{BrowserClient},{BoundBrowser},{HostPurchaseGuards}]=await Promise.all(['worker-client.mjs','bound-browser.mjs','purchase-guard-ipc.mjs'].map(load));
const workerFile=new URL('./purchase-guard-worker.fixture.mjs',import.meta.url);
const identity={accountId:'QA-ACCOUNT',merchantCode:'QA-MERCHANT'};
const intent={id:'SYNTHETIC-INTENT',file:{sha256:'a'.repeat(64)}};

async function browser(t,{timeout}={}){
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'purchase-guard-ipc-'));await fs.mkdir(path.join(dir,'config'));
  const old=process.env.AX_GUARD_FIXTURE_TIMEOUT;
  if(timeout)process.env.AX_GUARD_FIXTURE_TIMEOUT=String(timeout);else delete process.env.AX_GUARD_FIXTURE_TIMEOUT;
  const binding={enabled:true,userDataRoot:dir,actualProfilePath:path.join(dir,'Default'),edgeExecutable:path.join(dir,'synthetic-edge.exe')};
  class Client extends BrowserClient{constructor(options){super({...options,workerFile});}}
  const b=new BoundBrowser({state:{root:dir,recordReceipt:()=>false,recordPurchaseReceipt:()=>{}},
    config:{tenantBinding:identity,resourceLimits:{connectTimeoutMs:1000,pageTimeoutMs:1000}},binding,
    Client,discoverEnvironment:async()=>({profiles:[{actualProfilePath:binding.actualProfilePath}],installations:[{executable:binding.edgeExecutable}]})});
  const baseline=await b.adapter.preparePurchaseCreate(intent);
  if(old===undefined)delete process.env.AX_GUARD_FIXTURE_TIMEOUT;else process.env.AX_GUARD_FIXTURE_TIMEOUT=old;
  t.after(async()=>{await b.close();await fs.rm(dir,{recursive:true,force:true});});
  return {b,baseline};
}

test('actual fork relay invokes the bound adapter host check before exactly one native release',async t=>{
  const {b,baseline}=await browser(t);let checked=0,receipts=0;
  b.adapter.beforePurchaseImport=async({requestSha256,isActive})=>{checked++;assert.match(requestSha256,/^[a-f0-9]{64}$/);assert.equal(isActive(),true);return ()=>assert.fail('released upload must not be rolled back');};
  b.adapter.onImportReceipt=()=>{receipts++;};
  const result=await b.adapter.uploadPurchaseOnce({intent,baseline});
  assert.equal(result.intentId,intent.id);assert.equal(checked,1);assert.equal(receipts,1);
  assert.deepEqual(await b.client.inspect(),{released:1,aborted:0,pendingGuards:0});
  assert.equal(b.client.pending.size,0);assert.equal(b.client.hostGuards.states.size,0);
  await assert.rejects(()=>b.adapter.uploadPurchaseOnce({intent,baseline}),{code:'IMPORT_NOT_PREPARED'});
  assert.equal((await b.client.inspect()).released,1);
});

test('missing callback refuses the request, then the same unuploaded intent can resume',async t=>{
  const {b,baseline}=await browser(t);
  await assert.rejects(()=>b.adapter.uploadPurchaseOnce({intent,baseline}),{code:'HOST_GUARD_REQUIRED'});
  assert.equal((await b.client.inspect()).released,0);
  b.adapter.beforePurchaseImport=async()=>()=>{};
  await b.adapter.uploadPurchaseOnce({intent,baseline});
  assert.deepEqual(await b.client.inspect(),{released:1,aborted:1,pendingGuards:0});
});

test('payment changed during browser wait is rejected in the parent before release',async t=>{
  const {b,baseline}=await browser(t);let current=7;current=2;
  b.adapter.beforePurchaseImport=()=>{if(current!==7)throw Object.assign(Error('payment version changed'),{code:'PURCHASE_BASIS_CHANGED'});};
  await assert.rejects(()=>b.adapter.uploadPurchaseOnce({intent,baseline}),{code:'PURCHASE_BASIS_CHANGED'});
  assert.deepEqual(await b.client.inspect(),{released:0,aborted:1,pendingGuards:0});
});

test('child cancellation after parent approval awaits the original scoped refusal',async t=>{
  const {b,baseline}=await browser(t);let submitted=0,refused=0;
  b.adapter.beforePurchaseImport=()=>{submitted++;return ()=>{refused++;};};
  await assert.rejects(()=>b.adapter.uploadPurchaseOnce({intent,baseline,cancelAfterApproval:true}),{code:'SUBMISSION_UNKNOWN'});
  assert.equal(submitted,1);assert.equal(refused,1);
  assert.deepEqual(await b.client.inspect(),{released:0,aborted:1,pendingGuards:0});
});

test('timed-out child approval cannot later release a request or leak guard timers',async t=>{
  const {b,baseline}=await browser(t,{timeout:30});let refused=0;
  b.adapter.beforePurchaseImport=async()=>{await new Promise(r=>setTimeout(r,100));return ()=>{refused++;};};
  await assert.rejects(()=>b.adapter.uploadPurchaseOnce({intent,baseline}),{code:'HOST_GUARD_TIMEOUT'});
  assert.equal(refused,1);assert.deepEqual(await b.client.inspect(),{released:0,aborted:1,pendingGuards:0});
});

test('foreign, duplicate and late guard messages do not validate another upload',async()=>{
  const sent=[],pending=new Map();let calls=0;
  pending.set('op',{method:'uploadPurchaseOnce',intentId:'A',beforePurchaseImport:()=>{calls++;}});
  const channel=new HostPurchaseGuards(m=>sent.push(m),pending);
  const message={event:'purchase_guard_check',operationId:'op',guardId:'nonce',intentId:'OTHER',requestSha256:'a'.repeat(64)};
  await channel.receive(message);assert.equal(calls,0);assert.equal(sent.at(-1).error.code,'HOST_GUARD_SCOPE');
  message.intentId='A';await channel.receive(message);assert.equal(calls,1);assert.equal(sent.at(-1).ok,true);
  await channel.receive(message);assert.equal(calls,1);assert.equal(sent.at(-1).error.code,'HOST_GUARD_SCOPE');
  channel.finish('op');pending.delete('op');await channel.receive(message);assert.equal(calls,1);
});

test('failed durable refusal stays failed without replay or false acknowledgement',async()=>{
  const sent=[],pending=new Map();let rollbacks=0;
  pending.set('op',{method:'uploadPurchaseOnce',intentId:'A',beforePurchaseImport:()=>()=>{rollbacks++;throw Object.assign(Error('local write failed'),{code:'SQLITE_BUSY'});}});
  const channel=new HostPurchaseGuards(m=>sent.push(m),pending);
  const message={event:'purchase_guard_check',operationId:'op',guardId:'nonce',intentId:'A',requestSha256:'a'.repeat(64)};
  await channel.receive(message);assert.equal(sent.at(-1).ok,true);
  for(let i=0;i<2;i++){
    await channel.receive({...message,event:'purchase_guard_abort'});
    assert.equal(sent.at(-1).error.code,'SQLITE_BUSY');assert.equal(sent.at(-1).ok,undefined);
  }
  assert.equal(rollbacks,1);assert.equal(channel.states.get('op').refused,false);
});
