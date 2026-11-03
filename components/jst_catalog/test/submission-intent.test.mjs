import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {exportAll,nativeExportEndpoint} from '../src/product-export.mjs';

test('native route matches the same path with query parameters',()=>{
  assert.equal(nativeExportEndpoint(new URL('https://www.erp321.com/erp/webapi/ItemApi/Export/ItemSkuAndInventory?requestId=test')),true);
  assert.equal(nativeExportEndpoint(new URL('https://www.erp321.com/erp/webapi/ItemApi/Export/Other')),false);
});
async function attempt(t,{wrong=false,lost=false}={}){
  const output=await fs.mkdtemp(path.join(os.tmpdir(),'catalog-intent-'));t.after(()=>fs.rm(output,{recursive:true}));
  const page=new EventEmitter(),snapshots=[];page.route=async()=>{};page.unroute=async()=>{};
  page.locator=()=>{throw Error('STOP_BEFORE_DOWNLOAD');};
  const scope={coid:'1',uid:'2',expectedRows:1};
  const body={coid:wrong?'other':'1',uid:'2',data:{type:3,filterType:2,searchCondition:{sku_type:1,enabled:'1,0,-1'}}};
  const request={method:()=> 'POST',postDataJSON:()=>body};
  const response={url:()=> 'https://www.erp321.com/erp/webapi/ItemApi/Export/ItemSkuAndInventory?requestId=test',request:()=>request,ok:()=>true,json:async()=>({code:0,act:0,data:'123'})};
  let clicks=0;
  const frame={locator:()=>({filter:()=>({count:async()=>1,isVisible:async()=>true,evaluate:async()=>false,dispatchEvent:async event=>assert.equal(event,'mouseover')})}),getByText:label=>({count:async()=>1,isVisible:async()=>true,evaluate:async()=>false,
    dispatchEvent:async()=>{
      if(label!=='导出商品')return;
      clicks++;assert.equal(snapshots.at(-1).stage,'SUBMITTING');assert.equal(snapshots.at(-1).submissions,1);
      if(lost)throw Error('connection lost after click');
      // Simulate a native response even if an interception hook did not run.
      queueMicrotask(()=>page.emit('response',response));
    }})};
  await assert.rejects(exportAll(page,{output,queried:{frame,scope},onManifest:async m=>snapshots.push(m)}),wrong?/范围无法核验/:lost?/connection lost/:/STOP_BEFORE_DOWNLOAD/);
  assert.equal(clicks,1);return snapshots;
}
test('accepted native response retains one durable attempt without relying on route hook',async t=>{
  const records=await attempt(t);assert.equal(records.at(-1).stage,'ACCEPTED');assert.equal(records.at(-1).submissions,1);assert.equal(records.at(-1).nativeTaskRef,'123');
});
test('lost response or mismatched observed scope remains unresolved, never zero attempts',async t=>{
  for(const options of [{wrong:true},{lost:true}]){
    const records=await attempt(t,options);assert.equal(records.at(-1).stage,'SUBMISSION_UNKNOWN');assert.equal(records.at(-1).submissions,1);
  }
});
