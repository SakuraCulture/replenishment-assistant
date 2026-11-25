import {fail,now} from './common.mjs';
import {errorInfo} from './machine-runtime.mjs';
import {purchasePublic} from './purchase-create-ledger.mjs';

function requireOriginalTask(record,expectedTaskId){
  if(expectedTaskId&&(!record.uploadAttempted||record.receipt?.taskId!==expectedTaskId))
    fail('PURCHASE_RECEIPT_CONFLICT','原任务受理号与核对请求不一致，核对请求未进入上传');
}

function mainVerificationRetry(record){
  const error=record.error;
  const details=error?.details;
  if(record.state!=='NEEDS_REVIEW'||!record.receipt||!['SUBMISSION_UNKNOWN','PURCHASE_VERIFY_EXPORT_UNKNOWN'].includes(error?.code)
    ||details?.sourceId!=='D16'||details.taskId||details.canRecoverMain===false)return null;
  return details.runId||null;
}

// A browser adapter is deliberately small: prepare is read-only; upload is called once;
// observe only queries a known import task and verifies the resulting native documents.
export class MachinePurchaseCreate{
  constructor({ledger,adapter,preflight=async()=>{},onFatal=()=>{}}){Object.assign(this,{ledger,adapter,preflight,onFatal});this.pending=null;this.job=null;}
  get busy(){return !!this.pending;}
  status(id){const job=this.job?.id===id?this.job:null;return {...purchasePublic(this.ledger.get(id)),active:!!job&&this.busy,stage:job?.stage||null,pauseRequested:job?.cancelled||false,verificationError:job?.verificationError||null};}
  check(){if(this.job.cancelled)fail('PAUSED','已停止新动作；已受理任务和采购单不会撤销');}
  progress(stage){this.job.stage=stage;this.job.updatedAt=now();}
  start(id,{submitOnly=false,verifyOnly=false,expectedTaskId,originalUnits}={}){
    if(this.busy)fail('PURCHASE_CREATE_BUSY','当前采购任务仍在安全结束，请勿重复执行');
    const r=this.ledger.get(id);requireOriginalTask(r,expectedTaskId);
    if(r.state==='BOUND'&&!verifyOnly)return this.status(id);
    if(r.state==='RETIRED')fail('SOURCE_ORDER_DELETED','来源订单已删除，任务已退役');
    if(r.state==='SUPERSEDED')fail('PURCHASE_SUPERSEDED','旧任务已结束并重建，请打开后继任务；原平台单未撤销',{successorId:r.successorId});
    if(r.state==='CANCELLED_BEFORE_UPLOAD')fail('PURCHASE_CANCELLED','该任务已在上传前取消');
    if(r.receipt&&!r.uploadAttempted)fail('PURCHASE_RECEIPT_CONFLICT','原采购已有受理记录，上传标记不一致，请核对原任务');
    if(r.uploadAttempted&&!r.receipt)fail('SUBMISSION_UNKNOWN','已进入上传边界但无唯一受理任务号，只能核对，不允许重新上传');
    if(r.additionalTaskIds?.length)fail('MULTIPLE_IMPORT_TASKS','导入任务不唯一，请人工核对');
    if(verifyOnly&&(!r.uploadAttempted||!r.receipt))fail('SUBMISSION_UNKNOWN','原采购缺少唯一已受理导入回执；核对请求未进入上传');
    if(submitOnly&&r.receipt)return this.status(id);
    this.job={id,submitOnly,verifyOnly,expectedTaskId,originalUnits,stage:r.uploadAttempted?'核对原导入任务':'核验已确认付款及采购文件',cancelled:false,updatedAt:now()};
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
      requireOriginalTask(r,this.job.expectedTaskId);
      if(this.job.verifyOnly&&(!r.uploadAttempted||!r.receipt))fail('SUBMISSION_UNKNOWN','原导入回执缺失，无法继续核对');
      if(!r.uploadAttempted){
        r=this.ledger.update(id,r.sequence,'PREPARING',{error:null});
        await this.preflight(r.intent);this.check();
        const baseline=await this.adapter.prepare(r.intent,{signal:this.control.signal});this.check();
        r=this.ledger.update(id,r.sequence,'READY',{baseline});
        // Recheck the frozen file and local business token after potentially slow connection.
        await this.preflight(r.intent);this.check();
        this.progress('核对最新付款与采购文件，准备上传');
        const beforeRelease=()=>{
          this.check();r=this.ledger.get(id);
          r=this.ledger.update(id,r.sequence,'SUBMITTING',{uploadAttempted:true});
          this.progress('正在上传采购文件；随后核对原任务');
        };
        // Native transport calls this from its request guard, after all browser
        // waits. Legacy offline adapters keep their existing one-shot contract.
        if(!this.adapter.managesSubmissionBoundary)beforeRelease();
        try{
          const receipt=await this.adapter.upload(r.intent,baseline,{signal:this.control.signal,beforeRelease});
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
      this.check();r=this.ledger.get(id);
      if(this.job.submitOnly){this.progress('平台已受理；可继续提交其他订单，原任务等待核对');return;}
      this.progress('核对原导入任务、采购主单和商品数量金额');
      const retryMainRunId=mainVerificationRetry(r);
      if(r.state!=='BOUND')r=this.ledger.update(id,r.sequence,'VERIFYING',{error:null});
      const evidence=await this.adapter.observe(r.intent,r.receipt,{signal:this.control.signal,retryMainRunId,
        verifyOnly:this.job.verifyOnly, originalUnits:this.job.originalUnits});this.check();
      const bound=this.ledger.bind(id,evidence,{originalUnits:this.job.originalUnits});
      this.progress('已核实原采购单 '+bound.binding.orderId+' · '+bound.binding.status);
    }catch(e){
      r=this.ledger.get(id);const error=errorInfo(e);
      if(r.state==='RETIRED'){this.progress('来源订单已删除；当前动作已结束，不建立本地业务关联');return;}
      if(r.state==='BOUND'&&this.job.verifyOnly){this.job.verificationError=error;this.progress('本次原单核对未完成，保留已有采购关联');return;}
      if(['BOUND','CANCELLED_BEFORE_UPLOAD'].includes(r.state))throw e;
      if(!r.uploadAttempted){if(!['FAILED_PRECHECK','PAUSED'].includes(r.state))this.ledger.update(id,r.sequence,this.job.cancelled?'PAUSED':'FAILED_PRECHECK',{error});}
      else if(r.state==='SUBMITTING')this.ledger.update(id,r.sequence,'NEEDS_REVIEW',{error});
      else if(r.state==='VERIFYING')this.ledger.update(id,r.sequence,this.job.cancelled||['TASK_PENDING','TASK_NOT_FOUND','BROWSER_DISCONNECTED','WORKER_RESPONSE_UNKNOWN'].includes(e.code)?'ACCEPTED':'NEEDS_REVIEW',{error});
      else if(r.state==='ACCEPTED'&&!this.job.cancelled)this.ledger.update(id,r.sequence,'NEEDS_REVIEW',{error});
      // A pause immediately after receipt leaves ACCEPTED intact, preserving exactly that task.
      const terminal=r.importResult?.kind;
      this.progress(terminal==='failed'?'原导入任务失败':terminal==='partial'?'原导入任务部分成功':
        !r.uploadAttempted?'准备未完成，未上传':r.receipt?'原导入任务已保存，等待继续核对':'上传结果待核，禁止重复建单');
    }
  }
}
