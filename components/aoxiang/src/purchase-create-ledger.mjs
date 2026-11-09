import path from 'node:path';
import fs from 'node:fs/promises';
import {DatabaseSync,backup} from 'node:sqlite';
import {canonical,sha256,now,uid,fail} from './common.mjs';
import {orderKey,validatePurchaseIntent,checkImportReceipt,checkCreatedPurchase,canBind,bindingEligible,checkedBinding} from './purchase-create-contract.mjs';
import {requireUnretired,requireHostOrder,retiredRecord,lateRetiredReceipt} from './order-retirement.mjs';

const transitions={
  RESERVED:['PREPARING','PAUSED','CANCELLED_BEFORE_UPLOAD'],PREPARING:['READY','PAUSED','FAILED_PRECHECK'],
  READY:['SUBMITTING','PAUSED','FAILED_PRECHECK'],PAUSED:['PREPARING','CANCELLED_BEFORE_UPLOAD'],
  FAILED_PRECHECK:['PREPARING','CANCELLED_BEFORE_UPLOAD'],SUBMITTING:['ACCEPTED','NEEDS_REVIEW'],
  ACCEPTED:['VERIFYING','NEEDS_REVIEW'],VERIFYING:['ACCEPTED','BOUND','NEEDS_REVIEW'],
  NEEDS_REVIEW:['ACCEPTED','VERIFYING','NEEDS_REVIEW'],BOUND:['NEEDS_REVIEW'],CANCELLED_BEFORE_UPLOAD:[]
};
export class PurchaseCreates{
  static async open(state){
    const db=state.db,tables=['purchase_creates','purchase_create_events'],present=tables.filter(t=>db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(t)),marker=db.prepare("SELECT value FROM metadata WHERE key='purchaseCreateProtocol'").get();
    if(!marker&&!present.length){
      const file=path.join(state.root,'recovery','before-purchase-create-'+uid()+'.sqlite');await fs.mkdir(path.dirname(file),{recursive:true});await backup(db,file);
      const saved=new DatabaseSync(file,{readOnly:true});try{if(saved.prepare('PRAGMA quick_check').get().quick_check!=='ok')fail('STATE_BACKUP_FAILED','采购任务升级前备份校验失败');}finally{saved.close();}
      state.write(()=>db.exec(`CREATE TABLE purchase_creates(id TEXT PRIMARY KEY,order_key TEXT NOT NULL,identity_key TEXT NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,sequence INTEGER NOT NULL,platform_order TEXT,payload TEXT NOT NULL);
        CREATE UNIQUE INDEX purchase_create_order ON purchase_creates(order_key) WHERE state<>'CANCELLED_BEFORE_UPLOAD';
        CREATE UNIQUE INDEX purchase_create_platform ON purchase_creates(identity_key,platform_order) WHERE platform_order IS NOT NULL;
        CREATE TABLE purchase_create_events(id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,sequence),FOREIGN KEY(id) REFERENCES purchase_creates(id));
        INSERT INTO metadata VALUES('purchaseCreateProtocol','1');`));
    }else if(marker?.value!=='1'||present.length!==tables.length)fail('PURCHASE_CREATE_STATE_CORRUPT','采购创建记录结构缺失或版本不符，不能重建后再次建单');
    for(const name of ['purchase_create_order','purchase_create_platform'])if(!db.prepare("SELECT name FROM sqlite_master WHERE type='index' AND name=?").get(name))fail('PURCHASE_CREATE_STATE_CORRUPT','采购重复建单保护索引缺失，停止新提交');
    return new PurchaseCreates(state);
  }
  constructor(state){this.state=state;this.db=state.db;}
  get(id){const r=this.db.prepare('SELECT payload FROM purchase_creates WHERE id=?').get(id);if(!r){const gone=retiredRecord(this.db,id);if(gone)return gone;fail('PURCHASE_CREATE_NOT_FOUND','未找到采购创建记录');}return JSON.parse(r.payload);}
  list(){return this.db.prepare('SELECT payload FROM purchase_creates ORDER BY rowid').all().map(r=>JSON.parse(r.payload));}
  events(id){return this.db.prepare('SELECT payload FROM purchase_create_events WHERE id=? ORDER BY sequence').all(id).map(r=>JSON.parse(r.payload));}
  reserve(intent,requestHash){
    requireUnretired(this.db,intent.id,intent.basis?.oid);requireHostOrder(intent.basis?.oid);
    validatePurchaseIntent(intent);const key=orderKey(intent);
    return this.state.write(()=>{
      const existing=this.db.prepare('SELECT request_hash FROM purchase_creates WHERE id=?').get(intent.id);
      if(existing){if(existing.request_hash!==requestHash)fail('PURCHASE_INTENT_CHANGED','同一采购命令不能替换付款依据或文件');return {created:false,record:this.get(intent.id)};}
      const prior=this.db.prepare("SELECT id FROM purchase_creates WHERE order_key=? AND state<>'CANCELLED_BEFORE_UPLOAD'").get(key);
      if(prior)fail('PURCHASE_ALREADY_RESERVED','此订单已有采购任务；付款变化也不能自动重建，请先核对原任务',{id:prior.id});
      const at=now(),r={id:intent.id,intent:structuredClone(intent),requestHash,state:'RESERVED',sequence:1,createdAt:at,updatedAt:at,uploadAttempted:false,receipt:null,binding:null,error:null};
      this.db.prepare('INSERT INTO purchase_creates VALUES(?,?,?,?,?,?,?,?)').run(r.id,key,sha256(canonical(intent.identity)),requestHash,r.state,r.sequence,null,JSON.stringify(r));this.event(r,'RESERVED');return {created:true,record:r};
    });
  }
  event(r,kind){this.db.prepare('INSERT INTO purchase_create_events VALUES(?,?,?)').run(r.id,r.sequence,JSON.stringify({id:r.id,sequence:r.sequence,state:r.state,at:r.updatedAt,kind,error:r.error,taskId:r.receipt?.taskId||null,orderId:r.binding?.orderId||null,
    ...(r.bindingProposal?{proposalId:r.bindingProposal.id,proposalState:r.bindingProposal.state,proofSha256:r.bindingProposal.manifest?.sha256||null,proposalError:r.bindingProposal.error}:{} )}));}
  update(id,sequence,next,changes={},kind=next){
    return this.state.write(()=>{
      const old=this.get(id);if(old.sequence!==sequence)fail('PURCHASE_STATE_CONFLICT','采购任务已更新，请读取原记录');
      requireUnretired(this.db,id,old.intent?.basis?.oid);
      if(!transitions[old.state]?.includes(next))fail('PURCHASE_STATE_TRANSITION','采购任务不允许从 '+old.state+' 进入 '+next);
      if(Object.keys(changes).some(k=>!['baseline','uploadAttempted','receipt','binding','error','evidence','additionalTaskIds','uploadWarning'].includes(k)))fail('PURCHASE_STATE_IMMUTABLE','不能修改采购冻结依据');
      if(old.uploadAttempted&&changes.uploadAttempted===false)fail('PURCHASE_STATE_IMMUTABLE','已进入上传边界，不能清除提交事实');
      if(old.receipt&&changes.receipt?.taskId&&old.receipt.taskId!==changes.receipt.taskId)fail('PURCHASE_RECEIPT_CONFLICT','原导入任务号不能覆盖');
      const r={...old,...structuredClone(changes),state:next,sequence:sequence+1,updatedAt:now()};
      this.db.prepare('UPDATE purchase_creates SET state=?,sequence=?,platform_order=?,payload=? WHERE id=?').run(r.state,r.sequence,r.binding?.orderId||null,JSON.stringify(r),id);this.event(r,kind);return r;
    });
  }
  accepted(receipt){
    if(lateRetiredReceipt(this.db,receipt))return this.get(receipt.intentId);
    const r=this.get(receipt.intentId);checkImportReceipt(r.intent,receipt);
    if(r.receipt?.taskId===receipt.taskId)return r;
    if(r.receipt){return this.update(r.id,r.sequence,'NEEDS_REVIEW',{additionalTaskIds:[...new Set([...(r.additionalTaskIds||[]),receipt.taskId])],error:{code:'MULTIPLE_IMPORT_TASKS',message:'发现不同导入任务号，禁止自动关联或重传'}},'CONFLICTING_RECEIPT');}
    if(!r.uploadAttempted)fail('UNEXPECTED_IMPORT_RECEIPT','未进入上传边界却收到受理记录');
    if(r.state==='BOUND'&&r.binding.method==='operator_confirmed_exact_native_rows'){
      return this.state.write(()=>{
        const current=this.get(r.id);if(current.sequence!==r.sequence)fail('PURCHASE_STATE_CONFLICT','原采购记录已变化');
        const next={...current,receipt:structuredClone(receipt),sequence:current.sequence+1,updatedAt:now()};
        this.db.prepare('UPDATE purchase_creates SET sequence=?,payload=? WHERE id=?').run(next.sequence,JSON.stringify(next),r.id);
        this.event(next,'LATE_RECEIPT_AFTER_MANUAL_BIND');return next;
      });
    }
    return this.update(r.id,r.sequence,'ACCEPTED',{receipt,error:null},'ACCEPTED');
  }
  putBindingProposal(id,sequence,proposal,kind){
    requireUnretired(this.db,id);
    return this.state.write(()=>{
      const r=this.get(id);
      if(r.sequence!==sequence)fail('PURCHASE_STATE_CONFLICT','采购记录已变化，请重新核对');
      // A late original receipt may supersede a running read-only proof. Retire
      // only that exact proposal; this exception cannot create or alter evidence.
      const retiring=!bindingEligible(r)&&r.bindingProposal?.state==='RUNNING'&&proposal.state==='SUPERSEDED'&&
        canonical({...proposal,state:null,error:null})===canonical({...r.bindingProposal,state:null,error:null});
      if(!retiring)canBind(r);
      const next={...r,bindingProposal:structuredClone(proposal),sequence:r.sequence+1,updatedAt:now()};
      this.db.prepare('UPDATE purchase_creates SET sequence=?,payload=? WHERE id=?').run(next.sequence,JSON.stringify(next),id);
      this.event(next,kind);return next;
    });
  }
  confirmBinding(id,sequence,proof,proposalId,proofHash){
    requireUnretired(this.db,id);
    return this.state.write(()=>{
      const r=this.get(id);
      if(r.sequence!==sequence||r.bindingProposal?.id!==proposalId||r.bindingProposal.state!=='READY'||r.bindingProposal.manifest.sha256!==proofHash)fail('BINDING_PREVIEW_STALE','关联核对期间记录已更新，请重新预览');
      const binding={...checkedBinding(r,proof),proposalId,proofSha256:proofHash};
      const next={...r,state:'BOUND',binding,evidence:{manual:true,manifest:r.bindingProposal.manifest},error:null,sequence:r.sequence+1,updatedAt:now(),bindingProposal:{...r.bindingProposal,state:'CONFIRMED'}};
      this.db.prepare('UPDATE purchase_creates SET state=?,sequence=?,platform_order=?,payload=? WHERE id=?').run(next.state,next.sequence,binding.orderId,JSON.stringify(next),id);
      this.event(next,'OPERATOR_CONFIRMED_NATIVE_BINDING');return next;
    });
  }
  bind(id,evidence){
    requireUnretired(this.db,id);
    const r=this.get(id);if(r.additionalTaskIds?.length)fail('MULTIPLE_IMPORT_TASKS','导入任务不唯一，需人工核对');
    const binding=checkCreatedPurchase(r.intent,r.receipt,evidence);
    return this.update(id,r.sequence,'BOUND',{binding,evidence,error:null},'EXACT_ORDER_BOUND');
  }
  recover(){
    for(const r of this.list()){
      if(['PREPARING','READY'].includes(r.state))this.update(r.id,r.sequence,'PAUSED',{error:{code:'PROCESS_INTERRUPTED',message:'准备阶段中断，尚未进入上传'}});
      if(r.state==='SUBMITTING')this.update(r.id,r.sequence,'NEEDS_REVIEW',{error:{code:'SUBMISSION_UNKNOWN',message:'上传期间中断，只核对原任务，禁止重传'}});
      if(r.state==='VERIFYING')this.update(r.id,r.sequence,'ACCEPTED',{error:{code:'VERIFY_INTERRUPTED',message:'结果核对中断，可继续查询原任务'}});
      const latest=this.get(r.id);
      if(latest.bindingProposal?.state==='RUNNING')this.putBindingProposal(r.id,latest.sequence,{...latest.bindingProposal,state:bindingEligible(latest)?'PAUSED':'SUPERSEDED',error:{code:'BINDING_INTERRUPTED',message:'关联核对中断；保留原采购与下载任务，请刷新本单记录继续原任务'}},'BINDING_RECOVERED');
    }
  }
}
export function purchasePublic(r){
  if(r.state==='RETIRED')return {id:r.id,state:'RETIRED',sourceDeleted:true,uploadAttempted:r.uploadAttempted,taskIds:r.taskIds};
  return {id:r.id,state:r.state,sequence:r.sequence,createdAt:r.createdAt,updatedAt:r.updatedAt,
    basis:r.intent.basis,target:r.intent.target,supplierCode:r.intent.supplierCode,rows:r.intent.rows.length,totalQuantity:r.intent.totalQuantity,totalAmount:r.intent.totalAmount,
    uploadAttempted:r.uploadAttempted,taskId:r.receipt?.taskId||null,binding:r.binding,error:r.error,uploadWarning:r.uploadWarning||null,additionalTaskIds:r.additionalTaskIds||[],
    ...(r.bindingProposal?{bindingProposal:r.bindingProposal}:{})};
}
