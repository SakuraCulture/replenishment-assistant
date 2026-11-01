import {fail} from './common.mjs';
import {errorInfo} from './machine-runtime.mjs';

export class MachinePurchaseReceipt{
  constructor({ledger,service,onFatal=()=>{}}){Object.assign(this,{ledger,service,onFatal});this.pending=null;this.job=null;}
  get busy(){return !!this.pending;}
  status(id){const r=this.ledger.get(id)||this.service.register(id);return {...r,active:this.busy&&this.job?.id===id,stage:this.job?.id===id?this.job.stage:null,pauseRequested:this.job?.id===id?this.job.cancelled:false};}
  progress(stage){if(this.job)this.job.stage=stage;}
  start(id){
    if(this.busy)fail('RECEIPT_BUSY','收货同步正在执行或安全停止');
    const r=this.service.register(id);
    if(['READY','SYNCED'].includes(r.state))return this.status(id);
    this.ledger.begin(id);this.control=new AbortController();this.job={id,cancelled:false,stage:r.terminal?'恢复最终实收文件，不恢复常规监控':'查询当前采购主单及实收明细'};
    this.pending=Promise.resolve().then(async()=>{try{
      if(this.control.signal.aborted)fail('PAUSED','已停止新动作');
      await this.service.observe(id,{signal:this.control.signal});this.progress('原件已保存，正式实收按完整终态另行核验');
    }catch(e){this.ledger.failed(id,errorInfo(e),this.control.signal.aborted);this.progress('原任务与停止标记已保存，可继续核对');}}).catch(this.onFatal).finally(()=>{this.pending=null;});
    return this.status(id);
  }
  pause(id){if(this.busy&&this.job.id===id){this.job.cancelled=true;this.control.abort();this.progress('正在停止；保留已受理下载任务，不撤销平台单据');}return this.status(id);}
  async close(){if(this.busy)this.pause(this.job.id);await this.pending;}
}
