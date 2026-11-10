// Local collection maintenance only. No browser, filesystem deletion or business tables.
import {canonical,sha256,now,fail,sameIdentity} from './common.mjs';
import {publicRecord} from './machine-protocol.mjs';
import {resumableTask} from './collection-recovery.mjs';
import {collectionPurpose} from './state.mjs';

const ACTIVE=new Set(['QUEUED','RUNNING','PAUSE_REQUESTED']);
const DONE=new Set(['SUCCEEDED','REUSED','FAILED_CONFIRMED','CANCELLED_BEFORE_SUBMIT','ABANDONED']);
const CLOSED=new Set(['SUCCEEDED','ENDED']);
export class CollectionMaintenance{
  constructor(state,commands,runtime){
    Object.assign(this,{state,commands,runtime});this.db=state.db;
    this.db.exec('CREATE TABLE IF NOT EXISTS machine_retired(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,token TEXT NOT NULL,deleted_at TEXT NOT NULL)');
  }
  purpose(command){return command.request.consumer?.purpose||this.state.collectionPurposeForRun(command.items[0]?.run_id,command.request);}
  retirePublications(runId,at){
    for(const p of this.state.rows('publishes'))if((p.asset.runId||p.asset.sourceRunId||this.state.getAttempt(p.attemptId)?.runId)===runId)
      this.db.prepare('UPDATE publishes SET payload=? WHERE id=?').run(JSON.stringify({...p,originRunId:runId,retiredAt:at}),p.id);
  }
  classify(id,fingerprint,purpose,owner){
    const c=this.commands.get(id);if(!c)fail('COMMAND_NOT_FOUND','未找到原采集');
    if(c.fingerprint!==fingerprint||c.request.consumer&&collectionPurpose(c.request)!==purpose)fail('MAINTENANCE_SCOPE','原采集用途或校验值不符，未改属');
    const rows=c.items.map(i=>({id:i.run_id,old:this.db.prepare('SELECT * FROM collection_consumers WHERE run_id=?').get(i.run_id)}));
    if(rows.some(r=>r.old&&r.old.purpose!==purpose))fail('MAINTENANCE_SCOPE','原采集用途已登记，不能改属其他业务');
    if(rows.every(r=>r.old))return {classified:id};
    this.state.write(()=>{for(const r of rows)this.db.prepare('INSERT OR IGNORE INTO collection_consumers VALUES(?,?,?,?)').run(r.id,purpose,owner||null,id);});
    return {classified:id};
  }
  idle(runs){
    const affected=c=>!runs||c.items.some(i=>runs.has(i.run_id)&&i.state!=='ENDED');
    const active=this.runtime.active&&this.commands.get(this.runtime.active.id);
    if(this.runtime.closing||(this.runtime.active&&(!active||affected(active)))||this.commands.list().some(c=>ACTIVE.has(c.state)&&affected(c)))
      fail('COLLECTION_BUSY','先点“停止后续取数”，等待当前动作安全暂停后，再结束或删除采集记录');
  }
  preview(id,action){
    if(!['end','delete'].includes(action))fail('REQUEST_SCHEMA','未知采集维护动作');
    const retired=this.db.prepare('SELECT * FROM machine_retired WHERE id=?').get(id);
    if(retired){
      if(action!=='delete')fail('COMMAND_RETIRED','该采集记录已删除，不能恢复或重发');
      return {id,action,token:retired.token,already_deleted:true,fingerprint:retired.fingerprint,stores:[],related:[],blocked:[]};
    }
    const command=this.commands.get(id);
    if(!command)fail('COMMAND_NOT_FOUND','未找到原采集任务，请刷新记录');
    if(action==='end'&&command.state==='ENDED')fail('COLLECTION_ALREADY_ENDED','本次本地采集已结束，请操作当前采集记录');
    if(action==='delete'&&!CLOSED.has(command.state))fail('COLLECTION_NOT_ENDED','请先结束本次采集，再删除记录');
    const runs=new Set(command.items.map(i=>i.run_id)),identity=command.config_snapshot.tenantBinding;
    const blocked=[],retained=[];
    // A blocked new command may point at earlier failed attempts for these stores.
    if(action==='end')for(const code of command.request.scope.warehouse_codes){
      for(const a of this.state.pending(identity,code,undefined,this.purpose(command))){
        if(!a.runId){blocked.push({store:code,task_id:a.taskId||null,reason:'迁入任务尚未核对，请先处理原迁入记录'});continue;}
        runs.add(a.runId);
      }
    }
    this.idle(runs);
    const attempts=this.state.rows('attempts').filter(a=>runs.has(a.runId));
    const publications=this.state.rows('publishes').filter(p=>attempts.some(a=>a.id===p.attemptId));
    for(const a of attempts){
      if(action==='end'){
        if(a.taskId||!DONE.has(a.stage))retained.push({store:a.buyerWarehouseCode,task_id:a.taskId||null,run_id:a.runId});
        continue;
      }
      if(this.state.collectionEnded(a.runId))continue;
      if(action==='end'&&resumableTask(a)){
        const items=this.state.getItems(a.runId);
        if(items.length!==1||items[0].attemptId!==a.id||this.state.pending(identity,a.buyerWarehouseCode).length!==1)
          blocked.push({store:a.buyerWarehouseCode,task_id:a.taskId,reason:'存在多个或范围不明的原任务，请先核对原采集记录'});
        retained.push({store:a.buyerWarehouseCode,task_id:a.taskId,run_id:a.runId});
        continue;
      }
      const summary=a.taskSummary;
      const finished=['FINISHED','DOWNLOADED','VALIDATED'].includes(a.stage)&&a.taskId&&summary?.taskId===a.taskId&&
        Number.isSafeInteger(summary.successCount)&&summary.successCount>=0&&Number.isSafeInteger(summary.failCount)&&summary.failCount>=0;
      if(!DONE.has(a.stage)&&!(action==='end'&&finished))blocked.push({store:a.buyerWarehouseCode,task_id:a.taskId||null,
        reason:['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(a.stage)?'平台受理情况未核清，不能丢弃防重记录；请核对原平台任务':'平台任务尚未确认完成，请继续原采集任务核对'});
    }
    if(action==='delete'&&publications.some(p=>!this.state.collectionEnded(this.state.getAttempt(p.attemptId)?.runId)))blocked.push({store:'',task_id:null,reason:'已有文件正在恢复保存，请先安全结束原采集'});
    const related=this.commands.list().filter(c=>c.items.some(i=>runs.has(i.run_id)));
    for(const c of related)if(!sameIdentity(c.config_snapshot.tenantBinding,identity))fail('MAINTENANCE_SCOPE','关联记录身份不一致，未修改');
    const basis={id,action,commands:related,runs:[...runs].map(r=>this.state.getRun(r)),
      items:[...runs].flatMap(r=>this.state.getItems(r)),attempts,publications,blocked};
    return {id,action,token:sha256(canonical(basis)),fingerprint:command.fingerprint,
      stores:command.items.map(i=>({code:i.warehouse_code,name:command.config_snapshot.targets.find(t=>t.buyerWarehouseCode===i.warehouse_code)?.displayName||i.warehouse_code})),
      related:related.filter(c=>c.id!==id).map(c=>({id:c.id,stores:c.items.filter(i=>runs.has(i.run_id)).map(i=>i.warehouse_code)})),
      blocked,retained,run_ids:[...runs],command_ids:related.map(c=>c.id)};
  }
  apply(id,action,token){
    const current=this.commands.get(id);
    if(action==='end'&&current?.end_receipt?.token===token)
      return {records:current.end_receipt.command_ids.map(i=>publicRecord(this.commands.get(i))).filter(Boolean)};
    return this.state.write(()=>{
      const plan=this.preview(id,action);
      if(plan.token!==token)fail('COLLECTION_CHANGED','采集记录已变化，请重新预览确认');
      if(plan.blocked.length)fail('COLLECTION_UNRESOLVED',plan.blocked.map(b=>`${b.store}：${b.reason}${b.task_id?'（任务 '+b.task_id+'）':''}`).join('\n'));
      if(plan.already_deleted)return {deleted:id,fingerprint:plan.fingerprint};
      const at=now(),runs=new Set(plan.run_ids);
      if(action==='delete'){
        // Keep all original files, manifests and assets. Only discard this command's metadata.
        this.db.prepare('INSERT INTO machine_retired VALUES(?,?,?,?)').run(id,plan.fingerprint,token,at);
        this.db.prepare('DELETE FROM machine_events WHERE command_id=?').run(id);
        this.db.prepare('DELETE FROM machine_commands WHERE id=?').run(id);
        for(const runId of runs){
          // A defensive guard for older records that might share a run.
          if(this.commands.list().some(c=>c.items.some(i=>i.run_id===runId)))continue;
          for(const item of this.state.getItems(runId))this.db.prepare('DELETE FROM attempts WHERE item_id=?').run(item.id);
          this.db.prepare('DELETE FROM items WHERE run_id=?').run(runId);
          this.db.prepare('DELETE FROM runs WHERE id=?').run(runId);
        }
        return {deleted:id,fingerprint:plan.fingerprint};
      }
      for(const a of this.state.rows('attempts').filter(a=>runs.has(a.runId)&&!DONE.has(a.stage))){
        const ended={...a,stage:'ABANDONED',previousStage:a.stage,endedAt:at,endedByCommand:id};
        this.db.prepare('UPDATE attempts SET stage=?,payload=? WHERE id=?').run('ABANDONED',JSON.stringify(ended),a.id);
      }
      for(const runId of runs){
        const run=this.state.getRun(runId);if(!run)continue;
        this.db.prepare('INSERT OR IGNORE INTO retired_collections VALUES(?,?,?)').run(runId,at,id);
        this.retirePublications(runId,at);
        for(const item of this.state.getItems(runId))if(!['SUCCEEDED','REUSED','ENDED'].includes(item.stage)){
          this.db.prepare('UPDATE items SET payload=? WHERE id=?').run(JSON.stringify({...item,stage:'ENDED',previousStage:item.stage,endedAt:at}),item.id);
        }
        this.db.prepare('UPDATE runs SET payload=? WHERE id=?').run(JSON.stringify({...run,status:'ENDED',endedAt:at,resumePending:false}),runId);
      }
      const records=[];
      for(const commandId of plan.command_ids){
        const old=this.commands.get(commandId);
        const items=old.items.map(i=>runs.has(i.run_id)&&!['SUCCEEDED','ENDED'].includes(i.state)?{...i,state:'ENDED',previous_state:i.state,ended_at:at,retained_platform_task:plan.retained.some(a=>a.run_id===i.run_id)}:i);
        const all=items.every(i=>['SUCCEEDED','ENDED'].includes(i.state));
        const next=all?'ENDED':old.state==='NEEDS_REVIEW'&&!items.some(i=>i.state==='NEEDS_REVIEW')?'PAUSED':old.state;
        const record={...old,items,state:next,sequence:old.sequence+1,updated_at:at};
        if(commandId===id)record.end_receipt={token,command_ids:plan.command_ids};
        this.db.prepare('UPDATE machine_commands SET state=?,sequence=?,payload=? WHERE id=?').run(record.state,record.sequence,JSON.stringify(record),commandId);
        this.commands.insertEvent(record,'COLLECTION_ENDED',{source_command:id,stores:items.filter(i=>runs.has(i.run_id)).map(i=>i.warehouse_code)});
        records.push(publicRecord(record));
      }
      return {records};
    });
  }

  reexportPreview(config,operationId,codes,purpose='replenishment'){
    const prior=this.db.prepare('SELECT payload FROM collection_reexports WHERE id=?').get(operationId);
    if(prior){const receipt=JSON.parse(prior.payload);if(canonical(receipt.scope)!==canonical({codes,purpose,merchant:config.tenantBinding.merchantCode}))fail('COLLECTION_CHANGED','重导操作编号对应不同范围');return {...receipt.plan,applied:true};}
    const available=new Map(config.targets.map(t=>[t.buyerWarehouseCode,t]));
    if(!codes.length||codes.some(c=>!available.has(c)))fail('MAINTENANCE_SCOPE','重导门店不属于当前已核验范围');
    const wanted=new Set(codes),merchant=config.tenantBinding.merchantCode;
    const related=this.commands.list().filter(c=>c.request.account_binding.merchant_code===merchant&&this.purpose(c)===purpose&&
      c.items.some(i=>wanted.has(i.warehouse_code)&&!['SUCCEEDED','ENDED'].includes(i.state)));
    const runs=new Set(related.flatMap(c=>c.items.filter(i=>wanted.has(i.warehouse_code)&&!['SUCCEEDED','ENDED'].includes(i.state)).map(i=>i.run_id)));
    const attempts=this.state.rows('attempts').filter(a=>a.merchantCode===merchant&&wanted.has(a.buyerWarehouseCode)&&this.state.collectionPurposeForRun(a.runId,a)===purpose&&!this.state.collectionEnded(a.runId)&&!['SUCCEEDED','ABANDONED'].includes(a.stage));
    for(const a of attempts)runs.add(a.runId);
    for(const rid of runs){const r=this.state.getRun(rid);if(!r||r.targets.some(t=>!wanted.has(t.buyerWarehouseCode)))fail('MAINTENANCE_SCOPE','原采集归属范围不完整，未结束其他门店');}
    this.idle(runs);
    const imported=this.db.prepare('SELECT record_key,payload FROM imported_pending').all().map(r=>({...JSON.parse(r.payload),_recordKey:r.record_key}))
      .filter(a=>a.merchantCode===merchant&&wanted.has(a.buyerWarehouseCode)&&collectionPurpose(a)===purpose&&!a.retiredAt);
    const stores=codes.map(code=>({code,name:available.get(code).displayName}));
    const basis={operationId,codes,purpose,merchant,commands:related,runs:[...runs].map(id=>this.state.getRun(id)),attempts,imported,
      publications:this.state.rows('publishes').filter(p=>runs.has(this.state.getAttempt(p.attemptId)?.runId))};
    return {id:operationId,action:'reexport',token:sha256(canonical(basis)),stores,blocked:[],purpose,
      merchant,run_ids:[...runs],command_ids:related.map(c=>c.id),imported,
      related:related.map(c=>({id:c.id,stores:c.items.filter(i=>runs.has(i.run_id)).map(i=>i.warehouse_code)})),
      retained:attempts.map(a=>({store:a.buyerWarehouseCode,task_id:a.taskId||null,run_id:a.runId}))};
  }
  reexportApply(config,operationId,codes,purpose,token){
    const prior=this.db.prepare('SELECT payload FROM collection_reexports WHERE id=?').get(operationId);
    if(prior){const plan=this.reexportPreview(config,operationId,codes,purpose);if(plan.token!==token)fail('COLLECTION_CHANGED','重导确认凭据不同');return JSON.parse(prior.payload).receipt;}
    return this.state.write(()=>{
      const plan=this.reexportPreview(config,operationId,codes,purpose);
      if(plan.token!==token)fail('COLLECTION_CHANGED','所选旧采集已变化，请重新核对重导范围');
      const at=now(),runs=new Set(plan.run_ids);
      for(const rid of runs){
        this.db.prepare('INSERT OR IGNORE INTO retired_collections VALUES(?,?,?)').run(rid,at,operationId);
        this.retirePublications(rid,at);
        const r=this.state.getRun(rid);this.db.prepare('UPDATE runs SET payload=? WHERE id=?').run(JSON.stringify({...r,status:'ENDED',endedAt:at,resumePending:false}),rid);
        for(const i of this.state.getItems(rid)){
          this.db.prepare('UPDATE items SET payload=? WHERE id=?').run(JSON.stringify({...i,stage:'ENDED',endedAt:at}),i.id);
          const a=this.state.getAttempt(i.attemptId);if(a)this.db.prepare('UPDATE attempts SET stage=?,payload=? WHERE id=?').run('ABANDONED',JSON.stringify({...a,stage:'ABANDONED',previousStage:a.stage,endedAt:at,endedByCommand:operationId}),a.id);
        }
      }
      for(const {_recordKey,...a} of plan.imported)this.db.prepare('UPDATE imported_pending SET payload=? WHERE record_key=?').run(JSON.stringify({...a,retiredAt:at,retiredBy:operationId}),_recordKey);
      const records=[];
      for(const id of plan.command_ids){
        const old=this.commands.get(id),items=old.items.map(i=>runs.has(i.run_id)?{...i,state:'ENDED',previous_state:i.state,ended_at:at}:i);
        const all=items.every(i=>['SUCCEEDED','ENDED'].includes(i.state));
        const record={...old,items,state:all?'ENDED':old.state==='NEEDS_REVIEW'&&!items.some(i=>i.state==='NEEDS_REVIEW')?'PAUSED':old.state,sequence:old.sequence+1,updated_at:at};
        this.db.prepare('UPDATE machine_commands SET state=?,sequence=?,payload=? WHERE id=?').run(record.state,record.sequence,JSON.stringify(record),id);
        this.commands.insertEvent(record,'REEXPORTED_SELECTED',{operation_id:operationId,stores:codes});records.push(publicRecord(record));
      }
      const receipt={id:operationId,records,ended_at:at,stores:plan.stores};
      this.db.prepare('INSERT INTO collection_reexports VALUES(?,?)').run(operationId,JSON.stringify({scope:{codes,purpose,merchant:config.tenantBinding.merchantCode},plan,receipt}));
      return receipt;
    });
  }
}
