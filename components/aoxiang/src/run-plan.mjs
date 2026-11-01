import {uid,now,businessDate,fail} from './common.mjs';

// Shared by standalone CLI and machine commands. Pure preparation, no I/O.
export function planReplenishmentRun(config,{mode='normal',batchId='default',warehouseCode}={}){
  const batch=config.batches.find(b=>b.id===batchId);
  const codes=warehouseCode?[warehouseCode]:batch?.targetWarehouseCodes;
  if(!codes?.length)fail('BATCH_NOT_FOUND','批次不存在或没有仓店');
  const targets=[...new Set(codes)].map(code=>{
    const target=config.targets.find(t=>t.buyerWarehouseCode===code);
    if(!target)fail('TARGET_NOT_CONFIGURED','请先在门店批次中核对并保存该仓点');
    return structuredClone(target);
  });
  const run={id:uid(),mode,day:businessDate(),createdAt:now(),identity:structuredClone(config.tenantBinding),status:'RUNNING',targets};
  const items=targets.map(target=>({id:uid(),runId:run.id,target,stage:'PLANNED',attemptId:null,createdAt:now()}));
  return {run,items};
}

// Caller owns the transaction, including the machine command if applicable.
export function insertRunPlan(db,{run,items}){
  if(!items.length||items.some(item=>item.runId!==run.id))fail('INVALID_RUN_PLAN','作业与明细关联无效');
  db.prepare('INSERT INTO runs VALUES(?,?)').run(run.id,JSON.stringify(run));
  const put=db.prepare('INSERT INTO items VALUES(?,?,?)');
  for(const item of items)put.run(item.id,item.runId,JSON.stringify(item));
}
