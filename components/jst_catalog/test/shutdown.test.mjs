import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {CatalogService} from '../src/service.mjs';
import {JobStore} from '../src/job-store.mjs';
import {watchParent} from '../src/service-lifetime.mjs';

async function fixture(t,driver){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-shutdown-'));
  const service=await CatalogService.open({stateRoot:root,driver});
  await service.store.write(d=>{d.batches.original={id:'original',state:'NEEDS_ATTENTION',nativeTaskRef:'KEEP-EXACT-TASK'};});
  t.after(async()=>{
    await service.store.close();
    assert.equal(path.dirname(root),path.resolve(os.tmpdir()));await fs.rm(root,{recursive:true,force:true});
  });return {root,service};
}

test('browser disconnect failure must still release the task-state guard and preserve jobs',async t=>{
  const {root,service}=await fixture(t,{close:async()=>{throw Error('synthetic browser disconnect failure');}});
  const before=await fs.readFile(path.join(root,'jobs.json'),'utf8');
  await assert.rejects(service.close(),/synthetic browser disconnect failure/);
  const reopened=await JobStore.open(root);
  try{assert.equal(await fs.readFile(path.join(root,'jobs.json'),'utf8'),before);assert.equal(reopened.batch('original').nativeTaskRef,'KEEP-EXACT-TASK');}
  finally{await reopened.close();}
});

test('shutdown waits for the in-flight state commit before releasing ownership',async t=>{
  const {root,service}=await fixture(t,{close:async()=>{}});let finish;
  const controller=new AbortController();service.active={controller,promise:new Promise(r=>{finish=r;})};
  const closing=service.close();await new Promise(r=>setTimeout(r,20));assert(controller.signal.aborted);
  await assert.rejects(JobStore.open(root));
  await service.store.write(d=>{d.batches.original.nativeTaskRef='EXACT-LATE-RECEIPT';});finish();await closing;
  const reopened=await JobStore.open(root);try{assert.equal(reopened.batch('original').nativeTaskRef,'EXACT-LATE-RECEIPT');}finally{await reopened.close();}
});

test('stuck idle browser cleanup is bounded after work settles and cannot retain the store',async t=>{
  const {root,service}=await fixture(t,{close:()=>new Promise(()=>{})});
  await assert.rejects(service.close({disconnectTimeout:25}),{code:'DISCONNECT_TIMEOUT'});
  const reopened=await JobStore.open(root);try{assert.equal(reopened.batch('original').nativeTaskRef,'KEEP-EXACT-TASK');}finally{await reopened.close();}
});

test('simultaneous shutdown signals share one drain and one browser cleanup',async t=>{
  let count=0;const {service}=await fixture(t,{close:async()=>{count++;await new Promise(r=>setTimeout(r,10));}});
  await Promise.all([service.close(),service.close(),service.close()]);assert.equal(count,1);
});

test('busy errors identify the original service; never delete or reset its records',async t=>{
  const {root,service}=await fixture(t,{close:async()=>{}}),before=await fs.readFile(path.join(root,'jobs.json'),'utf8');
  await assert.rejects(JobStore.open(root),e=>e.code==='CATALOG_STATE_BUSY'&&e.details.owner_pid===process.pid&&e.message.includes('进程 '+process.pid));
  assert.equal(await fs.readFile(path.join(root,'jobs.json'),'utf8'),before);assert.equal(service.store.closed,false);
});

test('parent loss notifies once; alive or unknown parent never requests shutdown',async()=>{
  let alive=true,count=0;const stop=watchParent(123,()=>{count++;},{interval:5,alive:()=>alive});
  try{await new Promise(r=>setTimeout(r,15));assert.equal(count,0);alive=false;await new Promise(r=>setTimeout(r,20));assert.equal(count,1);}
  finally{stop();}
  assert.throws(()=>watchParent(process.pid,()=>{}),{code:'PARENT_ID_INVALID'});
});

test('parent loss during a commit only requests cooperative shutdown, preserves its late receipt',async t=>{
  const {root,service}=await fixture(t,{close:async()=>{}});let finish,closing,alive=true;
  service.active={controller:new AbortController(),promise:new Promise(r=>{finish=r;})};
  const stop=watchParent(123,()=>{closing=service.close();},{interval:5,alive:()=>alive});
  try{
    alive=false;await new Promise(r=>setTimeout(r,20));assert(service.active.controller.signal.aborted);
    await assert.rejects(JobStore.open(root),{code:'CATALOG_STATE_BUSY'});
    await service.store.write(d=>{d.batches.original.nativeTaskRef='PERSIST-BEFORE-EXIT';});finish();await closing;
    const reopened=await JobStore.open(root);try{assert.equal(reopened.batch('original').nativeTaskRef,'PERSIST-BEFORE-EXIT');}finally{await reopened.close();}
  }finally{stop();finish();await closing;}
});
