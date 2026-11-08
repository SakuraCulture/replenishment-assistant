import {fail,now} from './common.mjs';
import {errorInfo} from './machine-runtime.mjs';
import {purchasePublic} from './purchase-create-ledger.mjs';

// A browser adapter is deliberately small: prepare is read-only; upload is called once;
// observe only queries a known import task and verifies the resulting native documents.
export class MachinePurchaseCreate{
  constructor({ledger,adapter,preflight=async()=>{},onFatal=()=>{}}){Object.assign(this,{ledger,adapter,preflight,onFatal});this.pending=null;this.job=null;}
  get busy(){return !!this.pending;}
  status(id){return {...purchasePublic(this.ledger.get(id)),active:this.job?.id===id&&this.busy,stage:this.job?.id===id?this.job.stage:null,pauseRequested:this.job?.id===id?this.job.cancelled:false};}
  check(){if(this.job.cancelled)fail('PAUSED','已停止新动作；已受理任务和采购单不会撤销');}
  progress(stage){this.job.stage=stage;this.job.updatedAt=now();}
  start(id){
    if(this.busy)fail('PURCHASE_CREATE_BUSY','当前采购任务仍在安全结束，请勿重复执行');
    const r=this.ledger.get(id);if(r.state==='BOUND')return this.status(id);
    if(r.state==='CANCELLED_BEFORE_UPLOAD')fail('PURCHASE_CANCELLED','该任务已在上传前取消');
    if(r.uploadAttempted&&!r.receipt)fail('SUBMISSION_UNKNOWN','已进入上传边界但无唯一受理任务号，只能核对，不允许重新上传');
    if(r.additionalTaskIds?.length)fail('MULTIPLE_IMPORT_TASKS','导入任务不唯一，请人工核对');
    this.job={id,stage:r.uploadAttempted?'核对原导入任务':'核验已确认付款及采购文件',cancelled:false,updatedAt:now()};
    this.control=new AbortController();
    this.pending=Promise.resolve().then(()=>this.run(id)).catch(e=>{this.onFatal(e);}).finally(()=>{this.pending=null;});
    return this.status(id);
  }
  pause(id){this.ledger.get(id);if(this.job?.id===id&&this.busy){this.job.cancelled=true;this.control.abort();this.progress('正在停止；当前已开始动作结束后不再发起新动作');}return this.status(id);}
  cancelBeforeUpload(id){
    if(this.busy)fail('PURCHASE_CREATE_BUSY','先停止并等待当前动作结束');const r=this.ledger.get(id);
    if(r.uploadAttempted)fail('SUBMISSION_UNKNOWN','上传边界之后不能声称撤销，也不能释放订单重新建单');
    return purchasePublic(this.ledger.update(id,r.sequence,'CANCELLED_BEFORE_UPLOAD',{},'CANCELLED_BEFORE_UPLOAD'));
  }
  receipt(r){return this.ledger.accepted(r);}
  async close(){if(this.busy)this.pause(this.job.id);await this.pending;}
  async run(id){
    let r=this.ledger.get(id);
    try{
      this.check();
      if(!r.uploadAttempted){
        r=this.ledger.update(id,r.sequence,'PREPARING',{error:null});
        await this.preflight(r.intent);this.check();
        const baseline=await this.adapter.prepare(r.intent,{signal:this.control.signal});this.check();
        r=this.ledger.update(id,r.sequence,'READY',{baseline});
        // Recheck the frozen file and local business token after potentially slow connection.
        await this.preflight(r.intent);this.check();
        this.progress('正在上传采购文件；此后只查询原任务，不重复上传');
        r=this.ledger.update(id,r.sequence,'SUBMITTING',{uploadAttempted:true});
        try{
          const receipt=await this.adapter.upload(r.intent,baseline,{signal:this.control.signal});
          this.ledger.accepted(receipt); // Persist even when pause arrived in flight.
        }catch(error){
          // The guard has aborted the extra request, while its exact successful
          // upload receipt may already be durably recorded. Continue ONLY the
          // normal complete proof of that original task; never replay the upload.
          r=this.ledger.get(id);
          if(error.code!=='IMPORT_SIDE_EFFECT_BLOCKED'||!r.receipt||r.additionalTaskIds?.length)throw error;
          r=this.ledger.update(id,r.sequence,'NEEDS_REVIEW',{uploadWarning:errorInfo(error),error:errorInfo(error)},'BLOCKED_REQUEST_RECOVERY');
          this.progress('额外请求已阻断，原上传回执已保存；仅继续核验原任务和采购明细');
        }
      }
      this.check();r=this.ledger.get(id);this.progress('核对原导入任务、采购主单和商品数量金额');
      r=this.ledger.update(id,r.sequence,'VERIFYING',{error:null});
      const evidence=await this.adapter.observe(r.intent,r.receipt,{signal:this.control.signal});this.check();
      const bound=this.ledger.bind(id,evidence);this.progress(bound.binding.status==='已作废'?'已核实原采购单已作废，可返回本地删除页重新核对':'采购单已核验并关联，保留待提交');
    }catch(e){
      r=this.ledger.get(id);const error=errorInfo(e);
      if(['BOUND','CANCELLED_BEFORE_UPLOAD'].includes(r.state))throw e;
      if(!r.uploadAttempted)this.ledger.update(id,r.sequence,this.job.cancelled?'PAUSED':'FAILED_PRECHECK',{error});
      else if(r.state==='SUBMITTING')this.ledger.update(id,r.sequence,'NEEDS_REVIEW',{error});
      else if(r.state==='VERIFYING')this.ledger.update(id,r.sequence,this.job.cancelled||['TASK_PENDING','TASK_NOT_FOUND','BROWSER_DISCONNECTED','WORKER_RESPONSE_UNKNOWN'].includes(e.code)?'ACCEPTED':'NEEDS_REVIEW',{error});
      else if(r.state==='ACCEPTED'&&!this.job.cancelled)this.ledger.update(id,r.sequence,'NEEDS_REVIEW',{error});
      // A pause immediately after receipt leaves ACCEPTED intact, preserving exactly that task.
      this.progress(!r.uploadAttempted?'准备未完成，未上传':r.receipt?'原导入任务已保存，等待继续核对':'上传结果待核，禁止重复建单');
    }
  }
}
