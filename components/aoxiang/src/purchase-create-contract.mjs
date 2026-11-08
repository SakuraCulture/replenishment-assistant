import {dateBounds,purchaseSkuCount} from './purchase-contract.mjs';
import {canonical,sha256,fail,countValue,businessDate} from './common.mjs';

export const IMPORT_CODE='SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-MULTI-UNIT-IMPORT-V2';
export const taskPattern=new RegExp('^GEI@'+IMPORT_CODE+'@\\d{12}@\\d+$');
const hash=/^[a-f0-9]{64}$/;
const text=(v,max=256)=>typeof v==='string'&&v.length>0&&v.length<=max&&v===v.trim()&&!/[\x00-\x1f]/.test(v);
// Compare decimal facts exactly; no binary float, unit conversion, or tolerance.
export function decimal(v){
  if(typeof v!=='string'||v.length>100||!/^\d+(?:\.\d+)?$/.test(v))fail('PURCHASE_DECIMAL','采购数量或金额必须是非负十进制文本');
  const [i,f='']=v.split('.'),integer=i.replace(/^0+(?=\d)/,''),fraction=f.replace(/0+$/,'');
  return integer+(fraction?'.'+fraction:'');
}
export function sum(values){
  const parts=values.map(v=>decimal(v).split('.')),scale=Math.max(0,...parts.map(v=>(v[1]||'').length));
  const total=parts.reduce((a,[i,f=''])=>a+BigInt(i+f.padEnd(scale,'0')),0n).toString().padStart(scale+1,'0');
  return decimal(scale?total.slice(0,-scale)+'.'+total.slice(-scale):total);
}
export const PRICE_POLICY='unit_price_half_up_2_v1';
function fixed(n,scale){
  const sign=n<0n?'-':'';const s=(n<0n?-n:n).toString().padStart(scale+1,'0');
  return sign+(scale?s.slice(0,-scale)+'.'+s.slice(-scale):s);
}
function signed(v){return v.startsWith('-')&&decimal(v.slice(1))!=='0'?'-'+decimal(v.slice(1)):decimal(v.replace(/^\-/,''));}
export function roundedPurchaseRow(quantity,paymentAmount){
  const [qi,qf='']=decimal(quantity).split('.'),[ai,af='']=decimal(paymentAmount).split('.');
  const q=BigInt(qi+qf),a=BigInt(ai+af),qs=10n**BigInt(qf.length),as=10n**BigInt(af.length);
  if(!q)fail('PURCHASE_DECIMAL','采购数量必须大于0');
  const numerator=a*qs*100n,denominator=as*q;
  const cents=numerator/denominator+(2n*(numerator%denominator)>=denominator?1n:0n);
  const product=cents*q,amount=product/qs+(2n*(product%qs)>=qs?1n:0n);
  const scale=Math.max(2,af.length),delta=amount*10n**BigInt(scale-2)-a*10n**BigInt(scale-af.length);
  return {unitPrice:fixed(cents,2),amount:fixed(amount,2),roundingDelta:signed(fixed(delta,scale))};
}
export function validatePurchaseIntent(p){
  if(!p||p.schema!==1||p.operation!=='purchase.create'||!text(p.id,80)||!/^[A-Za-z0-9][A-Za-z0-9._-]*$/.test(p.id))fail('PURCHASE_INTENT_SCHEMA','采购创建任务格式或编号无效');
  if(!p.identity||Object.keys(p.identity).sort().join(',')!=='accountId,merchantCode'||!text(p.identity.accountId)||!text(p.identity.merchantCode)||!hash.test(p.mappingVersion||''))fail('PURCHASE_INTENT_SCHEMA','采购任务缺少已核验商家与门店版本');
  const b=p.basis;
  if(!b||!text(b.number)||!text(b.warehouse)||!text(b.store)||!hash.test(b.planFingerprint||'')||!hash.test(b.paymentDigest||'')||!Number.isSafeInteger(b.paymentVersion)||b.paymentVersion<1)fail('PURCHASE_INTENT_SCHEMA','采购任务缺少本地订单及付款版本依据');
  if(!p.target||p.target.buyerWarehouseCode!==b.store||!text(p.target.displayName)||!text(p.supplierCode))fail('PURCHASE_INTENT_SCHEMA','门店编码或供应商编码无效');
  if(p.marker!=='AX_CREATE:'+p.id||p.note!==p.marker+' 本地单:'+b.number||p.createExternalOrder!==false||p.postCreationState!=='待提交')fail('PURCHASE_INTENT_SCHEMA','采购创建范围不符：不得创建外部订单或自动提交');
  if(!p.file||!text(p.file.path,2048)||!hash.test(p.file.sha256||'')||!Number.isSafeInteger(p.file.bytes)||p.file.bytes<100||p.file.bytes>10485760)fail('PURCHASE_INTENT_SCHEMA','采购文件凭据无效');
  if(!p.template||!text(p.template.recordFile,2048)||!hash.test(p.template.sha256||'')||!text(p.template.attemptId,80))fail('PURCHASE_INTENT_SCHEMA','缺少最新原生模板获取记录');
  if(!Array.isArray(p.rows)||!p.rows.length||p.rows.length>20000)fail('PURCHASE_INTENT_SCHEMA','采购商品数无效或超出本机保护值');
  if(p.pricePolicy!==undefined&&p.pricePolicy!==PRICE_POLICY)fail('PURCHASE_PRICE_POLICY','采购单价舍入规则未支持');
  const seen=new Set();for(const r of p.rows){
    if(!r||!text(r.sku)||seen.has(r.sku)||typeof r.name!=='string'||r.name.length>1024||decimal(r.quantity)==='0')fail('PURCHASE_INTENT_SCHEMA','采购商品编码、名称或数量不符，不能合并猜测');
    decimal(r.amount);
    if(p.pricePolicy===PRICE_POLICY){
      const expected=roundedPurchaseRow(r.quantity,r.paymentAmount);
      if(r.unitPrice!==expected.unitPrice||decimal(r.amount)!==decimal(expected.amount)||typeof r.roundingDelta!=='string'||signed(r.roundingDelta)!==expected.roundingDelta)fail('PURCHASE_PRICE_POLICY','采购单价、金额及舍入差额与原付款不符');
    }
    seen.add(r.sku);
  }
  if(sum(p.rows.map(r=>r.quantity))!==decimal(p.totalQuantity)||sum(p.rows.map(r=>r.amount))!==decimal(p.totalAmount))fail('PURCHASE_INTENT_SCHEMA','采购逐项数量金额与汇总不一致');
  if(p.pricePolicy===PRICE_POLICY){
    if(sum(p.rows.map(r=>r.paymentAmount))!==decimal(p.paymentTotalAmount))fail('PURCHASE_PRICE_POLICY','原付款汇总不符');
    const [ai,af='']=decimal(p.totalAmount).split('.'),[bi,bf='']=decimal(p.paymentTotalAmount).split('.'),scale=Math.max(af.length,bf.length);
    const delta=BigInt(ai+af.padEnd(scale,'0'))-BigInt(bi+bf.padEnd(scale,'0'));
    if(typeof p.roundingDelta!=='string'||signed(p.roundingDelta)!==signed(fixed(delta,scale)))fail('PURCHASE_PRICE_POLICY','采购舍入差额汇总不符');
  }
  return p;
}
export const orderKey=p=>sha256(canonical({identity:p.identity,number:p.basis.number,warehouse:p.basis.warehouse,store:p.basis.store}));
export function checkImportReceipt(p,r){
  if(!r||r.intentId!==p.id||!taskPattern.test(r.taskId||'')||r.code!==IMPORT_CODE||r.fileSha256!==p.file.sha256||r.exactSourceBytesPresent!==true||r.importRequests!==1||!text(r.requestSha256,64)||!hash.test(r.requestSha256))fail('IMPORT_RECEIPT_MISMATCH','导入回执未准确绑定本次文件和唯一提交，保留待核，不重复上传');
  return r;
}
// Evidence is supplied by the exact known-task query and validated native D16/D17 files.
// A marker or a similar amount alone is never sufficient to bind a platform order.
export function checkCreatedPurchase(p,receipt,e){
  checkImportReceipt(p,receipt);
  if(!e||canonical(e.identity)!==canonical(p.identity)||e.task?.taskId!==receipt.taskId||e.task?.code!==IMPORT_CODE||e.task?.status!=='FINISHED'||countValue(e.task.failCount)!==0||countValue(e.task.successCount)!==p.rows.length)fail('PURCHASE_TASK_NOT_COMPLETE','原导入任务尚未核实全量成功，不绑定采购单');
  const status=e.main?.status;
  if(!['待提交','已作废'].includes(status))fail('PURCHASE_STATUS_UNSUPPORTED','原采购单当前状态不在自动关联范围，请核对平台原单',{status});
  // A void is a verified historical association, never a successful new purchase
  // or a released reservation. The native main AND every SKU must prove it.
  if(e.listingStatus!==undefined&&((status==='待提交')!==(e.listingStatus==='0')))fail('PURCHASE_STATUS_CHANGED','采购查询状态与原生核对文件状态不同，请继续核对原单',{listingStatus:e.listingStatus,nativeStatus:status});
  checkNativePurchase(p,e,{status,requireNote:true});
  return {orderId:e.candidates[0],status,taskId:receipt.taskId,files:e.files,verifiedAt:e.observedAt,method:'known_import_task_and_exact_native_rows'};
}

