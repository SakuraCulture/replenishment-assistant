import test from 'node:test';
import assert from 'node:assert/strict';
import {ModuleSession} from '../src/module-session.mjs';
import {releaseSession} from '../src/machine-session.mjs';
import {BoundBrowserPool,invokeBoundRead} from '../src/bound-browser.mjs';
import {AoxiangUI} from '../src/aoxiang-ui.mjs';
const identity={accountId:'a',merchantCode:'m',displayName:'fixture'};
const error=code=>Object.assign(new Error(code),{code});
test('page/navigation failure is not mislabeled as an expired login',async()=>{
  const context={activate:async()=>{},identityAfter:async()=>{throw new Error('navigation timeout');}};
  await assert.rejects(()=>AoxiangUI.prototype.open.call(context,identity),{code:'IDENTITY_CHECK_FAILED'});
  context.identityAfter=async()=>{throw error('WAIT_LOGIN');};
  await assert.rejects(()=>AoxiangUI.prototype.open.call(context,identity),{code:'WAIT_LOGIN'});
  context.identityAfter=async()=>{throw error('WRONG_IDENTITY');};
  await assert.rejects(()=>AoxiangUI.prototype.open.call(context,identity),{code:'WRONG_IDENTITY'});
});
test('identity body is captured before navigation completes; expired or invalid body never uses cached identity',async()=>{
  let valid=true,read=false;
  const response={text:async()=>{if(!valid)throw Error('response discarded');read=true;return JSON.stringify({status:true,success:true,data:{account:{accountId:'a'},user:{accountId:'a',userId:'u',merchantCode:'m'}}});}};
  const context={timeoutMs:100,isBusinessResponse:()=>true,page:{waitForRequest:()=>Promise.resolve({response:async()=>response})}};
  const result=await AoxiangUI.prototype.identityAfter.call(context,()=>new Promise(resolve=>setTimeout(()=>{valid=false;resolve();},10)));
  assert.equal(result.accountId,'a');assert.equal(read,true);
  await assert.rejects(()=>AoxiangUI.prototype.identityAfter.call(context,async()=>{}),/response discarded/);
});
test('read-only query identity timeout can recover once; writes and uncertain response never replay',async()=>{
  const events=[];let failures=1;
  const client={querySingle:async(...args)=>{events.push(args);if(failures-->0)throw error('IDENTITY_RESPONSE_TIMEOUT');return 12;},
    recheckLogin:async who=>{assert.deepEqual(who,identity);events.push('verify');return {identity};}};
  const browser={client,config:{tenantBinding:identity},check:()=>{},onProgress:()=>{}};
  assert.equal(await invokeBoundRead(browser,'querySingle',['W','store']),12);
  assert.deepEqual(events,[['W','store'],'verify',['W','store']]);
  failures=9;await assert.rejects(()=>invokeBoundRead(browser,'querySingle',['W','store']),{code:'IDENTITY_RESPONSE_TIMEOUT'});
  let calls=0;client.uploadPurchaseOnce=async()=>{calls++;throw error('IDENTITY_RESPONSE_TIMEOUT');};
  await assert.rejects(()=>invokeBoundRead(browser,'uploadPurchaseOnce',[{}]));assert.equal(calls,1);
  calls=0;client.querySingle=async()=>{calls++;throw error('WORKER_RESPONSE_UNKNOWN');};
  await assert.rejects(()=>invokeBoundRead(browser,'querySingle',[]));assert.equal(calls,1);
});
function fixture(){
  const events=[];let fail=[],who=identity;
  class UI{
    constructor(page,opts){this.page=page;this.opts=opts;events.push('new');}
    async open(expected){events.push(['open',expected]);if(fail.length)throw error(fail.shift());return who;}
    async dispose(){events.push('dispose');}
  }
  const page={isClosed:()=>false},session=new ModuleSession({page,adapters:{D13:UI,purchase:UI,purchase_create:UI},onPhase:x=>events.push(x)});
  return {session,page,events,setFailures:x=>{fail=x;},setIdentity:x=>{who=x;}};
}
test('one page D13 -> purchase -> purchase_create -> D13; listeners disposed, fixed identity',async()=>{
  const f=fixture();let first;
  for(const source of ['D13','purchase','purchase_create','D13']){
    const ui=await f.session.open(source,identity);first??=ui;
    assert.equal(ui.page,f.page);assert.equal(f.session.ready,true);assert.deepEqual(f.session.identity,identity);
    assert.equal(await f.session.open(source),ui);
  }
  assert.equal(f.events.filter(x=>x==='new').length,4);assert.equal(f.events.filter(x=>x==='dispose').length,3);
  await f.session.dispose();assert.equal(f.session.ready,false);
});
test('failed module switch never marked ready, next attempt reopens with original identity',async()=>{
  const f=fixture();await f.session.open('D13',identity);f.setFailures(['WRONG_IDENTITY']);
  await assert.rejects(()=>f.session.open('purchase'),{code:'WRONG_IDENTITY'});
  assert.equal(f.session.ready,false);assert.equal(f.session.source,null);
  const count=f.events.filter(x=>Array.isArray(x)).length;
  await f.session.open('purchase');assert.equal(f.events.filter(x=>Array.isArray(x)).length,count+1);
  f.setIdentity({...identity,merchantCode:'other'});
  await assert.rejects(()=>f.session.open('D13'),{code:'WRONG_IDENTITY'});assert.deepEqual(f.session.identity,identity);
});
test('identity read timeout retries once on same page; second timeout/unknown acceptance not retried',async()=>{
  const f=fixture();await f.session.open('D13',identity);
  f.setFailures(['IDENTITY_RESPONSE_TIMEOUT']);await f.session.open('purchase');assert.equal(f.session.ready,true);
  f.setFailures(['IDENTITY_RESPONSE_TIMEOUT','IDENTITY_RESPONSE_TIMEOUT']);
  await assert.rejects(()=>f.session.open('D13'),{code:'IDENTITY_RESPONSE_TIMEOUT'});assert.equal(f.session.ready,false);
  const count=f.events.filter(x=>Array.isArray(x)).length;f.setFailures(['SUBMISSION_UNKNOWN']);
  await assert.rejects(()=>f.session.open('purchase'),{code:'SUBMISSION_UNKNOWN'});
  assert.equal(f.events.filter(x=>Array.isArray(x)).length,count+1);
});
test('failed initial identity remains pinned across retries, never adopts another merchant',async()=>{
  const f=fixture();f.setFailures(['WRONG_IDENTITY']);await assert.rejects(()=>f.session.open('D13',identity));
  f.setIdentity({...identity,accountId:'other'});await assert.rejects(()=>f.session.open('purchase'),{code:'WRONG_IDENTITY'});
  assert.deepEqual(f.session.identity,identity);
});
test('handoff pauses all actors, waits for actual settlement, no browser close or resume',async()=>{
  const calls=[],runtime={active:{id:'c'},pause:id=>calls.push(id)},commands={list:()=>[{id:'c',state:'RUNNING'},{id:'q',state:'QUEUED'},{id:'done',state:'COMPLETED'}]};
  const controller=id=>({busy:true,job:{id},pause:x=>calls.push(x)});
  const purchase=controller('p'),receiptSync=controller('r'),bindingCheck=controller('b');
  const setup={busy:true,job:{id:'s'},cancel:async x=>calls.push(x)},template={busy:true,job:{id:'t'},cancel:x=>calls.push(x)};
  const actors={runtime,commands,purchase,receiptSync,bindingCheck,setup,template};
  assert.deepEqual(await releaseSession(actors),{idle:false});assert.deepEqual(calls,['p','r','b','c','q','t','s']);
  runtime.active=null;commands.list=()=>[];for(const c of [purchase,receiptSync,bindingCheck,setup,template])c.busy=false;
  assert.deepEqual(await releaseSession(actors),{idle:true});assert.equal(calls.length,7);
});
test('setup adopts same worker; callback routing updated once; different profile closes old worker',async()=>{
  const config={tenantBinding:identity,resourceLimits:{pageTimeoutMs:30,connectTimeoutMs:60}},binding={actualProfilePath:'C:/p',edgeExecutable:'C:/edge',userDataRoot:'C:/u',profileDirectory:'p'};
  let stops=0;const client={child:{pid:123},stop:async()=>{stops++;client.closed=true;}};
  const seen=[],args={config,binding,state:{recordReceipt:r=>{seen.push(r);return true;}},onProgress:()=>{}};
  const pool=new BoundBrowserPool();pool.adopt(args,client,{identity});
  const found=await pool.forSetup({actual_profile_path:'C:/p',edge_executable:'C:/edge'},{config,binding},()=>{});
  assert.equal(found,client);assert.equal(stops,0);
  pool.adopt(args,found,{identity});(await pool.acquire(args)).adapter.onImportReceipt=r=>seen.push(r);
  client.onReceipt('r');client.onImportReceipt('i');assert.deepEqual(seen,['r','i']);
  assert.deepEqual(pool.status(),{worker_pid:123,connected:true});
  assert.equal(await pool.forSetup({actual_profile_path:'C:/other',edge_executable:'C:/edge'},{config,binding},()=>{}),null);assert.equal(stops,1);
});
