import {uid,now,fail,readJson} from './common.mjs';
import {loadConfig,validateConfig} from './config.mjs';
import {assertHandoff} from './preflight.mjs';
import {PurchaseTemplateService} from './purchase-template.mjs';
import {errorInfo} from './machine-runtime.mjs';
import path from 'node:path';

// Native template GET only. Its existing service persists immutable source/version records.
export class MachineTemplate{
  constructor({root,state,browsers,execute}){
    Object.assign(this,{root,state,browsers});this.job=null;this.pending=null;
    this.execute=execute||((signal,onProgress)=>this.fetch(signal,onProgress));
  }
  get busy(){return !!this.pending;}
  status(id){if(!this.job||this.job.id!==id)fail('TEMPLATE_JOB_NOT_FOUND','模板获取记录不在当前会话，请查看已保存模板获取记录');return structuredClone(this.job);}
  start(){
    if(this.busy)fail('TEMPLATE_BUSY','当前模板获取尚未结束');
    const control=new AbortController();this.control=control;
    this.job={id:uid(),state:'RUNNING',stage:'准备获取最新采购模板',created_at:now(),updated_at:now(),receipt:null,error:null,cancel_requested:false};
    this.pending=Promise.resolve().then(async()=>{
      try{
        const receipt=await this.execute(control.signal,stage=>{this.job.stage=stage;this.job.updated_at=now();});
        if(control.signal.aborted){this.job.state='CANCELLED';this.job.stage='已停止；可能保留已下载的模板获取记录';}
        else{this.job.state='READY';this.job.receipt=receipt;this.job.stage='最新模板已下载并通过字段契约核验';}
      }catch(e){this.job.state=control.signal.aborted?'CANCELLED':'FAILED';this.job.error=errorInfo(e);this.job.stage=control.signal.aborted?'已停止模板获取':'最新模板未取得或结构待核';}
      finally{this.job.updated_at=now();this.pending=null;}
    });
    return this.status(this.job.id);
  }
  cancel(id){this.status(id);if(this.busy){this.job.cancel_requested=true;this.control.abort();}return this.status(id);}
  async close(){if(this.busy)this.cancel(this.job.id);await this.pending;}
  async fetch(signal,onProgress){
    const config=await loadConfig(this.root);await assertHandoff(this.root,config.tenantBinding);
    if(!config.enabled)fail('SETUP_REQUIRED','请先核验并启用翱象连接');
    const binding=await validateConfig('machine.binding',await readJson(path.join(this.root,'config/machine.binding.json')));
    const browser=await this.browsers.acquire({state:this.state,config,binding,signal,onProgress:e=>onProgress(e.stage)});
    return new PurchaseTemplateService({adapter:browser.adapter,outputRoot:binding.outputRoot,identity:config.tenantBinding,signal}).prepareImport();
  }
}
