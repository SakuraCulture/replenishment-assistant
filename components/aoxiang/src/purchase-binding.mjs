import fs from 'node:fs/promises';
import path from 'node:path';
import {canonical,sha256,fail,now,uid,businessDate,sameIdentity} from './common.mjs';
import {PurchaseCoordinator} from './purchase-coordinator.mjs';
import {nativeCreationEvidence} from './purchase-create-service.mjs';
import {bindingRequest,canBind,bindingEligible,checkedBinding} from './purchase-create-contract.mjs';
import {fileHash} from './workbook-validator.mjs';
import {errorInfo} from './machine-runtime.mjs';

async function checkedFile(manifest){
  const bytes=await fs.readFile(manifest.file);
  if(sha256(bytes)!==manifest.sha256)fail('BINDING_PROOF_CHANGED','关联核对文件已变化，请重新核对');
  const proof=JSON.parse(bytes);
  for(const f of proof.evidence.files)if(await fileHash(f.file)!==f.sha256)fail('BINDING_PROOF_CHANGED','关联主子单原件已变化，请重新核对');
  return proof;
}

export class PurchaseBindingService{
  constructor({state,creates,creationService,progress=()=>{},coordinatorFactory=o=>new PurchaseCoordinator(o)}){Object.assign(this,{state,creates,creationService,progress,coordinatorFactory});}
  async observe(id,proposal,{signal}){
    const r=this.creates.get(id);canBind(r);
    const p=r.intent,request=bindingRequest(proposal.request),context=await this.creationService.context(p,signal);
    await this.creationService.preflight(p,{forBinding:true});
    const stop=()=>{if(signal?.aborted)fail('PAUSED','已停止核对；原采购任务和已受理导出保留');};stop();
    let marked=[];
    if(r.uploadAttempted){
      if(!r.baseline?.from||!Array.isArray(r.baseline.orderIds))fail('BINDING_BASELINE_MISSING','缺少原上传前范围，无法排除旧单和意外多单');
      const current=await context.adapter.queryCreateOrders({identity:p.identity,buyerWarehouseCode:p.basis.store,name:p.target.displayName,from:r.baseline.from,to:businessDate(),dateBasis:'created'});
      stop();
      if(!sameIdentity(current.query?.identity,p.identity))fail('WRONG_IDENTITY','采购候选查询身份变化');
      marked=current.rows.filter(c=>c.buyerMessage===p.note&&!r.baseline.orderIds.includes(c.supplyOrderId)).map(c=>c.supplyOrderId);
      if(marked.length!==1||marked[0]!==request.order_id)fail('PURCHASE_NOT_UNIQUE','本次上传标记下不是恰好这一张采购单，保留待核，不重传');
    }
    const engine=this.coordinatorFactory({state:this.state,...context,onProgress:e=>{
      if(e.stage==='PURCHASE_RUNNING'){
        const current=this.creates.get(id);this.creates.putBindingProposal(id,current.sequence,{...current.bindingProposal,runId:e.runId},'BINDING_DOWNLOAD_BOUND');
      }
      this.progress(e.stage);
    }});
    const run=await engine.run(proposal.runId?{runId:proposal.runId}:{target:p.target,from:request.created_day,to:request.created_day,dateBasis:'created',orderId:request.order_id,rerun:true});
    stop();
    if(!sameIdentity(run.identity,p.identity)||run.from!==request.created_day||run.to!==request.created_day)fail('BINDING_PROOF','采购查询日期或身份不符');
    const candidate={supplyOrderId:request.order_id,buyerMessage:r.uploadAttempted?p.note:''};
    const evidence=await nativeCreationEvidence(p,null,candidate,engine,run);
    const proof={schema:1,operation:'purchase.binding.proof',purchaseId:id,basis:p.basis,request,requireNote:r.uploadAttempted,uniqueMarkedOrders:marked,runId:run.id,evidence};
    checkedBinding(this.creates.get(id),proof);stop();
    const folder=path.join(this.state.root,'purchase-binding-proofs');await fs.mkdir(folder,{recursive:true});
    const file=path.join(folder,proposal.id+'.json'),bytes=canonical(proof)+'\n';
    let saved=proof;
    try{const f=await fs.open(file,'wx');try{await f.writeFile(bytes,'utf8');await f.sync();}finally{await f.close();}}
    catch(e){if(e.code!=='EEXIST')throw e;saved=JSON.parse(await fs.readFile(file,'utf8'));
      const stable=x=>({...x,evidence:{...x.evidence,observedAt:null}});
      if(canonical(stable(saved))!==canonical(stable(proof)))fail('BINDING_PROOF_CHANGED','原核对文件已存在但内容不同，不能覆盖');}
    const manifest={file,sha256:sha256(await fs.readFile(file))};stop();
    const current=this.creates.get(id);
    return this.creates.putBindingProposal(id,current.sequence,{...current.bindingProposal,state:'READY',manifest,summary:{orderId:request.order_id,status:saved.evidence.main.status,rows:p.rows.length,quantity:p.totalQuantity,amount:p.totalAmount,store:p.basis.store,supplier:p.supplierCode},error:null},'BINDING_PREVIEW_READY');
  }
  async confirm(id,proposalId,sequence,hash){
    let r=this.creates.get(id),proposal=r.bindingProposal;
    if(r.state==='BOUND'&&r.binding.proposalId===proposalId&&r.binding.proofSha256===hash)return r;
    if(r.sequence!==sequence||proposal?.id!==proposalId||proposal.state!=='READY'||proposal.manifest.sha256!==hash)fail('BINDING_PREVIEW_STALE','关联预览已变化，请重新核对');
    const proof=await checkedFile(proposal.manifest);checkedBinding(r,proof);
    await this.creationService.preflight(r.intent,{forBinding:true});
    return this.creates.confirmBinding(id,sequence,proof,proposalId,hash);
  }
}

