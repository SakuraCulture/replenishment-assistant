import {DatabaseSync} from 'node:sqlite';
import {canonical,fail} from './common.mjs';

const unsentStates = new Set(['RESERVED','PAUSED','FAILED_PRECHECK','CANCELLED_BEFORE_UPLOAD','SUPERSEDED']);
const exists=(db, name)=>db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name);

function hasLocalReceipt(basis){
  const path=process.env.REPLENISHMENT_BUSINESS_DB;
  if(!path)return false; // Isolated standalone component has no host facts.
  const host=new DatabaseSync(path,{readOnly:true});
  try{
    return !!exists(host,'lifecycle_versions') && !!host.prepare(`
      SELECT 1 FROM lifecycle_versions WHERE oid=? AND warehouse=? AND stage='receipt'
      AND coalesce(json_extract(payload,'$.pending'),0)=0 LIMIT 1`).get(basis.oid,basis.warehouse);
  }finally{host.close();}
}

export function requireReplaceable(db, original){
  const {id,intent,state,uploadAttempted,receipt,binding,additionalTaskIds}=original;
  if(unsentStates.has(state)&&uploadAttempted===false&&!receipt&&!binding&&!additionalTaskIds?.length)return;
  if(binding?.orderId&&!additionalTaskIds?.length&&exists(db,'purchase_receipts')){
    const row=db.prepare('SELECT payload FROM purchase_receipts WHERE id=?').get(id);
    const observed=row?JSON.parse(row.payload):null;
    if(observed?.state==='VOIDED'&&observed.terminalKind==='VOIDED'&&observed.terminal===true&&
       observed.summary?.status==='已作废'&&observed.summary.issueCount===0&&
       observed.finalSync==='NOT_DUE'&&!observed.hostReceipt&&!observed.hostConflict&&
       observed.scope?.orderId===binding.orderId&&observed.scope.store===intent.basis.store&&
       observed.scope.supplier===intent.supplierCode&&canonical(observed.scope.identity)===canonical(intent.identity)&&
       canonical(observed.basis)===canonical(intent.basis)&&!hasLocalReceipt(intent.basis))return;
  }
  fail('PURCHASE_ORIGINAL_UNRESOLVED','原采购已上传、已受理或存在待处理的采购／实收事实，请继续核对原采购',
       {id,taskId:receipt?.taskId??null,orderId:binding?.orderId??null});
}
