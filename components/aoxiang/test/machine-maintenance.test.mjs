import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {CollectionMaintenance} from '../src/machine-maintenance.mjs';
import {MachineRuntime} from '../src/machine-runtime.mjs';
import {Coordinator} from '../src/coordinator.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';
import {fixture} from './maintenance-fixture.mjs';
async function ctx(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-maintenance-'));const c=await fixture(root);
  t.after(()=>c.state.close());t.after(()=>fs.rm(root,{recursive:true,force:true}));
  c.runtime={active:null,closing:false};c.maintenance=new CollectionMaintenance(c.state,c.commands,c.runtime);return c;
}
const apply=(c,id,op='end')=>{const p=c.maintenance.preview(id,op);return c.maintenance.apply(id,op,p.token);};
test('结束当前受阻任务同时释放同门店旧失败；新批次可正常建立，文件和成功项不变',async t=>{
  const c=await ctx(t),used=c.commands.get('adopted'),file=used.items[0].result.file,before=await fs.readFile(file);
  const plan=c.maintenance.preview('blocked','end');assert.equal(plan.related.length,1);assert.equal(plan.blocked.length,0);
  const out=c.maintenance.apply('blocked','end',plan.token);assert.equal(out.records.length,2);
  for(const code of ['W2','W1'])assert.deepEqual(c.state.pending(c.identity,code),[]);
  assert.equal(c.commands.get('old-failed').state,'ENDED');assert.equal(c.commands.get('blocked').state,'ENDED');
  assert.deepEqual(c.commands.get('adopted'),used);assert.deepEqual(await fs.readFile(file),before);
  assert.equal((await c.commands.reserve(c.command('new'),c.config)).created,true);
});
test('结束与删除回执可重复查询；删除防重标识阻止旧命令重放',async t=>{
  const c=await ctx(t),plan=c.maintenance.preview('blocked','end');
  assert.deepEqual(c.maintenance.apply('blocked','end',plan.token),c.maintenance.apply('blocked','end',plan.token));
  const d=c.maintenance.preview('blocked','delete');const receipt=c.maintenance.apply('blocked','delete',d.token);
  assert.deepEqual(c.maintenance.apply('blocked','delete',d.token),receipt);
  assert.equal(c.commands.get('blocked'),null);assert.deepEqual(c.commands.events('blocked'),[]);
  await assert.rejects(()=>c.commands.reserve(c.command('blocked'),c.config),{code:'COMMAND_RETIRED'});
  assert.equal(c.maintenance.preview('blocked','delete').already_deleted,true);
});
// 0.50.4 deliberately replaces the former platform-state lock with explicit local retirement.
for(const stage of ['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','ACCEPTED','RUNNING','DOWNLOADED'])
test('明确结束不等待平台状态，保留原证据并允许独立新作业：'+stage,async t=>{
  const c=await ctx(t),a={...c.state.getAttempt('attempt-W2'),stage,taskSummary:null};c.state.putAttempt(a);
  const plan=c.maintenance.preview('blocked','end');assert.deepEqual(plan.blocked,[]);
  c.maintenance.apply('blocked','end',plan.token);
  const ended=c.state.getAttempt(a.id);assert.equal(ended.stage,'ABANDONED');assert.equal(ended.previousStage,stage);
  assert.equal(ended.taskId,a.taskId);assert.deepEqual(ended.downloaded,a.downloaded);assert.deepEqual(c.state.pending(c.identity,'W2'),[]);
  const next=await c.commands.reserve(c.command('new-after-end',['W2']),c.config);
  assert.notEqual(next.record.items[0].run_id,a.runId);assert.equal(next.record.items[0].task_id,undefined);
  await assert.rejects(()=>new Coordinator({state:c.state,config:c.config,binding:{outputRoot:c.root}}).run({runId:a.runId}),{code:'RUN_ENDED'});
});
for(const patch of [{taskId:null},{additionalTaskIds:['another-task']}])test('回执不完整仍可明确结束，原不确定事实不伪装为成功：'+JSON.stringify(patch),async t=>{
  const c=await ctx(t),a={...c.state.getAttempt('attempt-W2'),stage:'ACCEPTED',...patch};
  c.state.db.prepare('UPDATE attempts SET stage=?,payload=? WHERE id=?').run(a.stage,JSON.stringify(a),a.id);
  apply(c,'blocked');const ended=c.state.getAttempt(a.id);
  assert.equal(ended.stage,'ABANDONED');assert.equal(ended.taskId,a.taskId);assert.deepEqual(ended.additionalTaskIds,a.additionalTaskIds);
  assert.deepEqual(c.state.pending(c.identity,'W2'),[]);
});

