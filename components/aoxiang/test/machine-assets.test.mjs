import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {StateStore,assetKey} from '../src/state.mjs';
import {freezeRunAsset} from '../src/machine-assets.mjs';
import {sha256} from '../src/common.mjs';

async function context(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-freeze-')),state=await StateStore.open(path.join(root,'state'));
  t.after(()=>state.close());const binding={outputRoot:path.join(root,'output')};await fs.mkdir(binding.outputRoot);
  const bytes=Buffer.from('synthetic content v1'),asset={accountId:'100',merchantCode:'M',buyerWarehouseCode:'W1',acquisitionDate:'2026-11-01',scopeHash:'scope',semanticVersion:'D13-all-info-2',taskId:'t1',relativePath:'daily.xlsx',sha256:sha256(bytes),bytes:bytes.length,rows:1,columns:54,collectedAt:'2026-11-01T00:00:00Z'};
  const item={index:0,warehouse_code:'W1',run_id:'r1'};
  state.putRun({id:'r1',status:'SUCCEEDED'});state.putItem({id:'i1',runId:'r1',stage:'REUSED',assetKey:assetKey(asset),assetSnapshot:asset});
  await fs.writeFile(path.join(binding.outputRoot,'daily.xlsx'),bytes);
  return {root,state,binding,item,asset,bytes,commandId:'job1'};
}
test('命令采用确切文件副本；刷新日文件后重复读取仍返回原字节',async t=>{
  const c=await context(t),first=await freezeRunAsset(c);
  assert.deepEqual(await fs.readFile(first.file),c.bytes);
  await fs.writeFile(path.join(c.binding.outputRoot,'daily.xlsx'),'new daily content');
  const again=await freezeRunAsset(c);assert.deepEqual(again,first);assert.deepEqual(await fs.readFile(again.file),c.bytes);
});
test('崩溃在日文件完成与冻结之间，按原哈希备份恢复，不能使用新的日版本',async t=>{
  const c=await context(t);await fs.mkdir(path.join(c.binding.outputRoot,'.staging/backups'),{recursive:true});
  await fs.writeFile(path.join(c.binding.outputRoot,'.staging/backups',c.asset.sha256+'.xlsx'),c.bytes);
  await fs.writeFile(path.join(c.binding.outputRoot,'daily.xlsx'),'newer content');
  const result=await freezeRunAsset(c);assert.equal(result.sha256,c.asset.sha256);assert.deepEqual(await fs.readFile(result.file),c.bytes);
});
test('原文件版本缺失或已冻结文件被改均阻断，不能静默换为最新文件',async t=>{
  const c=await context(t);await fs.writeFile(path.join(c.binding.outputRoot,'daily.xlsx'),'different');
  await assert.rejects(()=>freezeRunAsset(c),{code:'ASSET_VERSION_MISSING'});
  await fs.writeFile(path.join(c.binding.outputRoot,'daily.xlsx'),c.bytes);const result=await freezeRunAsset(c);
  await fs.writeFile(result.file,'modified');await assert.rejects(()=>freezeRunAsset(c),{code:'FROZEN_ASSET_CHANGED'});
});
