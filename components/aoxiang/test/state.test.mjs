import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {ExclusiveLock,StateStore} from '../src/state.mjs';
import {scopeHash} from '../src/common.mjs';

const identity={accountId:'100',merchantCode:'merchant'};
test('两个执行器不能同时持有同一本机锁，释放后可恢复',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-lock-'));const f=path.join(root,'runner.lock.sqlite');
  const a=await ExclusiveLock.acquire(f);
  await assert.rejects(()=>ExclusiveLock.acquire(f),{code:'APP_BUSY'});
  a.release();const b=await ExclusiveLock.acquire(f);b.release();
});
test('未知提交持久化，重建进程和换日不能解除',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-state-'));let s=await StateStore.open(root);
  const a={id:'attempt-1',itemId:'item-1',...identity,buyerWarehouseCode:'WH',scopeHash:scopeHash('WH'),stage:'SUBMISSION_UNKNOWN',taskId:null};
  s.putAttempt(a);s.close();s=await StateStore.open(root);
  assert.equal(s.pending(identity,'WH',scopeHash('WH')).length,1);
  assert.throws(()=>s.putAttempt({...a,stage:'SUBMITTING'}),{code:'UNKNOWN_SUBMISSION'});s.close();
});
test('迁入UNKNOWN后，旧包已知状态不能消除未知；重复导入幂等',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-import-'));const s=await StateStore.open(root);
  const record={recordId:'r1',sourceRunId:'run1',sourceAttemptId:'a1',...identity,buyerWarehouseCode:'WH',scopeHash:scopeHash('WH'),state:'SUBMISSION_UNKNOWN',taskId:null};
  const m={migrationId:'migration1',handoffStatus:'SOURCE_STOPPED',unresolvedRecords:[record]};
  s.importRecords(m,'hash1');assert.equal(s.importRecords(m,'hash1').alreadyImported,true);
  assert.throws(()=>s.importRecords(m,'different'),{code:'MIGRATION_ID_CONFLICT'});
  s.importRecords({...m,migrationId:'old-migration',unresolvedRecords:[{...record,state:'ACCEPTED',taskId:'123'}]},'hash2');
  assert.equal(s.pending(identity,'WH',scopeHash('WH'))[0].state,'SUBMISSION_UNKNOWN');s.close();
});
