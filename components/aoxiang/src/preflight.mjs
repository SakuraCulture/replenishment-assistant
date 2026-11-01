import fs from 'node:fs/promises';
import path from 'node:path';
import {uid,exists,fail,readJson,sameIdentity,now} from './common.mjs';

export async function checkOutput(root,{minimumFreeBytes=16777216}={}){
  if(!path.isAbsolute(root)||/^\\\\/.test(root))fail('OUTPUT_ROOT_UNSUPPORTED','请选择本机磁盘上的绝对输出目录');
  await fs.mkdir(root,{recursive:true});const real=await fs.realpath(root);
  const probe=path.join(real,'.ax-write-check-'+uid());let h;
  try{h=await fs.open(probe,'wx');await h.writeFile('AX');await h.sync();await h.close();h=null;await fs.unlink(probe);}catch(e){try{await h?.close();}catch{}fail('OUTPUT_NOT_WRITABLE','输出目录无法写入；未提交新导出任务',{systemCode:e.code});}
  const info=await fs.statfs(real,{bigint:true}),free=info.bavail*info.bsize;
  if(free<BigInt(minimumFreeBytes))fail('LOW_DISK_SPACE','输出磁盘剩余空间不足；保留原文件，暂不创建新任务',{freeBytes:free.toString(),minimumFreeBytes});
  return {root:real,freeBytes:free.toString(),checkedAt:now()};
}
export async function assertHandoff(root,identity){
  for(const file of ['recovery/migration-status.json','recovery/source-handoff.json']){
    const p=path.join(root,file);if(!await exists(p))continue;
    const s=await readJson(p);if(!sameIdentity(s.tenantBinding,identity))continue;
    if(s.needsMachineSetup)fail('MACHINE_REBIND_REQUIRED','迁入配置尚未完成本机setup核验，不能开始作业');
    if(s.runBlocked)fail('HANDOFF_CONFIRMATION_REQUIRED','本机处于迁移交接状态；请先在迁移菜单确认当前执行电脑');
  }
}
