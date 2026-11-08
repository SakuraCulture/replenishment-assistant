import {fail} from './common.mjs';

export const normalizedStoreName=value=>String(value||'').normalize('NFKC').replace(/\s+/gu,'');

// Only exact normalized display names are selectable. The existing response
// contract subsequently verifies the exact store CODE before using any results.
export async function selectPurchaseStore(page,input,timeoutMs=30000,click=locator=>locator.click()){
  const control=page.locator('#storeCodeList');
  const group=[...input.name.matchAll(/[（(]([^（）()]+)[）)]/g)].at(-1)?.[1];
  const terms=[...new Set([input.buyerWarehouseCode,group?.replace(/(?:门店|店|仓)$/,''),input.name].filter(Boolean))];
  const wanted=normalizedStoreName(input.name),deadline=Date.now()+timeoutMs;
  for(const term of terms){
    await click(control);await control.fill(term);
    const until=Math.min(deadline,Date.now()+Math.min(5000,Math.max(1,timeoutMs/terms.length)));
    while(Date.now()<until){
      const leaves=page.locator('.next-tree-node-label').filter({visible:true});
      const names=await leaves.allTextContents();
      const hits=names.map((name,index)=>normalizedStoreName(name)===wanted?index:-1).filter(i=>i>=0);
      if(hits.length>1)fail('AMBIGUOUS_TARGET','采购门店名称对应多个选项，停止查询，请核对门店编码',{storeCode:input.buyerWarehouseCode});
      if(hits.length===1){await click(leaves.nth(hits[0]));await control.press('Escape');return;}
      await new Promise(resolve=>setTimeout(resolve,100));
    }
    if(Date.now()>=deadline)break;
  }
  fail('PURCHASE_STORE_NOT_FOUND','采购页未定位到门店，请核验门店目录和页面加载后继续原任务；不要重新上传',
    {storeCode:input.buyerWarehouseCode,storeName:input.name,searchTerms:terms});
}
