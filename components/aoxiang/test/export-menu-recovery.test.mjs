import test from 'node:test';
import assert from 'node:assert/strict';
import {isolatedEdge} from './edge-fixture.mjs';
import {AoxiangUI,dispatchExportOption} from '../src/aoxiang-ui.mjs';

test('已观察到请求时，即使菜单准备失败也不降为未派发',async()=>{
  const ui={spentIntents:new Set(),requestIntents:new Map(),receipts:[],preflightScope:async()=>{},activate:async()=>{},waitResponse:()=>Promise.resolve({})};
  ui.page={locator:()=>({getByRole:()=>({hover:async()=>{ui.requestIntents.set({},'intent-observed');throw Error('菜单失败但已有请求');}})})};
  await assert.rejects(()=>AoxiangUI.prototype.submitOnce.call(ui,{intentId:'intent-observed',expected:{}}),{code:'SUBMISSION_UNKNOWN'});
  assert.equal(ui.activeIntentId,null);
});

test('隔离Edge：准备后的菜单收起，提交时重新展开，恰好一个原生请求',{timeout:30000},async()=>{
  const f=await isolatedEdge();let ui;let requests=0;
  try{
    const page=f.browser.contexts()[0].pages()[0];
    await page.route('**/*',r=>{requests++;return r.fulfill({status:200,contentType:'application/json',headers:{'access-control-allow-origin':'*'},body:'{"success":true,"data":{"taskId":"exact-task"}}'});});
    await page.setContent(`<main id="suggestTable"><button onmouseenter="document.querySelector('#option').style.display='block'">导出</button></main>
      <div id="option" style="display:none" onclick="window.clicks++;fetch('https://tc-supply-chain-portal.ele.me/ebai/gei/create/export/task',{method:'POST',body:JSON.stringify({code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',query:JSON.stringify({buyerWarehouseCodeList:['W1']})})})">全部信息导出</div><script>window.clicks=0</script>`);
    ui=new AoxiangUI(page,{timeoutMs:1000});ui.preflightScope=async()=>{};
    await page.locator('#suggestTable button').hover();
    await page.mouse.move(500,500);await page.locator('#option').evaluate(e=>e.style.display='none');
    const result=await ui.submitOnce({intentId:'intent-menu',expected:{warehouseCode:'W1'}});
    assert.equal(result.taskId,'exact-task');assert.equal(await page.evaluate(()=>clicks),1);assert.equal(requests,1);
    await assert.rejects(()=>ui.submitOnce({intentId:'intent-menu',expected:{warehouseCode:'W1'}}),{code:'INTENT_ALREADY_DISPATCHED'});
    assert.equal(await page.evaluate(()=>clicks),1);
  }finally{await ui?.dispose();await f.close();}
});

test('隔离Edge：隐藏、禁用及遮挡均不派发导出；派发后异常不能称为未提交',{timeout:30000},async()=>{
  const f=await isolatedEdge();
  try{
    const page=f.browser.contexts()[0].pages()[0];
    for(const style of ['display:none','visibility:hidden','']){
      await page.setContent(`<button id="x" style="${style}" ${style?'':'disabled'} onclick="window.clicks++">导出</button><script>window.clicks=0</script>`);
      await assert.rejects(()=>dispatchExportOption(page.locator('#x'),100),{code:'EXPORT_NOT_DISPATCHED'});
      assert.equal(await page.evaluate(()=>clicks),0);
    }
    await page.setContent('<button id="x" onclick="window.clicks++">导出</button><div style="position:fixed;inset:0;background:white"></div><script>window.clicks=0</script>');
    await assert.rejects(()=>dispatchExportOption(page.locator('#x'),100),{code:'EXPORT_NOT_DISPATCHED'});
    assert.equal(await page.evaluate(()=>clicks),0);
    const locator={waitFor:async()=>{},count:async()=>1,isEnabled:async()=>true,evaluate:async()=>{throw Error('模拟派发期间断开');}};
    await assert.rejects(()=>dispatchExportOption(locator),e=>e.code!=='EXPORT_NOT_DISPATCHED');
  }finally{await f.close();}
});
