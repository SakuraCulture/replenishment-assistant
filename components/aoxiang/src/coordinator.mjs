import path from 'node:path';
import fs from 'node:fs/promises';
import platform from '../assets/platform-profile.json' with {type:'json'};
import {fieldContract,validateConfig} from './config.mjs';
import {assetKey} from './state.mjs';
import {downloadKnownTask} from './downloader.mjs';
import {validateWorkbook,fileHash} from './workbook-validator.mjs';
import {publishAsset,recoverPublications,outputPath,dailyRelative} from './publisher.mjs';
import {uid,now,businessDate,scopeHash,exists,fail,countValue,sameIdentity,AppError,wait} from './common.mjs';
import {checkOutput} from './preflight.mjs';
import {writeRunReport} from './event-log.mjs';
import {planReplenishmentRun,insertRunPlan} from './run-plan.mjs';

const DONE=new Set(['SUCCEEDED','REUSED']);
function matchesReceipt(receipt,warehouseCode,semanticVersion){
  const s=receipt?.scope,expectedCode=semanticVersion==='D13-all-info-1'?'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT':platform.currentExportCode;
  const extras=s?.extraKeys||Object.keys(s||{}).filter(k=>k!=='buyerWarehouseCodeList');
  return receipt?.code===expectedCode&&JSON.stringify(s?.buyerWarehouseCodeList)===JSON.stringify([warehouseCode])&&!extras.length;
}
export class Coordinator{
  constructor({state,config,binding,adapter,onProgress=()=>{},signal,download=downloadKnownTask,publish=publishAsset}){
    Object.assign(this,{state,config,binding,adapter,onProgress,signal,download,publish});
  }
  stopped(){if(this.signal?.aborted)fail('PAUSED','作业已暂停');}
  async progress(stage,data={}){await this.onProgress({stage,...data});}
  async validateAsset(asset){
    const file=await outputPath(this.binding.outputRoot,asset.relativePath);
    if(!await exists(file))fail('ASSET_FILE_MISSING','已有日文件缺失，将按原任务恢复');
    if(await fileHash(file)!==asset.sha256)fail('ASSET_NOT_REUSABLE','已有日文件已修改，不自动覆盖');
    const contract=await fieldContract(asset.semanticVersion==='D13-all-info-1'?'v1':'current');
    await validateWorkbook(file,{warehouseCode:asset.buyerWarehouseCode,expectedRows:asset.rows,contract,maxCompressedBytes:this.config.resourceLimits.maxCompressedFileBytes});return file;
  }
  async run({mode='normal',batchId='default',warehouseCode,runId,phase='complete'}={}){
    if(!['complete','submit','collect'].includes(phase))fail('INVALID_PHASE','未知采集阶段');
    await validateConfig('portable.config',this.config);
    if(!this.config.enabled||!this.binding.outputRoot)fail('SETUP_REQUIRED','请先完成本机配置');
    await checkOutput(this.binding.outputRoot);
    if(this.config.exportPolicy.semanticVersion!==platform.currentSemanticVersion)fail('SOURCE_VERSION_CHANGED','当前平台已使用V2，请重新连接并核对配置后升级导出口径');
    const publication=await recoverPublications({state:this.state,outputRoot:this.binding.outputRoot,identity:this.config.tenantBinding});
    for(const p of publication)if(p.attemptId){const a=this.state.getAttempt(p.attemptId);if(a)this.state.putAttempt({...a,stage:'SUCCEEDED',completedAt:now()});}
    let run=runId?this.state.getRun(runId):null;
    if(runId&&!run)fail('RUN_NOT_FOUND','未找到指定作业');
    if(run?.status==='ENDED')fail('RUN_ENDED','本次采集已结束，请明确新建采集任务');
    if(!run){
      const plan=planReplenishmentRun(this.config,{mode,batchId,warehouseCode});
      this.state.write(()=>insertRunPlan(this.state.db,plan));run=plan.run;
    }
    if(!sameIdentity(run.identity,this.config.tenantBinding))fail('RUN_IDENTITY_MISMATCH','原作业与当前配置账号/商家不同');
    await this.progress('RUNNING',{runId:run.id,targets:run.targets.length});
    try{
      for(let item of this.state.getItems(run.id)){
        if(DONE.has(item.stage))continue;this.stopped();
        let attempt=item.attemptId?this.state.getAttempt(item.attemptId):null;
        const identity=run.identity,target=item.target,semanticVersion=attempt?.semanticVersion||this.config.exportPolicy.semanticVersion,scope=attempt?.scopeHash||scopeHash(target.buyerWarehouseCode,semanticVersion);
        const key=assetKey({...identity,buyerWarehouseCode:target.buyerWarehouseCode,acquisitionDate:run.day,scopeHash:scope,semanticVersion});
        const existing=this.state.getAsset(key);
        if(attempt?.taskId&&existing?.taskId===attempt.taskId){
          try{await this.validateAsset(existing);this.state.putAttempt({...attempt,stage:'SUCCEEDED'});item={...item,stage:'SUCCEEDED',assetKey:key,assetSnapshot:existing};this.state.putItem(item);continue;}
          catch(e){if(e.code!=='ASSET_FILE_MISSING')throw e;}
        }
        const unresolved=this.state.pending(identity,target.buyerWarehouseCode,scope).filter(a=>a.id!==attempt?.id);
        if(unresolved.length)fail('UNRESOLVED_TASK','该仓点存在其他未决提交，先继续原作业或核对迁移记录',{warehouseCode:target.buyerWarehouseCode,taskIds:unresolved.map(a=>a.taskId||null)});
        if(attempt&&['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(attempt.stage))fail('SUBMISSION_UNKNOWN','原提交仍未消歧，不能再点导出',{attemptId:attempt.id,taskId:attempt.taskId});
        if(!attempt){
          if(phase==='collect')fail('MISSING_SUBMISSION','尚未提交的门店不能在下载阶段创建导出');
          if(run.day!==businessDate())fail('NEW_DAY_REQUIRED','未提交项已跨日，请先结束旧作业，再建立当日批次');
          const query=await this.adapter.querySingle(target.buyerWarehouseCode,target.displayName);
          if(!sameIdentity(query.identity,identity))fail('WRONG_IDENTITY','当前查询的账号/商家与作业不符');
          if(run.mode!=='rerun'&&existing){
            try{const file=await this.validateAsset(existing);item={...item,stage:'REUSED',assetKey:key,assetSnapshot:existing};this.state.putItem(item);await this.progress('REUSED',{warehouseCode:target.buyerWarehouseCode,file,collectedAt:existing.collectedAt});continue;}
            catch(e){if(e.code!=='ASSET_FILE_MISSING')throw e;
              attempt={id:uid(),itemId:item.id,runId:run.id,...identity,buyerWarehouseCode:target.buyerWarehouseCode,scopeHash:existing.scopeHash,semanticVersion:existing.semanticVersion,acquisitionDate:existing.acquisitionDate,createdAt:now(),stage:'ACCEPTED',taskId:existing.taskId,query:{warehouseCode:target.buyerWarehouseCode,name:target.displayName,total:existing.rows,observedAt:existing.collectedAt,identity},recoveryFromAsset:true};
              this.state.putAttempt(attempt);item={...item,stage:'ACCEPTED',attemptId:attempt.id};this.state.putItem(item);await this.progress('RECOVER_KNOWN_TASK',{warehouseCode:target.buyerWarehouseCode,taskId:existing.taskId});
            }
          }
          if(!attempt){
          const destination=await outputPath(this.binding.outputRoot,existing?.relativePath||dailyRelative(identity,target,run.day,this.config.resultLayout));
          if(await exists(destination)&&(!existing||await fileHash(destination)!==existing.sha256))fail('EXISTING_ASSET_CHANGED','目标路径已有未登记或已修改的文件，未提交新任务');
          if(!query.total)fail('EMPTY_UNVERIFIED','当前查询为0，零行文件契约尚未验证，不制造空Excel');
          await checkOutput(this.binding.outputRoot,{minimumFreeBytes:this.config.resourceLimits.maxCompressedFileBytes*2+(existing?.bytes||0)+16777216});
          const baseline=await this.adapter.prepareExport(query);
          attempt={id:uid(),itemId:item.id,runId:run.id,...identity,buyerWarehouseCode:target.buyerWarehouseCode,scopeHash:scope,semanticVersion:this.config.exportPolicy.semanticVersion,createdAt:now(),acquisitionDate:run.day,stage:'INTENT_RECORDED',taskId:null,query,baseline};
          this.state.putAttempt(attempt);item={...item,stage:'INTENT_RECORDED',attemptId:attempt.id};this.state.putItem(item);
          await this.progress('INTENT_RECORDED',{runId:run.id,attemptId:attempt.id,warehouseCode:target.buyerWarehouseCode,queryRows:query.total});
          try{this.stopped();}catch(e){this.state.putAttempt({...attempt,stage:'CANCELLED_BEFORE_SUBMIT',errorCode:'PAUSED_BEFORE_DISPATCH'});item={...item,stage:'PLANNED',attemptId:null};this.state.putItem(item);throw e;}
          attempt={...attempt,stage:'SUBMITTING'};this.state.putAttempt(attempt);
          const receiptHandler=receipt=>{
            if(receipt.intentId!==attempt.id)return;
            const current=this.state.getAttempt(attempt.id);
            if(current.taskId&&current.taskId!==receipt.taskId){this.state.putAttempt({...current,stage:'SUBMISSION_UNKNOWN',additionalTaskIds:[...(current.additionalTaskIds||[]),receipt.taskId]});return;}
            this.state.putAttempt({...current,stage:matchesReceipt(receipt,target.buyerWarehouseCode,semanticVersion)?'ACCEPTED':'WRONG_SCOPE_ACCEPTED',taskId:receipt.taskId,receipt});
          };
          this.adapter.onReceipt=receiptHandler;
          try{
            const accepted=await this.adapter.submitOnce({intentId:attempt.id,expected:query});
            if(this.state.getAttempt(attempt.id).additionalTaskIds?.length)fail('SUBMISSION_UNKNOWN','同一意图出现多个受理任务，保留所有证据并停止');
            if(!matchesReceipt(accepted,target.buyerWarehouseCode,semanticVersion)&&accepted.scope)fail('WRONG_SCOPE_ACCEPTED','导出受理范围与本项不一致',{taskId:accepted.taskId});
            attempt={...this.state.getAttempt(attempt.id),stage:'ACCEPTED',taskId:accepted.taskId,receipt:accepted};this.state.putAttempt(attempt);
            await this.progress('ACCEPTED',{runId:run.id,attemptId:attempt.id,warehouseCode:target.buyerWarehouseCode,taskId:attempt.taskId});
          }catch(e){
            const current=this.state.getAttempt(attempt.id);
            const positive=current.taskId&&!current.additionalTaskIds?.length&&matchesReceipt(current.receipt,target.buyerWarehouseCode,semanticVersion);
            attempt={...current,stage:e.code==='WRONG_SCOPE_ACCEPTED'||current.stage==='WRONG_SCOPE_ACCEPTED'?'WRONG_SCOPE_ACCEPTED':positive?'ACCEPTED':'SUBMISSION_UNKNOWN',taskId:e.details?.taskId||current.taskId||null,errorCode:e.code||'SUBMISSION_ERROR'};this.state.putAttempt(attempt);throw e;
          }
          }
        }
        this.stopped();
        if(attempt.stage==='SUCCEEDED'){item={...item,stage:'SUCCEEDED',assetSnapshot:item.assetSnapshot||existing};this.state.putItem(item);continue;}
        // Retain the exact accepted task; the platform can generate it while later stores submit.
        if(phase==='submit')continue;
        let downloaded=attempt.downloaded;
        if(!downloaded||!await exists(downloaded.file)||await fileHash(downloaded.file)!==downloaded.sha256){
          if(!attempt.taskId)fail('SUBMISSION_UNKNOWN','未取得明确任务ID');
          const until=Date.now()+this.config.resourceLimits.taskWaitBudgetMs;let task;
          do{
            this.stopped();task=await this.adapter.findTask(attempt.taskId);
            if(task.taskStatus==='FINISHED')break;
            attempt={...attempt,stage:'RUNNING'};this.state.putAttempt(attempt);await this.progress('WAIT_TASK',{warehouseCode:target.buyerWarehouseCode,taskId:attempt.taskId});
            if(phase==='collect')return {...run,pending:true};
            if(Date.now()>=until)fail('TASK_PENDING','任务仍待完成；继续时跟踪原任务');
            await this.abortableWait(this.config.resourceLimits.pollIntervalMs);
          }while(true);
          if(countValue(task.failCount)!==0){attempt={...attempt,stage:'FAILED_CONFIRMED',taskSummary:{taskId:attempt.taskId,successCount:countValue(task.successCount),failCount:countValue(task.failCount)}};this.state.putAttempt(attempt);fail('TASK_HAS_FAILURES','平台任务包含失败行，不能按完整文件发布');}
          if(!platform.exportCodes.includes(task.code||task.taskCode))fail('TASK_TYPE_MISMATCH','任务类型不属于已验证补货建议导出');
          attempt={...attempt,stage:'FINISHED',taskSummary:{taskId:attempt.taskId,code:task.code||task.taskCode,successCount:countValue(task.successCount),failCount:countValue(task.failCount)}};this.state.putAttempt(attempt);
          const currentIdentity=await this.adapter.verifyIdentity(identity);if(!sameIdentity(currentIdentity,identity))fail('WRONG_IDENTITY','当前登录身份变化，保留原任务并停止');
          task=await this.adapter.findTask(attempt.taskId);
          let object;const directory=await outputPath(this.binding.outputRoot,'.staging/'+run.id+'/placeholder');
          for(let retry=0;retry<2;retry++){
            this.stopped();const u=new URL(task.fileUrl),currentObject=u.origin+u.pathname;
            if(object&&currentObject!==object)fail('RESULT_OBJECT_CHANGED','刷新后结果对象变化，不能自动继续');object=currentObject;
            try{downloaded=await this.download({fileUrl:task.fileUrl,taskId:attempt.taskId,object,allowedHosts:platform.downloadHosts,directory:path.dirname(directory),signal:this.signal,timeoutMs:this.config.resourceLimits.downloadTimeoutMs,maxBytes:this.config.resourceLimits.maxCompressedFileBytes});break;}
            catch(e){if(e.code!=='RESULT_LINK_EXPIRED_OR_DENIED'||retry)throw e;task=await this.adapter.findTask(attempt.taskId);}
          }
          attempt={...attempt,stage:'DOWNLOADED',downloaded};this.state.putAttempt(attempt);
        }
        this.stopped();
        const validation=await validateWorkbook(downloaded.file,{warehouseCode:target.buyerWarehouseCode,expectedRows:attempt.taskSummary.successCount,contract:await fieldContract(attempt.semanticVersion==='D13-all-info-1'?'v1':'current'),maxCompressedBytes:this.config.resourceLimits.maxCompressedFileBytes});
        if(validation.rows!==attempt.query.total)fail('QUERY_FILE_COUNT_DIFF','查询与导出行数不同，保留文件待复核，不擅自认定齐全',{queryRows:attempt.query.total,fileRows:validation.rows});
        attempt={...attempt,stage:'VALIDATED',validation};this.state.putAttempt(attempt);
        const asset={...identity,buyerWarehouseCode:target.buyerWarehouseCode,acquisitionDate:attempt.acquisitionDate,scopeHash:attempt.scopeHash,semanticVersion:attempt.semanticVersion,contractId:attempt.semanticVersion==='D13-all-info-1'?'D13-V1-52cols':platform.currentContractId,taskId:attempt.taskId,rows:validation.rows,columns:validation.columns,bytes:validation.bytes,sha256:validation.sha256,collectedAt:attempt.query.observedAt,relativePath:dailyRelative(identity,target,attempt.acquisitionDate,this.config.resultLayout)};
        this.stopped();
        const saved=await this.publish({state:this.state,outputRoot:this.binding.outputRoot,sourceFile:downloaded.file,asset,attemptId:attempt.id,replace:run.mode==='rerun'});
        attempt={...attempt,stage:'SUCCEEDED',assetKey:saved.assetKey,completedAt:now()};this.state.putAttempt(attempt);
        item={...item,stage:'SUCCEEDED',assetKey:saved.assetKey,assetSnapshot:saved};this.state.putItem(item);await this.progress('SAVED',{warehouseCode:target.buyerWarehouseCode,taskId:attempt.taskId,file:path.join(this.binding.outputRoot,saved.relativePath),rows:validation.rows});
      }
      if(phase==='submit'&&this.state.getItems(run.id).some(i=>!DONE.has(i.stage)))return {...run,pending:true};
      const {errorCode,...cleanRun}=run;run={...cleanRun,status:'SUCCEEDED',completedAt:now()};this.state.putRun(run);await writeRunReport(this.state,run,this.binding);await this.progress('COMPLETE',{runId:run.id});return run;
    }catch(e){run={...run,status:this.signal?.aborted?'PAUSED':'NEEDS_ATTENTION',errorCode:e.code||'FAILED',updatedAt:now()};this.state.putRun(run);await writeRunReport(this.state,run,this.binding);throw e;}
  }
  async abortableWait(ms){
    if(!this.signal)return wait(ms);
    this.stopped();await new Promise((resolve,reject)=>{const stop=()=>{clearTimeout(timer);reject(new AppError('PAUSED','作业已暂停'));};const timer=setTimeout(()=>{this.signal.removeEventListener('abort',stop);resolve();},ms);this.signal.addEventListener('abort',stop,{once:true});});
  }
}
