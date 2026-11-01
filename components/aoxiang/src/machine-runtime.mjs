import {fail,redact} from './common.mjs';

const ordinaryFailures=new Set(['EMPTY_UNVERIFIED','TASK_HAS_FAILURES','QUERY_FILE_COUNT_DIFF','TASK_TYPE_MISMATCH']);
const reviewFailures=new Set(['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','UNRESOLVED_TASK']);
export const errorInfo=e=>({code:e.code||'FAILED',message:redact(e.message||'自动化操作失败'),details:redact(e.details||{})});

// Serial browser queue; control requests remain independent of awaited browser I/O.
export class MachineRuntime{
  constructor({commands,executeItem,onEvent=()=>{},needsReview=()=>false,onFatal=()=>{}}){
    Object.assign(this,{commands,executeItem,onEvent,needsReview,onFatal});
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
    return this.change(id,record.state,{kind:'PROGRESS',detail:redact(detail)});
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
    return this.change(id,'QUEUED',{error:null});
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
      for(const initial of this.commands.get(id).items){
        if(['SUCCEEDED','FAILED'].includes(initial.state))continue;
        if(control.signal.aborted){this.change(id,'PAUSED');return;}
        this.updateItem(id,initial.index,{state:'RUNNING',error:null});
        try{
          const record=this.commands.get(id);
          const result=await this.executeItem({record,item:initial,signal:control.signal,onProgress:e=>this.progress(id,{index:initial.index,...e})});
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
          this.updateItem(id,initial.index,{state:'INTERRUPTED',error:info});
          this.change(id,'INTERRUPTED',{error:info});return;
        }
      }
      const items=this.commands.get(id).items;
      this.change(id,items.some(i=>i.state==='FAILED')?'COMPLETED_WITH_ERRORS':'SUCCEEDED',{
        result:{items,succeeded:items.filter(i=>i.state==='SUCCEEDED').length,failed:items.filter(i=>i.state==='FAILED').length}
      });
    }finally{this.active=null;}
  }
}
