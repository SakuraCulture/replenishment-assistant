import fs from 'node:fs/promises';
import path from 'node:path';
import {DatabaseSync,backup} from 'node:sqlite';
import {fail,uid,now,canonical,sha256,scopeHash,exists,writeJson,sameIdentity} from './common.mjs';
import {checkPurchaseReceipt} from './purchase-contract.mjs';
import {retirementSchema,requireUnretired,requireHostOrder} from './order-retirement.mjs';

export class ExclusiveLock{
  static async acquire(file){
    await fs.mkdir(path.dirname(file),{recursive:true});let db;
    try{
      db=new DatabaseSync(file);db.exec('PRAGMA journal_mode=DELETE; PRAGMA busy_timeout=0; CREATE TABLE IF NOT EXISTS guard(v INTEGER); BEGIN EXCLUSIVE;');
      return {release(){try{db.exec('ROLLBACK');}finally{db.close();}}};
    }catch(e){try{db?.close();}catch{}fail('APP_BUSY','已有补货建议助手进程正在使用本机状态或浏览器');}
  }
}
export const tenantKey = identity=>sha256(canonical({accountId:identity.accountId,merchantCode:identity.merchantCode}));
export const assetKey = x=>sha256(canonical({accountId:x.accountId,merchantCode:x.merchantCode,buyerWarehouseCode:x.buyerWarehouseCode,acquisitionDate:x.acquisitionDate,scopeHash:x.scopeHash,semanticVersion:x.semanticVersion}));
export const pendingKey = p=>sha256(canonical({accountId:p.accountId,merchantCode:p.merchantCode,sourceAttemptId:p.sourceAttemptId,buyerWarehouseCode:p.buyerWarehouseCode,scopeHash:p.scopeHash}));
const TERMINAL=new Set(['SUCCEEDED','REUSED','FAILED_CONFIRMED','CANCELLED_BEFORE_SUBMIT','ABANDONED']);

