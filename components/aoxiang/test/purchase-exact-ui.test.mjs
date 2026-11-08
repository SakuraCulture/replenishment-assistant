import test from 'node:test';
import assert from 'node:assert/strict';
import {PurchaseUI} from '../src/purchase-ui.mjs';
import {isolatedEdge} from './edge-fixture.mjs';

test('offline Edge exact-PO form clears native dates, never selects store, and rejects hidden scope',async()=>{
  const fixture=await isolatedEdge();
  try{
    const page=await fixture.browser.contexts()[0].newPage();
    await page.setContent(`<form><input id="storeCodeList"><span id="date"><input value="2026-11-08"><input value="2026-11-08"><button type="button" class="next-range-picker-symbol-clear" onclick="this.parentElement.querySelectorAll('input').forEach(n=>n.value='')">清空</button></span><span id="modifiedDate"><input value=""><input value=""></span><input id="supplyOrderId"><button type="button">重置</button><button type="button">查询</button></form>`);
    const identity={accountId:'A',merchantCode:'M'},ui={page,identity,timeoutMs:1500,open:async()=>identity,click:l=>l.click(),readFilterState:()=>PurchaseUI.prototype.readFilterState.call({page})};let queries=0,hidden=false;
    ui.queryAction=async action=>{await action();queries++;const values=await page.locator('#date input').evaluateAll(ns=>ns.map(n=>n.value));assert.deepEqual(values,['','']);return {request:{orderType:'3',merchantSubBizType:'CONSIGNEE',supplyOrderId:await page.locator('#supplyOrderId').inputValue(),pageIndex:'1',...(hidden?{storeCodeList:['WRONG']}:{})},rows:[{supplyOrderId:'PO1',buyerWarehouseCode:'T',totalSku:1}],total:1};};
    const input={buyerWarehouseCode:'T',orderId:'PO1',queryMode:'order_id',from:null,to:null};
    const result=await PurchaseUI.prototype.queryPurchase.call(ui,input);assert.deepEqual(result.orderIds,['PO1']);assert.equal(await page.locator('#storeCodeList').inputValue(),'');assert.equal(queries,1);
    hidden=true;await assert.rejects(()=>PurchaseUI.prototype.queryPurchase.call(ui,input),{code:'PURCHASE_SCOPE_MISMATCH'});
    await page.locator('#date').evaluate(e=>{e.querySelector('button').remove();for(const n of e.querySelectorAll('input')){n.value='2026-11-08';n.readOnly=true;}});
    const before=queries;await assert.rejects(()=>PurchaseUI.prototype.queryPurchase.call(ui,input),{code:'PURCHASE_EXACT_FILTERS_UNSUPPORTED'});assert.equal(queries,before);
  }finally{await fixture.close();}
});
