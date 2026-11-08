import test from 'node:test';
import assert from 'node:assert/strict';
import {checkPurchaseQuery,purchaseScopeHash,dateBounds} from '../src/purchase-contract.mjs';
import {validatePurchasePair} from '../src/purchase-coordinator.mjs';
import {runKnownPurchase} from '../src/purchase-known-scope.mjs';
const exact={buyerWarehouseCode:'T',orderId:'PO1',from:null,to:null,dateBasis:'created',queryMode:'order_id'};
const request={orderType:'3',merchantSubBizType:'CONSIGNEE',supplyOrderId:'PO1',pageIndex:'1'};
test('known PO query has its own scope and rejects any hidden date/store filter',()=>{
  assert.equal(checkPurchaseQuery(request,exact).supplyOrderId,'PO1');
  assert.equal(checkPurchaseQuery({...request,storeCodeList:[],start:null,end:''},exact).supplyOrderId,'PO1');
  for(const extra of [{start:'1'},{storeCodeList:['T']},{supplyOrderId:'PO2'},{sellerCode:'OTHER'}])assert.throws(()=>checkPurchaseQuery({...request,...extra},exact),{code:'PURCHASE_SCOPE_MISMATCH'});
  assert.notEqual(purchaseScopeHash(exact),purchaseScopeHash({...exact,queryMode:undefined,from:'2026-11-01',to:'2026-11-01'}));
});
test('exact PO native pair permits the true older creation date but still validates all identities and dates',()=>{
  const run={...exact,query:{orderIds:['PO1'],total:1,orderSkuCounts:{PO1:1}}},main={rows:1,capturedRows:[{orderId:'PO1',skuCount:'1'}]},detail={rows:1,capturedRows:[{orderId:'PO1',skuCode:'A',createdAt:'2026-10-01 10:20:30'}]};
  assert.equal(validatePurchasePair(run,main,detail).createdDay,'2026-10-01');
  assert.throws(()=>validatePurchasePair({...run,queryMode:undefined,from:'2026-11-01',to:'2026-11-01'},main,detail),{code:'PURCHASE_FILE_DATE'});
  assert.throws(()=>validatePurchasePair(run,main,{...detail,capturedRows:[{...detail.capturedRows[0],orderId:'OTHER'}]}));
  assert.throws(()=>validatePurchasePair(run,main,{...detail,capturedRows:[{...detail.capturedRows[0],createdAt:''}]}));
});
test('date fallback uses only a proven day, retires only unexported exact query, and cannot replay accepted tasks',async()=>{
  const identity={accountId:'A',merchantCode:'M'},record={id:'r1',queryMode:'order_id',identity,orderId:'PO1',target:{buyerWarehouseCode:'T'},exports:{D16:{stage:'PLANNED'},D17:{stage:'PLANNED'}}},state={rows:()=>[record],getPurchase:()=>record,putPurchase:r=>Object.assign(record,r)},calls=[];
  const engine={config:{tenantBinding:identity},run:async input=>{calls.push(input);if(calls.length===1)throw Object.assign(Error('no clear'),{code:'PURCHASE_EXACT_FILTERS_UNSUPPORTED'});return input;}};
  const r=await runKnownPurchase(engine,state,{target:record.target,orderId:'PO1'},{createdDay:'2026-10-01'});
  assert.equal(r.from,'2026-10-01');assert.equal(r.to,'2026-10-01');assert.equal(r.queryMode,undefined);assert.equal(record.status,'SUPERSEDED');
  calls.length=0;record.exports.D16={stage:'ACCEPTED',taskId:'known'};
  await assert.rejects(()=>runKnownPurchase(engine,state,{target:record.target,orderId:'PO1'},{createdDay:'2026-10-01'}),{code:'PURCHASE_EXACT_FILTERS_UNSUPPORTED'});assert.equal(calls.length,1);
  calls.length=0;record.exports.D16={stage:'PLANNED'};
  await assert.rejects(()=>runKnownPurchase(engine,state,{target:record.target,orderId:'PO1'}),{code:'PURCHASE_EXACT_DATE_REQUIRED'});assert.equal(calls.length,1);
});
