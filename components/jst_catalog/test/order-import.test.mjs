import test from 'node:test';
import assert from 'node:assert/strict';
import {CatalogService} from '../src/service.mjs';

const online='BH'+'A'.repeat(24);
const warehouse='河北仓';
const account={id:'hb',warehouse,expectedCompany:'合成河北仓',expectedCoid:'123',expectedUid:'45',
  mappingVerified:true,bindingVersion:2};
const shop='合成店铺';
const spec={warehouse,shop,online,internal:'802'};

function setup(){
  const calls=[];
  const data={accounts:{[warehouse]:{...account}},batches:{},operations:{}};
  const driver={
    page:async a=>{calls.push(['page',a.warehouse]);return {};},
    query:async()=>({scope:{company:account.expectedCompany,coid:account.expectedCoid,uid:account.expectedUid}}),
  };
  const orderPages={
    openImport:async()=>{calls.push(['open']);return {shop_id:'100'};},
    preview:async()=>{calls.push(['preview']);return {shop_id:'100',online,shop,lines:[],amount:'1',contact_ok:true};},
    lookup:async()=>({found:true,shop_id:'100',online,shop,internal:'802',status:'awaiting'}),
    finishImport:async()=>{calls.push(['submit']);},
    finishAudit:async()=>{calls.push(['audit']);},
  };
  const service=new CatalogService({store:{read:()=>data},driver,orderPages});
  return {service,calls,data,driver,orderPages};
}

test('default component checks actual account identity and the exact shop on preview',async()=>{
  const {service,calls,driver}=setup();
  assert.equal((await service.command({action:'order.capabilities'})).preview,true);
  const r=await service.command({action:'order.probe',spec});
  assert.equal(r.binding.warehouse,warehouse);
  assert.equal(r.binding.shop_id,'100');
  const p=await service.command({action:'order.preview',spec});
  assert.equal(p.binding.shop_id,'100');
  await assert.rejects(service.command({action:'order.stage_import',spec:{...spec,binding:{...p.binding,shop_id:'101'}}}),{code:'ACCOUNT_CHANGED'});
  driver.query=async()=>({scope:{company:'另一个公司',coid:'123',uid:'45'}});
  await assert.rejects(service.command({action:'order.stage_import',spec}),{code:'ACCOUNT_CHANGED'});
  assert.equal(calls.filter(([op])=>op==='submit'||op==='audit').length,0);
});

test('simulation accepts one release of the exact import and ordinary audit',async()=>{
  const {service,calls}=setup();
  const p=await service.command({action:'order.stage_import',spec});
  await service.command({action:'order.import',warehouse,online,permit:p.permit});
  await assert.rejects(service.command({action:'order.import',warehouse,online,permit:p.permit}),{code:'PREVIEW_CHANGED'});
  const p2=await service.command({action:'order.stage_audit',spec:{...spec,binding:p.binding}});
  await service.command({action:'order.audit',warehouse,online,internal:'802',permit:p2.permit});
  assert.equal(calls.filter(([op])=>op==='submit').length,1);
  assert.equal(calls.filter(([op])=>op==='audit').length,1);
});

test('account changes, wrong internal number and lease release invalidate permits',async()=>{
  const {service,data,calls}=setup();
  let p=await service.command({action:'order.stage_import',spec});
  data.accounts[warehouse].bindingVersion++;
  await assert.rejects(service.command({action:'order.import',warehouse,online,permit:p.permit}),{code:'ACCOUNT_CHANGED'});
  p=await service.command({action:'order.stage_audit',spec});
  await assert.rejects(service.command({action:'order.audit',warehouse,online,internal:'803',permit:p.permit}),{code:'PREVIEW_CHANGED'});
  p=await service.command({action:'order.stage_import',spec});
  assert.equal((await service.command({action:'session.release'})).idle,true);
  await assert.rejects(service.command({action:'order.import',warehouse,online,permit:p.permit}),{code:'PREVIEW_CHANGED'});
  assert.equal(calls.filter(([op])=>op==='submit'||op==='audit').length,0);
});

test('an active native order action makes the shared browser busy',async()=>{
  const {service,orderPages}=setup();
  let waiting,release;
  const started=new Promise(resolve=>{waiting=resolve;});
  orderPages.preview=async()=>{waiting();return new Promise(resolve=>{release=resolve;});};
  const running=service.command({action:'order.preview',spec});
  await started;
  const status=await service.command({action:'sync.status'});
  assert.equal(status.active.kind,'order');
  const stopped=await service.command({action:'sync.stop'});
  assert.equal(stopped.still_running,true);
  release({shop_id:'100',online});
  await assert.rejects(running,{code:'CANCELLED'});
  assert.equal((await service.command({action:'session.release'})).idle,true);
});
