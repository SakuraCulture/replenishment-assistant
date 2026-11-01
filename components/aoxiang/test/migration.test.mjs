import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {scopeHash,readJson} from '../src/common.mjs';
import {exportMigration,previewMigration,applyMigration} from '../src/migration.mjs';
import {openArchive,createArchive} from '../src/zip.mjs';

function config(){return freshConfig({accountId:'100',merchantCode:'merchant',displayName:'测试商家'},[{buyerWarehouseCode:'WH',displayName:'独立仓店点',operatingStores:[],mappingSource:'native-selector',mappingVerifiedAt:'2026-11-01T00:00:00+08:00'}]);}
test('迁移恢复未知任务，重建配置但不携带本机绑定或浏览器资料',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-move-'));
  const source=await StateStore.open(path.join(root,'source'));
  source.putAttempt({id:'a1',itemId:'i1',runId:'r1',accountId:'100',merchantCode:'merchant',buyerWarehouseCode:'WH',scopeHash:scopeHash('WH'),stage:'SUBMISSION_UNKNOWN',taskId:null,createdAt:'2026-11-01T00:00:00+08:00'});
  const file=path.join(root,'handoff.zip');await exportMigration({state:source,config:config(),output:file,sourceStopped:true});
  const targetRoot=path.join(root,'different-user','state');const target=await StateStore.open(targetRoot);
  const preview=await previewMigration(file);const first=await applyMigration(preview,{state:target,stateRoot:targetRoot});
  assert.equal(first.unresolvedCount,1);assert.equal(target.pending({accountId:'100',merchantCode:'merchant'},'WH',scopeHash('WH')).length,1);
  const rebuilt=await readJson(path.join(targetRoot,'config/portable.config.json'));assert.deepEqual(rebuilt,config());
  const zipped=await openArchive(file);assert.deepEqual([...zipped.entries.keys()].sort(),['config/portable.config.json','migration.manifest.json']);zipped.close();
  const again=await previewMigration(file,{currentConfig:rebuilt});assert.equal((await applyMigration(again,{state:target,stateRoot:targetRoot})).alreadyImported,true);
  source.close();target.close();
});
test('迁移包配置被篡改或混入非白名单成员时拒绝',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-badmove-'));const source=await StateStore.open(path.join(root,'state'));
  const good=path.join(root,'good.zip');await exportMigration({state:source,config:config(),output:good});
  const zip=await openArchive(good);const members=[];for(const n of zip.entries.keys())members.push({relativePath:n,buffer:await zip.buffer(n)});zip.close();
  const tampered=path.join(root,'tampered.zip');await createArchive(tampered,members.map(x=>x.relativePath.startsWith('config/')?{...x,buffer:Buffer.from('{}')}:x));
  await assert.rejects(()=>previewMigration(tampered),{code:'MIGRATION_HASH'});
  const extra=path.join(root,'extra.zip');await createArchive(extra,[...members,{relativePath:'machine.binding.json',buffer:'{}'}]);
  await assert.rejects(()=>previewMigration(extra),{code:'MIGRATION_UNEXPECTED_MEMBER'});source.close();
});
test('未决记录被删减不能以空清单导入',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-pendingloss-')),s=await StateStore.open(path.join(root,'state'));
  s.putAttempt({id:'a1',runId:'r1',itemId:'i1',accountId:'100',merchantCode:'merchant',buyerWarehouseCode:'WH',scopeHash:scopeHash('WH'),stage:'SUBMISSION_UNKNOWN',taskId:null,createdAt:'2026-11-01T00:00:00Z'});
  const good=path.join(root,'good.zip');await exportMigration({state:s,config:config(),output:good});
  const zip=await openArchive(good),members=[];for(const n of zip.entries.keys()){let buffer=await zip.buffer(n);if(n==='migration.manifest.json'){const manifest=JSON.parse(buffer);manifest.unresolvedRecords=[];buffer=Buffer.from(JSON.stringify(manifest));}members.push({relativePath:n,buffer});}zip.close();
  const broken=path.join(root,'missing-pending.zip');await createArchive(broken,members);await assert.rejects(()=>previewMigration(broken),{code:'MIGRATION_RECOVERY_INCOMPLETE'});s.close();
});
