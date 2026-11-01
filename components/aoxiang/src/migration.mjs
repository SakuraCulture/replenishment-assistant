import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {createArchive,openArchive} from './zip.mjs';
import {validateConfig,fieldContract} from './config.mjs';
import {readJson,writeJson,sha256,canonical,uid,now,exists,fail,sameIdentity,scopeHash,relativeSafe} from './common.mjs';
import {fileHash,validateWorkbook} from './workbook-validator.mjs';
import {outputPath,publishAsset} from './publisher.mjs';
import {recoverPublications} from './publisher.mjs';
import {addPurchaseMigration,validatePurchaseMigration,importPurchaseFiles} from './purchase-migration.mjs';

const PENDING_STAGES=new Set(['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED','ACCEPTED','RUNNING','FINISHED','DOWNLOADED','VALIDATED','PUBLISHING']);
function requiredText(value,label){if(typeof value!=='string'||!value)fail('RECOVERY_FIELDS_MISSING',`恢复记录缺少${label}，不能省略未决项生成迁移包`);return value;}
export function recoveryRecords(state,identity){
  const items=new Map(state.rows('items').map(i=>[i.id,i])),runs=new Map(state.rows('runs').map(r=>[r.id,r]));
  const records=state.rows('attempts').filter(a=>PENDING_STAGES.has(a.stage)&&(!identity||sameIdentity(a,identity))).map(a=>{
    const r={recordId:a.id,sourceRunId:requiredText(a.runId||items.get(a.itemId)?.runId,'作业ID'),sourceAttemptId:a.id,
      accountId:a.accountId,merchantCode:a.merchantCode,buyerWarehouseCode:a.buyerWarehouseCode,
      scopeHash:a.scopeHash,state:a.stage,taskId:a.taskId||null,createdAt:requiredText(a.createdAt,'创建时间'),evidenceRelativePaths:[]};
    if(a.query?.total&&a.semanticVersion&&a.acquisitionDate)Object.assign(r,{semanticVersion:a.semanticVersion,acquisitionDate:a.acquisitionDate,runMode:runs.get(a.runId)?.mode||'normal',querySnapshot:{warehouseCode:a.buyerWarehouseCode,name:a.query.name,total:a.query.total,observedAt:a.query.observedAt}});
    return r;
  });
  for(const p of state.rows('publishes').filter(p=>(!identity||sameIdentity(p.asset,identity))&&!records.some(r=>r.sourceAttemptId===p.attemptId))){
    const a=p.asset;if(!a.taskId)fail('RECOVERY_FIELDS_MISSING','待发布文件缺少原任务号，不能省略它迁移');
    records.push({recordId:'publish-'+p.id,sourceRunId:'publish-'+p.id,sourceAttemptId:'publish-'+p.id,accountId:a.accountId,merchantCode:a.merchantCode,buyerWarehouseCode:a.buyerWarehouseCode,scopeHash:a.scopeHash,state:'PUBLISHING',taskId:a.taskId,createdAt:p.createdAt,evidenceRelativePaths:[],semanticVersion:a.semanticVersion,acquisitionDate:a.acquisitionDate,runMode:p.oldSha256?'rerun':'normal',querySnapshot:{warehouseCode:a.buyerWarehouseCode,name:a.buyerWarehouseCode,total:a.rows,observedAt:a.collectedAt}});
  }
  return [...records,...state.rows('imported_pending').filter(r=>!identity||sameIdentity(r,identity))];
}
function checkManifest(manifest,config){
  validatePurchaseMigration(manifest);
  if(manifest.pendingRecordCount!==manifest.unresolvedRecords.length||manifest.pendingRecordsSha256!==sha256(canonical(manifest.unresolvedRecords)))fail('MIGRATION_RECOVERY_INCOMPLETE','恢复记录数量或集合哈希不符，不能以该包解锁新提交');
  if(canonical(config.tenantBinding)!==canonical(manifest.tenantBinding))fail('MIGRATION_IDENTITY','迁移清单与业务配置的账号隔离域不一致');
  const seen=new Set();
  for(const p of manifest.unresolvedRecords){
    if(!sameIdentity(p,manifest.tenantBinding))fail('MIGRATION_IDENTITY','恢复记录含不同账号/商家');
    const key=canonical([p.accountId,p.merchantCode,p.sourceAttemptId,p.buyerWarehouseCode,p.scopeHash]);
    if(seen.has(key))fail('MIGRATION_DUPLICATE_RECORD','迁移包含重复的未决记录');seen.add(key);
    if(p.semanticVersion&&p.scopeHash!==scopeHash(p.buyerWarehouseCode,p.semanticVersion))fail('RECOVERY_SCOPE_INVALID','恢复范围哈希不一致');
    if(p.querySnapshot&&p.querySnapshot.warehouseCode!==p.buyerWarehouseCode)fail('RECOVERY_SCOPE_INVALID','冻结查询仓码不一致');
    for(const r of p.evidenceRelativePaths)relativeSafe(r);
  }
  const assetKeys=new Set();
  for(const a of manifest.optionalAssets){
    if(!sameIdentity(a,manifest.tenantBinding)||!['D13-all-info-1','D13-all-info-2'].includes(a.semanticVersion)||a.scopeHash!==scopeHash(a.buyerWarehouseCode,a.semanticVersion))fail('MIGRATION_ASSET_SCOPE','结果资产的账号或范围不符合契约');
    if(!a.file.relativePath.startsWith('assets/results/'))fail('MIGRATION_ASSET_PATH','结果资产必须位于专用相对目录');
    relativeSafe(a.file.relativePath);
    const key=canonical([a.accountId,a.merchantCode,a.buyerWarehouseCode,a.acquisitionDate,a.semanticVersion]);
    if(assetKeys.has(key))fail('MIGRATION_DUPLICATE_ASSET','同日仓点资产重复');assetKeys.add(key);
  }
}
export async function exportMigration({state,config,output,sourceStopped=false,includeResults=false,outputRoot}){
  // The legacy component-only package cannot carry host payment packages or creation
  // identities. Never label a partial transfer as safe handoff after procurement exists.
  if(state.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name='purchase_creates'").get()&&state.db.prepare("SELECT 1 FROM purchase_creates WHERE state<>'CANCELLED_BEFORE_UPLOAD' LIMIT 1").get())fail('INTEGRATED_PURCHASE_MIGRATION_REQUIRED','此状态含补货助手采购创建记录；旧组件迁移包不会完整保留订单关联，请保留整套应用数据，不使用旧迁移入口接力建单');
  await validateConfig('portable.config',config);
  const body=Buffer.from(JSON.stringify(config,null,2)+'\n'),members=[{relativePath:'config/portable.config.json',buffer:body}];
  const manifest={schemaVersion:'2.0',exampleOnly:false,migrationId:uid(),createdAt:now(),sourceStateGeneration:Math.max(1,state.generation()),handoffStatus:sourceStopped?'SOURCE_STOPPED':'CONFIG_ONLY_SNAPSHOT',tenantBinding:config.tenantBinding,
    unresolvedRecords:recoveryRecords(state,config.tenantBinding),files:[{relativePath:'config/portable.config.json',bytes:body.length,sha256:sha256(body)}],optionalAssets:[]};
  manifest.pendingRecordCount=manifest.unresolvedRecords.length;manifest.pendingRecordsSha256=sha256(canonical(manifest.unresolvedRecords));
  if(includeResults){
    if(!outputRoot)fail('SETUP_REQUIRED','携带结果需要已核对的本机输出目录');
    for(const a of state.rows('assets').filter(a=>sameIdentity(a,config.tenantBinding))){
      const file=await outputPath(outputRoot,a.relativePath);
      if(!await exists(file)||await fileHash(file)!==a.sha256)fail('ASSET_NOT_REUSABLE','随迁结果缺失或已修改，请先核对',{warehouseCode:a.buyerWarehouseCode,date:a.acquisitionDate});
      const v=await validateWorkbook(file,{warehouseCode:a.buyerWarehouseCode,expectedRows:a.rows,contract:await fieldContract(a.semanticVersion==='D13-all-info-1'?'v1':'current')});
      const entry={relativePath:'assets/results/'+relativeSafe(a.relativePath),bytes:v.bytes,sha256:v.sha256};
      manifest.files.push(entry);manifest.optionalAssets.push({accountId:a.accountId,merchantCode:a.merchantCode,buyerWarehouseCode:a.buyerWarehouseCode,acquisitionDate:a.acquisitionDate,scopeHash:a.scopeHash,semanticVersion:a.semanticVersion,taskId:a.taskId,rows:v.rows,columns:v.columns,collectedAt:a.collectedAt,contractId:a.contractId,file:entry});
      members.push({relativePath:entry.relativePath,file});
    }
  }
  await addPurchaseMigration({state,config,manifest,members,includeResults,outputRoot});
  await validateConfig('migration.manifest',manifest);checkManifest(manifest,config);
  await createArchive(output,[{relativePath:'migration.manifest.json',buffer:JSON.stringify(manifest,null,2)+'\n'},...members]);
  return {file:output,sha256:await fileHash(output),migrationId:manifest.migrationId,unresolvedCount:manifest.unresolvedRecords.length,handoffStatus:manifest.handoffStatus,resultsIncluded:manifest.optionalAssets.length+(manifest.purchaseAssets?.length||0),purchaseRuns:manifest.purchaseRecords?.length||0};
}
export async function previewMigration(file,{currentConfig}={}){
  const zip=await openArchive(file,{maxUncompressedBytes:17179869184,maxEntries:10000});
  try{
    await zip.validateAll();
    const manifest=JSON.parse((await zip.buffer('migration.manifest.json',16777216)).toString('utf8'));
    await validateConfig('migration.manifest',manifest);
    const declared=manifest.files.map(f=>f.relativePath),assetPaths=[...manifest.optionalAssets.map(a=>a.file.relativePath),...(manifest.purchaseAssets||[]).map(a=>a.relativePath)];
    if(new Set(declared).size!==declared.length||new Set(assetPaths).size!==assetPaths.length||!declared.includes('config/portable.config.json')||declared.some(p=>p!=='config/portable.config.json'&&!assetPaths.includes(p)))fail('MIGRATION_UNEXPECTED_MEMBER','迁移文件清单含非白名单成员');
    const expected=new Set(['migration.manifest.json',...declared]);
    if(zip.entries.size!==expected.size||[...zip.entries.keys()].some(n=>!expected.has(n)))fail('MIGRATION_UNEXPECTED_MEMBER','迁移包包含非白名单文件');
    for(const entry of manifest.files){
      relativeSafe(entry.relativePath);let bytes=0;const hash=crypto.createHash('sha256');
      await zip.read(entry.relativePath,b=>{bytes+=b.length;hash.update(b);});
      if(bytes!==entry.bytes||hash.digest('hex')!==entry.sha256)fail('MIGRATION_HASH','迁移成员大小或哈希不一致',{relativePath:entry.relativePath});
    }
    for(const a of manifest.optionalAssets)if(canonical(manifest.files.find(f=>f.relativePath===a.file.relativePath))!==canonical(a.file))fail('MIGRATION_HASH','资产与文件清单不一致');
    for(const a of manifest.purchaseAssets||[])if(canonical(manifest.files.find(f=>f.relativePath===a.relativePath))!==canonical({relativePath:a.relativePath,bytes:a.bytes,sha256:a.sha256}))fail('MIGRATION_HASH','采购资产与文件清单不一致');
    const config=JSON.parse((await zip.buffer('config/portable.config.json',1048576)).toString('utf8'));
    await validateConfig('portable.config',config);checkManifest(manifest,config);
    const configConflict=!!currentConfig&&canonical(currentConfig)!==canonical(config);
    return {file:path.resolve(file),manifest,config,packageHash:await fileHash(file),configConflict,currentConfigFingerprint:currentConfig?sha256(canonical(currentConfig)):null,
      summary:{migrationId:manifest.migrationId,tenant:config.tenantBinding.displayName,targets:config.targets.length,batches:config.batches.length,unresolvedCount:manifest.unresolvedRecords.length,assets:manifest.optionalAssets.length,purchaseRuns:manifest.purchaseRecords?.length||0,purchaseAssets:manifest.purchaseAssets?.length||0,handoffStatus:manifest.handoffStatus,needsMachineSetup:true}};
  }finally{zip.close();}
}
export async function applyMigration(preview,{state,stateRoot,acceptConfigConflict=false}){
  const configPath=path.join(stateRoot,'config/portable.config.json');
  const current=await exists(configPath)?await readJson(configPath):null;
  const fingerprint=current?sha256(canonical(current)):null;
  if(fingerprint!==preview.currentConfigFingerprint)fail('MIGRATION_PREVIEW_STALE','预览后本机配置发生变化，请重新核对');
  if(await fileHash(preview.file)!==preview.packageHash)fail('MIGRATION_PREVIEW_STALE','预览后迁移包发生变化');
  if(preview.configConflict&&!acceptConfigConflict)fail('MIGRATION_CONFIG_CONFLICT','迁入配置与本机配置不同，需先明确处理差异');
  const cached=path.join(stateRoot,'recovery/imports',preview.packageHash+'.zip');await fs.mkdir(path.dirname(cached),{recursive:true});
  if(!await exists(cached))await fs.copyFile(preview.file,cached);
  if(await fileHash(cached)!==preview.packageHash)fail('MIGRATION_HASH','本机迁入缓存不完整');
  if(preview.manifest.purchaseRecords?.length){await state.enablePurchases();state.importPurchases(preview.manifest.purchaseRecords);}
  const result=state.importRecords(preview.manifest,preview.packageHash);
  if(result.alreadyImported&&!preview.configConflict){const p=path.join(stateRoot,'recovery/migration-status.json'),status=await exists(p)?await readJson(p):null;return {...result,needsMachineSetup:status?.migrationId===preview.manifest.migrationId?!!status.needsMachineSetup:false};}
  if(current&&preview.configConflict)await writeJson(path.join(stateRoot,'recovery','config-before-import-'+uid()+'.json'),current);
  await writeJson(configPath,preview.config);
  await writeJson(path.join(stateRoot,'recovery','migration-status.json'),{migrationId:preview.manifest.migrationId,tenantBinding:preview.config.tenantBinding,handoffStatus:preview.manifest.handoffStatus,needsMachineSetup:true,packageFile:cached,assetsPending:preview.manifest.optionalAssets.length>0||!!preview.manifest.purchaseAssets?.length,runBlocked:preview.manifest.handoffStatus!=='SOURCE_STOPPED'});
  return {...result,needsMachineSetup:true};
}
export async function importMigrationAssets(file,{state,outputRoot,expectedIdentity}){
  const preview=await previewMigration(file),zip=await openArchive(file,{maxUncompressedBytes:17179869184,maxEntries:10000});const saved=[],conflicts=[];
  try{
    const registered=state.rows('migration_imports').find(m=>m.migrationId===preview.manifest.migrationId);
    if(!registered||registered.packageHash!==preview.packageHash)fail('MIGRATION_NOT_IMPORTED','该结果包尚未作为本机迁入包登记，或缓存包内容变化');
    if(expectedIdentity&&!sameIdentity(expectedIdentity,preview.config.tenantBinding))fail('MIGRATION_IDENTITY','结果缓存与当前配置商家不同');
    await recoverPublications({state,outputRoot,identity:preview.config.tenantBinding});
    for(const a of preview.manifest.optionalAssets){
      const relative=a.file.relativePath.slice('assets/results/'.length),staging=await outputPath(outputRoot,'.staging/imports/'+a.file.sha256+'.xlsx');
      if(!await exists(staging)||await fileHash(staging)!==a.file.sha256){
        const h=await fs.open(staging,'w');try{await zip.read(a.file.relativePath,async b=>{for(let i=0;i<b.length;){const {bytesWritten}=await h.write(b,i,b.length-i);if(!bytesWritten)fail('MIGRATION_WRITE_FAILED','结果写入未取得进展');i+=bytesWritten;}});await h.sync();}finally{await h.close();}
      }
      if(await fileHash(staging)!==a.file.sha256)fail('MIGRATION_HASH','迁入结果的实际内容不一致');
      const v=await validateWorkbook(staging,{warehouseCode:a.buyerWarehouseCode,expectedRows:a.rows,contract:await fieldContract(a.semanticVersion==='D13-all-info-1'?'v1':'current')});
      const asset={...preview.config.tenantBinding,...a,bytes:v.bytes,sha256:v.sha256,relativePath:relative};delete asset.file;
      try{saved.push(await publishAsset({state,outputRoot,sourceFile:staging,asset,replace:false}));}
      catch(e){if(!['ASSET_CONFLICT','UNTRACKED_FILE_EXISTS','EXISTING_ASSET_CHANGED'].includes(e.code))throw e;conflicts.push(state.addConflict({type:'IMPORTED_ASSET_CONFLICT',errorCode:e.code,buyerWarehouseCode:a.buyerWarehouseCode,acquisitionDate:a.acquisitionDate,importedSha256:a.file.sha256,packageHash:preview.packageHash}));}
    }const purchase=await importPurchaseFiles(zip,preview.manifest,{state,outputRoot});return {saved:saved.length+purchase.saved,conflicts:[...conflicts,...purchase.conflicts]};
  }finally{zip.close();}
}
