import fs from 'node:fs/promises';
import path from 'node:path';
import {DatabaseSync,backup} from 'node:sqlite';
import {canonical,fail,now,uid} from './common.mjs';
import {receiptSummary} from './purchase-receipts.mjs';

export class PurchaseReceipts{
  static async open(state){
    const db=state.db,tables=['purchase_receipts','purchase_receipt_events'];
    const present=tables.filter(t=>db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(t));
    const version=db.prepare("SELECT value FROM metadata WHERE key='purchaseReceiptProtocol'").get();
    if(!version&&!present.length){
      const file=path.join(state.root,'recovery','before-purchase-receipt-'+uid()+'.sqlite');await fs.mkdir(path.dirname(file),{recursive:true});await backup(db,file);
      const copy=new DatabaseSync(file,{readOnly:true});try{if(copy.prepare('PRAGMA quick_check').get().quick_check!=='ok')fail('STATE_BACKUP_FAILED','收货同步升级前备份失败');}finally{copy.close();}
      state.write(()=>db.exec(`CREATE TABLE purchase_receipts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        CREATE TABLE purchase_receipt_events(id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(id,sequence));
        INSERT INTO metadata VALUES('purchaseReceiptProtocol','1');`));
    }else if(version?.value!=='1'||present.length!==tables.length)fail('RECEIPT_STATE_CORRUPT','收货同步记录不完整，不能重建并恢复监控');
    return new PurchaseReceipts(state);
  }
  constructor(state){this.state=state;this.db=state.db;}
  get(id){const row=this.db.prepare('SELECT payload FROM purchase_receipts WHERE id=?').get(id);return row?JSON.parse(row.payload):null;}
  list(){return this.db.prepare('SELECT payload FROM purchase_receipts ORDER BY rowid').all().map(r=>JSON.parse(r.payload));}
  register(created,createdDay){
    if(created.state!=='BOUND'||!created.binding)fail('RECEIPT_NOT_BOUND','先核实唯一采购单关联，再查询收货');
    return this.state.write(()=>{
      const old=this.get(created.id),scope={orderId:created.binding.orderId,identity:created.intent.identity,store:created.intent.basis.store,supplier:created.intent.supplierCode,createdDay};
      if(old){if(canonical(old.scope)!==canonical(scope))fail('RECEIPT_BINDING_CHANGED','原收货监控关联不可替换');return old;}
      return this.save({id:created.id,scope,basis:created.intent.basis,sequence:0,state:'IDLE',terminal:false,terminalEvidence:[],finalSync:'NOT_DUE',activeRunId:null,summary:null,manifest:null,error:null,createdAt:now()},'REGISTERED');
    });
  }
  save(r,kind){
    const next={...r,sequence:r.sequence+1,updatedAt:now()};
    this.db.prepare('INSERT INTO purchase_receipts VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload').run(r.id,JSON.stringify(next));
    this.db.prepare('INSERT INTO purchase_receipt_events VALUES(?,?,?)').run(r.id,next.sequence,JSON.stringify({sequence:next.sequence,kind,at:next.updatedAt,state:next.state,terminal:next.terminal,finalSync:next.finalSync,runId:next.activeRunId,error:next.error}));return next;
  }
  change(id,fn,kind){return this.state.write(()=>{const r=this.get(id);if(!r)fail('RECEIPT_NOT_FOUND','尚无本单收货同步记录');return this.save(fn(r),kind);});}
  begin(id){return this.change(id,r=>({...r,state:'RUNNING',error:null}),'STARTED');}
  run(id,runId){return this.change(id,r=>{if(r.activeRunId&&r.activeRunId!==runId)fail('RECEIPT_RUN_CONFLICT','必须先恢复原下载任务');return {...r,activeRunId:runId};},'RUN_ATTACHED');}
  terminal(id,source){
    return this.change(id,r=>{
      const evidence={source:source.source,sha256:source.sha256,file:source.file,observedAt:now()};
      if(r.terminalEvidence.some(e=>e.source===evidence.source&&e.sha256===evidence.sha256))return r;
      return {...r,terminal:true,finalSync:r.finalSync==='SYNCED'?'SYNCED':'PENDING',terminalEvidence:[...r.terminalEvidence,evidence]};
    },'TERMINAL_OBSERVED_ROUTINE_STOPPED');
  }
  finish(id,o,manifest){return this.change(id,r=>{
    if(o.purchaseId!==id||o.runId!==r.activeRunId||o.orderId!==r.scope.orderId)fail('RECEIPT_RUN_CONFLICT','收货结果不属于当前下载任务');
    if(r.terminal&&!o.terminal)fail('RECEIPT_TERMINAL_CONFLICT','此前已见完成状态，本次状态回退，保留停止监控并待核');
    return {...r,state:o.eligible?'READY':o.terminal||o.issues.some(i=>i.code!=='RECEIPT_NUMBER_UNKNOWN')?'NEEDS_REVIEW':'IDLE',
      terminal:r.terminal||o.terminal,finalSync:o.terminal?(o.eligible?'READY':'NEEDS_REVIEW'):r.finalSync,
      summary:receiptSummary(o),manifest,activeRunId:null,error:null};
  },'SNAPSHOT_SAVED');}
  failed(id,error,paused=false){return this.change(id,r=>({...r,state:paused?'PAUSED':'NEEDS_REVIEW',error}),'INTERRUPTED');}
  synced(id,manifestSha,receipt){return this.change(id,r=>{
    if(r.manifest?.sha256!==manifestSha||!r.summary?.eligible)fail('RECEIPT_SYNC_CONFLICT','实收回执不属于本次完整终态文件');
    if(!receipt||receipt.number!==r.basis.number||receipt.stage!=='receipt'||!Number.isSafeInteger(receipt.version)||receipt.version<1)fail('RECEIPT_SYNC_CONFLICT','正式实收服务回执不完整或属于别的订单');
    return {...r,state:'SYNCED',finalSync:'SYNCED',hostReceipt:receipt};
  },'HOST_RECEIPT_CONFIRMED');}
  recover(){for(const r of this.list())if(r.state==='RUNNING')this.failed(r.id,{code:'PROCESS_INTERRUPTED',message:'收货同步中断；继续原下载任务，终态停止标记保留'},true);}
}
