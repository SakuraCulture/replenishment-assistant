import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {MachineCommands,mappingVersion} from '../src/machine-commands.mjs';
import {MachineRuntime} from '../src/machine-runtime.mjs';
import {AppError} from '../src/common.mjs';
import {BoundBrowser,BoundBrowserPool} from '../src/bound-browser.mjs';

const identity={accountId:'100',merchantCode:'M',displayName:'测试'};
const targets=['W2','W1','W3'].map(code=>({buyerWarehouseCode:code,displayName:code,operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00.000Z'}));
const config=freshConfig(identity,targets);
const deferred=()=>{let resolve;const promise=new Promise(r=>{resolve=r;});return {promise,resolve};};
async function context(t,executeItem,options={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-runtime-')),state=await StateStore.open(root),commands=new MachineCommands(state);
  const runtime=new MachineRuntime({commands,executeItem,...options});t.after(async()=>{await runtime.shutdown();state.close();});
  const reserve=async(id='job-1')=>commands.reserve({protocol_version:1,command_id:id,operation:'replenishment.export',
    account_binding:{account_id:'100',merchant_code:'M'},scope:{warehouse_codes:['W2','W1','W3']},
    basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'reuse'}},config);
  return {root,state,commands,runtime,reserve};
}
test('一店受理未知不拦其他店；无回执不可重发，迟到确切回执只恢复原项',async t=>{
  const visits=[];let receipt=false;
  const c=await context(t,async({item})=>{
    visits.push(item.index);
    if(item.index===0&&!receipt)throw new AppError('SUBMISSION_UNKNOWN','缺少回执');
    return {file:item.warehouse_code+'.xlsx'};
  },{canResumeReview:item=>item.index===0&&receipt});
  await c.reserve();c.runtime.kick();await c.runtime.idle();
  assert.deepEqual(c.commands.get('job-1').items.map(i=>i.state),['NEEDS_REVIEW','PENDING','PENDING']);
  c.runtime.continueReviewed('job-1');c.runtime.kick();await c.runtime.idle();
  const before=c.commands.get('job-1');assert.equal(before.state,'NEEDS_REVIEW');
  assert.deepEqual(before.items.map(i=>i.state),['NEEDS_REVIEW','SUCCEEDED','SUCCEEDED']);
  assert.throws(()=>c.runtime.continueReviewed('job-1'),{code:'COMMAND_TRANSITION'});assert.deepEqual(visits,[0,1,2]);
  const forged=structuredClone(before.items);forged[0].state='PENDING';
  assert.throws(()=>c.commands.advance('job-1',before.sequence,'QUEUED',{kind:'RESUME_WITH_REVIEW_RETAINED',items:forged}),{code:'COMMAND_TRANSITION'});
  const source=c.state.getItems(before.items[0].run_id)[0];
  c.state.putAttempt({id:'late-receipt',itemId:source.id,runId:source.runId,...identity,buyerWarehouseCode:'W2',scopeHash:'fixture',stage:'ACCEPTED',taskId:'exact-original-task'});
  c.state.putItem({...source,attemptId:'late-receipt'});
  receipt=true;c.runtime.continueReviewed('job-1');c.runtime.kick();await c.runtime.idle();
  const after=c.commands.get('job-1');assert.equal(after.state,'SUCCEEDED');assert.deepEqual(visits,[0,1,2,0]);
  assert.deepEqual(after.items.slice(1),before.items.slice(1));assert.equal(after.items[0].run_id,before.items[0].run_id);
});

test('显式单项重跑保留成功文件和原任务号，旧预览与重复点击被拒绝',async t=>{
  let failed=true;const visited=[];
  const c=await context(t,async({item,onProgress})=>{
    visited.push(item.index);onProgress({taskId:'task-'+item.index});
    if(item.index===1&&failed)throw new AppError('EMPTY_UNVERIFIED','fixture');
    return {file:item.warehouse_code+'.xlsx'};
  });
  await c.reserve();c.runtime.kick();await c.runtime.idle();
  const before=c.commands.get('job-1');assert.equal(before.state,'COMPLETED_WITH_ERRORS');
  failed=false;c.runtime.retry('job-1',[1],before.sequence);
  assert.throws(()=>c.runtime.retry('job-1',[1],before.sequence));
  c.runtime.kick();await c.runtime.idle();const after=c.commands.get('job-1');
  assert.equal(after.state,'SUCCEEDED');assert.deepEqual(visited,[0,1,2,1]);
  assert.deepEqual(after.items[0],before.items[0]);assert.deepEqual(after.items[2],before.items[2]);
  assert.equal(after.items[1].run_id,before.items[1].run_id);assert.equal(after.items[1].task_id,before.items[1].task_id);
});

test('平台受理不明不可用重跑绕过核对',async t=>{
  const c=await context(t,async()=>{throw new AppError('SUBMISSION_UNKNOWN','fixture');});
  await c.reserve();c.runtime.kick();await c.runtime.idle();const r=c.commands.get('job-1');
  assert.equal(r.state,'NEEDS_REVIEW');assert.throws(()=>c.runtime.retry(r.id,[0],r.sequence));assert.throws(()=>c.runtime.resume(r.id));
});

test('菜单明确未提交仅自动恢复一次，其余门店各自提交并收取',async t=>{
  const calls=new Map();
  const c=await context(t,async({item,phase})=>{
    const key=phase+item.index,n=(calls.get(key)||0)+1;calls.set(key,n);
    if(item.index===0&&phase==='submit'&&n===1)throw new AppError('EXPORT_NOT_DISPATCHED','未点击');
    return phase==='submit'?{pending:true}:{file:item.warehouse_code};
  },{pipeline:true});
  await c.reserve();c.runtime.kick();await c.runtime.idle();assert.equal(c.commands.get('job-1').state,'SUCCEEDED');
  assert.equal(calls.get('submit0'),2);assert.equal(calls.get('submit1'),1);assert.equal(calls.get('collect0'),1);
});

test('登录全局异常即使伴随受理未知也停止后续门店',async t=>{
  let calls=0;
  const c=await context(t,async()=>{calls++;throw new AppError('WAIT_LOGIN','请登录');},{pipeline:true,needsReview:()=>true});
  await c.reserve();c.runtime.kick();await c.runtime.idle();assert.equal(calls,1);assert.equal(c.commands.get('job-1').state,'NEEDS_REVIEW');
});

test('一店文件校验失败保留失败，不混入结果，其余门店继续收取',async t=>{
  const seen=[];
  const c=await context(t,async({item,phase})=>{
    seen.push([item.index,phase]);if(phase==='submit')return {pending:true};
    if(item.index===0)throw new AppError('ENTITY_MISMATCH','文件门店不符');return {file:item.warehouse_code};
  },{pipeline:true});
  await c.reserve();c.runtime.kick();await c.runtime.idle();
  const done=c.commands.get('job-1');assert.equal(done.state,'COMPLETED_WITH_ERRORS');
  assert.deepEqual(done.items.map(i=>i.state),['FAILED','SUCCEEDED','SUCCEEDED']);assert.equal(done.items[0].result,null);
  assert.deepEqual(seen.filter(i=>i[1]==='collect').map(i=>i[0]),[0,1,2]);
});

test('浏览器任务严格串行，单店普通失败保留原顺序并继续其他店',async t=>{
  const visited=[];let busy=0,maxBusy=0;
  const c=await context(t,async({record,item,onProgress})=>{
    busy++;maxBusy=Math.max(maxBusy,busy);visited.push([record.id,item.warehouse_code]);
    onProgress({stage:'WAIT_TASK',taskId:'stable-task'});await new Promise(r=>setTimeout(r,3));busy--;
    if(item.warehouse_code==='W1')throw new AppError('EMPTY_UNVERIFIED','测试空店');
    return {file:item.warehouse_code+'.xlsx'};
  });
  await c.reserve();await c.reserve('job-2');c.runtime.kick();await c.runtime.idle();
  assert.equal(maxBusy,1);assert.deepEqual(visited,[['job-1','W2'],['job-1','W1'],['job-1','W3'],['job-2','W2'],['job-2','W1'],['job-2','W3']]);
  const done=c.commands.get('job-1');assert.equal(done.state,'COMPLETED_WITH_ERRORS');
  assert.deepEqual(done.result.items.map(i=>[i.warehouse_code,i.state]),[['W2','SUCCEEDED'],['W1','FAILED'],['W3','SUCCEEDED']]);
  assert.equal(done.result.succeeded,2);assert.equal(done.result.failed,1);
});

test('先提交全部门店，慢首店不阻塞后店完成；结果保留所选顺序',async t=>{
  const seen=[],polls=new Map();let submits=0;
  const c=await context(t,async({item,phase,onProgress})=>{
    seen.push(phase+':'+item.warehouse_code);
    if(phase==='submit'){submits++;onProgress({stage:'ACCEPTED',taskId:'task-'+item.warehouse_code});return {pending:true};}
    assert.equal(submits,3);
    const count=(polls.get(item.index)||0)+1;polls.set(item.index,count);
    if(item.index===0&&count===1)return {pending:true};
    return {file:item.warehouse_code+'.xlsx'};
  },{pipeline:true});
  await c.reserve();const r=c.commands.get('job-1');r.config_snapshot.resourceLimits.pollIntervalMs=1;
  c.commands.db.prepare('UPDATE machine_commands SET payload=? WHERE id=?').run(JSON.stringify(r),r.id);
  c.runtime.kick();await c.runtime.idle();
  assert.deepEqual(seen,['submit:W2','submit:W1','submit:W3','collect:W2','collect:W1','collect:W3','collect:W2']);
  const done=c.commands.get('job-1');assert.equal(done.state,'SUCCEEDED');
  assert.deepEqual(done.items.map(i=>i.result.file),['W2.xlsx','W1.xlsx','W3.xlsx']);
  assert.deepEqual(done.items.map(i=>i.task_id),['task-W2','task-W1','task-W3']);
});

test('流水线未知受理项不重发，其余门店独立提交收取，不猜最新任务',async t=>{
  const seen=[];
  const c=await context(t,async({item,phase})=>{seen.push([item.warehouse_code,phase]);if(item.index===1)throw new AppError('SUBMISSION_UNKNOWN','受理不明');return phase==='submit'?{pending:true}:{file:item.warehouse_code};},{pipeline:true});
  await c.reserve();c.runtime.kick();await c.runtime.idle();
  assert.deepEqual(seen,[['W2','submit'],['W1','submit'],['W3','submit'],['W2','collect'],['W3','collect']]);assert.equal(c.commands.get('job-1').state,'NEEDS_REVIEW');
  assert.throws(()=>c.runtime.resume('job-1'),{code:'COMMAND_TRANSITION'});
});
test('首店任务暂未找到不阻塞后店多轮收取，继续仍使用原任务身份',async t=>{
  const seen=[],polls=new Map();let missing=true;
  const c=await context(t,async({item,phase,onProgress})=>{
    seen.push([item.index,phase,item.run_id]);
    if(phase==='submit'){onProgress({taskId:'task-'+item.index});return {pending:true};}
    const n=(polls.get(item.index)||0)+1;polls.set(item.index,n);
    if(item.index===0&&missing)throw new AppError('TASK_NOT_FOUND','not in task history yet');
    if(item.index===1&&n===1)return {pending:true};
    return {file:'file-'+item.index};
  },{pipeline:true});
  const original=(await c.reserve()).record;original.config_snapshot.resourceLimits.pollIntervalMs=1;
  c.commands.db.prepare('UPDATE machine_commands SET payload=? WHERE id=?').run(JSON.stringify(original),original.id);
  c.runtime.kick();await c.runtime.idle();const interrupted=c.commands.get(original.id);
  assert.equal(interrupted.state,'INTERRUPTED');assert.deepEqual(interrupted.items.map(i=>i.state),['INTERRUPTED','SUCCEEDED','SUCCEEDED']);
  assert.equal(polls.get(0),1);assert.equal(polls.get(1),2);
  missing=false;c.runtime.resume(original.id);c.runtime.kick();await c.runtime.idle();
  const completed=c.commands.get(original.id);assert.equal(completed.state,'SUCCEEDED');
  assert.deepEqual(completed.items.slice(1),interrupted.items.slice(1));
  assert.deepEqual(completed.items.map(i=>i.run_id),original.items.map(i=>i.run_id));
  assert.deepEqual(completed.items.map(i=>i.task_id),['task-0','task-1','task-2']);
});

test('流水线等待可立即暂停，继续保持run和task身份',async t=>{
  const reached=deferred(),seen=[];let ready=false;
  const c=await context(t,async({item,phase,onProgress})=>{
    seen.push([item.run_id,phase]);
    if(phase==='submit'){onProgress({stage:'ACCEPTED',taskId:'task-'+item.warehouse_code});return {pending:true};}
    if(item.index===2)reached.resolve();return ready?{file:item.warehouse_code}:{pending:true};
  },{pipeline:true});
  await c.reserve();c.runtime.kick();await reached.promise;c.runtime.pause('job-1');await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'PAUSED');const original=c.commands.get('job-1').items;
  ready=true;c.runtime.resume('job-1');c.runtime.kick();await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'SUCCEEDED');
  assert.deepEqual(c.commands.get('job-1').items.map(i=>[i.run_id,i.task_id]),original.map(i=>[i.run_id,i.task_id]));
});

test('读取中暂停响应立即返回，等待当前动作安全结束，继续只执行剩余店',async t=>{
  const started=deferred(),release=deferred(),seen=[];
  const c=await context(t,async({item,signal})=>{
    seen.push([item.warehouse_code,item.run_id]);
    if(seen.length===1){started.resolve();await release.promise;assert.equal(signal.aborted,true);}
    return {file:item.warehouse_code};
  });
  const before=(await c.reserve()).record;c.runtime.kick();await started.promise;
  assert.equal(c.runtime.pause('job-1').state,'PAUSE_REQUESTED');
  assert.equal(c.commands.get('job-1').items[0].state,'RUNNING');assert.equal(seen.length,1);
  release.resolve();await c.runtime.idle();assert.equal(c.commands.get('job-1').state,'PAUSED');
  c.runtime.resume('job-1');c.runtime.kick();await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'SUCCEEDED');assert.deepEqual(seen,before.items.map(i=>[i.warehouse_code,i.run_id]));
});

