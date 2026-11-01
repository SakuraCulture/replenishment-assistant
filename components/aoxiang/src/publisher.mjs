import fs from 'node:fs/promises';
import path from 'node:path';
import {constants} from 'node:fs';
import {fileHash} from './workbook-validator.mjs';
import {relativeSafe,safeName,uid,exists,fail,now,sameIdentity} from './common.mjs';
import {assetKey} from './state.mjs';

function below(root,candidate){const r=path.relative(root,candidate);return r===''||(!r.startsWith('..'+path.sep)&&r!=='..'&&!path.isAbsolute(r));}
export async function outputPath(root,relative){
  relativeSafe(relative);await fs.mkdir(root,{recursive:true});const realRoot=await fs.realpath(root);
  const pieces=relative.split('/');let current=realRoot;
  for(const part of pieces.slice(0,-1)){
    const next=path.join(current,part);
    if(!await exists(next))await fs.mkdir(next);
    current=await fs.realpath(next);if(!below(realRoot,current))fail('OUTPUT_PATH_ESCAPE','结果目录的链接超出所选输出根');
  }
  const final=path.join(current,pieces.at(-1));
  if(await exists(final)){const st=await fs.lstat(final);if(st.isSymbolicLink()||!st.isFile()||!below(realRoot,await fs.realpath(final)))fail('OUTPUT_PATH_ESCAPE','结果目标不是允许的普通文件');}
  return final;
}
export function dailyRelative(identity,target,date,layout){
  const tokens={tenantAlias:safeName(identity.displayName||identity.merchantCode)+'_'+safeName(identity.merchantCode),merchantCode:safeName(identity.merchantCode),storeName:safeName(target.displayName),buyerWarehouseCode:safeName(target.buyerWarehouseCode),'YYYY-MM-DD':date,YYYYMMDD:date.replaceAll('-','')};
  const render=s=>s.replace(/\{([^{}]+)\}/g,(_,k)=>{if(!(k in tokens))fail('CONFIG_TEMPLATE','文件布局使用了不支持的占位符：'+k);return tokens[k];});
  const folder=render(layout?.folderTemplate||'{tenantAlias}/{YYYY-MM-DD}'),file=render(layout?.filenameTemplate||'{storeName}_{buyerWarehouseCode}_{YYYYMMDD}_补货建议.xlsx');
  if(!file.toLowerCase().endsWith('.xlsx')||/[\\/<>:"|?*]/.test(file))fail('CONFIG_TEMPLATE','文件名模板必须生成安全的.xlsx文件名');
  return relativeSafe(folder+'/'+file);
}

export async function publishAsset({state,outputRoot,sourceFile,asset,attemptId,replace=false,rename=fs.rename}){
  const key=assetKey(asset),old=state.getAsset(key);
  const relative=old?.relativePath||asset.relativePath;
  const target=await outputPath(outputRoot,relative);
  if(await exists(target)){
    if(!old)fail('UNTRACKED_FILE_EXISTS','目标文件已存在但不属于本机已验收资产，不覆盖');
    if(await fileHash(target)!==old.sha256)fail('EXISTING_ASSET_CHANGED','现有日文件被修改，保留现文件并等待核对');
    if(!replace){if(old.sha256===asset.sha256)return old;fail('ASSET_CONFLICT','已有不同日文件，需要明确重跑');}
  }
  if(await fileHash(sourceFile)!==asset.sha256)fail('STAGING_HASH_CHANGED','待保存文件与核验记录不一致');
  const id=uid();const tempRelative=path.posix.dirname(relative)+'/.pending-'+id+'.xlsx';const temp=await outputPath(outputRoot,tempRelative);
  await fs.copyFile(sourceFile,temp,constants.COPYFILE_EXCL);
  if(await fileHash(temp)!==asset.sha256)fail('STAGING_COPY_FAILED','暂存复制内容不一致');
  const handle=await fs.open(temp,'r+');try{await handle.sync();}finally{await handle.close();}
  if(old&&await exists(target)){
    const backup=await outputPath(outputRoot,'.staging/backups/'+old.sha256+'.xlsx');
    if(!await exists(backup))await fs.copyFile(target,backup,constants.COPYFILE_EXCL);
    if(await fileHash(backup)!==old.sha256)fail('BACKUP_HASH_FAILED','原日文件备份校验失败，暂不替换');
  }
  const published={...asset,assetKey:key,relativePath:relative,publishedAt:now()};
  const intent={id,attemptId:attemptId||null,outputRoot:path.resolve(outputRoot),relativePath:relative,tempRelative,oldSha256:old?.sha256||null,newSha256:asset.sha256,asset:published,createdAt:now()};
  state.putPublish(intent);
  try{await rename(temp,target);}catch(e){fail('SAVE_BLOCKED','日文件未能替换，旧文件已保留；继续时只恢复本地保存',{publishId:id,systemCode:e.code});}
  if(await fileHash(target)!==asset.sha256)fail('FINAL_HASH_FAILED','最终文件与核验结果不一致，保留恢复记录');
  state.putAsset(published,{replace});state.deletePublish(id);return published;
}
export async function recoverPublications({state,outputRoot,identity}){
  const restored=[];
  for(const intent of state.rows('publishes')){
    if(identity&&!sameIdentity(intent.asset,identity))continue;
    if(path.resolve(outputRoot)!==intent.outputRoot)fail('OUTPUT_ROOT_CHANGED','存在旧输出目录的保存记录，请先核对目录');
    const target=await outputPath(outputRoot,intent.relativePath),temp=await outputPath(outputRoot,intent.tempRelative);
    const finalHash=await exists(target)?await fileHash(target):null;
    if(finalHash!==intent.newSha256){
      if(finalHash!==intent.oldSha256||!await exists(temp)||await fileHash(temp)!==intent.newSha256)fail('PUBLISH_RECOVERY_REQUIRED','保存现场与发布记录不一致，不能自动覆盖');
      try{await fs.rename(temp,target);}catch(e){fail('SAVE_BLOCKED','文件仍被占用，保留旧文件并等待本地保存恢复',{publishId:intent.id,systemCode:e.code});}
    }
    if(await fileHash(target)!==intent.newSha256)fail('FINAL_HASH_FAILED','恢复后的文件哈希不符');
    state.putAsset(intent.asset,{replace:true});state.deletePublish(intent.id);restored.push(intent);
  }return restored;
}
