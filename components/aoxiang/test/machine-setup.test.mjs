import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {MachineSetup,readConfiguration,recoverConfiguration,saveConfiguration,configurationHash} from '../src/machine-setup.mjs';
import {freshConfig} from '../src/config.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';
import {writeJson,exists,AppError} from '../src/common.mjs';
import {BoundBrowserPool} from '../src/bound-browser.mjs';

const identity={accountId:'001',merchantCode:'M',displayName:'测试商家'};
const target=code=>({buyerWarehouseCode:code,displayName:code,operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00Z'});
const params=root=>({candidate:{edge_executable:'C:/Edge/msedge.exe',actual_profile_path:'C:/EdgeData/Profile1'},output_root:path.join(root,'output'),targets:[{code:'W2',name:'门店2'},{code:'W1',name:'门店1'}]});
test('startup identity failure hands off same transport without claiming verified identity',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};t.after(()=>c.setup.browsers.close());
  const job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const before=await readConfiguration(c.root),client=c.setup.browsers.browser.client,original=client.recheckLogin.bind(client);
  client.recheckLogin=async()=>{throw new AppError('IDENTITY_CHECK_FAILED','page timed out');};
  await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(c.setup.job.state,'FAILED');assert.equal(client.closed,undefined);
  await c.setup.handoffLogin();assert.equal(c.setup.client,null);assert.equal(c.setup.browsers.browser.client,client);assert.equal(c.setup.browsers.browser.connected,null);
  client.recheckLogin=original;c.calls.length=0;
  await c.setup.verifySaved([]);await c.setup.promise;
  assert.deepEqual(c.calls,['recheck']);assert.equal(c.setup.job.state,'VERIFIED');assert.deepEqual(await readConfiguration(c.root),before);
});
test('startup checks saved login without querying stores, changing mappings or committing configuration',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};
  t.after(()=>c.setup.browsers.close());
  let job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const before=await readConfiguration(c.root),client=c.setup.browsers.browser.client;c.calls.length=0;
  c.setup.checkOutputRoot=async()=>{throw Error('Login check must not inspect or create export output directories');};
  validateRequest({protocol_version:1,request_id:'startup',action:'setup.check_login'});
  assert.throws(()=>validateRequest({protocol_version:1,request_id:'bad',action:'setup.check_login',codes:['W1']}));
  assert.throws(()=>validateRequest({protocol_version:1,request_id:'bad',action:'setup.verify_saved',codes:[]}));
  job=await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(c.setup.status(job.id).state,'VERIFIED');assert.deepEqual(c.calls,['recheck']);
  assert.deepEqual(await readConfiguration(c.root),before);assert.equal(c.setup.browsers.browser.client,client);
});
test('startup rejects missing configuration and changed login without saving a replacement identity',async t=>{
  const c=await context(t);await assert.rejects(c.setup.verifySaved([]),{code:'NOT_CONFIGURED'});
  c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};t.after(()=>c.setup.browsers.close());
  const job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const before=await readConfiguration(c.root);
  c.setup.browsers.browser.client.recheckLogin=async()=>({identity:{...identity,merchantCode:'different'}});
  await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(c.setup.job.error.code,'WRONG_IDENTITY');assert.deepEqual(await readConfiguration(c.root),before);
});
test('login recheck rebuilds a disconnected browser worker instead of borrowing a stale successful connection',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};t.after(()=>c.setup.browsers.close());
  const job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const old=c.setup.browsers.browser.client;old.connectionState='DISCONNECTED';c.calls.length=0;
  await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(old.closed,true);assert.equal(c.setup.job.state,'VERIFIED');assert.deepEqual(c.calls,['stop','new','connect']);
});
test('startup keeps the login page for the user and retries the same client without changing bindings',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};t.after(()=>c.setup.browsers.close());
  const job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const before=await readConfiguration(c.root),client=c.setup.browsers.browser.client;
  const original=client.recheckLogin.bind(client);let login=false;
  client.recheckLogin=async()=>{if(!login)throw new AppError('WAIT_LOGIN','please login');return original();};
  await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(c.setup.job.error.code,'WAIT_LOGIN');assert.equal(client.closed,undefined);assert.equal(c.setup.busy,false);
  assert.equal(c.setup.client,client);assert.deepEqual(await readConfiguration(c.root),before);
  login=true;c.calls.length=0;await c.setup.verifySaved([]);await c.setup.promise;
  assert.equal(c.setup.job.state,'VERIFIED');assert.deepEqual(c.calls,['recheck']);assert.equal(c.setup.browsers.browser.client,client);
  assert.deepEqual(await readConfiguration(c.root),before);
});
test('scheduled preflight reads saved binding and cancels preview without modifying configuration',async t=>{
  const c=await context(t);let job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const before=await readConfiguration(c.root);
  validateRequest({protocol_version:1,request_id:'scheduled',action:'setup.verify_saved',codes:['W1']});
  job=await c.setup.verifySaved(['W1']);await c.setup.promise;
  assert.equal(c.setup.status(job.id).state,'VERIFIED');assert.deepEqual(c.setup.status(job.id).targets.map(x=>x.code),['W1']);
  await c.setup.cancel(job.id);assert.deepEqual(await readConfiguration(c.root),before);
  await assert.rejects(c.setup.verifySaved(['missing']),/预案门店尚未核验/);
});
async function context(t,behavior={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-setup-')),calls=[];
  class Client{
    constructor(){calls.push('new');}
    async connect(){calls.push('connect');return {identity:behavior.identity||identity,facts:{edgeVersion:'test',actualProfilePath:'C:/EdgeData/Profile1'}};}
    async querySingle(code,name){calls.push(code);if(behavior.query)return behavior.query(code,name);return {identity,warehouseCode:code,name,total:1,observedAt:'2026-11-01T00:00:00Z'};}
    async stop(){calls.push('stop');this.closed=true;}
    async recheckLogin(){calls.push('recheck');return {identity,facts:{edgeVersion:'test',actualProfilePath:'C:/EdgeData/Profile1'}};}
  }
  const setup=new MachineSetup({root,Client,stepTimeoutMs:behavior.timeout,discoverEnvironment:async()=>({profiles:[{actualProfilePath:'C:/EdgeData/Profile1',userDataRoot:'C:/EdgeData',profileDirectory:'Profile1'}],installations:[{executable:'C:/Edge/msedge.exe'}]}),checkOutputRoot:async root=>({root})});
  t.after(()=>setup.close());return {root,setup,calls};
}
test('shared setup keeps verified worker until explicit save, then reuses it for subsequent setup',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};
  t.after(()=>c.setup.browsers.close());
  const first=c.setup.begin(params(c.root));await c.setup.promise;
  const client=c.setup.client;assert.deepEqual(c.calls,['new','connect','W2','W1']);
  await c.setup.commit(first.id);assert.equal(c.setup.client,null);assert.equal(c.setup.browsers.browser.client,client);
  await c.setup.close();assert.equal(client.closed,undefined);
  const next=c.setup.begin(params(c.root));await c.setup.promise;assert.equal(c.setup.client,client);
  assert.deepEqual(c.calls,['new','connect','W2','W1','recheck','W2','W1']);
  await c.setup.cancel(next.id);assert.equal(client.closed,undefined);assert.equal(c.setup.busy,false);
  const pair=await readConfiguration(c.root);assert.equal(pair.config.targets.length,2);
});
test('uncommitted new setup releases connection on cancel; failed borrowed setup stops unsafe worker',async t=>{
  const c=await context(t);c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};
  t.after(()=>c.setup.browsers.close());
  let job=c.setup.begin(params(c.root));await c.setup.promise;const pending=c.setup.client;
  await c.setup.cancel(job.id);assert.equal(pending.closed,true);assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
  job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.commit(job.id);
  const borrowed=c.setup.browsers.browser.client;borrowed.querySingle=async()=>{throw new AppError('WRONG_IDENTITY','changed');};
  c.setup.begin(params(c.root));await c.setup.promise;assert.equal(c.setup.job.state,'FAILED');assert.equal(borrowed.closed,true);
  assert.equal((await readConfiguration(c.root)).config.tenantBinding.merchantCode,identity.merchantCode);
});
test('shared setup recovers one query identity timeout on same worker without starting another connection',async t=>{
  let attempts=0;
  const c=await context(t,{query:async(code,name)=>{
    if(attempts++===0)throw new AppError('IDENTITY_RESPONSE_TIMEOUT','fixture');
    return {identity,warehouseCode:code,name,total:1,observedAt:'2026-11-01T00:00:00Z'};
  }});
  c.setup.browsers=new BoundBrowserPool();c.setup.state={root:c.root,recordReceipt:()=>true};t.after(()=>c.setup.browsers.close());
  const job=c.setup.begin(params(c.root));await c.setup.promise;
  assert.equal(c.setup.job.state,'READY');assert.deepEqual(c.calls,['new','connect','W2','recheck','W2','W1']);
  assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});await c.setup.cancel(job.id);
});
test('配置动作严格校验编码、列表与已发现浏览器路径，不允许未知字段',async t=>{
  const c=await context(t),good={protocol_version:1,request_id:'r1',action:'setup.start',...params(c.root)};validateRequest(good);
  for(const bad of [{...good,targets:[{code:1,name:'n'}]},{...good,targets:[{code:'W',name:'n'},{code:'W',name:'n'}]},{...good,candidate:{...good.candidate,password:'x'}},{...good,targets:[]}])assert.throws(()=>validateRequest(bad),{code:'REQUEST_SCHEMA'});
  c.setup.begin({...params(c.root),candidate:{edge_executable:'C:/arbitrary.exe',actual_profile_path:'C:/EdgeData/Profile1'}});await c.setup.promise;
  assert.equal(c.setup.job.error.code,'MACHINE_REBIND_REQUIRED');assert.deepEqual(c.calls,[]);
});
test('核验仅查询、保留选店顺序，明确保存前配置不变；保存后可读取复用',async t=>{
  const c=await context(t),job=c.setup.begin(params(c.root));await c.setup.promise;
  assert.equal(c.setup.status(job.id).state,'READY');assert.deepEqual(c.calls,['new','connect','W2','W1','stop']);
  assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
  const saved=await c.setup.commit(job.id);assert.equal(saved.state,'SAVED');
  const pair=await readConfiguration(c.root);assert.deepEqual(pair.config.targets.map(t=>t.buyerWarehouseCode),['W2','W1']);assert.equal(pair.binding.outputRoot,params(c.root).output_root);
  assert.equal((await fs.readdir(path.join(c.root,'recovery'))).filter(n=>n.startsWith('config-before-')).length,1);
  assert.equal(await exists(path.join(c.root,'recovery/configuration.pending.json')),false);
});
test('部分门店编码不符只保存已核验项，不按名字猜编码',async t=>{
  const c=await context(t,{query:async(code,name)=>({identity,warehouseCode:code==='W1'?'wrong':code,name,total:0,observedAt:'2026-11-01T00:00:00Z'})});
  const job=c.setup.begin(params(c.root));await c.setup.promise;const status=c.setup.status(job.id);
  assert.equal(status.verified,1);assert.equal(status.failed,1);assert.equal(status.targets[1].error.code,'TARGET_CODE_MISMATCH');
  await c.setup.commit(job.id);assert.deepEqual((await readConfiguration(c.root)).config.targets.map(t=>t.buyerWarehouseCode),['W2']);
});
test('换商家阻断查询和保存，旧参数保持',async t=>{
  const c=await context(t,{identity:{...identity,merchantCode:'other'}}),old=freshConfig(identity,[target('old')]);
  await writeJson(path.join(c.root,'config/portable.config.json'),old);c.setup.begin(params(c.root));await c.setup.promise;
  assert.equal(c.setup.job.state,'FAILED');assert.equal(c.setup.job.error.code,'WRONG_IDENTITY');assert.deepEqual(c.calls,['new','connect','stop']);
  assert.deepEqual((await readConfiguration(c.root)).config,old);
});

