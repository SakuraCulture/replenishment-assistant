import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {CatalogService} from '../src/service.mjs';
import {CatalogError,sha,check} from '../src/common.mjs';
import {nativeTaskId,nativeFileUrl} from '../src/product-export.mjs';

const warehouses=['山东仓','河北仓'];
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function fixture(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'catalog-service-')),counts={exports:[],recovers:[],queries:[]},failure={};let number=100;
  const scope=a=>({company:a.expectedCompany,coid:a.expectedCoid,uid:a.expectedUid,expectedRows:1,statuses:['启用','备用','禁用']});
  const download=async(o,recordId)=>{await fs.mkdir(o.output,{recursive:true});const file=path.join(o.output,'source.xlsx');const bytes=Buffer.from('synthetic offline file');await fs.writeFile(file,bytes);return {file,recordId:String(recordId),sha256:sha(bytes),nativePath:'https://jstzbstatic.erp321.com/export/abc.xlsx'};};
  const driver={
    page:async a=>{if(failure[a.warehouse]==='login')throw new CatalogError('LOGIN_REQUIRED','login');return a;},
    query:async(p,a)=>{counts.queries.push(a.warehouse);return {scope:scope(a),frame:{}};},
    export:async(p,o)=>{counts.exports.push(p.warehouse);const n={scope:scope(p),submissions:1,stage:'SUBMITTING'};await o.onManifest(n);
      if(failure[p.warehouse]==='unknown')throw Error('lost response');
      n.nativeTaskRef=String(++number);n.stage='ACCEPTED';await o.onManifest(n);
      if(failure[p.warehouse]==='download')throw Error('download failed');
      Object.assign(n,await download(o,n.nativeTaskRef),{stage:'DOWNLOADED'});await o.onManifest(n);return n;
    },
    recover:async(p,o)=>{counts.recovers.push([p.warehouse,o.recordId]);return download(o,o.recordId);},
    validate:async(file,o)=>{check(o.signal);const candidate=path.join(o.output,'candidate.json');const body=Buffer.from('{}');await fs.writeFile(candidate,body);return {status:'VALIDATED',source_sha256:sha(await fs.readFile(file)),warehouse:o.warehouse,candidate,candidate_sha256:sha(body),rows:1};},
    close:async()=>{},
  };
  const service=await CatalogService.open({stateRoot:root,driver});
  await service.store.write(d=>{d.accounts=Object.fromEntries(warehouses.map((w,i)=>[w,{id:i?'hb':'sd',warehouse:w,expectedCompany:'company'+i,expectedCoid:String(i+1),expectedUid:'99',enabled:true,mappingVerified:true,bindingVersion:1,binding:{profile:'test'+i}}]));});
  t.after(async()=>{await service.close();await fs.rm(root,{recursive:true,force:true});});return {service,counts,failure,root};
}
const request=(operation='operation_1',order=warehouses)=>({action:'sync.start',operation_id:operation,purpose:'default_catalog',items:order.map(warehouse=>({warehouse,expected_catalog_digest:null}))});
test('scheduled catalogs have separate adoption scope and durable replay',async t=>{
  const {service:s,counts}=await fixture(t),r={...request('scheduled_1'),purpose:'scheduled_replenishment'};
  const x=await s.command(r);const b=await finish(s,x.batch_id);
  assert.equal(b.purpose,'scheduled_replenishment');assert.deepEqual(counts.exports,warehouses);
  const i=b.items[0];await s.command({action:'sync.acknowledge_adoption',batch_id:b.id,item_id:i.id,receipt:{warehouse:i.account.warehouse,source_sha256:i.receipt.source_sha256,purpose:r.purpose,run_id:'local_run'}});
  assert.equal((await s.command(r)).batch_id,b.id);assert.equal(counts.exports.length,2);
});
test('stockout analysis owns native receipts and cannot replay as default catalog',async t=>{
  const {service:s,counts}=await fixture(t),r={...request('analysis_1'),purpose:'stockout_analysis'};
  const x=await s.command(r),b=await finish(s,x.batch_id);
  assert.equal(b.purpose,'stockout_analysis');assert.deepEqual(counts.exports,warehouses);
  for(const i of b.items)await s.command({action:'sync.acknowledge_adoption',batch_id:b.id,item_id:i.id,
    receipt:{warehouse:i.account.warehouse,source_sha256:i.receipt.source_sha256,purpose:'stockout_analysis',run_id:'analysis_run'}});
  assert.equal((await s.command(r)).batch_id,b.id);
  await assert.rejects(s.command({...r,purpose:'default_catalog'}),/不同范围/);
  assert.equal(counts.exports.length,2);
});
async function finish(s,bid){
  const until=Date.now()+4000;
  while(s.active){
    for(const i of s.store.batch(bid).items)if(i.stage==='WAIT_HOST'&&!i.observation)
      await s.command({action:'sync.provide_observation',batch_id:bid,item_id:i.id,token:i.observationRequest,observation:{state:null,included:[]}});
    if(Date.now()>until)throw Error('test worker timeout');await sleep(5);
  }
  return s.store.batch(bid);
}
test('same driver handles both accounts in user order and repeated intent never resubmits',async t=>{
  const {service:s,counts}=await fixture(t),r=request('operation_1',[...warehouses].reverse());
  const x=await s.command(r),duplicate=await s.command(r);assert.equal(duplicate.batch_id,x.batch_id);
  const b=await finish(s,x.batch_id);assert.deepEqual(counts.exports,[...warehouses].reverse());assert.ok(b.items.every(i=>i.stage==='READY_TO_ADOPT'));
  await assert.rejects(s.command({...r,purpose:'allocation_baseline'}),/不同范围/);
  const again=await s.command(r);assert.equal(again.batch_id,b.id);assert.equal(counts.exports.length,2);
});
test('login required only pauses one account; resume does not export successful account again',async t=>{
  const {service:s,counts,failure}=await fixture(t);failure['山东仓']='login';const {batch_id}=await s.command(request());let b=await finish(s,batch_id);
  assert.equal(b.items[0].connection_state,'LOGIN_REQUIRED');assert.equal(b.items[1].stage,'READY_TO_ADOPT');
  delete failure['山东仓'];await s.command({action:'sync.resume',batch_id,item_ids:[b.items[0].id]});b=await finish(s,batch_id);
  assert.deepEqual(counts.exports,['河北仓','山东仓']);assert.ok(b.items.every(i=>i.receipt));
});
test('accepted download failure resumes the original task number',async t=>{
  const {service:s,counts,failure}=await fixture(t);failure['山东仓']='download';const {batch_id}=await s.command(request());let b=await finish(s,batch_id);const id=b.items[0].native.nativeTaskRef;
  delete failure['山东仓'];await s.command({action:'sync.resume',batch_id,item_ids:[b.items[0].id]});b=await finish(s,batch_id);
  assert.deepEqual(counts.exports,warehouses);assert.deepEqual(counts.recovers,[['山东仓',id]]);assert.equal(b.items[0].native.nativeTaskRef,id);
});
test('legacy accepted task with zero submission counter still blocks a new export',async t=>{
  const {service:s,counts,failure}=await fixture(t);failure['山东仓']='download';
  const {batch_id}=await s.command(request('legacy_1',['山东仓']));let b=await finish(s,batch_id);
  await s.store.write(d=>{d.batches[batch_id].items[0].native.submissions=0;});
  await assert.rejects(s.command(request('legacy_2',['山东仓'])),/未决/);
  delete failure['山东仓'];await s.command({action:'sync.resume',batch_id});b=await finish(s,batch_id);
  assert.deepEqual(counts.exports,['山东仓']);assert.equal(counts.recovers.length,1);assert.ok(b.items[0].receipt);
});
test('unknown acceptance blocks new intent and resume; other account still completes',async t=>{
  const {service:s,counts,failure}=await fixture(t);failure['山东仓']='unknown';const {batch_id}=await s.command(request());let b=await finish(s,batch_id);
  assert.equal(b.items[0].stage,'SUBMISSION_UNKNOWN');assert.ok(b.items[1].receipt);
  await assert.rejects(s.command(request('operation_2')),/未决/);
  await s.command({action:'sync.resume',batch_id,item_ids:[b.items[0].id]});b=await finish(s,batch_id);assert.deepEqual(counts.exports,warehouses);assert.equal(b.items[0].error.code,'SUBMISSION_UNKNOWN');
});
test('stop responds while work waits, starts no additional accounts, later resume works',async t=>{
  const {service:s,counts}=await fixture(t);const {batch_id}=await s.command(request());await sleep(20);
  const start=Date.now();const stopped=await s.command({action:'sync.stop',batch_id});assert.equal(stopped.stop_requested,true);assert.ok(Date.now()-start<100);
  await finish(s,batch_id);assert.equal(counts.exports.length,0);
  await s.command({action:'sync.resume',batch_id});await finish(s,batch_id);assert.deepEqual(counts.exports,warehouses);
});
test('observation token and adopted hash cannot target another item',async t=>{
  const {service:s}=await fixture(t);const {batch_id}=await s.command(request());await sleep(20);let b=s.store.batch(batch_id);
  await assert.rejects(s.command({action:'sync.provide_observation',batch_id,item_id:b.items[0].id,token:'stale',observation:{}}),/已变化/);
  b=await finish(s,batch_id);await assert.rejects(s.command({action:'sync.acknowledge_adoption',batch_id,item_id:b.items[0].id,receipt:{warehouse:'河北仓',source_sha256:b.items[0].receipt.source_sha256}}),/不符/);
});
test('concurrent starts cannot launch two browser workers',async t=>{
  const {service:s,counts}=await fixture(t);const r=await Promise.allSettled([s.command(request()),s.command(request('operation_2'))]);
  assert.equal(r.filter(x=>x.status==='fulfilled').length,1);await finish(s,r[0].value.batch_id);assert.equal(counts.exports.length,2);
});
test('task IDs and download paths are strict; URLs with secrets are not task IDs',()=>{
  assert.equal(nativeTaskId({taskId:123}), '123');assert.throws(()=>nativeTaskId({id:1,taskId:2}));assert.throws(()=>nativeTaskId('https://example.test/file'));
  assert.equal(nativeFileUrl('https://jstzbstatic.erp321.com/export/abc.xlsx').pathname,'/export/abc.xlsx');
  assert.throws(()=>nativeFileUrl('https://evil.test/export/abc.xlsx'));assert.throws(()=>nativeFileUrl('http://jstzbstatic.erp321.com/export/abc.xlsx'));
});
