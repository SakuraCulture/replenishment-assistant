import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';
import {CatalogError} from './common.mjs';

async function atomic(file,data){
  const tmp=file+'.'+crypto.randomUUID()+'.tmp',handle=await fs.open(tmp,'wx');
  try{await handle.writeFile(JSON.stringify(data,null,2));await handle.sync();}finally{await handle.close();}
  await fs.rename(tmp,file);
}
const exists=async file=>{try{await fs.access(file);return true;}catch(e){if(e.code==='ENOENT')return false;throw e;}};
export class JobStore {
  static async open(root,{parentPid}={}){
    await fs.mkdir(root,{recursive:true});const guard=new DatabaseSync(path.join(root,'writer.lock.sqlite'));
    try{
      guard.exec('PRAGMA journal_mode=DELETE; PRAGMA busy_timeout=0; CREATE TABLE IF NOT EXISTS guard(v INTEGER); BEGIN EXCLUSIVE;');
      const file=path.join(root,'jobs.json'),marker=path.join(root,'store.guard.json');
      const [dataPresent,markerPresent]=await Promise.all([exists(file),exists(marker)]);
      if(dataPresent!==markerPresent)throw Error('任务状态或身份标记缺失，不能重建空状态后重复导出');
      if(!dataPresent){const storeId=crypto.randomUUID();await atomic(file,{schemaVersion:1,storeId,batches:{},assets:{}});await atomic(marker,{schemaVersion:1,storeId});}
      const data=JSON.parse(await fs.readFile(file,'utf8')),meta=JSON.parse(await fs.readFile(marker,'utf8'));
      if(data.schemaVersion!==1||data.storeId!==meta.storeId||!data.batches||!data.assets)throw Error('任务状态版本或身份不一致');
      const owner={schema:1,pid:process.pid,parentPid:parentPid||process.ppid,token:crypto.randomUUID(),startedAt:new Date().toISOString()};
      const ownerFile=path.join(root,'service-owner.json');await atomic(ownerFile,owner);
      return new JobStore(file,data,guard,{file:ownerFile,...owner});
    }catch(e){
      try{guard.close();}catch{}
      if([5,6].includes(e.errcode)){
        const owner=await fs.readFile(path.join(root,'service-owner.json'),'utf8').then(JSON.parse,()=>null).catch(()=>null);
        throw new CatalogError('CATALOG_STATE_BUSY','聚水潭原采集服务仍占用本地任务锁'+(Number.isSafeInteger(owner?.pid)?'（进程 '+owner.pid+'）':'')+'；可能正在安全退出。请等待原任务结束后重试；任务和已下载文件已保留。',
          {owner_pid:owner?.pid||null,parent_pid:owner?.parentPid||null,state_root:path.resolve(root)});
      }
      throw e;
    }
  }
  constructor(file,data,guard,owner=null){this.file=file;this.data=data;this.guard=guard;this.owner=owner;this.tail=Promise.resolve();this.closed=false;}
  read(){return structuredClone(this.data);}
  accounts(){return structuredClone(Object.values(this.data.accounts||{}));}
  batch(id){const b=this.data.batches[id];if(!b)throw Error('批次不存在：'+id);return structuredClone(b);}
  write(fn){
    const op=this.tail.catch(()=>{}).then(async()=>{
      if(this.closed)throw Error('任务状态已关闭');const next=structuredClone(this.data);const value=fn(next);await atomic(this.file,next);this.data=next;return structuredClone(value);
    });this.tail=op;return op;
  }
  async close(){
    await this.tail.catch(()=>{});if(this.closed)return;this.closed=true;
    try{
      // Remove only our diagnostic marker, while still holding the guard. A new
      // owner cannot publish its marker between this check and unlink.
      if(this.owner){
        const current=await fs.readFile(this.owner.file,'utf8').then(JSON.parse,()=>null).catch(()=>null);
        if(current?.token===this.owner.token)await fs.unlink(this.owner.file).catch(e=>{if(e.code!=='ENOENT')throw e;});
      }
    }finally{try{this.guard.exec('ROLLBACK');}finally{this.guard.close();}}
  }
}
