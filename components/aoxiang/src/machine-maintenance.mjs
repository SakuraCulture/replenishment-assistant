// Local collection maintenance only. No browser, filesystem deletion or business tables.
import {canonical,sha256,now,fail,sameIdentity} from './common.mjs';
import {publicRecord} from './machine-protocol.mjs';

const ACTIVE=new Set(['QUEUED','RUNNING','PAUSE_REQUESTED']);
const DONE=new Set(['SUCCEEDED','REUSED','FAILED_CONFIRMED','CANCELLED_BEFORE_SUBMIT','ABANDONED']);
const CLOSED=new Set(['SUCCEEDED','ENDED']);
export class CollectionMaintenance{
  constructor(state,commands,runtime){
    Object.assign(this,{state,commands,runtime});this.db=state.db;
    this.db.exec('CREATE TABLE IF NOT EXISTS machine_retired(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,token TEXT NOT NULL,deleted_at TEXT NOT NULL)');
  }
  idle(){
    if(this.runtime.closing||this.runtime.active||this.commands.list().some(c=>ACTIVE.has(c.state)))
      fail('COLLECTION_BUSY','先点“停止后续取数”，等待当前动作安全暂停后，再结束或删除采集记录');
  }
  preview(id,action){
    if(!['end','delete'].includes(action))fail('REQUEST_SCHEMA','未知采集维护动作');
    this.idle();
    const retired=this.db.prepare('SELECT * FROM machine_retired WHERE id=?').get(id);
    if(retired){
      if(action!=='delete')fail('COMMAND_RETIRED','该采集记录已删除，不能恢复或重发');
      return {id,action,token:retired.token,already_deleted:true,fingerprint:retired.fingerprint,stores:[],related:[],blocked:[]};
    }
    const command=this.commands.get(id);
    if(!command)fail('COMMAND_NOT_FOUND','未找到原采集任务，请刷新记录');
    if(action==='delete'&&!CLOSED.has(command.state))fail('COLLECTION_NOT_ENDED','请先结束本次采集，再删除记录');
    const runs=new Set(command.items.map(i=>i.run_id)),identity=command.config_snapshot.tenantBinding;
    const blocked=[];
    // A blocked new command may point at earlier failed attempts for these stores.
    if(action==='end')for(const code of command.request.scope.warehouse_codes){
      for(const a of this.state.pending(identity,code)){
        if(!a.runId){blocked.push({store:code,task_id:a.taskId||null,reason:'迁入任务尚未核对，请先处理原迁入记录'});continue;}
        runs.add(a.runId);
      }
    }
    const attempts=this.state.rows('attempts').filter(a=>runs.has(a.runId));
    const publications=this.state.rows('publishes').filter(p=>attempts.some(a=>a.id===p.attemptId));
    for(const a of attempts){
      const summary=a.taskSummary;
      const finished=['FINISHED','DOWNLOADED','VALIDATED'].includes(a.stage)&&a.taskId&&summary?.taskId===a.taskId&&
        Number.isSafeInteger(summary.successCount)&&summary.successCount>=0&&Number.isSafeInteger(summary.failCount)&&summary.failCount>=0;
      if(!DONE.has(a.stage)&&!(action==='end'&&finished))blocked.push({store:a.buyerWarehouseCode,task_id:a.taskId||null,
        reason:['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(a.stage)?'平台受理情况未核清，不能丢弃防重记录；请核对原平台任务':'平台任务尚未确认完成，请继续原采集任务核对'});
    }
    if(publications.length)blocked.push({store:'',task_id:null,reason:'已有文件正在恢复保存，请先继续原任务完成文件保存'});
    const related=this.commands.list().filter(c=>c.items.some(i=>runs.has(i.run_id)));
    for(const c of related)if(!sameIdentity(c.config_snapshot.tenantBinding,identity))fail('MAINTENANCE_SCOPE','关联记录身份不一致，未修改');
    const basis={id,action,commands:related,runs:[...runs].map(r=>this.state.getRun(r)),
      items:[...runs].flatMap(r=>this.state.getItems(r)),attempts,publications,blocked};
    return {id,action,token:sha256(canonical(basis)),fingerprint:command.fingerprint,
      stores:command.items.map(i=>({code:i.warehouse_code,name:command.config_snapshot.targets.find(t=>t.buyerWarehouseCode===i.warehouse_code)?.displayName||i.warehouse_code})),
      related:related.filter(c=>c.id!==id).map(c=>({id:c.id,stores:c.items.filter(i=>runs.has(i.run_id)).map(i=>i.warehouse_code)})),
      blocked,run_ids:[...runs],command_ids:related.map(c=>c.id)};
  }
  apply(id,action,token){
    this.idle();
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
        for(const item of this.state.getItems(runId))if(!['SUCCEEDED','REUSED','ENDED'].includes(item.stage)){
          this.db.prepare('UPDATE items SET payload=? WHERE id=?').run(JSON.stringify({...item,stage:'ENDED',previousStage:item.stage,endedAt:at}),item.id);
        }
        if(run.status!=='SUCCEEDED')this.db.prepare('UPDATE runs SET payload=? WHERE id=?').run(JSON.stringify({...run,status:'ENDED',endedAt:at}),runId);
      }
      const records=[];
      for(const commandId of plan.command_ids){
        const old=this.commands.get(commandId);
        const items=old.items.map(i=>runs.has(i.run_id)&&!['SUCCEEDED','ENDED'].includes(i.state)?{...i,state:'ENDED',previous_state:i.state,ended_at:at}:i);
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
}
