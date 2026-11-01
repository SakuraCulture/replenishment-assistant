import fs from 'node:fs/promises';
import path from 'node:path';
import {canonical,sha256,fail,sameIdentity,relativeSafe,exists} from './common.mjs';
import {purchaseScopeHash,checkPurchaseQuery,purchaseContract,PURCHASE_CODES} from './purchase-contract.mjs';
import {outputPath} from './publisher.mjs';
import {fileHash,validateWorkbook} from './workbook-validator.mjs';

const STAGES=new Set(['PLANNED','INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','SUCCEEDED','FAILED_CONFIRMED']);
export function portablePurchases(state,identity){return state.rows('purchase_runs').filter(r=>sameIdentity(r.identity,identity)).map(r=>({...r,exports:Object.fromEntries(Object.entries(r.exports).map(([key,e])=>{const {downloaded,...keep}=e;return [key,keep];}))}));}
export function validatePurchaseMigration(manifest){
  const records=manifest.purchaseRecords||[],assets=manifest.purchaseAssets||[];
  if(manifest.schemaVersion==='2.1'&&(!Array.isArray(manifest.purchaseRecords)||!Array.isArray(manifest.purchaseAssets)||manifest.purchaseRecordCount!==records.length||manifest.purchaseRecordsSha256!==sha256(canonical(records))))fail('MIGRATION_PURCHASE_INCOMPLETE','采购作业数量或集合哈希缺失、不一致');
  if(manifest.schemaVersion==='2.0'&&(records.length||assets.length))fail('MIGRATION_PURCHASE_VERSION','采购记录必须使用2.1迁移格式');
  const ids=new Set();
  for(const r of records){
    if(typeof r.id!=='string'||!r.id||ids.has(r.id)||!sameIdentity(r.identity,manifest.tenantBinding)||!r.target?.buyerWarehouseCode||!r.target.displayName||r.from!==r.to||!['created','updated'].includes(r.dateBasis)||r.scopeHash!==purchaseScopeHash({...r,buyerWarehouseCode:r.target.buyerWarehouseCode})||typeof r.createdAt!=='string'||!Number.isFinite(Date.parse(r.createdAt)))fail('MIGRATION_PURCHASE_SCOPE','采购恢复记录身份、日期或范围无效');ids.add(r.id);
    if(canonical(Object.keys(r.exports||{}).sort())!==canonical(['D16','D17']))fail('MIGRATION_PURCHASE_INCOMPLETE','采购主单或明细恢复记录缺失');
    if(r.query){checkPurchaseQuery(r.query.request,{...r,buyerWarehouseCode:r.target.buyerWarehouseCode});if(!sameIdentity(r.query.identity,r.identity)||!Array.isArray(r.query.orderIds)||r.query.orderIds.length!==r.query.total||new Set(r.query.orderIds).size!==r.query.total||r.query.total>3000||r.query.orderIds.some(id=>typeof id!=='string'||!id||!Number.isSafeInteger(r.query.orderSkuCounts?.[id])||r.query.orderSkuCounts[id]<0))fail('MIGRATION_PURCHASE_SCOPE','采购查询集合不完整');}
    for(const sourceId of ['D16','D17']){
      const e=r.exports[sourceId];if(e.sourceId!==sourceId||!STAGES.has(e.stage)||e.downloaded!==undefined)fail('MIGRATION_PURCHASE_STATE','采购恢复状态不合法或混入本机路径');
      if(e.stage!=='PLANNED'&&(!r.query||!e.intentId))fail('MIGRATION_PURCHASE_INCOMPLETE','采购提交记录缺少意图或冻结查询');
      if(e.taskId&&(!/^GEI@[A-Z0-9-]+@\d{12}@\d+$/.test(e.taskId)||(!['SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(e.stage)&&!e.taskId.startsWith('GEI@'+PURCHASE_CODES[sourceId]+'@'))))fail('MIGRATION_PURCHASE_STATE','采购任务ID不属于约定类型');
      if(['ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','SUCCEEDED'].includes(e.stage)&&!e.taskId)fail('MIGRATION_PURCHASE_INCOMPLETE','已受理采购记录缺少原任务ID');
      if(e.query){checkPurchaseQuery(e.query.request,{...r,buyerWarehouseCode:r.target.buyerWarehouseCode});if(!sameIdentity(e.query.identity,r.identity)||canonical([...e.query.orderIds].sort())!==canonical([...r.query.orderIds].sort()))fail('MIGRATION_PURCHASE_SCOPE','采购分任务的冻结查询不一致');}
      if(e.asset){relativeSafe(e.asset.relativePath);if(!/^[a-f0-9]{64}$/.test(e.asset.sha256)||!Number.isSafeInteger(e.asset.bytes)||e.asset.bytes<100||!Number.isSafeInteger(e.asset.rows)||e.asset.rows<1)fail('MIGRATION_PURCHASE_ASSET','采购结果元数据不合法');}
    }
  }
  const assetIds=new Set();for(const a of assets){const key=a.runId+'/'+a.sourceId,r=records.find(r=>r.id===a.runId),e=r?.exports[a.sourceId];if(assetIds.has(key)||!e?.asset||a.relativePath!=='assets/purchase/'+e.asset.relativePath||a.bytes!==e.asset.bytes||a.sha256!==e.asset.sha256)fail('MIGRATION_PURCHASE_ASSET','采购随迁文件与作业元数据不一致');relativeSafe(a.relativePath);assetIds.add(key);}
}
export async function addPurchaseMigration({state,config,manifest,members,includeResults,outputRoot}){
  const records=portablePurchases(state,config.tenantBinding);if(!records.length)return;
  manifest.schemaVersion='2.1';manifest.purchaseRecords=records;manifest.purchaseRecordCount=records.length;manifest.purchaseRecordsSha256=sha256(canonical(records));manifest.purchaseAssets=[];
  if(!includeResults)return;
  for(const r of records)for(const sourceId of ['D16','D17']){
    const e=r.exports[sourceId];if(!e.asset)continue;if(!outputRoot)fail('SETUP_REQUIRED','采购结果随迁需要本机输出目录');
    const file=await outputPath(outputRoot,e.asset.relativePath);if(!await exists(file)||await fileHash(file)!==e.asset.sha256)fail('ASSET_NOT_REUSABLE','采购结果缺失或已修改，不能随迁');
    await validateWorkbook(file,{warehouseCode:r.target.buyerWarehouseCode,expectedRows:e.asset.rows,contract:purchaseContract(sourceId),maxCompressedBytes:67108864});
    const entry={relativePath:'assets/purchase/'+e.asset.relativePath,bytes:e.asset.bytes,sha256:e.asset.sha256};manifest.purchaseAssets.push({runId:r.id,sourceId,...entry});manifest.files.push(entry);members.push({relativePath:entry.relativePath,file});
  }
}
export async function importPurchaseFiles(zip,manifest,{state,outputRoot}){
  let saved=0;const conflicts=[];
  for(const a of manifest.purchaseAssets||[]){
    const r=state.getPurchase(a.runId),e=r.exports[a.sourceId],staging=await outputPath(outputRoot,'.staging/imports/'+a.sha256+'.xlsx');
    if(!await exists(staging)||await fileHash(staging)!==a.sha256){const h=await fs.open(staging,'w');try{await zip.read(a.relativePath,async bytes=>{for(let offset=0;offset<bytes.length;){const w=await h.write(bytes,offset,bytes.length-offset);if(!w.bytesWritten)fail('MIGRATION_WRITE_FAILED','采购随迁文件写入失败');offset+=w.bytesWritten;}});await h.sync();}finally{await h.close();}}
    if(await fileHash(staging)!==a.sha256)fail('MIGRATION_HASH','采购随迁文件内容不符');await validateWorkbook(staging,{warehouseCode:r.target.buyerWarehouseCode,expectedRows:e.asset.rows,contract:purchaseContract(a.sourceId),maxCompressedBytes:67108864});
    const destination=await outputPath(outputRoot,e.asset.relativePath);
    try{await fs.copyFile(staging,destination,fs.constants.COPYFILE_EXCL);}catch(error){if(error.code!=='EEXIST')throw error;if(await fileHash(destination)!==a.sha256){conflicts.push(state.addConflict({type:'IMPORTED_PURCHASE_FILE_CONFLICT',runId:r.id,sourceId:a.sourceId,importedSha256:a.sha256}));continue;}}
    if(await fileHash(destination)!==a.sha256)fail('MIGRATION_HASH','采购文件落盘核验失败');saved++;
  }return {saved,conflicts};
}
