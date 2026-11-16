// Functions stay in their owning process. Only one scoped guard request crosses IPC.
import {AppError,uid,redact} from './common.mjs';

const error=()=>new AppError('HOST_GUARD_SCOPE','最终采购核验与本次上传不对应；原生请求未放行');
const dto=e=>redact({code:e.code||'HOST_GUARD_FAILED',message:e.message,details:e.details});
const result=(m,aborted=false)=>({event:aborted?'purchase_guard_aborted':'purchase_guard_result',operationId:m.operationId,guardId:m.guardId,intentId:m.intentId});

export class HostPurchaseGuards{
  constructor(send,pending){this.send=send;this.pending=pending;this.states=new Map();}
  async receive(m){
    if(!['purchase_guard_check','purchase_guard_abort'].includes(m?.event))return false;
    const response=result(m,m.event==='purchase_guard_abort');
    try{
      if(m.event==='purchase_guard_abort'){
        const state=this.states.get(m.operationId);
        if(!state||state.guardId!==m.guardId||state.intentId!==m.intentId||state.hash!==m.requestSha256)throw error();
        state.active=false;await state.done;await this.refuse(state);
        this.send({...response,ok:true});return true;
      }
      const call=this.pending.get(m.operationId);
      if(!call||call.method!=='uploadPurchaseOnce'||call.intentId!==m.intentId||
          typeof m.guardId!=='string'||!/^[a-f0-9]{64}$/.test(m.requestSha256||'')||this.states.has(m.operationId))throw error();
      if(typeof call.beforePurchaseImport!=='function')throw new AppError('HOST_GUARD_REQUIRED','缺少主程序最终采购核验；原生导入请求尚未放行');
      const state={guardId:m.guardId,intentId:m.intentId,hash:m.requestSha256,active:true,refused:false,rollback:null};
      this.states.set(m.operationId,state);
      state.done=(async()=>{
        try{
          const isActive=()=>state.active&&this.pending.get(m.operationId)===call;
          state.rollback=await call.beforePurchaseImport({requestSha256:m.requestSha256,isActive});
          if(!isActive()){
            await this.refuse(state);
            throw new AppError('PAUSED','原生采购请求放行前已停止，未上传');
          }
          this.send({...response,ok:true});
        }catch(e){this.send({...response,error:dto(e)});}
      })();
      await state.done;
    }catch(e){this.send({...response,error:dto(e)});}
    return true;
  }
  async refuse(state){
    if(typeof state.rollback!=='function')return;
    // A failed durable refusal must remain failed. Do not acknowledge a later
    // abort, or replay a partially completed rollback, merely because it ran.
    state.rollbackPromise??=Promise.resolve().then(()=>state.rollback());
    await state.rollbackPromise;state.refused=true;
  }
  finish(id){const state=this.states.get(id);if(state)state.active=false;this.states.delete(id);}
  close(){for(const state of this.states.values())state.active=false;this.states.clear();}
}

export class WorkerPurchaseGuards{
  constructor(send,{timeoutMs=30000,abortTimeoutMs=5000}={}){
    Object.assign(this,{send,timeoutMs,abortTimeoutMs});this.pending=new Map();this.operations=new Map();this.closed=false;
  }
  open(id,intentId){if(this.closed||this.operations.has(id))throw error();this.operations.set(id,intentId);}
  receive(m){
    if(!['purchase_guard_result','purchase_guard_aborted'].includes(m?.event))return false;
    const call=this.pending.get(m.guardId);
    if(!call||call.expected!==m.event||call.scope.operationId!==m.operationId||call.scope.intentId!==m.intentId)return true;
    this.pending.delete(m.guardId);clearTimeout(call.timer);
    if(m.error)call.reject(new AppError(m.error.code,m.error.message,m.error.details));
    else if(m.ok===true)call.resolve();else call.reject(error());
    return true;
  }
  ask(scope,abort=false){
    if(this.closed||this.operations.get(scope.operationId)!==scope.intentId)return Promise.reject(error());
    return new Promise((resolve,reject)=>{
      const expected=abort?'purchase_guard_aborted':'purchase_guard_result';
      const timer=setTimeout(()=>{
        this.pending.delete(scope.guardId);
        reject(new AppError('HOST_GUARD_TIMEOUT','最终采购核验应答超时；原生导入请求未放行'));
      },abort?this.abortTimeoutMs:this.timeoutMs);
      this.pending.set(scope.guardId,{scope,expected,timer,resolve,reject});
      this.send({...scope,event:abort?'purchase_guard_abort':'purchase_guard_check'});
    });
  }
  async check(operationId,intentId,{requestSha256,isActive=()=>true}){
    if(!isActive())throw new AppError('PAUSED','原生采购请求放行前已停止');
    const scope={operationId,intentId,guardId:uid(),requestSha256};
    try{await this.ask(scope);}
    catch(e){
      if(e.code==='HOST_GUARD_TIMEOUT')await this.ask(scope,true).catch(()=>{});
      throw e;
    }
    let refused=false;
    return async()=>{if(refused)return;refused=true;await this.ask(scope,true);};
  }
  finish(id){this.operations.delete(id);}
  close(){
    this.closed=true;this.operations.clear();
    for(const call of this.pending.values()){clearTimeout(call.timer);call.reject(new AppError('BROWSER_DISCONNECTED','最终核验期间执行器已断开；原生请求未放行'));}
    this.pending.clear();
  }
}

export async function uploadWithHostGuard(ui,params,operationId,channel){
  channel.open(operationId,params.intent.id);
  const prior=ui.beforePurchaseImport,guard=details=>channel.check(operationId,params.intent.id,details);
  ui.beforePurchaseImport=guard;
  try{return await ui.uploadPurchaseOnce(params);}
  finally{if(ui.beforePurchaseImport===guard)ui.beforePurchaseImport=prior;channel.finish(operationId);}
}
