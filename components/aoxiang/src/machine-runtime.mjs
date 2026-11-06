import {fail,redact} from './common.mjs';

const ordinaryFailures=new Set(['EMPTY_UNVERIFIED','TASK_HAS_FAILURES','QUERY_FILE_COUNT_DIFF','TASK_TYPE_MISMATCH']);
const reviewFailures=new Set(['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','UNRESOLVED_TASK']);
export const errorInfo=e=>({code:e.code||'FAILED',message:redact(e.message||'自动化操作失败'),details:redact(e.details||{})});

// Serial browser queue; control requests remain independent of awaited browser I/O.
export class MachineRuntime{
  constructor({commands,executeItem,onEvent=()=>{},needsReview=()=>false,onFatal=()=>{},pipeline=false}){
    Object.assign(this,{commands,executeItem,onEvent,needsReview,onFatal,pipeline});
    this.active=null;this.pumping=null;this.closing=false;
  }
  change(id,next,options){
    const old=this.commands.get(id);
    if(!old)fail('COMMAND_NOT_FOUND','未找到自动化作业');
    const changed=this.commands.advance(id,old.sequence,next,options);
    this.onEvent(changed.event);return changed.record;
  }
  progress(id,detail){
    const record=this.commands.get(id);
    const items=structuredClone(record.items);
    if(Number.isInteger(detail.index)&&items[detail.index]){
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
    if(!['PAUSED','INTERRUPTED'].includes(record?.state))fail('COMMAND_TRANSITION','请使用重跑失败项，不能直接重派终态作业');
    return this.change(id,'QUEUED',{error:null});
  }
  retry(id,indices,sequence){
    if(this.closing)fail('SHUTTING_DOWN','组件正在安全退出');
    const record=this.commands.get(id);
    if(!record||record.sequence!==sequence)fail('COMMAND_CONFLICT','作业已变化，请刷新');
    if(!['PAUSED','INTERRUPTED','COMPLETED_WITH_ERRORS'].includes(record.state))fail('COMMAND_TRANSITION','先安全停止当前取数，再重跑原失败项');
    if(!Array.isArray(indices)||!indices.length||new Set(indices).size!==indices.length||indices.some(i=>!Number.isInteger(i)||!record.items[i]||record.items[i].state!=='FAILED'))fail('COMMAND_RETRY_SCOPE','仅可选择失败项；结果待核项不能重新提交');
    const items=structuredClone(record.items);
    for(const i of indices){items[i].previous_error=items[i].error;items[i].error=null;items[i].state='PENDING';}
    return this.change(id,'QUEUED',{kind:'RETRY_ORIGINAL_ITEMS',detail:{indices},items,error:null});
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
  async shutdown(){
    this.closing=true;
    for(const record of this.commands.list())if(['QUEUED','RUNNING'].includes(record.state))this.pause(record.id);
    await this.idle();
  }
  async pump(){
    for(;;){
      if(this.closing)return;
      const record=this.commands.list().find(r=>r.state==='QUEUED');if(!record)return;
      await this.run(record.id);
    }
  }
  async run(id){
    const control=new AbortController();this.active={id,control};
    this.change(id,'RUNNING');
    try{
      const phases=this.pipeline?['submit','collect']:['complete'];
      for(const phase of phases){
      const deadline=Date.now()+this.commands.get(id).config_snapshot.resourceLimits.taskWaitBudgetMs;
      const missing=new Map();
      do{
      for(const initial of this.commands.get(id).items){
        if(['SUCCEEDED','FAILED','ENDED'].includes(initial.state)||missing.has(initial.index))continue;
        if(control.signal.aborted){this.change(id,'PAUSED');return;}
        this.updateItem(id,initial.index,{state:'RUNNING',error:null});
        try{
          const record=this.commands.get(id);
          const result=await this.executeItem({record,item:initial,phase,signal:control.signal,onProgress:e=>{
            this.progress(id,{index:initial.index,...e});
          }});
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
            this.change(id,'NEEDS_REVIEW',{error:info});return;
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
      const pending=this.commands.get(id).items.some(i=>!['SUCCEEDED','FAILED','ENDED'].includes(i.state)&&!missing.has(i.index));
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
      this.change(id,items.some(i=>['FAILED','ENDED'].includes(i.state))?'COMPLETED_WITH_ERRORS':'SUCCEEDED',{
        result:{items,succeeded:items.filter(i=>i.state==='SUCCEEDED').length,failed:items.filter(i=>i.state==='FAILED').length}
      });
    }finally{this.active=null;}
  }
}
