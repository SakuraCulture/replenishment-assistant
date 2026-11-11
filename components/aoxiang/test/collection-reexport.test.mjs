import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fixture} from './maintenance-fixture.mjs';
import {CollectionMaintenance} from '../src/machine-maintenance.mjs';
import {publishAsset,recoverPublications} from '../src/publisher.mjs';
import {assetKey} from '../src/state.mjs';
import {sha256} from '../src/common.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';

async function ctx(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-reexport-'));const c=await fixture(root);
  t.after(async()=>{c.state.close();await fs.rm(root,{recursive:true,force:true});});
  c.runtime={active:null,closing:false};c.maintenance=new CollectionMaintenance(c.state,c.commands,c.runtime);return c;
}
const preview=(c,id='again',codes=['W2'],purpose='replenishment')=>c.maintenance.reexportPreview(c.config,id,codes,purpose);
const apply=(c,p)=>c.maintenance.reexportApply(c.config,p.id,p.stores.map(s=>s.code),p.purpose,p.token);

test('仅所选门店退役，未知/多回执证据保留，未选项不改位且新任务独立',async t=>{
  const c=await ctx(t),a={...c.state.getAttempt('attempt-W2'),stage:'SUBMISSION_UNKNOWN',additionalTaskIds:['late-other']};c.state.putAttempt(a);
  const other=c.state.getAttempt('attempt-W1'),before=c.commands.get('old-failed').items[1];
  const p=preview(c);assert.deepEqual(p.blocked,[]);const receipt=apply(c,p);
  assert.deepEqual(apply(c,p),receipt);assert.equal(c.state.getAttempt(a.id).previousStage,'SUBMISSION_UNKNOWN');
  assert.deepEqual(c.state.getAttempt(a.id).additionalTaskIds,a.additionalTaskIds);assert.deepEqual(c.state.pending(c.identity,'W2'),[]);
  assert.deepEqual(c.state.getAttempt(other.id),other);assert.deepEqual(c.commands.get('old-failed').items[1],before);
  assert.deepEqual(c.commands.get('old-failed').items.map(i=>i.warehouse_code),['W2','W1']);
  const next=await c.commands.reserve(c.command('fresh',['W2']),c.config);assert.notEqual(next.record.items[0].run_id,a.runId);
  assert.throws(()=>c.maintenance.reexportApply(c.config,p.id,['W1'],p.purpose,p.token),{code:'COLLECTION_CHANGED'});
});

test('晚到回执只记旧证据；旧run/item/attempt/asset及伪造换属均不能恢复',async t=>{
  const c=await ctx(t),a=c.state.getAttempt('attempt-W2'),run=c.state.getRun(a.runId),item=c.state.getItems(a.runId)[0];
  apply(c,preview(c));
  const late={intentId:a.id,taskId:'different-late-task',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:['W2']}};
  c.state.recordReceipt(late);c.state.recordReceipt(late);
  const ended=c.state.getAttempt(a.id);assert.equal(ended.stage,'ABANDONED');assert.equal(ended.taskId,a.taskId);assert.equal(ended.lateReceipts.length,1);
  assert.throws(()=>c.state.putRun(run),{code:'RUN_ENDED'});assert.throws(()=>c.state.putItem(item),{code:'RUN_ENDED'});
  assert.throws(()=>c.state.putAttempt(a),{code:'RUN_ENDED'});
  assert.throws(()=>c.state.putAttempt({...a,runId:'forged-new'}),{code:'RUN_ENDED'});
  const command=c.commands.get('old-failed'),items=structuredClone(command.items);items[0].state='PENDING';
  assert.throws(()=>c.commands.advance(command.id,command.sequence,'QUEUED',{items}),{code:'COLLECTION_ENDED'});
});

test('业务与分析同门店互不退役、不抢当前缓存；别的商家保持',async t=>{
  const c=await ctx(t),command={...c.command('analysis',['W2']),consumer:{purpose:'stockout_analysis',owner:'analysis-owner'}};
  const reserved=await c.commands.reserve(command,c.config);c.commands.advance(command.command_id,1,'PAUSED');
  const before=c.commands.get(command.command_id),run=c.state.getRun(before.items[0].run_id);
  const item=c.state.getItems(run.id)[0];c.state.putAttempt({...c.state.getAttempt('attempt-W2'),id:'analysis-intent',runId:run.id,itemId:item.id,purpose:'stockout_analysis'});
  apply(c,preview(c));assert.deepEqual(c.commands.get(command.command_id),before);
  assert.equal(c.state.collectionEnded(reserved.record.items[0].run_id),false);
  assert.equal(c.state.pending(c.identity,'W2',undefined,'stockout_analysis').length,1);
  const meta={...c.identity,buyerWarehouseCode:'W2',acquisitionDate:'2026-11-11',scopeHash:'scope',semanticVersion:'v'};
  assert.notEqual(assetKey(meta),assetKey({...meta,purpose:'stockout_analysis'}));
});

test('重导预览过期和落库失败整体回滚；当前相关动作未安全结束不退役',async t=>{
  const c=await ctx(t),p=preview(c);c.state.putAttempt({...c.state.getAttempt('attempt-W2'),newEvidence:'extra'});
  assert.throws(()=>apply(c,p),{code:'COLLECTION_CHANGED'});
  const before=c.state.rows('attempts'),next=preview(c,'retry-plan');const insert=c.commands.insertEvent;
  c.commands.insertEvent=()=>{throw Error('isolated disk failure');};assert.throws(()=>apply(c,next),/disk failure/);
  c.commands.insertEvent=insert;assert.deepEqual(c.state.rows('attempts'),before);assert.equal(c.state.collectionEnded(before[0].runId),false);
  c.runtime.active={id:'blocked'};assert.throws(()=>preview(c,'busy'),{code:'COLLECTION_BUSY'});
});