export class MachinePurchaseBinding{
  constructor({creates,service,onFatal=()=>{}}){Object.assign(this,{creates,service,onFatal});this.job=null;this.pending=null;}
  get busy(){return !!this.pending;}
  status(id){const r=this.creates.get(id);return {purchase_id:id,sequence:r.sequence,proposal:r.bindingProposal||null,active:this.busy&&this.job?.id===id,stage:this.job?.id===id?this.job.stage:null};}
  progress(stage){if(this.job)this.job.stage=stage;}
  start(id,request){
    if(this.busy)fail('BINDING_BUSY','当前核对仍在执行或安全结束');
    bindingRequest(request);let r=this.creates.get(id);canBind(r);
    const previous=r.bindingProposal,same=previous&&canonical(previous.request)===canonical(request);
    if(same&&previous.state==='READY')return this.status(id);
    const proposal=same?{...previous,state:'RUNNING',error:null}:{id:'bind-'+uid(),request,state:'RUNNING',runId:null,manifest:null,summary:null,error:null,createdAt:now()};
    r=this.creates.putBindingProposal(id,r.sequence,proposal,'BINDING_CHECK_STARTED');
    this.job={id,stage:'核对指定采购单，未上传或修改平台单据'};this.control=new AbortController();
    this.pending=Promise.resolve().then(()=>this.service.observe(id,proposal,{signal:this.control.signal})).catch(e=>{
      const latest=this.creates.get(id);
      const superseded=!bindingEligible(latest);
      this.creates.putBindingProposal(id,latest.sequence,{...latest.bindingProposal,state:superseded?'SUPERSEDED':this.control.signal.aborted?'PAUSED':'FAILED',error:superseded?{code:'BINDING_SUPERSEDED',message:'原采购任务状态已变化，已保留原回执或取消记录；请刷新本单记录继续原任务'}:errorInfo(e)},'BINDING_CHECK_STOPPED');
    }).catch(this.onFatal).finally(()=>{this.pending=null;});
    return this.status(id);
  }
  pause(id){if(this.busy&&this.job.id===id){this.control.abort();this.progress('停止后续核对，等待当前只读动作安全结束');}return this.status(id);}
  async close(){if(this.busy)this.pause(this.job.id);await this.pending;}
}