test('暂停与未知受理同时出现时保留待核，不允许通过继续按钮再次提交',async t=>{
  const started=deferred(),release=deferred();let calls=0;
  const c=await context(t,async()=>{calls++;started.resolve();await release.promise;throw new AppError('WORKER_RESPONSE_UNKNOWN','响应丢失');},{needsReview:()=>true});
  await c.reserve();c.runtime.kick();await started.promise;c.runtime.pause('job-1');release.resolve();await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'NEEDS_REVIEW');
  assert.throws(()=>c.runtime.resume('job-1'),{code:'COMMAND_TRANSITION'});assert.equal(calls,1);
});

test('登录等全局错误中断，显式继续复用原run；旧取消状态不污染继续',async t=>{
  let fail=true;const seen=[];
  const c=await context(t,async({item,signal})=>{seen.push(item.run_id);assert.equal(signal.aborted,false);if(fail)throw new AppError('WAIT_LOGIN','请登录');return {file:'ok'};});
  const before=(await c.reserve()).record;c.runtime.kick();await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'INTERRUPTED');assert.equal(seen.length,1);
  fail=false;c.runtime.resume('job-1');c.runtime.kick();await c.runtime.idle();
  assert.equal(c.commands.get('job-1').state,'SUCCEEDED');assert.equal(seen[0],seen[1]);
  assert.deepEqual(seen.slice(1),before.items.map(i=>i.run_id));
});