// Both automatic and operator-confirmed associations share the same per-SKU checks.
export function checkNativePurchase(p,e,{status,requireNote}){
  if(!e||canonical(e.identity)!==canonical(p.identity)||!text(status))fail('PURCHASE_CREATED_MISMATCH','采购单身份或状态缺失');
  if(!Array.isArray(e.candidates)||e.candidates.length!==1)fail('PURCHASE_NOT_UNIQUE','未唯一定位新采购单；意外多单或缺单只待核，不重传');
  const m=e.main,id=e.candidates[0];
  if(!text(id)||!m||m.orderId!==id||m.store!==p.basis.store||m.supplier!==p.supplierCode||m.status!==status||m.externalOrderId!=='')fail('PURCHASE_CREATED_MISMATCH','采购单身份、状态或外部订单不符合核对范围');
  if((requireNote&&m.marker!==p.note)||decimal(m.quantity)!==decimal(p.totalQuantity)||decimal(m.amount)!==decimal(p.totalAmount)||purchaseSkuCount(m.skuCount)!==p.rows.length)fail('PURCHASE_CREATED_MISMATCH','采购主单来源、数量或金额与付款依据不符');
  if(!Array.isArray(e.rows)||e.rows.length!==p.rows.length)fail('PURCHASE_DETAIL_MISMATCH','采购商品明细覆盖不完整');
  const expected=new Map(p.rows.map(r=>[r.sku,r])),seen=new Set();
  for(const r of e.rows){
    const old=expected.get(r.sku);
    if(!old||seen.has(r.sku)||r.orderId!==id||r.store!==p.basis.store||r.supplier!==p.supplierCode||r.status!==status||(requireNote&&r.note!==p.note)||decimal(r.quantity)!==decimal(old.quantity)||decimal(r.amount)!==decimal(old.amount))fail('PURCHASE_DETAIL_MISMATCH','采购商品编码、数量、金额或来源不符，不能只比汇总');
    seen.add(r.sku);
  }
  if(!Array.isArray(e.files)||e.files.length!==2||e.files.map(f=>f.source).join(',')!=='D16,D17'||e.files.some(f=>!hash.test(f.sha256||'')||!text(f.file,2048)))fail('PURCHASE_PROOF_MISSING','缺少已验收主单及子单原文件依据');
  return true;
}

