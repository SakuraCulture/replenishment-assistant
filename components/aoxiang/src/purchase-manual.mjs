import path from 'node:path';
import {businessDate,canonical,fail,now,sameIdentity,uid} from './common.mjs';
import {importResult,importSettled,sameImportResult} from './purchase-import-result.mjs';
import {fileHash} from './workbook-validator.mjs';
import {requireHostOrder,requireUnretired} from './order-retirement.mjs';
import {requireHostFacts} from './purchase-host-guard.mjs';
import {portablePath} from './portable-assets.mjs';

export function manualAuthor(operator,reason){
  if(typeof operator!=='string'||operator!==operator.trim()||!operator||operator.length>80||/[\x00-\x1f]/.test(operator)||
    typeof reason!=='string'||reason!==reason.trim()||reason.length<2||reason.length>500||/[\x00-\x1f]/.test(reason))
    fail('MANUAL_IMPORT_INPUT','请填写经办人及核对依据');
}

export async function markedOrders(adapter,record){
  const p=record.intent;
  const from=record.baseline?.from||(record.uploadAttempted?null:businessDate(new Date(record.createdAt)));
  if(!from||record.uploadAttempted&&!Array.isArray(record.baseline?.orderIds))
    fail('BINDING_BASELINE_MISSING','缺少原提交范围，请核对原任务');
  const to=businessDate();
  const current=await adapter.queryCreateOrders({identity:p.identity,buyerWarehouseCode:p.basis.store,name:p.target.displayName,
    from,to,dateBasis:'created',reuseExistingPage:true});
  if(!sameIdentity(current.query?.identity,p.identity))fail('WRONG_IDENTITY','采购候选查询身份变化');
  if(current.query.warehouseCode!==p.basis.store||current.query.total!==current.rows.length||
      canonical(current.query.orderIds)!==canonical(current.rows.map(row=>row.supplyOrderId)))
    fail('PURCHASE_QUERY_CHANGED','未核实原商家和门店的完整采购范围');
  if(current.rows.some(row=>row.buyerWarehouseCode!==p.basis.store||
      typeof row.buyerMessage!=='string'&&row.buyerMessage!==null))
    fail('PURCHASE_QUERY_CHANGED','候选采购单缺少可核验的来源字段');
  return {from,to,marked:current.rows.filter(row=>row.buyerMessage===p.note)};
}

function checkScope(creates,r){
  requireUnretired(creates.db,r.id,r.intent?.basis?.oid,r.intent?.basis?.warehouse);
  requireHostOrder(r.intent.basis.oid,r.intent.basis.warehouse);
}

export async function takeOverPurchase(creates,creationService,id,{sequence,operator,reason,host_facts},signal){
  manualAuthor(operator,reason);
  const r=creates.get(id);checkScope(creates,r);
  if(r.manualHandoff&&r.state==='MANUAL_HANDOFF'&&!r.binding&&!r.additionalTaskIds?.length)return r;
  if(r.sequence!==sequence||r.binding||r.additionalTaskIds?.length||
      r.bindingProposal?.state==='READY'||
      !['RESERVED','PAUSED','FAILED_PRECHECK','NEEDS_REVIEW','ACCEPTED'].includes(r.state))
    fail('MANUAL_IMPORT_PENDING','原任务需先核对，当前无法接管');
  const p=r.intent;
  let failure=null;
  const unattempted=!r.uploadAttempted&&!r.receipt&&['RESERVED','PAUSED','FAILED_PRECHECK'].includes(r.state);
  if(!unattempted&&(!r.receipt||!r.uploadAttempted))
    fail('MANUAL_IMPORT_PENDING','原提交结果待核，请先核对原任务');
  requireHostFacts(p,host_facts);
  const {adapter}=await creationService.context(p,signal);
  if(!unattempted){
    const native=await adapter.purchaseImportTask({taskId:r.receipt.taskId,identity:p.identity,reuseExistingPage:true});
    failure=importResult(native,r.receipt.taskId,p.rows.length);
    if(failure.kind!=='failed'||failure.successCount!==0||failure.failCount!==p.rows.length)
      fail('MANUAL_IMPORT_PENDING','原平台任务未核实为全部失败，请核对已有采购单');
    if(importSettled(r.importResult)&&!sameImportResult(r.importResult,failure))
      fail('MANUAL_IMPORT_PENDING','原受理记录的历次终态不一致，请核对已有采购单');
  }
  const {from,to,marked}=await markedOrders(adapter,r);
  if(marked.length||r.verification?.orderId)fail('MANUAL_IMPORT_ORDER_EXISTS','已发现原采购线索，请先核对并关联原采购');
  if(await fileHash(p.file.path)!==p.file.sha256)fail('PURCHASE_FILE_CHANGED','原采购上传文件已变化');
  const manualHandoff={id:'handoff-'+uid(),operator,reason,at:now(),purchaseId:id,fileSha256:p.file.sha256,
    from,to,taskId:r.receipt?.taskId??null,result:failure,evidence:unattempted?'unsent_and_marker_absent':'failed_and_marker_absent'};
  return requireHostFacts(p,host_facts,()=>{
    checkScope(creates,creates.get(id));
    return creates.update(id,sequence,'MANUAL_HANDOFF',{manualHandoff,
      ...(failure&&!r.importResult?{importResult:failure}:{})},'OPERATOR_IMPORT_HANDOFF');
  });
}

export function exportSource(record,usage){
  if(usage==='manual'&&(record.state!=='MANUAL_HANDOFF'||!record.manualHandoff||record.additionalTaskIds?.length||
      record.binding||['RUNNING','READY','STALE'].includes(record.bindingProposal?.state)))
    fail('MANUAL_IMPORT_NOT_RELEASED','请先确认接管。已关联的原件可导出核对');
  if(usage==='failure'){
    const asset=record.importResult?.failureFile;
    if(!asset||asset.taskId!==record.receipt?.taskId)fail('IMPORT_FAILURE_FILE_MISSING','平台失败原件未留存');
    return asset;
  }
  if(!['manual','check'].includes(usage))fail('MANUAL_IMPORT_INPUT','导出用途无效');
  return {file:record.intent.file.path,sha256:record.intent.file.sha256};
}

export async function originalExportSource(creates,id,{usage,host_facts}){
  const r=creates.get(id);checkScope(creates,r);
  if(usage==='manual')requireHostFacts(r.intent,host_facts);
  const source=exportSource(r,usage);
  return {...source,file:await portablePath(source.file)};
}

export async function recordOriginalExport(creates,id,{usage,output,export_sha256,host_facts}){
  const r=creates.get(id);checkScope(creates,r);
  const source=exportSource(r,usage);
  if(usage==='manual')requireHostFacts(r.intent,host_facts);
  if(!path.isAbsolute(output)||path.extname(output).toLowerCase()!=='.xlsx'||source.sha256!==export_sha256||
      await fileHash(output)!==export_sha256)fail('PURCHASE_FILE_CHANGED','导出的原件摘要、路径或格式不符');
  const receipt={usage,path:output,sha256:export_sha256,handoffId:usage==='manual'?r.manualHandoff.id:null};
  const same=(r.originalExports||[]).find(x=>Object.keys(receipt).every(k=>x[k]===receipt[k]));
  if(same)return same;
  receipt.at=now();
  creates.recordExport(id,r.sequence,receipt);
  return receipt;
}
