import fs from 'node:fs/promises';
import path from 'node:path';
import {fail,sameIdentity,canonical,sha256} from './common.mjs';
import {PurchaseCoordinator} from './purchase-coordinator.mjs';
import {readReceiptSource,receiptObservation} from './purchase-receipts.mjs';

export class PurchaseReceiptService{
  constructor({state,creates,ledger,creationService,progress=()=>{}}){Object.assign(this,{state,creates,ledger,creationService,progress});}
  register(id){
    const c=this.creates.get(id);
    if(c.state!=='BOUND')fail('RECEIPT_NOT_BOUND','必须先核验唯一采购单，才能同步收货');
    const original=this.state.rows('purchase_runs').filter(r=>r.status==='SUCCEEDED'&&r.orderId===c.binding.orderId&&r.dateBasis==='created'&&sameIdentity(r.identity,c.intent.identity)&&c.binding.files.every(f=>r.exports[f.source]?.asset?.sha256===f.sha256));
    if(!original.length||new Set(original.map(r=>r.from)).size!==1)fail('RECEIPT_CREATION_PROOF','缺少原采购单已验收的制单日与主子原件依据，不按本地日期猜测');
    return this.ledger.register(c,original[0].from);
  }
  async observe(id,{signal}){
    let record=this.ledger.get(id);const c=this.creates.get(id),context=await this.creationService.context(c.intent,signal);
    const engine=new PurchaseCoordinator({state:this.state,...context,
      onProgress:e=>{if(e.stage==='PURCHASE_RUNNING')this.ledger.run(id,e.runId);this.progress(e.stage);},
      onSourceValidated:async({run,sourceId,file})=>{
        const source=await readReceiptSource(file,sourceId,record.scope,run.exports[sourceId].taskSummary.successCount,run.exports[sourceId].asset?.sha256||run.exports[sourceId].downloaded?.sha256);
        // The latch is written before any following export/download, including D17 failures.
        if(source.rows.some(r=>r.status==='已完成'))this.ledger.terminal(id,source);
      }});
    const run=await engine.run(record.activeRunId?{runId:record.activeRunId}:{target:c.intent.target,from:record.scope.createdDay,to:record.scope.createdDay,dateBasis:'created',orderId:record.scope.orderId,rerun:true});
    if(run.status!=='SUCCEEDED')fail('RECEIPT_ORDER_NOT_FOUND','指定采购单本次没有可见完整数据；不记作收货为0');
    const sources=[];
    for(const source of ['D16','D17'])sources.push(await readReceiptSource(await engine.sourceFile(run,source),source,record.scope,run.exports[source].asset.rows,run.exports[source].asset.sha256));
    const observation=receiptObservation(c.intent,c.binding,run.id,sources);
    const directory=path.join(this.state.root,'receipt-observations');await fs.mkdir(directory,{recursive:true});
    const file=path.join(directory,run.id+'.json'),content=canonical(observation)+'\n';
    // A crash after saving the file but before the ledger update resumes this exact proof.
    let saved=observation;
    try{const h=await fs.open(file,'wx');try{await h.writeFile(content,'utf8');await h.sync();}finally{await h.close();}}
    catch(e){if(e.code!=='EEXIST')throw e;saved=JSON.parse(await fs.readFile(file,'utf8'));
      const stable=o=>{const {observedAt,...rest}=o;return rest;};
      if(canonical(stable(saved))!==canonical(stable(observation)))fail('RECEIPT_PROOF_CHANGED','相同下载任务的收货观察文件已存在但内容不同');}
    const bytes=await fs.readFile(file),manifest={file,sha256:sha256(bytes)};
    return this.ledger.finish(id,saved,manifest);
  }
}