test('身份超时和未分类浏览器异常停止整次核验，不逐店重复等待',async t=>{
  for(const error of [new AppError('IDENTITY_RESPONSE_TIMEOUT','身份超时'),new Error('页面异常')]){
    const c=await context(t,{query:async()=>{throw error;}});
    c.setup.begin(params(c.root));await c.setup.promise;
    assert.equal(c.setup.job.state,'FAILED');assert.deepEqual(c.calls,['new','connect','W2','stop']);
    assert.equal(c.setup.job.targets[1].state,'PENDING');assert.equal(c.setup.busy,false);
    assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
  }
});
test('停止只读核验立即释放执行器，不再等挂起查询或核验下一店，不保存半成品',async t=>{
  let release,started;const gate=new Promise(r=>{release=r;}),begun=new Promise(r=>{started=r;});
  const c=await context(t,{query:async(code,name)=>{started();await gate;return {identity,warehouseCode:code,name,total:1,observedAt:'2026-11-01T00:00:00Z'};}});
  const job=c.setup.begin(params(c.root));await begun;await c.setup.cancel(job.id);let closed=false;const ending=c.setup.close().then(()=>{closed=true;});
  await ending;assert.equal(closed,true);assert.equal(c.setup.job.state,'CANCELLED');assert.deepEqual(c.calls,['new','connect','W2','stop']);assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
  release();await Promise.resolve();assert.equal(c.setup.job.state,'CANCELLED');
});

