import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {AllocationFlow,allocationDisposition,exportQueryMatches,completeExportReply} from '../src/order-allocation.mjs';
import {sha} from '../src/common.mjs';

const account={warehouse:'河北仓',expectedCompany:'合成企业',expectedCoid:'1',expectedUid:'2',bindingVersion:3};
const binding={warehouse:account.warehouse,shop:'合成店',company:account.expectedCompany,
  coid:'1',uid:'2',version:3,shop_id:'4'};
const spec={warehouse:'河北仓',shop:'合成店',online:'BH'+'A'.repeat(24),internal:'478074',binding,contact:{buyer:'synthetic'}};
const id='1'.repeat(32);

async function setup(t,status='已发货') {
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-allocation-test-'));
  t.after(async()=>{
    // Remove only the resolved directory returned by mkdtemp for this test.
    assert.equal(path.dirname(await fs.realpath(root)),await fs.realpath(os.tmpdir()));
    assert.match(path.basename(root),/^jst-allocation-test-/);
    await fs.rm(root,{recursive:true,force:true});
  });
  let data={};
  const store={read:()=>structuredClone(data),write:async fn=>{fn(data);}};
  const calls=[];
  const driver={page:async()=>{calls.push('page');return {};},query:async()=>({scope:{company:'合成企业',coid:'1',uid:'2'}})};
  const pages={liveContractVerified:true,lookup:async()=>({found:true,internal:spec.internal,online:spec.online,shop:spec.shop,native_status:status}),
    download:async(page,found,scope,signal,before)=>{calls.push('download');await before();return Buffer.from('synthetic original');}};
  return {root,store,calls,driver,pages,flow:new AllocationFlow(driver,{store,root,pages})};
}

test('cancelled and unfinished original never creates an export or payment',async t=>{
  for (const status of ['已取消','发货中(打单拣货)','已客审待财审','被拆分']) {
    const f=await setup(t,status),result=await f.flow.run({spec,operation_id:id},account);
    assert.equal(result.state,allocationDisposition(status)[0]);
    assert.equal(result.binding.coid,'1');
    assert.deepEqual(f.calls,['page']);
    assert.deepEqual(f.store.read(),{});
  }
});

test('unverified production allocation contract cannot dispatch an export even for a sent order',async t=>{
  const f=await setup(t);
  f.pages.liveContractVerified=false;
  const result=await f.flow.run({spec,operation_id:id},account);
  assert.equal(result.state,'SOURCE_UNVERIFIED');
  assert.equal(f.calls.filter(c=>c==='download').length,0);
});

test('a successful original is received once and reused after a coordinator restart',async t=>{
  const f=await setup(t);
  const first=await f.flow.run({spec,operation_id:id},account);
  assert.equal(first.sha,sha(Buffer.from('synthetic original')));
  assert.equal(first.state,'FILE_READY');
  const again=new AllocationFlow(f.driver,{store:f.store,root:f.root,pages:f.pages});
  const second=await again.run({spec,operation_id:id},account);
  assert.equal(second.reused,true);
  assert.equal(f.calls.filter(c=>c==='download').length,1);
});

test('a dispatched export with no download is never replayed by a new operation or process',async t=>{
  const f=await setup(t);
  f.pages.download=async(p,o,s,signal,before)=>{f.calls.push('download');await before();throw Error('connection lost');};
  await assert.rejects(f.flow.run({spec,operation_id:id},account),/connection lost/);
  const restarted=new AllocationFlow(f.driver,{store:f.store,root:f.root,pages:f.pages});
  await assert.rejects(restarted.run({spec,operation_id:'2'.repeat(32)},account),{code:'ALLOCATION_DOWNLOAD_UNKNOWN'});
  assert.equal(f.calls.filter(c=>c==='download').length,1);
});

test('crash after original receipt but before central state commit recovers that file without another export',async t=>{
  const f=await setup(t),write=f.store.write;
  let fail=true;
  f.store.write=async fn=>{
    const next=f.store.read();fn(next);
    if(fail&&Object.values(next.allocationOperations||{}).some(op=>op.state==='FILE_READY')) {
      fail=false;throw Error('synthetic central state failure');
    }
    return write(fn);
  };
  await assert.rejects(f.flow.run({spec,operation_id:id},account),/synthetic central state failure/);
  assert.equal(f.store.read().allocationOperations[id].state,'DISPATCHING');
  const next=new AllocationFlow(f.driver,{store:f.store,root:f.root,pages:f.pages});
  const recovered=await next.run({spec,operation_id:'2'.repeat(32)},account);
  assert.equal(recovered.reused,true);
  assert.equal(recovered.sha,sha(Buffer.from('synthetic original')));
  assert.equal(f.calls.filter(c=>c==='download').length,1);
});

test('account change, wrong original and changed cached bytes stop before export or reuse',async t=>{
  const f=await setup(t);
  await assert.rejects(f.flow.run({spec:{...spec,binding:{...binding,coid:'99'}},operation_id:id},account),{code:'ACCOUNT_CHANGED'});
  f.pages.lookup=async()=>({found:true,internal:'999',online:spec.online,shop:spec.shop,native_status:'已发货'});
  await assert.rejects(f.flow.run({spec,operation_id:id},account),{code:'ALLOCATION_SCOPE'});
  assert.equal(f.calls.filter(c=>c==='download').length,0);
  f.pages.lookup=async()=>({found:true,internal:spec.internal,online:spec.online,shop:spec.shop,native_status:'已发货'});
  const done=await f.flow.run({spec,operation_id:id},account);
  await fs.writeFile(done.path,'changed');
  await assert.rejects(f.flow.run({spec,operation_id:id},account),{code:'ALLOCATION_FILE'});
  assert.equal(f.calls.filter(c=>c==='download').length,1);
});

test('native query requires exact internal and online keys and rejects widened filters',()=>{
  function request(filters){return {method:()=> 'POST',url:()=> 'https://www.erp321.com/app/order/export/list.aspx',
    postData:()=>new URLSearchParams({__CALLBACKID:'JTable1',__CALLBACKPARAM:JSON.stringify({Method:'LoadDataToJSON',Args:[0,JSON.stringify(filters)]})}).toString()};}
  const filters=[{k:'o_id',v:spec.internal,c:'@='},{k:'so_id',v:spec.online,c:'@='}];
  const url='https://www.erp321.com/app/order/export/list.aspx';
  assert.equal(exportQueryMatches(request(filters),url,spec),true);
  assert.equal(exportQueryMatches(request(filters.slice(1)),url,spec),false);
  assert.equal(exportQueryMatches(request([...filters,{k:'status',v:'sent',c:'@='}]),url,spec),false);
  assert.equal(exportQueryMatches(request(filters.map(f=>({...f,c:'like'}))),url,spec),false);
});

test('reply must prove unique complete first page, no login redirect or hidden later page',async()=>{
  function response(patch={}){return {ok:()=>true,text:async()=> '1|'+JSON.stringify({IsSuccess:true,ReturnValue:JSON.stringify({datas:[{}],dp:{PageIndex:1,IsLast:true},...patch})})};}
  await completeExportReply(response());
  await assert.rejects(completeExportReply(response({datas:[{},{}]})),{code:'ALLOCATION_SCOPE'});
  await assert.rejects(completeExportReply(response({dp:{PageIndex:1,IsLast:false}})),{code:'ALLOCATION_SCOPE'});
});
