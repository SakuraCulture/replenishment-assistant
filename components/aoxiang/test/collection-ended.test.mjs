import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {DatabaseSync} from 'node:sqlite';
import {StateStore} from '../src/state.mjs';
import {MachineCommands} from '../src/machine-commands.mjs';
import {CollectionMaintenance} from '../src/machine-maintenance.mjs';
import {fixture} from './maintenance-fixture.mjs';

async function legacy(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-ended-upgrade-')),c=await fixture(root);
  t.after(async()=>{c.state.close();await fs.rm(root,{recursive:true,force:true});});
  const a=c.state.getAttempt('attempt-W2');c.state.putAttempt({...a,stage:'ACCEPTED'});
  const run=c.state.getRun(a.runId);
  c.state.write(()=>{
    c.state.db.prepare('UPDATE runs SET payload=? WHERE id=?').run(JSON.stringify({...run,status:'ENDED',endedAt:'2026-11-01T00:00:00Z',resumePending:true}),run.id);
    for(const cmd of c.commands.list()){
      const items=cmd.items.map(i=>i.run_id===run.id?{...i,state:'ENDED'}:i);
      c.state.db.prepare('UPDATE machine_commands SET payload=? WHERE id=?').run(JSON.stringify({...cmd,items,state:items.every(i=>i.state==='ENDED')?'ENDED':cmd.state}),cmd.id);
    }
  });
  c.run=run;c.attempt=c.state.getAttempt(a.id);c.maintenance=()=>new CollectionMaintenance(c.state,c.commands,{active:null,closing:false});return c;
}
test('旧版明确结束自动释放占用，保留回执原件及其他门店；删除后晚到回执不复活',async t=>{
  const c=await legacy(t),commands=c.commands.list(),other=c.state.getAttempt('attempt-W1'),used=c.commands.get('adopted');
  const bytes=await fs.readFile(used.items[0].result.file);assert.equal(c.state.pending(c.identity,'W2').length,1);
  const maintenance=c.maintenance();assert.equal(c.state.collectionEnded(c.run.id),true);assert.deepEqual(c.state.pending(c.identity,'W2'),[]);
  assert.equal(c.state.getRun(c.run.id).resumePending,false);assert.deepEqual(c.state.getAttempt(c.attempt.id),c.attempt);
  assert.deepEqual(c.commands.list(),commands);assert.deepEqual(c.state.getAttempt(other.id),other);
  assert.equal(c.state.pending(c.identity,'W1').length,1);assert.deepEqual(await fs.readFile(used.items[0].result.file),bytes);
  const audit=c.state.db.prepare('SELECT value FROM metadata WHERE key=?').get('legacyCollectionEnd:'+c.run.id).value;
  assert.deepEqual(JSON.parse(audit).task_ids,[c.attempt.taskId]);
  const before=c.state.db.prepare('SELECT value FROM metadata WHERE key=?').get('generation');c.maintenance();
  assert.deepEqual(c.state.db.prepare('SELECT value FROM metadata WHERE key=?').get('generation'),before);
  // Remove the shared log only after its other unfinished member is explicitly ended.
  let p=maintenance.preview('old-failed','end');maintenance.apply('old-failed','end',p.token);
  p=maintenance.preview('old-failed','delete');maintenance.apply('old-failed','delete',p.token);
  assert.equal(c.commands.get('old-failed'),null);assert.equal(c.state.collectionEnded(c.run.id),true);
  c.state.recordReceipt({intentId:c.attempt.id,taskId:c.attempt.taskId,code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:['W2'],extraKeys:[]}});
  assert.deepEqual(c.state.pending(c.identity,'W2'),[]);assert.throws(()=>c.state.putRun(c.run),{code:'RUN_ENDED'});
  const next=await c.commands.reserve(c.command('new-after-upgrade',['W2']),c.config);assert.notEqual(next.record.items[0].run_id,c.run.id);
});
test('存在活动接管者或缺少身份、范围证据时不擅自结束',async t=>{
  const c=await legacy(t),command=c.commands.get('old-failed');
  const raw={...command,id:'active-owner',state:'RUNNING',items:command.items.map(i=>i.run_id===c.run.id?{...i,state:'RUNNING'}:i)};
  // Existing schema has separate fingerprint/request columns: use a real reserved row, then point its receipt at the run.
  await c.commands.reserve(c.command('active-owner',['W3']),c.config);
  c.state.db.prepare('UPDATE machine_commands SET payload=? WHERE id=?').run(JSON.stringify(raw),raw.id);
  c.maintenance();assert.equal(c.state.collectionEnded(c.run.id),false);assert.equal(c.state.pending(c.identity,'W2').length,1);
  c.state.db.prepare('DELETE FROM machine_events WHERE command_id=?').run(raw.id);
  c.state.db.prepare('DELETE FROM machine_commands WHERE id=?').run(raw.id);
  const run=c.state.getRun(c.run.id);delete run.identity;
  c.state.db.prepare('UPDATE runs SET payload=? WHERE id=?').run(JSON.stringify(run),run.id);
  c.maintenance();assert.equal(c.state.collectionEnded(c.run.id),false);
});
test('修复事务失败不留下半个失效屏障',async t=>{
  const c=await legacy(t);c.state.db.exec("CREATE TRIGGER fail_repair BEFORE UPDATE ON runs BEGIN SELECT RAISE(ABORT,'synthetic disk failure'); END");
  assert.throws(()=>c.maintenance(),/synthetic disk failure/);assert.equal(c.state.collectionEnded(c.run.id),false);
  assert.equal(c.state.getRun(c.run.id).resumePending,true);assert.equal(c.state.pending(c.identity,'W2').length,1);
  assert.equal(c.state.db.prepare('SELECT value FROM metadata WHERE key=?').get('legacyCollectionEnd:'+c.run.id),undefined);
});
test('实际打开旧库前自动备份，修复后重开不重复备份',async t=>{
  const c=await legacy(t);c.state.close();c.state=await StateStore.open(c.root);c.commands=new MachineCommands(c.state);
  const repairFiles=async()=> (await fs.readdir(path.join(c.root,'recovery'))).filter(f=>f.startsWith('before-ended-collection-')&&f.endsWith('.sqlite'));
  const files=await repairFiles();assert.equal(files.length,1);
  const backup=new DatabaseSync(path.join(c.root,'recovery',files[0]),{readOnly:true});
  try{assert.equal(JSON.parse(backup.prepare('SELECT payload FROM runs WHERE id=?').get(c.run.id).payload).resumePending,true);}finally{backup.close();}
  c.maintenance();c.state.close();c.state=await StateStore.open(c.root);
  assert.deepEqual(await repairFiles(),files);
});
