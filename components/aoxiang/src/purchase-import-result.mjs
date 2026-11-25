import {now,redact,fail} from './common.mjs';
import {IMPORT_CODE} from './purchase-create-contract.mjs';

const processing=new Set(['INIT','PENDING','CREATED','WAITING','QUEUED','ACCEPTED','RUNNING','PROCESSING','IN_PROGRESS','处理中','执行中','待处理']);
const rejected=new Set(['FAILED','FAILURE','CANCELLED','CANCELED','失败','已失败','已取消']);
const partial=new Set(['PARTIAL_FAILURE','PARTIAL_FAILED','PARTIALLY_FAILED','PART_FAILED','PARTIAL_FAIL','PART_FINISHED','PARTIAL_SUCCESS','部分失败','部分成功']);

function optionalCount(raw){
  if(!['number','string'].includes(typeof raw)||!/^\d+$/.test(String(raw)))return null;
  const count=Number(raw);
  return Number.isSafeInteger(count)?count:null;
}

function completionTime(raw){
  if(!['number','string'].includes(typeof raw))return null;
  const text=String(raw).trim();
  if(/^[1-9]\d{9}(?:\d{3})?$/.test(text))return text;
  return /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}/.test(text)&&Number.isFinite(Date.parse(text))?text:null;
}

export function importResult(task,taskId,expectedCount){
  if(task?.taskId!==taskId||task?.code!==IMPORT_CODE)
    fail('IMPORT_TASK_SCOPE','平台回执的任务号或类型与原采购不同，保留原任务待核',{taskId});
  const nativeStatus=typeof task.status==='string'?redact(task.status.slice(0,100)):null;
  const status=nativeStatus?.trim().toUpperCase();
  const successCount=optionalCount(task.successCount),failCount=optionalCount(task.failCount);
  const completedAt=completionTime(task.doneTime);
  const result={taskId,code:IMPORT_CODE,nativeStatus,successCount,failCount,expectedCount,completedAt,observedAt:now(),
    failureFileAvailable:task.failureFileAvailable===true};
  const settled=successCount!==null&&failCount!==null&&successCount+failCount===expectedCount;
  const badCounts=(task.successCount!=null&&successCount===null)||(task.failCount!=null&&failCount===null)||
    successCount>expectedCount||failCount>expectedCount;
  const reject=rejected.has(status),part=partial.has(status);
  if(processing.has(status)&&!completedAt)return {...result,kind:'pending',errorCode:'TASK_PENDING'};
  // The native completion time can corroborate a row-level failure on a new status
  // value. Only the established FINISHED contract may authorize PO association.
  if(status==='FINISHED'||reject||part||(!processing.has(status)&&completedAt&&settled&&failCount>0)){
    if(task.status==='FINISHED'&&settled&&!badCounts&&failCount===0)return {...result,kind:'complete'};
    if(!badCounts&&settled&&failCount>0){
      return {...result,kind:successCount===0?'failed':'partial',errorCode:successCount===0?'IMPORT_TASK_FAILED':'IMPORT_TASK_PARTIAL'};
    }
    if(reject&&!badCounts&&(successCount===null||successCount===0))
      return {...result,kind:'failed',errorCode:'IMPORT_TASK_FAILED'};
    return {...result,kind:'review',errorCode:'IMPORT_TASK_COUNTS_UNVERIFIED'};
  }
  return {...result,kind:'review',errorCode:'IMPORT_TASK_STATUS_UNKNOWN'};
}

export const importSettled=result=>['failed','partial','complete'].includes(result?.kind);

export function sameImportResult(saved,latest){
  return saved.taskId===latest.taskId&&saved.kind===latest.kind&&
    ['successCount','failCount'].every(key=>saved[key]===null||latest[key]===null||saved[key]===latest[key]);
}

export function requireCompletedImport(result){
  if(result.kind==='complete')return;
  const counts='成功 '+(result.successCount??'未留存')+'、失败 '+(result.failCount??'未留存');
  const messages={
    IMPORT_TASK_FAILED:'原导入任务失败（'+counts+'）；请查看原任务的失败明细',
    IMPORT_TASK_PARTIAL:'原导入任务部分成功（'+counts+'）；请核对已生成的采购单和失败商品',
    IMPORT_TASK_COUNTS_UNVERIFIED:'原导入任务的成功、失败数与原商品行数或状态不符（'+counts+'）；保留待核',
    IMPORT_TASK_STATUS_UNKNOWN:'原导入任务状态无法确认（'+(result.nativeStatus||'未返回')+'）；保留待核',
    TASK_PENDING:'原导入任务仍在处理中',
  };
  fail(result.errorCode,messages[result.errorCode],{importResult:result});
}
