import fs from 'node:fs/promises';
import {portablePath} from './portable-assets.mjs';
import path from 'node:path';
import {constants} from 'node:fs';
import {uid,now,fail,sameIdentity,canonical,exists,writeJson,safeName,countValue,AppError,wait,businessDate} from './common.mjs';
import {purchaseScopeHash,purchaseScope,checkPurchaseReceipt,purchaseContract,dateBounds,PURCHASE_CODES,PURCHASE_LABELS,purchaseSkuCount} from './purchase-contract.mjs';
import {validateWorkbook,fileHash} from './workbook-validator.mjs';
import {outputPath} from './publisher.mjs';
import {checkOutput,assertHandoff} from './preflight.mjs';

const BAD=new Set(['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED']);
function dateCell(value){
  if(/^\d+(?:\.\d+)?$/.test(value))return Math.round((Number(value)-25569)*86400000)-28800000;
  if(!/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(value))fail('PURCHASE_FILE_DATE','采购明细时间列格式变化或为空');return Date.parse(value.replace(' ','T')+'+08:00');
}
export function validatePurchasePair(run,main,detail){
  const expected=new Set(run.query.orderIds),parents=new Map();
  for(const r of main.capturedRows){if(parents.has(r.orderId))fail('PURCHASE_PARENT_DUPLICATE','主单文件包含重复采购单号');if(!expected.has(r.orderId))fail('PURCHASE_PARENT_COVERAGE','主单文件包含查询范围之外的采购单');parents.set(r.orderId,purchaseSkuCount(r.skuCount));}
  if(parents.size!==expected.size||main.rows!==run.query.total)fail('PURCHASE_PARENT_COVERAGE','主单文件没有完整覆盖本次查询单号');
  const childSku=new Map(),bounds=run.queryMode==='order_id'?null:dateBounds(run.from,run.to),field=run.dateBasis==='updated'?'updatedAt':'createdAt',createdDays=new Set();
  for(const r of detail.capturedRows){
    if(!parents.has(r.orderId))fail('PURCHASE_CHILD_ORPHAN','商品明细存在主单文件以外的采购单');
    const ms=dateCell(r[field]);if(!Number.isFinite(ms)||bounds&&(ms<Number(bounds.start)||ms>Number(bounds.end)))fail('PURCHASE_FILE_DATE','商品明细的'+(run.dateBasis==='updated'?'更新时间':'创建时间')+'无效或超出筛选日期',{orderId:r.orderId});
    if(run.queryMode==='order_id'){if(r.orderId!==run.orderId)fail('PURCHASE_CHILD_ORPHAN','单号精查原件混入其他采购单');createdDays.add(businessDate(new Date(dateCell(r.createdAt))));}
    if(!childSku.has(r.orderId))childSku.set(r.orderId,new Set());childSku.get(r.orderId).add(r.skuCode);
  }
  for(const [id,skuCount] of parents)if(skuCount!==run.query.orderSkuCounts[id]||(childSku.get(id)?.size||0)!==skuCount)fail('PURCHASE_CHILD_COVERAGE','商品明细的逐单商品种数与查询或主单不一致',{orderId:id,querySkuCount:run.query.orderSkuCounts[id],mainSkuCount:skuCount,detailDistinctSku:childSku.get(id)?.size||0});
  return {status:'VALIDATED',queryParents:expected.size,mainRows:main.rows,detailRows:detail.rows,linkedParents:childSku.size,orphanRows:0,parentCoverage:'EXACT_SET',skuCoverage:'DISTINCT_SKU_PER_ORDER',duplicatesPreserved:true,dateBasis:run.dateBasis,...(run.queryMode==='order_id'?{queryMode:'order_id',createdDay:createdDays.size===1?[...createdDays][0]:null}:{})};
}
export class PurchaseCoordinator{
  constructor({state,config,binding,adapter,onProgress=()=>{},onSourceValidated=null,signal,hostOrderId=null,hostPurchaseId=null,hostWarehouse=null}){Object.assign(this,{state,config,binding,adapter,onProgress,onSourceValidated,signal,hostOrderId,hostPurchaseId,hostWarehouse});}
  stopped(){if(this.signal?.aborted)fail('PAUSED','采购作业已暂停；继续时恢复原任务');}
  async progress(stage,body={}){await this.onProgress({stage,...body});}
  save(run){if(this.hostOrderId)run={...run,hostOrderId:this.hostOrderId,hostPurchaseId:this.hostPurchaseId,hostWarehouse:this.hostWarehouse};this.state.putPurchase(run);return run;}
  async sourceFile(run,sourceId){
    const e=run.exports[sourceId],file=await portablePath(await outputPath(this.binding.outputRoot,e.asset.relativePath),{sha256:e.asset.sha256});if(!await exists(file)||await fileHash(file)!==e.asset.sha256)fail('PURCHASE_ASSET_CHANGED','已验收采购文件缺失或已修改，保留文件并等待恢复',{sourceId,file});return file;
  }
  async run({target,from,to,dateBasis='created',orderId=null,queryMode,runId,rerun=false}={}){
    if(!this.config?.enabled||!this.binding?.outputRoot)fail('SETUP_REQUIRED','请先完成Edge与业务配置');await assertHandoff(this.state.root,this.config.tenantBinding);await checkOutput(this.binding.outputRoot);await this.state.enablePurchases();
    let run=runId?this.state.getPurchase(runId):null;if(runId&&!run)fail('PURCHASE_RUN_NOT_FOUND','未找到采购作业');
    if(!run){
      if(queryMode==='order_id'){if(!orderId||from!=null||to!=null)fail('PURCHASE_EXACT_SCOPE','单号精查必须有准确单号，且不能混入日期范围');from=null;to=null;}
      else{if(from!==to)fail('PURCHASE_DAILY_SLICE','执行器按一个仓店一个自然日创建一组主单及明细');dateBounds(from,to);}
      if(!['created','updated'].includes(dateBasis))fail('PURCHASE_DATE_BASIS','时间口径无效');
      const input={buyerWarehouseCode:target.buyerWarehouseCode,from,to,dateBasis,orderId,...(queryMode==='order_id'?{queryMode}:{})},hash=purchaseScopeHash(input);
      const prior=this.state.rows('purchase_runs').filter(r=>sameIdentity(r.identity,this.config.tenantBinding)&&r.scopeHash===hash).at(-1);
      if(prior&&!rerun)run=prior;
      else{
        const priorUnfinished=this.state.rows('purchase_runs').filter(r=>sameIdentity(r.identity,this.config.tenantBinding)&&r.target.buyerWarehouseCode===target.buyerWarehouseCode&&
          (!this.hostOrderId||(r.hostOrderId?r.hostOrderId===this.hostOrderId:r.scopeHash===hash))&&
          (Object.values(r.exports).some(e=>BAD.has(e.stage))||!['SUCCEEDED','EMPTY','SUPERSEDED'].includes(r.status)));
        const safeSupersede=r=>rerun&&r.scopeHash===hash&&Object.values(r.exports).every(e=>!BAD.has(e.stage)&&(e.asset||['PLANNED','FAILED_CONFIRMED','SUCCEEDED'].includes(e.stage)))&&Object.values(r.exports).some(e=>e.asset||e.stage==='FAILED_CONFIRMED');
        const unresolved=priorUnfinished.filter(r=>!safeSupersede(r));
        if(unresolved.length)fail('PURCHASE_UNRESOLVED','该仓店存在未完成采购作业，请先继续原作业',{runIds:unresolved.map(r=>r.id)});
        const id=uid();for(const old of priorUnfinished.filter(safeSupersede))this.save({...old,status:'SUPERSEDED',supersededBy:id});
        run=this.save({id,identity:{...this.config.tenantBinding},target:{buyerWarehouseCode:target.buyerWarehouseCode,displayName:target.displayName},from:from??null,to:to??null,dateBasis,orderId,...(queryMode==='order_id'?{queryMode}:{}),scopeHash:hash,createdAt:now(),status:'PLANNED',exports:{D16:{sourceId:'D16',stage:'PLANNED'},D17:{sourceId:'D17',stage:'PLANNED'}}});
      }
    }
    if(!sameIdentity(run.identity,this.config.tenantBinding))fail('WRONG_IDENTITY','采购作业与当前商家隔离域不同');
    if(run.status==='EMPTY'){await this.progress('PURCHASE_EMPTY',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,day:run.from});return run;}
    await this.progress('PURCHASE_RUNNING',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,day:run.from});
    const scopeLabel=run.queryMode==='order_id'?'单号_'+safeName(run.orderId):run.from;
    const update=(sourceId,changes)=>{run=this.state.getPurchase(run.id);run.exports[sourceId]={...run.exports[sourceId],...changes};this.save(run);};
    try{
      this.stopped();
      if(!run.query){
        const q=await this.adapter.queryPurchase({...purchaseScope({...run,buyerWarehouseCode:run.target.buyerWarehouseCode}),name:run.target.displayName,identity:run.identity});
        if(!sameIdentity(q.identity,run.identity))fail('WRONG_IDENTITY','采购查询身份不同');run=this.save({...run,query:q,status:q.total?'RUNNING':'EMPTY',errorCode:undefined});
        if(!q.total){const file=await outputPath(this.binding.outputRoot,`采购单/${safeName(run.identity.merchantCode)}/${safeName(run.target.buyerWarehouseCode)}/${scopeLabel}_${run.dateBasis}/${run.id}/验收记录.json`);await writeJson(file,{...run,note:'本次权限及筛选下无可见采购主单；未提交导出，未生成空Excel'});await this.progress('PURCHASE_EMPTY',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,day:run.from});return run;}
      }
      for(const sourceId of ['D16','D17']){
        this.stopped();let e=this.state.getPurchase(run.id).exports[sourceId];
        if(e.stage==='SUCCEEDED'){
          try{const file=await this.sourceFile(this.state.getPurchase(run.id),sourceId);if(this.onSourceValidated)await this.onSourceValidated({run:this.state.getPurchase(run.id),sourceId,file});continue;}catch(error){if(!e.taskId)throw error;const file=await outputPath(this.binding.outputRoot,e.asset.relativePath);if(await exists(file))throw error;update(sourceId,{stage:'ACCEPTED',downloaded:null});e=this.state.getPurchase(run.id).exports[sourceId];}
        }
        if(BAD.has(e.stage))fail('SUBMISSION_UNKNOWN','采购原提交仍未消歧，禁止重提',{runId:run.id,sourceId,taskId:e.taskId||null});
        if(e.stage==='FAILED_CONFIRMED')fail('PURCHASE_TASK_FAILED','采购原任务包含失败；请核对平台错误后再显式重跑');
        if(!e.taskId){
          // Re-establish the same exact native scope before each distinct export.
          const q=await this.adapter.queryPurchase({...purchaseScope({...run,buyerWarehouseCode:run.target.buyerWarehouseCode}),name:run.target.displayName,identity:run.identity});
          if(canonical([...q.orderIds].sort())!==canonical([...run.query.orderIds].sort())||canonical(q.orderSkuCounts)!==canonical(run.query.orderSkuCounts)||canonical(q.request)!==canonical(run.query.request))fail('PURCHASE_QUERY_CHANGED','导出前采购单集合或筛选已变化，原文件与任务保留');
          const baseline=await this.adapter.preparePurchase(q,sourceId),intentId=uid();update(sourceId,{stage:'INTENT_RECORDED',intentId,baseline,query:q});await this.progress('INTENT_RECORDED',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,sourceId});this.stopped();update(sourceId,{stage:'SUBMITTING'});
          try{
            const receipt=await this.adapter.submitPurchase({intentId,sourceId,query:q});const current=this.state.getPurchase(run.id).exports[sourceId];
            if(current.additionalTaskIds?.length||!checkPurchaseReceipt(receipt,sourceId,q))fail('WRONG_SCOPE_ACCEPTED','采购任务受理范围不一致',{taskId:receipt.taskId});update(sourceId,{stage:'ACCEPTED',taskId:receipt.taskId,receipt});await this.progress('ACCEPTED',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,sourceId,taskId:receipt.taskId});
          }catch(error){const current=this.state.getPurchase(run.id).exports[sourceId],valid=current.taskId&&!current.additionalTaskIds?.length&&checkPurchaseReceipt(current.receipt,sourceId,q);update(sourceId,{stage:current.stage==='WRONG_SCOPE_ACCEPTED'||error.code==='WRONG_SCOPE_ACCEPTED'?'WRONG_SCOPE_ACCEPTED':valid?'ACCEPTED':'SUBMISSION_UNKNOWN',errorCode:error.code||'SUBMISSION_ERROR'});throw error;}
          e=this.state.getPurchase(run.id).exports[sourceId];
        }
        const until=Date.now()+this.config.resourceLimits.taskWaitBudgetMs;let task;
        do{
          this.stopped();task=await this.adapter.purchaseTask(e.taskId,sourceId,e.query||run.query);
          if(task.taskStatus==='FINISHED')break;if(['FAILED','FAILURE','CANCELLED'].includes(task.taskStatus)){update(sourceId,{stage:'FAILED_CONFIRMED'});fail('PURCHASE_TASK_FAILED','采购原任务未成功');}
          update(sourceId,{stage:'RUNNING'});if(Date.now()>=until)fail('TASK_PENDING','采购任务仍在处理；继续时跟踪原任务');await this.pause(this.config.resourceLimits.pollIntervalMs);
        }while(true);
        const taskSummary={taskId:e.taskId,code:task.taskCode||task.code,successCount:countValue(task.successCount),failCount:countValue(task.failCount)};
        if(taskSummary.failCount){update(sourceId,{stage:'FAILED_CONFIRMED',taskSummary});fail('PURCHASE_TASK_FAILED','采购原任务包含失败行');}if(taskSummary.code!==PURCHASE_CODES[sourceId])fail('PURCHASE_TASK_TYPE','原采购任务类型不匹配');
        update(sourceId,{stage:'FINISHED',taskSummary});let downloaded=e.downloaded;
        if(!downloaded||!await exists(downloaded.file)||await fileHash(downloaded.file)!==downloaded.sha256){
          this.stopped();await checkOutput(this.binding.outputRoot,{minimumFreeBytes:150994944});const directory=path.dirname(await outputPath(this.binding.outputRoot,`.staging/purchase/${run.id}/${sourceId}/placeholder`));downloaded=await this.adapter.downloadPurchase({taskId:e.taskId,sourceId,query:e.query||run.query,directory,maxBytes:67108864,timeoutMs:this.config.resourceLimits.downloadTimeoutMs});update(sourceId,{stage:'DOWNLOADED',downloaded});
        }
        this.stopped();const validation=await validateWorkbook(downloaded.file,{warehouseCode:run.target.buyerWarehouseCode,expectedRows:taskSummary.successCount,contract:purchaseContract(sourceId),maxCompressedBytes:67108864});
        if(sourceId==='D16'&&validation.rows!==run.query.total)fail('PURCHASE_PARENT_COVERAGE','采购主单查询计数与导出物理行数不符');
        const {capturedRows,...summary}=validation;update(sourceId,{stage:'VALIDATED',validation:summary});
        if(this.onSourceValidated)await this.onSourceValidated({run:this.state.getPurchase(run.id),sourceId,file:downloaded.file});
      }
      run=this.state.getPurchase(run.id);this.stopped();const validations={};
      for(const sourceId of ['D16','D17']){const e=run.exports[sourceId],file=e.stage==='SUCCEEDED'?await this.sourceFile(run,sourceId):e.downloaded.file;validations[sourceId]=await validateWorkbook(file,{warehouseCode:run.target.buyerWarehouseCode,expectedRows:e.taskSummary.successCount,contract:purchaseContract(sourceId),maxCompressedBytes:67108864});}
      const pair=validatePurchasePair(run,validations.D16,validations.D17),folder=`采购单/${safeName(run.identity.merchantCode)}/${safeName(run.target.buyerWarehouseCode)}/${scopeLabel}_${run.dateBasis}/${run.id}`;
      run=this.save({...run,pairValidation:pair,status:'PUBLISHING'});
      for(const sourceId of ['D16','D17']){
        this.stopped();const e=run.exports[sourceId],v=validations[sourceId],relativePath=e.asset?.relativePath||`${folder}/${safeName(run.target.displayName)}_${scopeLabel}_${PURCHASE_LABELS[sourceId]}.xlsx`,destination=await outputPath(this.binding.outputRoot,relativePath);
        if(await exists(destination)){if(await fileHash(destination)!==v.sha256)fail('PURCHASE_ASSET_CHANGED','采购输出目标已被修改，不能覆盖');}
        else{
          const tempRelative=relativePath+'.'+uid()+'.pending',temp=await outputPath(this.binding.outputRoot,tempRelative);await fs.copyFile(await portablePath(e.downloaded.file,{sha256:v.sha256}),temp,constants.COPYFILE_EXCL);const h=await fs.open(temp,'r+');try{await h.sync();}finally{await h.close();}if(await fileHash(temp)!==v.sha256)fail('STAGING_COPY_FAILED','采购文件保存校验失败');
          // Link with no overwrite. Crash recovery validates the final hash and
          // resumes local publication, never creates another platform task.
          try{await fs.link(temp,destination);}catch(error){if(error.code!=='EEXIST'||await fileHash(destination)!==v.sha256)fail('SAVE_BLOCKED','采购文件本地保存受阻，原任务和暂存文件保留',{systemCode:error.code});}await fs.unlink(temp);
        }
        update(sourceId,{stage:'SUCCEEDED',errorCode:undefined,asset:{relativePath,sha256:v.sha256,bytes:v.bytes,rows:v.rows,columns:v.columns,publishedAt:now()}});run=this.state.getPurchase(run.id);await this.progress('SAVED',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,sourceId,taskId:e.taskId,file:destination,rows:v.rows});
      }
      const report=await outputPath(this.binding.outputRoot,folder+'/验收记录.json');run=this.save({...this.state.getPurchase(run.id),status:'SUCCEEDED',errorCode:undefined,completedAt:now(),reportRelativePath:folder+'/验收记录.json'});
      const portable={...run,exports:Object.fromEntries(Object.entries(run.exports).map(([k,e])=>{const {downloaded,...safe}=e;return [k,safe];}))};await writeJson(report,portable);await this.progress('PURCHASE_COMPLETE',{runId:run.id,warehouseCode:run.target.buyerWarehouseCode,day:run.from,report});return run;
    }catch(error){run=this.save({...this.state.getPurchase(run.id),status:this.signal?.aborted?'PAUSED':'NEEDS_ATTENTION',errorCode:error.code||'FAILED',updatedAt:now()});throw error;}
  }
  async pause(ms){if(!this.signal)return wait(ms);this.stopped();await new Promise((resolve,reject)=>{const stop=()=>{clearTimeout(timer);reject(new AppError('PAUSED','采购作业已暂停'));};const timer=setTimeout(()=>{this.signal.removeEventListener('abort',stop);resolve();},ms);this.signal.addEventListener('abort',stop,{once:true});});}
}