test('预览后版本变化或重新运行时拒绝；事务写入失败完整回滚',async t=>{
  const c=await ctx(t),p=c.maintenance.preview('blocked','end');
  c.state.putAttempt({...c.state.getAttempt('attempt-W2'),newEvidence:'changed'});
  assert.throws(()=>c.maintenance.apply('blocked','end',p.token),{code:'COLLECTION_CHANGED'});
  c.runtime.active={id:'other'};assert.throws(()=>c.maintenance.preview('blocked','end'),{code:'COLLECTION_BUSY'});c.runtime.active=null;
  const before=c.state.rows('attempts');const orig=c.commands.insertEvent;c.commands.insertEvent=()=>{throw Error('disk failure');};
  assert.throws(()=>apply(c,'blocked'),/disk failure/);assert.deepEqual(c.state.rows('attempts'),before);
  assert.equal(c.commands.get('blocked').state,'NEEDS_REVIEW');c.commands.insertEvent=orig;
});
test('只结束涉及门店；同旧批次其他失败门店仍可重跑，原顺序不变',async t=>{
  const c=await ctx(t);await c.commands.reserve(c.command('only-W2',['W2']),c.config);c.commands.advance('only-W2',1,'PAUSED');
  apply(c,'only-W2');const old=c.commands.get('old-failed');
  assert.deepEqual(old.items.map(i=>i.state),['ENDED','FAILED']);assert.equal(c.state.pending(c.identity,'W1').length,1);
  const calls=[],runtime=new MachineRuntime({commands:c.commands,executeItem:async({item})=>{calls.push(item.warehouse_code);return {};}});
  runtime.retry(old.id,[1],old.sequence);await runtime.run(old.id);assert.deepEqual(calls,['W1']);
  assert.deepEqual(c.commands.get(old.id).items.map(i=>i.warehouse_code),['W2','W1']);
});
test('原作业结束后不能从旧 run 恢复，旧回执不复活占用',async t=>{
  const c=await ctx(t);apply(c,'blocked');const a=c.state.getAttempt('attempt-W2');
  c.state.recordReceipt({intentId:a.id,taskId:a.taskId,code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:['W2'],extraKeys:[]}});
  assert.equal(c.state.getAttempt(a.id).stage,'ABANDONED');assert.equal(c.state.pending(c.identity,'W2').length,0);
  await assert.rejects(()=>new Coordinator({state:c.state,config:c.config,binding:{outputRoot:c.root}}).run({runId:a.runId}),{code:'RUN_ENDED'});
});
test('成功记录删除保留原件及日资产；未结束记录不能删除',async t=>{
  const c=await ctx(t),used=c.commands.get('adopted'),file=used.items[0].result.file,before=await fs.readFile(file);
  assert.throws(()=>c.maintenance.preview('old-failed','delete'),{code:'COLLECTION_NOT_ENDED'});
  const assets=c.state.rows('assets');apply(c,'adopted','delete');assert.deepEqual(await fs.readFile(file),before);assert.deepEqual(c.state.rows('assets'),assets);
});
test('维护协议严格验证动作与预览凭据',()=>{
  const r={protocol_version:1,request_id:'r',action:'collection.apply',command_id:'id',operation:'end',token:'a'.repeat(64)};
  validateRequest(r);assert.throws(()=>validateRequest({...r,operation:'erase-all'}));assert.throws(()=>validateRequest({...r,token:'wrong'}));
});