test('关闭停止排队，不强杀当前动作，重开恢复不会自动运行',async t=>{
  const started=deferred(),release=deferred();let calls=0;
  const c=await context(t,async()=>{calls++;started.resolve();await release.promise;return {file:'ok'};});
  await c.reserve();await c.reserve('job-2');c.runtime.kick();await started.promise;
  let closed=false;const closing=c.runtime.shutdown().then(()=>{closed=true;});
  await Promise.resolve();assert.equal(closed,false);assert.equal(c.commands.get('job-2').state,'PAUSED');
  release.resolve();await closing;assert.equal(calls,1);assert.equal(c.commands.get('job-1').state,'PAUSED');
  assert.throws(()=>c.runtime.resume('job-1'),{code:'SHUTTING_DOWN'});
});

test('同一身份配置连续门店复用连接，更换Profile先关闭原连接，更新取消信号',async()=>{
  let created=0,closed=0;
  class FakeBrowser{constructor(args){created++;Object.assign(this,args);}async close(){closed++;}}
  const pool=new BoundBrowserPool(FakeBrowser);
  const binding={actualProfilePath:'C:/profile1',edgeExecutable:'C:/edge.exe',userDataRoot:'C:/edge',profileDirectory:'Default'};
  const first=await pool.acquire({config,binding,signal:new AbortController().signal});
  const signal=new AbortController().signal;
  const second=await pool.acquire({config,binding:{...binding,identityVerifiedAt:'new'},signal});
  assert.equal(first,second);assert.equal(second.signal,signal);assert.equal(created,1);assert.equal(closed,0);
  const third=await pool.acquire({config,binding:{...binding,actualProfilePath:'C:/profile2'},signal});
  assert.notEqual(third,second);assert.equal(created,2);assert.equal(closed,1);
  await pool.close();assert.equal(closed,2);
});

test('连接等待登录后再次继续使用原执行器，避免新子进程被旧runner锁阻断',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-browser-retry-'));
  let created=0,connects=0,closed=0;
  class FakeClient{
    constructor(){created++;}
    async connect(){connects++;if(connects===1)throw new AppError('WAIT_LOGIN','待登录');return {identity,facts:{edgeVersion:'test',actualProfilePath:'C:/profile'}};}
    async stop(){closed++;}
  }
  const binding={enabled:true,userDataRoot:'C:/browser',actualProfilePath:'C:/profile',edgeExecutable:'C:/edge.exe'};
  const browser=new BoundBrowser({state:{root},config,binding,Client:FakeClient,
    discoverEnvironment:async()=>({profiles:[{actualProfilePath:'C:/profile'}],installations:[{executable:'C:/edge.exe'}]})});
  await assert.rejects(()=>browser.ensure(),{code:'WAIT_LOGIN'});await browser.ensure();
  assert.equal(created,1);assert.equal(connects,2);await browser.close();assert.equal(closed,1);
});
