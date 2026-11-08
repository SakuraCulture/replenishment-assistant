import test from 'node:test';
import assert from 'node:assert/strict';
import {selectPurchaseStore,normalizedStoreName} from '../src/purchase-store-selector.mjs';
import {isolatedEdge} from './edge-fixture.mjs';
const input={buyerWarehouseCode:'CXTAC20260613',name:'柴小囤便利超市（安次店）'};
function fixture(names){
  let term,selected=-1;const terms=[];
  const control={click:async()=>{},fill:async v=>{term=v;terms.push(v);},press:async()=>{}};
  const leaves={filter(){return this;},allTextContents:async()=>typeof names==='function'?names(term):names,nth:i=>({click:async()=>{selected=i;}})};
  return {page:{locator:s=>s==='#storeCodeList'?control:leaves},terms,get selected(){return selected;}};
}
test('store code first; exact name normalization permits whitespace/parentheses, never fuzzy shop identity',async()=>{
  assert.equal(normalizedStoreName(input.name),normalizedStoreName(' 柴小囤便利超市 ( 安次店 ) '));
  const f=fixture(['柴小囤便利超市（东阿店）',' 柴小囤便利超市 ( 安次店 ) ']);
  await selectPurchaseStore(f.page,input,200);assert.equal(f.selected,1);assert.deepEqual(f.terms,[input.buyerWarehouseCode]);
});
test('code unsupported falls back to district; wrong shop never selected; duplicates rejected',async()=>{
  const f=fixture(term=>term==='安次'?[input.name]:['其他门店']);await selectPurchaseStore(f.page,input,600);assert.equal(f.selected,0);assert.deepEqual(f.terms,[input.buyerWarehouseCode,'安次']);
  const absent=fixture(['其他门店']);await assert.rejects(()=>selectPurchaseStore(absent.page,input,60),{code:'PURCHASE_STORE_NOT_FOUND'});assert.equal(absent.selected,-1);
  const duplicate=fixture([input.name,input.name]);await assert.rejects(()=>selectPurchaseStore(duplicate.page,input,60),{code:'AMBIGUOUS_TARGET'});assert.equal(duplicate.selected,-1);
});
test('isolated real Edge selects the normalized exact option; no platform traffic', {skip:process.platform!=='win32',timeout:30000},async()=>{
  const f=await isolatedEdge();try{
    const page=f.browser.contexts()[0].pages()[0];await page.context().route('**/*',r=>r.abort());
    await page.setContent('<input id="storeCodeList"><span class="next-tree-node-label" onclick="window.chosen=1">柴小囤便利超市 ( 安次店 )</span><span class="next-tree-node-label">柴小囤便利超市（东阿店）</span>');
    await selectPurchaseStore(page,input,1000);assert.equal(await page.evaluate(()=>window.chosen),1);
  }finally{await f.close();}
});