export class StateStore{
  static async open(root){
    await fs.mkdir(root,{recursive:true});
    const present=await exists(path.join(root,'state.sqlite'));
    if(!present&&(await exists(path.join(root,'state.guard.json'))||await exists(path.join(root,'config/portable.config.json'))))fail('STATE_MISSING','本机作业状态库缺失，不能作为全新状态重复提交；请恢复状态备份');
    if(present){
      const prior=new DatabaseSync(path.join(root,'state.sqlite'),{readOnly:true});
      try{
        if(prior.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='metadata'").get()&&!prior.prepare("SELECT value FROM metadata WHERE key='orderRetirementProtocol'").get()){
          const file=path.join(root,'recovery','before-order-retirement-'+uid()+'.sqlite');await fs.mkdir(path.dirname(file),{recursive:true});await backup(prior,file);
          const check=new DatabaseSync(file,{readOnly:true});try{if(check.prepare('PRAGMA quick_check').get().quick_check!=='ok')fail('STATE_BACKUP_FAILED','退役升级前备份校验失败');}finally{check.close();}
        }
      }finally{prior.close();}
    }
    const state=new StateStore(root);
    if(!await exists(path.join(root,'state.guard.json')))await writeJson(path.join(root,'state.guard.json'),{stateId:uid(),createdAt:now()});
    return state;
  }
  constructor(root){
    this.root=root;this.db=new DatabaseSync(path.join(root,'state.sqlite'));
    if(this.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='metadata'").get()){
      const version=this.db.prepare("SELECT value FROM metadata WHERE key='schemaVersion'").get()?.value;
      if(!['1','2'].includes(version)){this.db.close();fail('STATE_VERSION','状态库版本不兼容，原库未升级或重建');}
      if(version==='2'&&!this.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_runs'").get()){this.db.close();fail('STATE_CORRUPT','采购作业表缺失，不能重建空表后重提');}
    }
    this.db.exec(`PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
      INSERT OR IGNORE INTO metadata VALUES('schemaVersion','1');
      INSERT OR IGNORE INTO metadata VALUES('generation','0');
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,item_id TEXT NOT NULL,tenant_key TEXT NOT NULL,warehouse_code TEXT NOT NULL,scope_hash TEXT NOT NULL,stage TEXT NOT NULL,task_id TEXT,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS assets(asset_key TEXT PRIMARY KEY,sha256 TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS publishes(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS imported_pending(record_key TEXT PRIMARY KEY,tenant_key TEXT NOT NULL,warehouse_code TEXT NOT NULL,scope_hash TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS migration_imports(migration_id TEXT PRIMARY KEY,package_hash TEXT NOT NULL,payload TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
    `);
    if(!['1','2'].includes(this.db.prepare("SELECT value FROM metadata WHERE key='schemaVersion'").get().value)){this.db.close();fail('STATE_VERSION','状态库版本不兼容，不能自动重建');}
    if(this.db.prepare('PRAGMA quick_check').get().quick_check!=='ok'){this.db.close();fail('STATE_CORRUPT','本地状态库需要恢复，不能新提交导出');}
    retirementSchema(this.db);
  }
  close(){this.db.close();}
  write(fn){this.db.exec('BEGIN IMMEDIATE');try{const result=fn();this.db.prepare("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'").run();this.db.exec('COMMIT');return result;}catch(e){this.db.exec('ROLLBACK');throw e;}}
  generation(){return Number(this.db.prepare("SELECT value FROM metadata WHERE key='generation'").get().value);}
  rows(table){if(!['runs','items','attempts','assets','publishes','imported_pending','migration_imports','conflicts','purchase_runs'].includes(table))throw Error('invalid table');if(table==='purchase_runs'&&!this.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_runs'").get())return [];return this.db.prepare('SELECT payload FROM '+table).all().map(r=>JSON.parse(r.payload));}
  async enablePurchases(){
    if(this.db.prepare("SELECT value FROM metadata WHERE key='schemaVersion'").get().value==='2')return;
    const file=path.join(this.root,'recovery/state-before-purchase-'+uid()+'.sqlite');await fs.mkdir(path.dirname(file),{recursive:true});await backup(this.db,file);
    const check=new DatabaseSync(file,{readOnly:true});try{if(check.prepare('PRAGMA quick_check').get().quick_check!=='ok')fail('STATE_BACKUP_FAILED','状态库备份核验失败');}finally{check.close();}
    this.write(()=>{this.db.exec('CREATE TABLE purchase_runs(id TEXT PRIMARY KEY,payload TEXT NOT NULL)');this.db.prepare("UPDATE metadata SET value='2' WHERE key='schemaVersion'").run();});
  }
  getPurchase(id){return this.rows('purchase_runs').find(r=>r.id===id)||null;}
  putPurchase(record){
    requireUnretired(this.db,record.hostPurchaseId,record.hostOrderId);
    if(this.db.prepare('SELECT 1 FROM retired_purchase_runs WHERE id=?').get(record.id))fail('SOURCE_ORDER_DELETED','原采购取数任务已退役，不接受晚到回写');
    if(record.hostOrderId)requireHostOrder(record.hostOrderId);
    const old=this.getPurchase(record.id);
    if(old){
      for(const key of ['identity','target','from','to','dateBasis','orderId','scopeHash','createdAt'])if(canonical(old[key]??null)!==canonical(record[key]??null))fail('PURCHASE_RECORD_CHANGED','已有采购作业的身份或冻结范围不能修改');
      for(const source of ['D16','D17']){const before=old.exports?.[source],after=record.exports?.[source];if(before?.taskId&&after?.taskId!==before.taskId)fail('TASK_ID_CHANGED','已有采购任务ID不能更改');if(before?.stage==='SUBMISSION_UNKNOWN'&&['PLANNED','INTENT_RECORDED','SUBMITTING'].includes(after?.stage))fail('SUBMISSION_UNKNOWN','采购受理未知的意图不能重放');}
    }
    this.write(()=>this.db.prepare('INSERT OR REPLACE INTO purchase_runs VALUES(?,?)').run(record.id,JSON.stringify(record)));
  }
  recordPurchaseReceipt(receipt){
    const run=this.rows('purchase_runs').find(r=>Object.values(r.exports||{}).some(e=>e.intentId===receipt?.intentId));if(!run)return false;
    const source=Object.keys(run.exports).find(k=>run.exports[k].intentId===receipt.intentId),e=run.exports[source];
    if(e.taskId&&e.taskId!==receipt.taskId){run.exports[source]={...e,stage:'SUBMISSION_UNKNOWN',additionalTaskIds:[...new Set([...(e.additionalTaskIds||[]),receipt.taskId])]};}
    else run.exports[source]={...e,taskId:receipt.taskId,stage:e.stage==='SUCCEEDED'?'SUCCEEDED':checkPurchaseReceipt(receipt,source,e.query||run.query)?'ACCEPTED':'WRONG_SCOPE_ACCEPTED',receipt};
    if(Object.values(run.exports).some(e=>['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(e.stage)))run.status='NEEDS_ATTENTION';
    this.putPurchase(run);return true;
  }
  importPurchases(records){
    const merged=[];for(const r of records){
      requireUnretired(this.db,r.hostPurchaseId,r.hostOrderId);
      if(this.db.prepare('SELECT 1 FROM retired_purchase_runs WHERE id=?').get(r.id))fail('SOURCE_ORDER_DELETED','迁入记录属于已退役任务，不能恢复');
      if(r.hostOrderId)requireHostOrder(r.hostOrderId);
      const old=this.getPurchase(r.id);if(!old){merged.push(r);continue;}
      if(old.scopeHash!==r.scopeHash||!sameIdentity(old.identity,r.identity))fail('MIGRATION_PURCHASE_CONFLICT','同一采购作业ID对应不同范围');
      const selected={...old,query:old.query||r.query,exports:{...old.exports}},rank=['PLANNED','INTENT_RECORDED','SUBMITTING','ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','SUCCEEDED','FAILED_CONFIRMED'];
      for(const sourceId of ['D16','D17']){const a=old.exports[sourceId],b=r.exports[sourceId];if((a.taskId&&b.taskId&&a.taskId!==b.taskId)||(a.intentId&&b.intentId&&a.intentId!==b.intentId))fail('MIGRATION_PURCHASE_CONFLICT','同一采购意图对应不同平台任务');if(a.stage==='SUBMISSION_UNKNOWN'||b.stage==='SUBMISSION_UNKNOWN')selected.exports[sourceId]={...a,...b,taskId:a.taskId||b.taskId||null,stage:'SUBMISSION_UNKNOWN'};else if(a.stage==='WRONG_SCOPE_ACCEPTED'||b.stage==='WRONG_SCOPE_ACCEPTED')selected.exports[sourceId]={...a,...b,taskId:a.taskId||b.taskId||null,stage:'WRONG_SCOPE_ACCEPTED'};else if(rank.indexOf(b.stage)>rank.indexOf(a.stage))selected.exports[sourceId]=b;}
      if(Object.values(selected.exports).some(e=>['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(e.stage)))selected.status='NEEDS_ATTENTION';merged.push(selected);
    }
    this.write(()=>{for(const r of merged)this.db.prepare('INSERT OR REPLACE INTO purchase_runs VALUES(?,?)').run(r.id,JSON.stringify(r));});return merged.length;
  }
  putRun(run){this.write(()=>this.db.prepare('INSERT OR REPLACE INTO runs VALUES(?,?)').run(run.id,JSON.stringify(run)));}
  getRun(id){const r=this.db.prepare('SELECT payload FROM runs WHERE id=?').get(id);return r?JSON.parse(r.payload):null;}
  putItem(item){this.write(()=>this.db.prepare('INSERT OR REPLACE INTO items VALUES(?,?,?)').run(item.id,item.runId,JSON.stringify(item)));}
  getItems(runId){return this.db.prepare('SELECT payload FROM items WHERE run_id=?').all(runId).map(r=>JSON.parse(r.payload));}
  putAttempt(attempt){
    const old=this.getAttempt(attempt.id);
    if(old?.taskId&&attempt.taskId!==old.taskId)fail('TASK_ID_CHANGED','既有任务身份不能被重写');
    if(old?.stage==='SUBMISSION_UNKNOWN'&&attempt.stage==='SUBMITTING')fail('UNKNOWN_SUBMISSION','未决提交不能再次发送');
    this.write(()=>this.db.prepare('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?,?,?,?,?)').run(attempt.id,attempt.itemId,tenantKey(attempt),attempt.buyerWarehouseCode,attempt.scopeHash,attempt.stage,attempt.taskId||null,JSON.stringify(attempt)));
  }
  getAttempt(id){const r=this.db.prepare('SELECT payload FROM attempts WHERE id=?').get(id);return r?JSON.parse(r.payload):null;}
  recordReceipt(receipt){
    const a=receipt?.intentId?this.getAttempt(receipt.intentId):null;if(!a)return false;
    if(a.taskId&&a.taskId!==receipt.taskId){this.putAttempt({...a,stage:'SUBMISSION_UNKNOWN',additionalTaskIds:[...new Set([...(a.additionalTaskIds||[]),receipt.taskId])]});return true;}
    const expected=a.semanticVersion==='D13-all-info-1'?'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT':'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',s=receipt.scope;
    const extras=s?.extraKeys||Object.keys(s||{}).filter(k=>k!=='buyerWarehouseCodeList');
    const correct=receipt.code===expected&&JSON.stringify(s?.buyerWarehouseCodeList)===JSON.stringify([a.buyerWarehouseCode])&&!extras.length;
    this.putAttempt({...a,stage:['SUCCEEDED','ABANDONED'].includes(a.stage)?a.stage:correct?'ACCEPTED':'WRONG_SCOPE_ACCEPTED',taskId:receipt.taskId,receipt});return true;
  }
  pending(identity,warehouseCode,scope){
    const key=tenantKey(identity);
    // This product exports full warehouse scopes. A schema/version change does not
    // prove disjointness, so any unresolved D13 attempt on the warehouse blocks it.
    return [...this.db.prepare('SELECT payload,stage FROM attempts WHERE tenant_key=? AND warehouse_code=?').all(key,warehouseCode).filter(r=>!TERMINAL.has(r.stage)).map(r=>JSON.parse(r.payload)),
      ...this.db.prepare('SELECT payload FROM imported_pending WHERE tenant_key=? AND warehouse_code=?').all(key,warehouseCode).map(r=>JSON.parse(r.payload))];
  }
  putAsset(asset,{replace=false}={}){
    const key=assetKey(asset),old=this.getAsset(key);
    if(old&&old.sha256!==asset.sha256&&!replace)fail('ASSET_CONFLICT','同日同范围已有不同内容的资产，需要明确重跑或处理冲突');
    this.write(()=>this.db.prepare('INSERT OR REPLACE INTO assets VALUES(?,?,?)').run(key,asset.sha256,JSON.stringify({...asset,assetKey:key})));return key;
  }
  getAsset(key){const r=this.db.prepare('SELECT payload FROM assets WHERE asset_key=?').get(key);return r?JSON.parse(r.payload):null;}
  putPublish(p){this.write(()=>this.db.prepare('INSERT OR REPLACE INTO publishes VALUES(?,?)').run(p.id,JSON.stringify(p)));}
  deletePublish(id){this.write(()=>this.db.prepare('DELETE FROM publishes WHERE id=?').run(id));}
  importRecords(manifest,packageHash,{assetRecords=[]}={}){
    const existing=this.db.prepare('SELECT package_hash,payload FROM migration_imports WHERE migration_id=?').get(manifest.migrationId);
    if(existing){if(existing.package_hash!==packageHash)fail('MIGRATION_ID_CONFLICT','同一迁移ID对应不同内容');return {alreadyImported:true,...JSON.parse(existing.payload)};}
    const conflicts=[];
    const record={migrationId:manifest.migrationId,packageHash,importedAt:now(),handoffStatus:manifest.handoffStatus,unresolvedCount:manifest.unresolvedRecords.length};
    this.write(()=>{
      for(const pending of manifest.unresolvedRecords){
        const key=pendingKey(pending);
        const prior=this.db.prepare('SELECT payload FROM imported_pending WHERE record_key=?').get(key);
        const old=prior?JSON.parse(prior.payload):null;
        // Unknown evidence is never erased by a stale imported "known" state.
        const selected=old?.state==='SUBMISSION_UNKNOWN'&&pending.state!=='SUBMISSION_UNKNOWN'?old:pending;
        this.db.prepare('INSERT OR REPLACE INTO imported_pending VALUES(?,?,?,?,?)').run(key,tenantKey(selected),selected.buyerWarehouseCode,selected.scopeHash,JSON.stringify(selected));
      }
      for(const asset of assetRecords){
        const key=assetKey(asset),old=this.getAsset(key);
        if(old&&old.sha256!==asset.sha256){const conflict={id:uid(),type:'IMPORTED_ASSET_CONFLICT',assetKey:key,existingSha256:old.sha256,importedSha256:asset.sha256};conflicts.push(conflict);this.db.prepare('INSERT INTO conflicts VALUES(?,?)').run(conflict.id,JSON.stringify(conflict));}
        else if(!old)this.db.prepare('INSERT INTO assets VALUES(?,?,?)').run(key,asset.sha256,JSON.stringify({...asset,assetKey:key}));
      }
      record.conflicts=conflicts;
      this.db.prepare('INSERT INTO migration_imports VALUES(?,?,?)').run(manifest.migrationId,packageHash,JSON.stringify(record));
    });
    return record;
  }
  addConflict(conflict){const r={id:uid(),createdAt:now(),...conflict};this.write(()=>this.db.prepare('INSERT INTO conflicts VALUES(?,?)').run(r.id,JSON.stringify(r)));return r;}
  adoptImported(recordId,config){
    const candidates=this.rows('imported_pending').filter(r=>r.recordId===recordId&&sameIdentity(r,config.tenantBinding));
    if(candidates.length!==1)fail('MIGRATED_RECORD_NOT_UNIQUE','迁入记录未唯一匹配当前商家');
    const p=candidates[0],target=config.targets.find(t=>t.buyerWarehouseCode===p.buyerWarehouseCode);
    if(!p.taskId||!['ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','PUBLISHING'].includes(p.state))fail('SUBMISSION_UNKNOWN','该迁入记录仍待消歧，不能重建任务');
    if(!target||!p.querySnapshot||!p.semanticVersion||!p.acquisitionDate||!p.runMode)fail('RECOVERY_PROOF_MISSING','旧迁入记录缺少冻结范围、日期或查询计数；保留阻断，不能自动恢复');
    if(p.scopeHash!==scopeHash(p.buyerWarehouseCode,p.semanticVersion)||p.querySnapshot.warehouseCode!==p.buyerWarehouseCode)fail('RECOVERY_SCOPE_INVALID','迁入记录的仓点或口径哈希不一致');
    if(this.pending(config.tenantBinding,p.buyerWarehouseCode).some(r=>r!==p&&('sourceAttemptId' in r?pendingKey(r)!==pendingKey(p):true)))fail('UNRESOLVED_TASK','同仓另有未决记录，不能自动接管迁入任务');
    const run={id:uid(),mode:p.runMode,day:p.acquisitionDate,createdAt:now(),identity:config.tenantBinding,status:'RUNNING',targets:[target],importedFrom:p.sourceRunId};
    const item={id:uid(),runId:run.id,target,stage:'ACCEPTED',createdAt:now(),attemptId:uid()};
    const attempt={id:item.attemptId,itemId:item.id,runId:run.id,...config.tenantBinding,buyerWarehouseCode:p.buyerWarehouseCode,scopeHash:p.scopeHash,semanticVersion:p.semanticVersion,createdAt:p.createdAt,acquisitionDate:p.acquisitionDate,stage:'ACCEPTED',taskId:p.taskId,query:{...p.querySnapshot,identity:config.tenantBinding},importedFrom:p.sourceAttemptId};
    this.write(()=>{
      this.db.prepare('INSERT INTO runs VALUES(?,?)').run(run.id,JSON.stringify(run));
      this.db.prepare('INSERT INTO items VALUES(?,?,?)').run(item.id,run.id,JSON.stringify(item));
      this.db.prepare('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?)').run(attempt.id,item.id,tenantKey(attempt),attempt.buyerWarehouseCode,attempt.scopeHash,attempt.stage,attempt.taskId,JSON.stringify(attempt));
      this.db.prepare('DELETE FROM imported_pending WHERE record_key=?').run(pendingKey(p));
    });return run;
  }
}
