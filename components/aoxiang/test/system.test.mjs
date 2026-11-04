import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fork,spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import {StateStore,ExclusiveLock} from '../src/state.mjs';
import {publishAsset,recoverPublications} from '../src/publisher.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {verifyInstallation} from '../src/integrity.mjs';
import {scopeHash,writeJson} from '../src/common.mjs';

test('真实独立worker持锁；父IPC断开后退出并释放锁',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-orphan-'));
  const worker=fork(fileURLToPath(new URL('../src/browser-worker.mjs',import.meta.url)),[],{stdio:['ignore','ignore','pipe','ipc'],windowsHide:true,execPath:process.execPath});
  try{
    const events=[];
    const response=new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{worker.off('message',receive);reject(Error('connect response timeout'));},5000);
      function receive(m){if(m.event){events.push(m);return;}if(m.id==='1'){clearTimeout(timer);worker.off('message',receive);resolve(m);}}
      worker.on('message',receive);
    });
    worker.send({id:'1',method:'connect',params:{stateRoot:root,candidate:{actualProfilePath:path.join(root,'nonexistent-profile'),edgeExecutable:process.execPath,userDataRoot:root,profileDirectory:'Profile'}}});
    const m=await response;assert.ok(m.error);assert.deepEqual(events.filter(m=>m.event==='connection').map(m=>m.state),['CONNECTING','DISCONNECTED']);
    await assert.rejects(()=>ExclusiveLock.acquire(path.join(root,'runner.lock.sqlite')),{code:'APP_BUSY'});
    const exit=once(worker,'exit');worker.disconnect();await exit;
    const lock=await ExclusiveLock.acquire(path.join(root,'runner.lock.sqlite'));lock.release();
  }finally{if(worker.connected)worker.disconnect();if(worker.exitCode===null)worker.kill();}
});
test('Windows真实独占文件锁下保留旧文件，解锁后只恢复本地发布',{skip:process.platform!=='win32'},async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-win-lock-')),state=await StateStore.open(path.join(root,'state')),output=path.join(root,'output');
  const source=path.join(root,'old'),next=path.join(root,'new');await fs.writeFile(source,'old-native-bytes');await fs.writeFile(next,'new-native-bytes');
  const base={accountId:'100',merchantCode:'M',buyerWarehouseCode:'WH',acquisitionDate:'2026-11-01',scopeHash:scopeHash('WH'),semanticVersion:'D13-all-info-2',relativePath:'M/2026-11-01/store.xlsx'};
  const a={...base,sha256:await fileHash(source)},b={...base,sha256:await fileHash(next)};
  await publishAsset({state,outputRoot:output,sourceFile:source,asset:a});const target=path.join(output,base.relativePath);
  const ps=path.join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0/powershell.exe');
  const child=spawn(ps,['-NoProfile','-NonInteractive','-Command',"$stream=[IO.File]::Open($env:AX_LOCK_FILE,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read);[Console]::WriteLine('READY');[Console]::Out.Flush();[Console]::ReadLine()|Out-Null;$stream.Dispose()"],{windowsHide:true,stdio:['pipe','pipe','pipe'],env:{...process.env,AX_LOCK_FILE:target}});
  try{
    await new Promise((resolve,reject)=>{child.once('error',reject);child.stdout.on('data',b=>{if(String(b).includes('READY'))resolve();});child.once('exit',code=>{if(code)reject(Error('文件锁助手退出'+code));});});
    await assert.rejects(()=>publishAsset({state,outputRoot:output,sourceFile:next,asset:b,replace:true}),{code:'SAVE_BLOCKED'});
    assert.equal(await fileHash(target),a.sha256);const exit=once(child,'exit');child.stdin.end('\n');await exit;
    await recoverPublications({state,outputRoot:output});assert.equal(await fileHash(target),b.sha256);
  }finally{child.kill();state.close();}
});
test('发行依赖哈希被改动时阻止执行',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-integrity-')),files=[];
  for(let i=0;i<10;i++){const relativePath='part'+i+'.txt',file=path.join(root,relativePath);await fs.writeFile(file,'original-'+i);files.push({relativePath,sha256:await fileHash(file)});}
  await writeJson(path.join(root,'integrity.manifest.json'),{format:1,files});assert.equal((await verifyInstallation(root)).files,10);
  await fs.writeFile(path.join(root,'part5.txt'),'changed');await assert.rejects(()=>verifyInstallation(root),{code:'INSTALLATION_HASH_MISMATCH'});
});
