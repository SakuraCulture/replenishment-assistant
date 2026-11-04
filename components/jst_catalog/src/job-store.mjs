import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {DatabaseSync} from 'node:sqlite';

async function atomic(file,data){
  const tmp=file+'.'+crypto.randomUUID()+'.tmp',handle=await fs.open(tmp,'wx');
  try{await handle.writeFile(JSON.stringify(data,null,2));await handle.sync();}finally{await handle.close();}
  await fs.rename(tmp,file);
}
const exists=async file=>{try{await fs.access(file);return true;}catch(e){if(e.code==='ENOENT')return false;throw e;}};
export class JobStore {
  static async open(root){
    await fs.mkdir(root,{recursive:true});const guard=new DatabaseSync(path.join(root,'writer.lock.sqlite'));
    try{
      guard.exec('PRAGMA journal_mode=DELETE; PRAGMA busy_timeout=0; CREATE TABLE IF NOT EXISTS guard(v INTEGER); BEGIN EXCLUSIVE;');
      const file=path.join(root,'jobs.json'),marker=path.join(root,'store.guard.json');
      const [dataPresent,markerPresent]=await Promise.all([exists(file),exists(marker)]);
      if(dataPresent!==markerPresent)throw Error('任务状态或身份标记缺失，不能重建空状态后重复导出');
      if(!dataPresent){const storeId=crypto.randomUUID();await atomic(file,{schemaVersion:1,storeId,batches:{},assets:{}});await atomic(marker,{schemaVersion:1,storeId});}
      const data=JSON.parse(await fs.readFile(file,'utf8')),meta=JSON.parse(await fs.readFile(marker,'utf8'));
      if(data.schemaVersion!==1||data.storeId!==meta.storeId||!data.batches||!data.assets)throw Error('任务状态版本或身份不一致');
      return new JobStore(file,data,guard);
    }catch(e){try{guard.close();}catch{}throw e;}
  }
  constructor(file,data,guard){this.file=file;this.data=data;this.guard=guard;this.tail=Promise.resolve();this.closed=false;}
  read(){return structuredClone(this.data);}
  accounts(){return structuredClone(Object.values(this.data.accounts||{}));}
  batch(id){const b=this.data.batches[id];if(!b)throw Error('批次不存在：'+id);return structuredClone(b);}
  write(fn){
    const op=this.tail.catch(()=>{}).then(async()=>{
      if(this.closed)throw Error('任务状态已关闭');const next=structuredClone(this.data);const value=fn(next);await atomic(this.file,next);this.data=next;return structuredClone(value);
    });this.tail=op;return op;
  }
  async close(){await this.tail.catch(()=>{});if(this.closed)return;this.closed=true;try{this.guard.exec('ROLLBACK');}finally{this.guard.close();}}
}