export function bindingRequest(request){
  if(!request||Object.keys(request).sort().join(',')!=='created_day,order_id,reason'||typeof request.order_id!=='string'||!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(request.order_id))fail('BINDING_INPUT','请填写准确采购单号，不支持模糊匹配或多单');
  if(request.created_day!==''&&request.created_day!==null)dateBounds(request.created_day,request.created_day);
  if(request.created_day&&request.created_day>businessDate()||typeof request.reason!=='string'||request.reason!==request.reason.trim()||request.reason.length<2||request.reason.length>500||/[\x00-\x1f]/.test(request.reason))fail('BINDING_INPUT','请填写准确采购单号及2至500字核对原因；制单日可留空按单号精查');
  return request;
}
export function bindingEligible(r){return ['RESERVED','PAUSED','FAILED_PRECHECK','NEEDS_REVIEW'].includes(r.state)&&!r.receipt&&!r.additionalTaskIds?.length;}
export function canBind(r){
  if(!bindingEligible(r))fail('BINDING_NOT_ELIGIBLE','仅可核对未上传预览或无回执待核任务；已有回执请继续原任务，不绕过冲突或改绑');
}
export function checkedBinding(r,proof){
  canBind(r);bindingRequest(proof?.request);
  if(proof.schema!==1||proof.operation!=='purchase.binding.proof'||proof.purchaseId!==r.id||canonical(proof.basis)!==canonical(r.intent.basis)||proof.requireNote!==r.uploadAttempted||typeof proof.runId!=='string'||!proof.runId)fail('BINDING_PROOF','关联依据不属于原采购付款任务');
  const e=proof.evidence;
  if(e?.candidates?.[0]!==proof.request.order_id)fail('BINDING_PROOF','输入采购单号与已核对单号不同');
  checkNativePurchase(r.intent,e,{status:e.main?.status,requireNote:r.uploadAttempted});
  if(r.uploadAttempted&&(proof.uniqueMarkedOrders?.length!==1||proof.uniqueMarkedOrders[0]!==proof.request.order_id))fail('PURCHASE_NOT_UNIQUE','原上传标记下未唯一核实一张采购单，不能人工跳过多单待核');
  return {orderId:proof.request.order_id,status:e.main.status,taskId:null,files:e.files,verifiedAt:e.observedAt,
          method:'operator_confirmed_exact_native_rows',createdDay:proof.request.created_day||proof.nativeCreatedDay||null,reason:proof.request.reason};
}
