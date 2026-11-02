import test from 'node:test';
import assert from 'node:assert/strict';
import {PurchaseUI} from '../src/purchase-ui.mjs';

function fixture(body,{empty=true,zero=true,duplicate=false,http=200}={}){
  const visited=[];
  const response={status:()=>http,text:async()=>JSON.stringify(body)};
  const request={response:async()=>response,postData:()=>'{"buyerWarehouseCodeList":["TSD20251211"]}'};
  const page={waitForRequest:async()=>request,getByText:text=>{
    const key=typeof text==='string'?'empty':'zero';visited.push(key);
    const locator={filter:()=>locator,waitFor:async()=>{if(!(key==='empty'?empty:zero))throw Error('no visible empty proof');},count:async()=>duplicate?2:1};return locator;
  }};
  const ui={page,timeoutMs:100,isBusinessResponse:()=>true};
  return {run:()=>PurchaseUI.prototype.queryAction.call(ui,async()=>{}),visited};
}
test('observed success null/null requires visible empty table AND exact zero total',async()=>{
 const f=fixture({success:true,status:true,data:null,total:null});
 const r=await f.run();assert.deepEqual(r.rows,[]);assert.equal(r.total,0);
 assert.deepEqual(f.visited,['empty','zero']);assert.equal(r.emptyProof,'success_null_pair_and_visible_empty_zero');
});
test('ordinary list and explicit [] zero preserve response types/order',async()=>{
 for(const data of [[],[{supplyOrderId:'002'},{supplyOrderId:'001'}]]){
  const f=fixture({success:true,data,total:data.length});const r=await f.run();
  assert.deepEqual(r.rows,data);assert.equal(r.total,data.length);assert.deepEqual(f.visited,[]);
 }
});
test('null is not an unconditional zero; failed, partial, missing and mismatching results rejected',async()=>{
 for(const body of [
  {success:false,status:false,data:null,total:null},{success:true,data:null,total:null},
  {success:true,status:true,data:null,total:1},{success:true,status:true,data:[],total:null},
  {success:true,status:true},{success:true,status:true,data:{},total:null}
 ])await assert.rejects(fixture(body).run());
});
test('UI proof absent, nonzero, ambiguous or unsuccessful HTTP rejected',async()=>{
 for(const opts of [{empty:false},{zero:false},{duplicate:true},{http:500}])
  await assert.rejects(fixture({success:true,status:true,data:null,total:null},opts).run());
});
