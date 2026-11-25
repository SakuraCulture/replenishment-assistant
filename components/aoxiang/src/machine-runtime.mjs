import {fail,redact,wait} from './common.mjs';
import {collectionPurpose} from './state.mjs';

const ordinaryFailures=new Set(['EMPTY_UNVERIFIED','TASK_HAS_FAILURES','QUERY_FILE_COUNT_DIFF','TASK_TYPE_MISMATCH',
  'SCHEMA_DRIFT','ENTITY_MISMATCH','ROW_COUNT_MISMATCH','NULL_KEYS','UNSAFE_NATIVE_ID','NOT_XLSX','SHEET_MISMATCH','HEADER_MISSING',
  'INVALID_SHARED_STRING','FILE_SIZE','ASSET_NOT_REUSABLE','EXISTING_ASSET_CHANGED','SAVE_BLOCKED','STAGING_HASH_CHANGED','FINAL_HASH_FAILED']);
const reviewFailures=new Set(['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','UNRESOLVED_TASK']);
const settled=new Set(['SUCCEEDED','FAILED','ENDED','NEEDS_REVIEW']);
export const errorInfo=e=>({code:e.code||'FAILED',message:redact(e.message||'自动化操作失败'),details:redact(e.details||{})});

// Serial browser queue; control requests remain independent of awaited browser I/O.
export class MachineRuntime{
  constructor({commands,executeItem,onEvent=()=>{},needsReview=()=>false,canResumeReview=()=>false,onFatal=()=>{},pipeline=false,canStart=()=>true,durableQueue=false}){
    Object.assign(this,{commands,executeItem,onEvent,needsReview,canResumeReview,onFatal,pipeline,canStart,durableQueue});
    this.active=null;this.pumping=null;this.closing=false;
  }
  change(id,next,options){
    const old=this.commands.get(id);
    if(!old)fail('COMMAND_NOT_FOUND','未找到自动化作业');
    const durableQueue=this.durableQueue&&collectionPurpose(old.request)==='replenishment';
    const changed=this.commands.advance(id,old.sequence,next,{...options,durableQueue});
    this.onEvent(changed.event);return changed.record;
  }
  progress(id,detail){
    const record=this.commands.get(id);
    const items=structuredClone(record.items);
    if(Number.isInteger(detail.index)&&items[detail.index]){
      if(items[detail.index].state==='ENDED')return record;
      if(detail.stage)items[detail.index].phase=detail.stage;
      if(detail.taskId)items[detail.index].task_id=detail.taskId;
    }
    return this.change(id,record.state,{kind:'PROGRESS',detail:redact(detail),items});
  }
  updateItem(id,index,values){
    const record=this.commands.get(id),items=structuredClone(record.items);
    items[index]={...items[index],...values};
    return this.change(id,record.state,{kind:'ITEM',detail:{index,warehouse_code:items[index].warehouse_code,state:items[index].state},items});
  }
  pause(id){
    const record=this.commands.get(id);
    if(!record)fail('COMMAND_NOT_FOUND','未找到自动化作业');
    if(record.state==='QUEUED')return this.change(id,'PAUSED');
    if(record.state==='RUNNING'){
      const changed=this.change(id,'PAUSE_REQUESTED',{detail:{message:'停止后续平台动作；当前动作结束后暂停，已受理任务不会撤销'}});
      this.active?.id===id&&this.active.control.abort();return changed;
    }
    return record;
  }
  resume(id){
    if(this.closing)fail('SHUTTING_DOWN','组件正在安全退出');
    const record=this.commands.get(id);
    if(['QUEUED','RUNNING'].includes(record?.state))return record;
    if(!['PAUSED','INTERRUPTED'].includes(record?.state))fail('COMMAND_TRANSITION','请使用核对原回执或重跑失败项，不能直接重派终态作业');
    return this.change(id,'QUEUED',{error:null,selectedRetry:null});
  }
  continueReviewed(id){
    if(this.closing)fail('SHUTTING_DOWN','组件正在安全退出');
    const record=this.commands.get(id);
    if(record?.state==='NEEDS_REVIEW'){
      const items=structuredClone(record.items);
      for(const item of items)if(item.state==='NEEDS_REVIEW'&&this.canResumeReview(item,record))item.state='PENDING';
      // Missing receipts stay blocked. Only independently runnable stores or
      // exact receipts already persisted by the component may resume.
      if(!items.some(i=>!['SUCCEEDED','FAILED','ENDED','NEEDS_REVIEW'].includes(i.state)))
        fail('COMMAND_TRANSITION','原任务尚无可靠受理回执，继续时保留待核。需要新文件时可新建独立采集。');
      return this.change(id,'QUEUED',{kind:'RESUME_WITH_REVIEW_RETAINED',items,error:null,selectedRetry:null});
    }
    fail('COMMAND_TRANSITION','仅可核对待核原采集，请刷新当前记录');
  }
  retry(id,indices,sequence){
    if(this.closing)fail('SHUTTING_DOWN','组件正在安全退出');
    const record=this.commands.get(id);
    if(!record||record.sequence!==sequence)fail('COMMAND_CONFLICT','作业已变化，请刷新');
    if(!['PAUSED','INTERRUPTED','COMPLETED_WITH_ERRORS'].includes(record.state))fail('COMMAND_TRANSITION','先安全停止当前取数，再重跑原失败项');
    if(!Array.isArray(indices)||!indices.length||new Set(indices).size!==indices.length||indices.some(i=>!Number.isInteger(i)||!record.items[i]||record.items[i].state!=='FAILED'))fail('COMMAND_RETRY_SCOPE','仅可选择失败项；结果待核项不能重新提交');
    const items=structuredClone(record.items);
    for(const i of indices){items[i].previous_error=items[i].error;items[i].error=null;items[i].state='PENDING';}
    return this.change(id,'QUEUED',{kind:'RETRY_ORIGINAL_ITEMS',detail:{indices},items,error:null,selectedRetry:null});
  }
  retrySelected(id,indices,sequence,retryId){
    if(this.closing)fail('SHUTTING_DOWN','组件正在安全退出');
    const record=this.commands.get(id);
    if(!record||collectionPurpose(record.request)!=='replenishment')fail('COMMAND_RETRY_SCOPE','只能继续原补货建议采集');
    if(!indices.length||indices.some(i=>!record.items[i]))fail('COMMAND_RETRY_SCOPE','重试门店不属于原采集');
    indices=[...indices].sort((a,b)=>a-b);
    const prior=record.selected_retry,same=prior?.id===retryId;
    if(same&&JSON.stringify(prior.indices)!==JSON.stringify(indices))fail('COMMAND_RETRY_SCOPE','同一次重试不能变更门店范围');
    if(same&&!['PAUSED','INTERRUPTED'].includes(record.state))return record;
    if(record.sequence!==sequence)fail('COMMAND_CONFLICT','作业已变化，请刷新');
    if(!['PAUSED','INTERRUPTED','NEEDS_REVIEW','COMPLETED_WITH_ERRORS'].includes(record.state))fail('COMMAND_TRANSITION','原采集仍在执行或已经完成，请刷新');
    const items=structuredClone(record.items);
    let runnable=0;
    for(const index of indices){
      const item=items[index];
      if(['SUCCEEDED','ENDED'].includes(item.state)||same&&item.state==='FAILED')continue;
      if(item.state==='NEEDS_REVIEW'&&!this.canResumeReview(item,record))continue;
      if(item.error)item.previous_error=item.error;
      item.error=null;item.state='PENDING';runnable++;
    }
    if(!runnable)return record;
    return this.change(id,'QUEUED',{kind:'RETRY_SELECTED_ITEMS',detail:{indices,retry_id:retryId},
      selectedRetry:{id:retryId,indices},items,error:null});
  }
  kick(){
    if(this.pumping||this.closing)return;
    this.pumping=this.pump().catch(error=>{
      this.failure=error;this.closing=true;
      // Persistence failures must stop scheduling, never become successful commands.
      this.onFatal(error);
    }).finally(()=>{this.pumping=null;});
  }
  async idle(){while(this.pumping)await this.pumping;}
  stop(){
    this.closing=true;
    for(const record of this.commands.list()){
      if(record.state==='RUNNING'||record.state==='QUEUED'&&!record.durable_queue)
        this.pause(record.id);
    }
  }
  async shutdown(){
    this.stop();
    await this.idle();
  }
  async pump(){
    for(;;){
      if(this.closing)return;
      const record=this.commands.list().find(r=>r.state==='QUEUED');if(!record)return;
      if(!this.canStart()){await wait(250);continue;}
      await this.run(record.id);
    }
  }
  async run(id){
    const control=new AbortController();this.active={id,control};
    const started=this.change(id,'RUNNING'),selected=started.selected_retry?.indices;
    const inScope=item=>!selected||selected.includes(item.index);
    try{
      const phases=this.pipeline?['submit','collect']:['complete'];
      for(const phase of phases){
      const deadline=Date.now()+this.commands.get(id).config_snapshot.resourceLimits.taskWaitBudgetMs;
      const missing=new Map();
      do{
      for(const initial of this.commands.get(id).items){
        if(!inScope(initial)||settled.has(initial.state)||missing.has(initial.index))continue;
        if(control.signal.aborted){this.change(id,'PAUSED');return;}
        this.updateItem(id,initial.index,{state:'RUNNING',error:null});
        try{
          const record=this.commands.get(id);
          const execute=()=>this.executeItem({record,item:initial,phase,signal:control.signal,onProgress:e=>{
            this.progress(id,{index:initial.index,...e});
          }});
          let result;
          for(let retry=0;;retry++){
            try{result=await execute();break;}catch(e){
              if(retry||control.signal.aborted||!(e.code==='EXPORT_NOT_DISPATCHED'||this.canResumeReview(initial,record)&&!['WRONG_IDENTITY','WAIT_LOGIN','QUERY_SCOPE_MISMATCH','WRONG_SCOPE_ACCEPTED'].includes(e.code)))throw e;
              this.progress(id,{index:initial.index,stage:'RECOVERING',message:'重新核验原范围并恢复一次；没有可靠未提交证据时不新建导出'});
            }
          }
          if(this.pipeline&&result?.pending){
            this.updateItem(id,initial.index,{state:'RUNNING',phase:'WAIT_TASK',error:null});
            continue;
          }
          this.updateItem(id,initial.index,{state:'SUCCEEDED',result,error:null});
        }catch(error){
          const info=errorInfo(error);
          // An unknown platform acceptance always outranks a local pause request.
          if(reviewFailures.has(info.code)||this.needsReview(initial)){
            this.updateItem(id,initial.index,{state:'NEEDS_REVIEW',error:info});
            if(!this.pipeline||control.signal.aborted||['WRONG_IDENTITY','WAIT_LOGIN','IDENTITY_CHECK_FAILED','IDENTITY_RESPONSE_TIMEOUT'].includes(info.code)){
              this.change(id,'NEEDS_REVIEW',{error:info});return;
            }
            // Keep this item frozen; each following store must independently re-query and verify scope.
            continue;
          }
          if(control.signal.aborted||info.code==='PAUSED'){
            this.updateItem(id,initial.index,{state:'PAUSED',error:info});
            this.change(id,'PAUSED',{error:info});return;
          }
          if(ordinaryFailures.has(info.code)){
            this.updateItem(id,initial.index,{state:'FAILED',error:info});continue;
          }
          if(this.pipeline&&phase==='collect'&&info.code==='TASK_NOT_FOUND'){
            this.updateItem(id,initial.index,{state:'INTERRUPTED',error:info});
            missing.set(initial.index,info);continue;
          }
          this.updateItem(id,initial.index,{state:'INTERRUPTED',error:info});
          this.change(id,'INTERRUPTED',{error:info});return;
        }
      }
      if(control.signal.aborted){this.change(id,'PAUSED');return;}
      const pending=this.commands.get(id).items.some(i=>inScope(i)&&!settled.has(i.state)&&!missing.has(i.index));
      if(!pending&&missing.size){this.change(id,'INTERRUPTED',{error:missing.values().next().value});return;}
      if(phase!=='collect'||!pending)break;
      if(Date.now()>=deadline){this.change(id,'INTERRUPTED',{error:{code:'TASK_PENDING',message:'平台仍在生成部分文件；已完成文件保留，请继续原采集任务',details:{}}});return;}
      // Sleep once per round, not per store. Pause wakes the wait immediately.
      await new Promise(resolve=>{
        const done=()=>{clearTimeout(timer);control.signal.removeEventListener('abort',done);resolve();};
        const timer=setTimeout(done,this.commands.get(id).config_snapshot.resourceLimits.pollIntervalMs);
        control.signal.addEventListener('abort',done,{once:true});
        if(control.signal.aborted)done();
      });
      }while(true);
      }
      const items=this.commands.get(id).items;
      const next=items.some(i=>i.state==='NEEDS_REVIEW')?'NEEDS_REVIEW':items.some(i=>!settled.has(i.state))?'INTERRUPTED':
        items.some(i=>['FAILED','ENDED'].includes(i.state))?'COMPLETED_WITH_ERRORS':'SUCCEEDED';
      this.change(id,next,{
        result:{items,succeeded:items.filter(i=>i.state==='SUCCEEDED').length,failed:items.filter(i=>i.state==='FAILED').length}
      });
    }finally{this.active=null;}
  }
}
