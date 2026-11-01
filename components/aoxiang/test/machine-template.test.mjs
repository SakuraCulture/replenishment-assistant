import test from 'node:test';
import assert from 'node:assert/strict';
import {MachineTemplate} from '../src/machine-template.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';
import {AppError} from '../src/common.mjs';
import {downloadNativePurchaseTemplate} from '../src/purchase-template-download.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';

test('template control validates exact request ids and rejects extra side-effect options',()=>{
  for(const action of ['template.start','template.status','template.cancel']){
    const request={protocol_version:1,request_id:'r1',action,...(action==='template.start'?{}:{template_id:'t1'})};
    assert.deepEqual(validateRequest(request),request);
    assert.throws(()=>validateRequest({...request,upload:true}),{code:'REQUEST_SCHEMA'});
  }
  assert.throws(()=>validateRequest({protocol_version:1,request_id:'r1',action:'template.status',template_id:1}),{code:'REQUEST_SCHEMA'});
});

test('template fetch is background work with progress; repeat cannot overlap and later refresh fetches again',async()=>{
  let resolve,calls=0;
  const hold=new Promise(r=>resolve=r);
  const service=new MachineTemplate({execute:async(signal,progress)=>{calls++;progress('WAIT_TEMPLATE');await hold;return {status:'SUPPORTED',sha256:'hash',importSubmissions:0};}});
  const first=service.start();await Promise.resolve();
  assert.equal(service.status(first.id).stage,'WAIT_TEMPLATE');
  assert.throws(()=>service.start(),{code:'TEMPLATE_BUSY'});
  resolve();await service.pending;
  assert.equal(service.status(first.id).state,'READY');
  const next=service.start();await service.pending;
  assert.notEqual(first.id,next.id);assert.equal(calls,2);
  assert.equal(service.status(next.id).receipt.importSubmissions,0);
});

test('cancel waits for current read, returns no ready receipt, and next fetch has fresh cancellation state',async()=>{
  let release;
  const hold=new Promise(r=>release=r);
  const service=new MachineTemplate({execute:async signal=>{await hold;return {status:'SUPPORTED'};}});
  const first=service.start();await Promise.resolve();service.cancel(first.id);
  assert.equal(service.status(first.id).cancel_requested,true);
  let closed=false;const closing=service.close().then(()=>closed=true);await Promise.resolve();assert.equal(closed,false);
  release();await closing;assert.equal(service.status(first.id).state,'CANCELLED');assert.equal(service.status(first.id).receipt,null);
  service.execute=async signal=>{assert.equal(signal.aborted,false);return {status:'SUPPORTED'};};
  const next=service.start();await service.pending;assert.equal(service.status(next.id).state,'READY');
});

test('template fetch failure remains failed without cached fallback; status cannot impersonate another request',async()=>{
  const service=new MachineTemplate({execute:async()=>{throw new AppError('PURCHASE_TEMPLATE_CHANGED','字段已变化');}});
  const job=service.start();await service.pending;
  assert.equal(service.status(job.id).state,'FAILED');assert.equal(service.status(job.id).receipt,null);
  assert.equal(service.status(job.id).error.code,'PURCHASE_TEMPLATE_CHANGED');
  assert.throws(()=>service.status('other'),{code:'TEMPLATE_JOB_NOT_FOUND'});
});

test('blocked write during awaited input inspection or final cleanup cannot produce a successful template receipt',async t=>{
  const directory=await fs.mkdtemp(path.join(os.tmpdir(),'ax-template-race-'));
  t.after(()=>fs.rm(directory,{recursive:true,force:true}));
  const body=await fs.readFile(new URL('./fixtures/purchase-import-template.xlsx',import.meta.url));
  const identity={accountId:'A',merchantCode:'M'},url='https://sc-gei-tc-supply-chain-portal.ele.me/gei/import/template/SAAS-PURCHASE-ORDER-IMPORT-V2';
  for(const phase of ['inspection','cleanup']){
    let guard,downloaded=false,blocked=0;
    const attempt=async()=>guard({request:()=>({url:()=>url.replace('/template/','/task/'),method:()=> 'PUT'}),abort:async()=>{blocked++;},fallback:async()=>assert.fail('write must never reach the network')});
    const button=name=>({name,filter(){return this;}});
    const modal={filter(){return this;},waitFor:async()=>{},count:async()=>1,innerText:async()=> '每次添加的商品数量不能超过 12000个',getByRole:(role,{name})=>button(name)};
    const page={
      locator:selector=>selector==='input[type=file]'?{evaluateAll:async()=>{if(downloaded&&phase==='inspection')await attempt();return false;}}:modal,
      route:async(pattern,fn)=>{guard=fn;},unroute:async()=>{},getByRole:(role,{name})=>button(name),
      waitForRequest:async()=>({url:()=>url}),
      context:()=>({request:{get:async()=>({status:()=>200,headers:()=>({}),body:async()=>{downloaded=true;return body;},dispose:async()=>{}})}})
    };
    const ui={page,identity,timeoutMs:1000,open:async()=>{},click:async selected=>{if(selected.name==='取消'&&phase==='cleanup')await attempt();}};
    await assert.rejects(()=>downloadNativePurchaseTemplate(ui,{directory,expectedIdentity:identity}),{code:'PURCHASE_TEMPLATE_SIDE_EFFECT'});
    assert.ok(blocked>=1);
  }
});
