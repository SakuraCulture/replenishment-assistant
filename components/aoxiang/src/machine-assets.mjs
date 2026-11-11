import fs from 'node:fs/promises';
import path from 'node:path';
import {constants} from 'node:fs';
import {exists,fail,uid} from './common.mjs';
import {outputPath} from './publisher.mjs';
import {fileHash} from './workbook-validator.mjs';
import {portablePath} from './portable-assets.mjs';

// Return a command-owned immutable copy, never a mutable daily export path.
export async function freezeRunAsset({state,binding,commandId,item}){
  state.requireCollection(item.run_id);
  const run=state.getRun(item.run_id),entries=state.getItems(item.run_id);
  if(run?.status!=='SUCCEEDED'||entries.length!==1)fail('RUN_NOT_COMPLETE','原导出作业尚未完整通过验收');
  const entry=entries[0],asset=entry.assetSnapshot;
  if(!asset||asset.buyerWarehouseCode!==item.warehouse_code)fail('ASSET_SNAPSHOT_MISSING','原作业缺少确切文件版本，不采用当前同日文件猜测');
  const relative=`command-files/${commandId}/${item.index}-${asset.sha256}.xlsx`;
  const destination=await outputPath(state.root,relative);
  if(!await exists(destination)){
    const attempt=entry.attemptId?state.getAttempt(entry.attemptId):null;
    const candidates=[attempt?.downloaded?.file,
      await outputPath(binding.outputRoot,asset.relativePath),
      await outputPath(binding.outputRoot,`.staging/backups/${asset.sha256}.xlsx`)];
    let source=null;
    for(const file of candidates){const candidate=file&&await portablePath(file,{sha256:asset.sha256});if(candidate&&await exists(candidate)&&await fileHash(candidate)===asset.sha256){source=candidate;break;}}
    if(!source)fail('ASSET_VERSION_MISSING','原命令已验收的文件版本缺失；保留任务，不能改用更新后的文件');
    const temp=destination+'.'+uid()+'.tmp';
    try{
      await fs.copyFile(source,temp,constants.COPYFILE_EXCL);
      if(await fileHash(temp)!==asset.sha256)fail('ASSET_COPY_CHANGED','复制期间源文件发生变化，未采用');
      const handle=await fs.open(temp,'r+');try{await handle.sync();}finally{await handle.close();}
      await fs.rename(temp,destination);
    }finally{await fs.rm(temp,{force:true});}
  }
  if(await fileHash(destination)!==asset.sha256)fail('FROZEN_ASSET_CHANGED','命令原件副本已变化，不能作为有效输入');
  return {warehouse_code:item.warehouse_code,run_id:run.id,task_id:asset.taskId,
    file:path.resolve(destination),sha256:asset.sha256,rows:asset.rows,columns:asset.columns,bytes:asset.bytes,
    collected_at:asset.collectedAt,acquisition_date:asset.acquisitionDate,semantic_version:asset.semanticVersion,
    account_binding:{account_id:asset.accountId,merchant_code:asset.merchantCode},reused:entry.stage==='REUSED'};
}