test('挂起查询到达步骤上限后失败退出、释放连接，重试不被旧结果污染',async t=>{
  let release,hung=true;
  const c=await context(t,{timeout:40,query:async(code,name)=>{
    if(hung)return new Promise(r=>{release=r;});
    return {identity,warehouseCode:code,name,total:1,observedAt:new Date().toISOString()};
  }});
  const first=c.setup.begin(params(c.root));await c.setup.promise;
  assert.equal(c.setup.job.state,'FAILED');assert.equal(c.setup.job.error.code,'SETUP_STEP_TIMEOUT');assert.equal(c.setup.busy,false);
  assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
  hung=false;c.setup.begin(params(c.root));await c.setup.promise;assert.equal(c.setup.job.state,'READY');
  release({identity,warehouseCode:'WRONG',total:999});await Promise.resolve();
  assert.notEqual(c.setup.job.id,first.id);assert.equal(c.setup.job.state,'READY');assert.equal(c.setup.job.verified,2);
});
test('核验后被其他操作更新时拒绝覆盖；重试不受旧取消状态影响',async t=>{
  const c=await context(t);let job=c.setup.begin(params(c.root));await c.setup.promise;await c.setup.cancel(job.id);
  job=c.setup.begin(params(c.root));await c.setup.promise;assert.equal(c.setup.job.state,'READY');
  const changed=freshConfig(identity,[target('external')]);await writeJson(path.join(c.root,'config/portable.config.json'),changed);
  await assert.rejects(()=>c.setup.commit(job.id),{code:'STALE_CONFIGURATION'});assert.deepEqual((await readConfiguration(c.root)).config,changed);
});
test('配置双文件中断恢复到已确认组合；外部修改不静默覆盖',async t=>{
  const c=await context(t);c.setup.begin(params(c.root));await c.setup.promise;const desired=c.setup.preview.desired;
  await writeJson(path.join(c.root,'recovery/configuration.pending.json'),{schema:1,previous:{config:null,binding:null},desired});
  await writeJson(path.join(c.root,'config/portable.config.json'),desired.config);
  assert.equal(await recoverConfiguration(c.root),true);assert.deepEqual(await readConfiguration(c.root),desired);assert.equal(await recoverConfiguration(c.root),false);
  await writeJson(path.join(c.root,'recovery/configuration.pending.json'),{schema:1,previous:{config:null,binding:null},desired});
  await writeJson(path.join(c.root,'config/portable.config.json'),freshConfig({...identity,merchantCode:'changed'},[target('W')]));
  await assert.rejects(()=>recoverConfiguration(c.root),{code:'CONFIG_RECOVERY_CONFLICT'});
});
