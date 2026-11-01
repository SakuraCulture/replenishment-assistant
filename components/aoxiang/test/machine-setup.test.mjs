import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {MachineSetup,readConfiguration,recoverConfiguration,saveConfiguration,configurationHash} from '../src/machine-setup.mjs';
import {freshConfig} from '../src/config.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';
import {writeJson,exists,AppError} from '../src/common.mjs';

const identity={accountId:'001',merchantCode:'M',displayName:'测试商家'};
const target=code=>({buyerWarehouseCode:code,displayName:code,operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00Z'});
const params=root=>({candidate:{edge_executable:'C:/Edge/msedge.exe',actual_profile_path:'C:/EdgeData/Profile1'},output_root:path.join(root,'output'),targets:[{code:'W2',name:'门店2'},{code:'W1',name:'门店1'}]});
async function context(t,behavior={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-setup-')),calls=[];
  class Client{
    constructor(){calls.push('new');}
    async connect(){calls.push('connect');return {identity:behavior.identity||identity,facts:{edgeVersion:'test',actualProfilePath:'C:/EdgeData/Profile1'}};}
    async querySingle(code,name){calls.push(code);if(behavior.query)return behavior.query(code,name);return {identity,warehouseCode:code,name,total:1,observedAt:'2026-11-01T00:00:00Z'};}
    async stop(){calls.push('stop');}
  }
  const setup=new MachineSetup({root,Client,discoverEnvironment:async()=>({profiles:[{actualProfilePath:'C:/EdgeData/Profile1',userDataRoot:'C:/EdgeData',profileDirectory:'Profile1'}],installations:[{executable:'C:/Edge/msedge.exe'}]}),checkOutputRoot:async root=>({root})});
  t.after(()=>setup.close());return {root,setup,calls};
}
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
test('停止和关闭等待当前查询结束，不再核验下一店，不保存半成品',async t=>{
  let release,started;const gate=new Promise(r=>{release=r;}),begun=new Promise(r=>{started=r;});
  const c=await context(t,{query:async(code,name)=>{started();await gate;return {identity,warehouseCode:code,name,total:1,observedAt:'2026-11-01T00:00:00Z'};}});
  const job=c.setup.begin(params(c.root));await begun;await c.setup.cancel(job.id);let closed=false;const ending=c.setup.close().then(()=>{closed=true;});
  await Promise.resolve();assert.equal(closed,false);assert.equal(c.calls.includes('stop'),false);
  release();await ending;assert.equal(c.setup.job.state,'CANCELLED');assert.deepEqual(c.calls,['new','connect','W2','stop']);assert.deepEqual(await readConfiguration(c.root),{config:null,binding:null});
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