test('每次原件独立保存；晚到旧版本不倒退当前指针，旧保存失败不阻塞新任务',async t=>{
  const c=await ctx(t),a=c.state.getAttempt('attempt-W2');
  const first=await c.commands.reserve(c.command('first',['W3']),c.config),second=await c.commands.reserve(c.command('second',['W3']),c.config);
  c.commands.advance('first',1,'PAUSED');c.commands.advance('second',1,'PAUSED');
  const file1=path.join(c.root,'one.xlsx'),file2=path.join(c.root,'two.xlsx');await fs.writeFile(file1,'one');await fs.writeFile(file2,'two');
  const meta=(record,text)=>({...c.identity,runId:record.items[0].run_id,purpose:'replenishment',buyerWarehouseCode:'W3',acquisitionDate:'2026-11-11',scopeHash:'x',semanticVersion:'v',sha256:sha256(text),taskId:text,relativePath:'.collections/'+record.items[0].run_id+'/original.xlsx'});
  const fresh=meta(second.record,'two'),old=meta(first.record,'one');
  await publishAsset({state:c.state,outputRoot:c.root,sourceFile:file2,asset:fresh,replace:true});
  await publishAsset({state:c.state,outputRoot:c.root,sourceFile:file1,asset:old,replace:true});
  assert.equal(c.state.getAsset(assetKey(fresh)).sha256,fresh.sha256);
  assert.equal(await fs.readFile(path.join(c.root,old.relativePath),'utf8'),'one');
  assert.equal(await fs.readFile(path.join(c.root,fresh.relativePath),'utf8'),'two');
  const stuck={...old,runId:a.runId,buyerWarehouseCode:'W2',relativePath:'.collections/'+a.runId+'/blocked.xlsx'};
  await assert.rejects(()=>publishAsset({state:c.state,outputRoot:c.root,sourceFile:file1,asset:stuck,attemptId:a.id,replace:true,rename:async()=>{throw Error('isolated file lock');}}),{code:'SAVE_BLOCKED'});
  apply(c,preview(c));assert.equal((await recoverPublications({state:c.state,outputRoot:c.root})).length,0);
  assert.equal(c.state.rows('publishes').length,1);assert.equal(c.state.getAsset(assetKey(stuck)),null);
});

test('重导请求必须有明确用途、唯一门店与确认token',()=>{
  const base={protocol_version:1,request_id:'request',action:'collection.reexport.apply',operation_id:'again',warehouse_codes:['W2'],purpose:'replenishment',token:'a'.repeat(64)};
  assert.deepEqual(validateRequest(base),base);
  for(const patch of [{warehouse_codes:[]},{warehouse_codes:['W2','W2']},{purpose:'purchase'},{token:'bad'},{force:true}])assert.throws(()=>validateRequest({...base,...patch}),{code:'REQUEST_SCHEMA'});
});

test('旧版无用途字段按原始上下文登记归属，业务重导不隐式结束旧分析',async t=>{
  const c=await ctx(t),old=c.commands.get('old-failed'),attempt=c.state.getAttempt('attempt-W2');
  assert.throws(()=>c.maintenance.classify(old.id,'b'.repeat(64),'stockout_analysis','analysis-old'),{code:'MAINTENANCE_SCOPE'});
  c.maintenance.classify(old.id,old.fingerprint,'stockout_analysis','analysis-old');
  assert.throws(()=>c.maintenance.classify(old.id,old.fingerprint,'replenishment',''),{code:'MAINTENANCE_SCOPE'});
  apply(c,preview(c));assert.deepEqual(c.commands.get(old.id),old);assert.deepEqual(c.state.getAttempt(attempt.id),attempt);
  assert.equal(c.state.pending(c.identity,'W2',undefined,'stockout_analysis').length,1);
  const p=preview(c,'analysis-again',['W2'],'stockout_analysis');apply(c,p);
  assert.equal(c.commands.get(old.id).items[0].state,'ENDED');assert.equal(c.commands.get(old.id).items[1].state,'FAILED');
});

test('旧版保存现场在结束及删除明细后仍失效，不能绕过新版任务原件',async t=>{
  const c=await ctx(t),a=c.state.getAttempt('attempt-W2'),source=path.join(c.root,'legacy-source.xlsx');await fs.writeFile(source,'legacy');
  const asset={...c.identity,buyerWarehouseCode:'W2',acquisitionDate:'2026-11-11',scopeHash:a.scopeHash,semanticVersion:a.semanticVersion,sha256:sha256('legacy'),relativePath:'legacy/daily.xlsx'};
  await assert.rejects(()=>publishAsset({state:c.state,outputRoot:c.root,sourceFile:source,asset,attemptId:a.id,rename:async()=>{throw Error('synthetic sharing lock');}}),{code:'SAVE_BLOCKED'});
  const p=c.maintenance.preview('old-failed','end');c.maintenance.apply('old-failed','end',p.token);
  const d=c.maintenance.preview('old-failed','delete');c.maintenance.apply('old-failed','delete',d.token);
  assert.equal(c.state.getAttempt(a.id),null);assert.equal(c.state.rows('publishes')[0].retiredAt!==undefined,true);
  assert.deepEqual(await recoverPublications({state:c.state,outputRoot:c.root}),[]);
  await assert.rejects(()=>publishAsset({state:c.state,outputRoot:c.root,sourceFile:source,asset,attemptId:a.id}),{code:'RUN_ENDED'});
});
