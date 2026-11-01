import {fail,now} from './common.mjs';
import {purchaseContract} from './purchase-contract.mjs';
import {validateWorkbook} from './workbook-validator.mjs';
import {decimal,sum} from './purchase-create-contract.mjs';

export const RECEIPT_COLUMNS={
  D16:{orderId:1,status:2,store:4,supplier:6,skuCount:7,quantity:8,amount:9,receivedSkuCount:17,receivedQuantity:18,receivedAmount:19},
  D17:{orderId:1,status:2,store:8,supplier:10,name:12,sku:13,unit:16,quantity:19,receivedQuantity:22,amount:27,receivedAmount:32,createdAt:41,updatedAt:42}
};
export async function readReceiptSource(file,sourceId,scope,expectedRows,sha256){
  const contract=purchaseContract(sourceId);contract.captureColumns1Based=RECEIPT_COLUMNS[sourceId];contract.captureSourceRow=true;
  const v=await validateWorkbook(file,{contract,warehouseCode:scope.store,expectedRows,maxCompressedBytes:67108864});
  if(sha256&&v.sha256!==sha256)fail('RECEIPT_FILE_CHANGED','采购收货原件在校验期间变化');
  if(!v.capturedRows.length||sourceId==='D16'&&v.capturedRows.length!==1||v.capturedRows.some(r=>r.orderId!==scope.orderId||r.store!==scope.store||r.supplier!==scope.supplier||!r.status))fail('RECEIPT_SCOPE','采购收货原件的单号、门店、供应商或状态不符');
  return {source:sourceId,file,sha256:v.sha256,rows:v.capturedRows};
}

// Native snapshots keep blank values as blank; only a complete, unambiguous snapshot
// is eligible for the host's existing formal receipt service.
export function receiptObservation(p,binding,runId,sources){
  if(sources.map(s=>s.source).join(',')!=='D16,D17')fail('RECEIPT_FILES','收货观察必须同时保存主单与子单');
  const main=sources[0].rows[0],rows=sources[1].rows,issues=[];
  const add=(code,message,sku=null)=>issues.push({code,message,sku});
  const expected=new Map(p.rows.map(r=>[r.sku,r])),seen=new Set();
  const terminal=main.status==='已完成'||rows.some(r=>r.status==='已完成');
  const number=(v,field,sku)=>{try{return decimal(v);}catch{add('RECEIPT_NUMBER_UNKNOWN',field+'为空或无效',sku);return null;}};
  if(rows.some(r=>r.status!==main.status))add('RECEIPT_STATUS_CONFLICT','主单与子单的单据状态不一致');
  if(/作废|取消|终止|关闭/.test(main.status))add('RECEIPT_ABNORMAL_STATE','平台状态需业务核对：'+main.status);
  if(number(main.skuCount,'采购商品种数')!==String(p.rows.length)||rows.length!==p.rows.length)add('RECEIPT_COVERAGE','采购商品数与已关联付款依据不一致');
  if(number(main.quantity,'采购主单数量')!==decimal(p.totalQuantity)||number(main.amount,'采购主单金额')!==decimal(p.totalAmount))add('RECEIPT_PURCHASE_CHANGED','平台采购数量或金额已改变，不能自动沿用原付款依据');
  for(const r of rows){
    const old=expected.get(r.sku);
    if(!old||seen.has(r.sku)){add('RECEIPT_SKU_CONFLICT','采购商品缺失、重复或不在原关联范围',r.sku);continue;}
    seen.add(r.sku);
    if(number(r.quantity,'采购数量',r.sku)!==decimal(old.quantity)||number(r.amount,'采购金额',r.sku)!==decimal(old.amount))add('RECEIPT_PURCHASE_CHANGED','平台商品采购数量或金额改变',r.sku);
    if(!r.unit?.trim())add('RECEIPT_UNIT_UNKNOWN','基础单位为空',r.sku);
    number(r.receivedQuantity,'实际入库数量',r.sku);number(r.receivedAmount,'单品入库金额',r.sku);
  }
  for(const sku of expected.keys())if(!seen.has(sku))add('RECEIPT_SKU_MISSING','采购明细缺少原商品',sku);
  const rq=number(main.receivedQuantity,'主单入库数量'),ra=number(main.receivedAmount,'主单入库金额');
  const known=(field)=>rows.every(r=>{try{decimal(r[field]);return true;}catch{return false;}});
  if(rq!==null&&known('receivedQuantity')&&rq!==sum(rows.map(r=>r.receivedQuantity)))add('RECEIPT_TOTAL_CONFLICT','主子单实际入库数量合计不一致');
  if(ra!==null&&known('receivedAmount')&&ra!==sum(rows.map(r=>r.receivedAmount)))add('RECEIPT_TOTAL_CONFLICT','主子单入库金额合计不一致');
  return {schema:1,operation:'purchase.receipt.observation',purchaseId:p.id,runId,identity:p.identity,basis:p.basis,
    orderId:binding.orderId,supplierCode:p.supplierCode,observedAt:now(),platformCompletedAt:null,
    terminal,eligible:terminal&&main.status==='已完成'&&issues.length===0,main,rows,issues,
    files:sources.map(({rows,...file})=>file),quantityBasis:'native_actual_inventory_quantity_no_conversion'};
}

export function receiptSummary(o){
  return {runId:o.runId,observedAt:o.observedAt,status:o.main.status,terminal:o.terminal,eligible:o.eligible,
    skuCount:o.rows.length,receivedQuantity:o.main.receivedQuantity,receivedAmount:o.main.receivedAmount,
    issueCount:o.issues.length,issues:o.issues.slice(0,30),issuesTruncated:o.issues.length>30};
}
