import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {CatalogService} from '../src/service.mjs';
import {AccountSessions} from '../src/accounts.mjs';
import {check} from '../src/common.mjs';

async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-auto-')),calls=[];
  const driver={
    automaticBinding:async(warehouse,others,signal)=>{check(signal);calls.push({warehouse,others});return {actualProfilePath:path.join(root,warehouse,'Default'),userDataRoot:path.join(root,warehouse)};},
    inspect:async(binding,company,signal,options)=>{check(signal);assert.equal(options.waitForLogin,true);return {company:'fixture',coid:binding.userDataRoot.endsWith('山东仓')?'1':'2',uid:'9'};},
    close:async()=>{},
  };
  const service=await CatalogService.open({stateRoot:root,driver});
  t.after(async()=>{await service.close();await fs.rm(root,{recursive:true,force:true});});
  const auto=async(warehouse='山东仓',version=0)=>{
    const ack=await service.command({action:'accounts.auto',warehouse,expected_version:version});
    await service.active?.promise;return service.status().operations[ack.operation_id];
  };
  return {root,service,driver,calls,auto};
}
test('按所选仓自动准备、核验和保存；两仓绑定独立且重复配置不改变版本',async t=>{
  const c=await fixture(t),first=await c.auto(),second=await c.auto('河北仓');
  assert.equal(first.state,'SUCCEEDED');assert.equal(second.state,'SUCCEEDED');
  const accounts=c.service.store.accounts();assert.equal(accounts.length,2);assert.ok(accounts.every(a=>a.mappingVerified&&a.enabled));
  assert.equal(c.calls[1].others[0].warehouse,'山东仓');
  const again=await c.auto('山东仓',1);assert.equal(again.result.account.bindingVersion,1);
  assert.deepEqual(c.service.store.accounts(),accounts);
  await assert.rejects(c.auto('山东仓',0),{code:'CONFIG_CONFLICT'});
});
test('账号不同或两个仓使用同一身份时不会自动覆盖绑定',async t=>{
  const c=await fixture(t);await c.auto();const before=c.service.store.accounts();
  c.driver.inspect=async()=>({company:'fixture',coid:'8',uid:'9'});
  assert.equal((await c.auto('山东仓',1)).error.code,'WRONG_ACCOUNT');assert.deepEqual(c.service.store.accounts(),before);
  c.driver.inspect=async()=>({company:'fixture',coid:'1',uid:'9'});
  assert.equal((await c.auto('河北仓')).state,'FAILED');assert.deepEqual(c.service.store.accounts(),before);
});
test('待收取任务可复用原绑定；自动迁移新目录仍受待处理任务保护',async t=>{
  const c=await fixture(t);await c.auto();const a=c.service.store.accounts()[0];
  await c.service.store.write(d=>{d.batches.pending={items:[{account:a,native:{nativeTaskRef:'original',stage:'ACCEPTED'}}]};});
  assert.equal((await c.auto('山东仓',1)).state,'SUCCEEDED');
  c.driver.automaticBinding=async()=>({userDataRoot:'new-sd',actualProfilePath:'new-sd/Default'});
  c.driver.inspect=async()=>({company:'fixture',coid:'1',uid:'9'});
  assert.equal((await c.auto('山东仓',1)).error.code,'PENDING_TASK');
  assert.deepEqual(c.service.store.accounts(),[a]);
});
test('取消登录等待不保存；自动保存完成前不返回成功',async t=>{
  const c=await fixture(t);let entered,release;const ready=new Promise(r=>{entered=r;}),wait=new Promise(r=>{release=r;});
  c.driver.inspect=async(binding,company,signal)=>{entered();await wait;check(signal);return {company:'fixture',coid:'1',uid:'9'};};
  const work=c.auto();await ready;await c.service.command({action:'sync.stop'});release();
  assert.equal((await work).state,'CANCELLED');assert.deepEqual(c.service.store.accounts(),[]);
  c.driver.inspect=async()=>({company:'fixture',coid:'1',uid:'9'});
  const configure=c.service.configure.bind(c.service);let saved;const gate=new Promise(r=>{saved=r;});
  c.service.configure=async(...args)=>{await gate;return configure(...args);};
  const ack=await c.service.command({action:'accounts.auto',warehouse:'山东仓',expected_version:0});
  await new Promise(r=>setTimeout(r,20));assert.equal(c.service.status().operations[ack.operation_id].state,'RUNNING');
  saved();await c.service.active.promise;assert.equal(c.service.status().operations[ack.operation_id].state,'SUCCEEDED');
  assert.equal(c.service.store.accounts().length,1);
});
test('自动目录可复用且按仓隔离，启动前阻止正常浏览器和其他仓目录',async t=>{
  const c=await fixture(t),s=new AccountSessions({stateRoot:c.root}),starts=[];
  s.components={env:{defaultEdgeRoot:()=>path.join(c.root,'normal'),discover:async()=>({installations:[{executable:'edge.exe'}],profiles:[]})}};
  s.ensureBrowser=async b=>{starts.push(b);};
  const sd=await s.automaticBinding('山东仓',[]),hb=await s.automaticBinding('河北仓',[{binding:sd}]);
  assert.notEqual(sd.userDataRoot,hb.userDataRoot);assert.deepEqual(await s.automaticBinding('山东仓',[]),sd);
  await assert.rejects(s.automaticBinding('山东仓',[{binding:sd}]),{code:'ACCOUNT_ISOLATION'});
  s.forbiddenRoots=[path.join(c.root,'browsers')];
  await assert.rejects(s.automaticBinding('山东仓',[]),{code:'BROWSER_ISOLATION'});assert.equal(starts.length,3);
});
