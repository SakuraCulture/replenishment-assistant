import fs from 'node:fs/promises';
import path from 'node:path';
import {redact,now,businessDate,writeJson} from './common.mjs';
export class EventLog{
  constructor(root){this.root=root;this.queue=Promise.resolve();}
  write(event){const safe=redact({time:now(),...event});this.queue=this.queue.then(async()=>{const file=path.join(this.root,'logs',businessDate()+'.jsonl');await fs.mkdir(path.dirname(file),{recursive:true});await fs.appendFile(file,JSON.stringify(safe)+'\n','utf8');});return this.queue;}
  flush(){return this.queue;}
}
const csv=v=>'"'+String(v??'').replaceAll('"','""')+'"';
export async function writeRunReport(state,run,binding){
  const rows=state.getItems(run.id).map(item=>{
    const a=item.attemptId?state.getAttempt(item.attemptId):null,asset=item.assetKey?state.getAsset(item.assetKey):null;
    return {warehouseCode:item.target.buyerWarehouseCode,name:item.target.displayName,status:item.stage,mode:item.stage==='REUSED'?'复用当日文件':run.mode==='rerun'?'重新导出':'本次获取',taskId:a?.taskId||asset?.taskId||null,rows:asset?.rows??null,collectedAt:asset?.collectedAt||a?.query?.observedAt||null,file:asset?path.join(binding.outputRoot,asset.relativePath):null,errorCode:a?.errorCode||item.errorCode||null};
  });
  const report={runId:run.id,status:run.status,day:run.day,merchantCode:run.identity.merchantCode,createdAt:run.createdAt,completedAt:run.completedAt||null,errorCode:run.errorCode||null,items:rows};
  const file=path.join(state.root,'reports',run.id+'.json');await writeJson(file,report);
  const headings=['仓店编码','仓店名称','状态','获取方式','平台任务号','文件行数','原采集时间','文件路径','异常代码'];
  const lines=[headings, ...rows.map(r=>[r.warehouseCode,r.name,r.status,r.mode,r.taskId,r.rows,r.collectedAt,r.file,r.errorCode])];
  await fs.writeFile(file.replace(/\.json$/,'.csv'),'\uFEFF'+lines.map(r=>r.map(csv).join(',')).join('\r\n')+'\r\n','utf8');return file;
}
