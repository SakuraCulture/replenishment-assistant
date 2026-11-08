import {canonical,sha256,fail,idValue,businessDate,countValue} from './common.mjs';

// Native XLSX numeric cells may encode an integer SKU count as "1.0".
// Shared by pair validation and binding; raw evidence stays unchanged.
export function purchaseSkuCount(value){
  if(!/^\d+(?:\.0+)?$/.test(String(value)))fail('PURCHASE_SKU_COUNT','原生主单商品种数无效');
  return countValue(String(value).split('.')[0]);
}

export const PURCHASE_CODES={D16:'SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-FOR-MAIN-ORDER-EXPORT',D17:'SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-MULTI-UNIT-EXPORT'};
export const PURCHASE_LABELS={D16:'采购主单',D17:'采购商品明细'};
const headers={
  D16:['采购单号','单据状态','仓库/门店名称','仓库/门店编码','供应商','供应商编码','采购商品（种）','采购数量（个）','采购金额（元）','外部订单号','下单账号','卖家旺旺','实付金额（元）','商品金额（元）','优惠金额（元）','运费（元）','入库商品（种）','入库数量（个）','入库金额（元）','差异商品（种）','差异数量（个）','差异金额（元）','物流单号','采购模式','制单人'],
  D17:['采购订单号','单据状态','采购模式','采购总金额','采购渠道订单实付总金额','实际入库总金额','仓库/门店名称','仓库/门店编码','供应商名称','供应商编码','卖家店铺','商品名称','商品编码','商品条码','商品规格','基础单位','箱规','包装单位','采购数量（基础单位）','采购数量（包装单位）','1688订单数量','实际入库数量','采购价（基础单位）','上一次采购价','供应商商品编码','采购渠道实际采购价','单品采购金额','采购渠道下单金额','采购渠道优惠金额','采购渠道运费','采购渠道实付金额','单品入库金额','物流单号','采购渠道订单号','单据备注','下单失败原因','买家留言','采购计划单号','采购日期','1688商品标签','创建时间','更新时间','制单人']
};
export function purchaseContract(sourceId){
  if(!PURCHASE_CODES[sourceId])fail('PURCHASE_SOURCE','采购导出仅支持主单与商品明细');
  return {sourceId,contractId:sourceId+'-20261101',nativeSheetName:sourceId==='D16'?'采购主单详情':'采购子单详情',headerRow:1,columns:headers[sourceId].map(rawHeader=>({rawHeader})),keyColumnPositions1Based:sourceId==='D16'?{buyerWarehouseCode:4,skuCode:1}:{buyerWarehouseCode:8,skuCode:13,purchaseOrder:1},captureColumns1Based:sourceId==='D16'?{orderId:1,skuCount:7}:{orderId:1,skuCode:13,createdAt:41,updatedAt:42}};
}
export function dateBounds(from,to){
  for(const d of [from,to]){const ms=Date.parse(d+'T00:00:00Z');if(typeof d!=='string'||!/^\d{4}-\d{2}-\d{2}$/.test(d)||!Number.isFinite(ms)||new Date(ms).toISOString().slice(0,10)!==d)fail('PURCHASE_DATE','采购日期必须是有效的YYYY-MM-DD自然日');}
  if(from>to)fail('PURCHASE_DATE','开始日不能晚于结束日');
  return {start:String(Date.parse(from+'T00:00:00+08:00')),end:String(Date.parse(to+'T23:59:59+08:00'))};
}
export function purchaseDays(from,to){
  dateBounds(from,to);const days=[];for(let ms=Date.parse(from+'T00:00:00Z');ms<=Date.parse(to+'T00:00:00Z');ms+=86400000){days.push(new Date(ms).toISOString().slice(0,10));if(days.length>366)fail('PURCHASE_DATE','单次最多规划366个自然日');}return days;
}
export const previousPurchaseDay=()=>businessDate(new Date(Date.now()-86400000));
export function purchaseScope(input){return {buyerWarehouseCode:idValue(input.buyerWarehouseCode),from:input.from,to:input.to,dateBasis:input.dateBasis||'created',orderId:input.orderId||null,...(input.queryMode==='order_id'?{queryMode:'order_id'}:{})};}
export const purchaseScopeHash=input=>sha256(canonical(purchaseScope(input)));
export function exportScope(request){return Object.fromEntries(Object.entries(request).filter(([k])=>!['pageIndex','pageSize','sort'].includes(k)));}
export function checkPurchaseQuery(request,input){
  if(input.queryMode==='order_id'){
    idValue(input.orderId,'采购单号');
    const actual={...exportScope(request)};
    for(const key of ['storeCodeList','start','end','modifiedStart','modifiedEnd'])if(actual[key]===null||actual[key]===''||Array.isArray(actual[key])&&actual[key].length===0)delete actual[key];
    const expected={orderType:'3',merchantSubBizType:'CONSIGNEE',supplyOrderId:input.orderId};
    if(canonical(actual)!==canonical(expected))fail('PURCHASE_SCOPE_MISMATCH','按单号查询仍携带日期、门店或其他筛选，未采用结果',{expected,actual});
    return expected;
  }
  const {start,end}=dateBounds(input.from,input.to),basis=input.dateBasis||'created';if(!['created','updated'].includes(basis))fail('PURCHASE_DATE_BASIS','时间口径仅支持created制单时间或updated更新时间');
  const expected={storeCodeList:[input.buyerWarehouseCode],orderType:'3',merchantSubBizType:'CONSIGNEE',...(basis==='created'?{start,end}:{modifiedStart:start,modifiedEnd:end}),...(input.orderId?{supplyOrderId:input.orderId}:{})};
  if(canonical(exportScope(request))!==canonical(expected))fail('PURCHASE_SCOPE_MISMATCH','采购页实际筛选与指定仓店、时间口径或单号不同',{expected,actual:exportScope(request)});
  return expected;
}
export function checkPurchaseReceipt(receipt,sourceId,query){
  return receipt?.code===PURCHASE_CODES[sourceId]&&canonical(receipt.scope)===canonical(exportScope(query.request));
}
