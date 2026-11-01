import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {publishAsset,recoverPublications,outputPath} from '../src/publisher.mjs';
import {sha256} from '../src/common.mjs';

function meta(content){return {accountId:'1',merchantCode:'M',buyerWarehouseCode:'W',acquisitionDate:'2026-11-01',scopeHash:'a'.repeat(64),semanticVersion:'D13-all-info-2',taskId:'task1',sha256:sha256(content),relativePath:'商家/2026-11-01/仓店.xlsx'};}
test('重跑保存失败保留旧文件，恢复只完成本地保存',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-publish-'));const state=await StateStore.open(path.join(root,'state'));const outputRoot=path.join(root,'results');
  const old=Buffer.from('old-native-bytes'),fresh=Buffer.from('new-native-bytes');
  const a=path.join(root,'old.xlsx'),b=path.join(root,'new.xlsx');await fs.writeFile(a,old);await fs.writeFile(b,fresh);
  const saved=await publishAsset({state,outputRoot,sourceFile:a,asset:meta(old)});const final=await outputPath(outputRoot,saved.relativePath);
  await assert.rejects(()=>publishAsset({state,outputRoot,sourceFile:b,asset:meta(fresh),replace:true,rename:async()=>{throw Object.assign(new Error('simulated Windows share lock'),{code:'EPERM'});}}),{code:'SAVE_BLOCKED'});
  assert.deepEqual(await fs.readFile(final),old);assert.equal(state.rows('publishes').length,1);
  assert.equal((await recoverPublications({state,outputRoot})).length,1);assert.deepEqual(await fs.readFile(final),fresh);assert.equal(state.rows('publishes').length,0);state.close();
});
test('未登记文件和用户改动不能被覆盖',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-foreign-'));const state=await StateStore.open(path.join(root,'state'));const outputRoot=path.join(root,'results');const content=Buffer.from('native');const source=path.join(root,'source.xlsx');await fs.writeFile(source,content);
  const target=await outputPath(outputRoot,meta(content).relativePath);await fs.writeFile(target,'user-file');
  await assert.rejects(()=>publishAsset({state,outputRoot,sourceFile:source,asset:meta(content),replace:true}),{code:'UNTRACKED_FILE_EXISTS'});assert.equal(await fs.readFile(target,'utf8'),'user-file');state.close();
});
