// Real fork/IPC, simulated page/request only. No Edge and no HTTP connection.
import {EventEmitter} from 'node:events';
import {pathToFileURL,fileURLToPath} from 'node:url';
import path from 'node:path';
const root=process.env.AX_GUARD_FIXTURE_COMPONENT||path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const load=name=>import(pathToFileURL(path.join(root,'src',name)).href);
const [{WorkerPurchaseGuards,uploadWithHostGuard},{NativeImportGuard},{IMPORT_CODE},{sha256}]=await Promise.all(
  ['purchase-guard-ipc.mjs','purchase-create-ui.mjs','purchase-create-contract.mjs','common.mjs'].map(load));
const send=m=>{if(process.connected)process.send(m,()=>{});};
const channel=new WorkerPurchaseGuards(send,{timeoutMs:Number(process.env.AX_GUARD_FIXTURE_TIMEOUT||2000)});
const counts={released:0,aborted:0};let prepared=null;
const used=new Set();
const ui={async uploadPurchaseOnce(params){
  const {intent:p}=params;
  if(used.has(p.id)||prepared!==p.id)throw Object.assign(Error('not prepared'),{code:'IMPORT_NOT_PREPARED'});
  if(typeof this.beforePurchaseImport!=='function')throw Object.assign(Error('missing final host check'),{code:'HOST_GUARD_REQUIRED'});
  const page=Object.assign(new EventEmitter(),{route:async()=>{},unroute:async()=>{}}),bytes=Buffer.from('isolated-frozen-xlsx-bytes');
  const before=this.beforePurchaseImport;
  const guard=new NativeImportGuard(page,p,bytes,r=>send({event:'purchase_import_receipt',receipt:r}),async details=>{
    const refuse=await before(details);if(params.cancelAfterApproval)guard.closed=true;return refuse;
  });
  used.add(p.id);await guard.install();
  try{
    const body=Buffer.concat([Buffer.from('multipart-'),bytes]);
    const request={url:()=>`https://sc-gei-tc-supply-chain-portal.ele.me/gei/import/task/${IMPORT_CODE}`,method:()=> 'PUT',postDataBuffer:()=>body};
    await guard.route({request:()=>request,continue:async()=>{counts.released++;},abort:async()=>{counts.aborted++;}});
    if(guard.error)throw guard.error;
    if(counts.released)await guard.observe({request:()=>request,status:()=>200,text:async()=>JSON.stringify({taskId:`GEI@${IMPORT_CODE}@261116000000@7114`})});
    return await guard.result();
  }finally{await guard.dispose();if(!guard.requests)used.delete(p.id);}
}};
process.on('disconnect',()=>{channel.close();process.exit(0);});
process.on('message',async m=>{
  if(channel.receive(m))return;
  const {id,method,params={}}=m;
  if(method==='stop'){channel.close();process.exit(0);}
  try{
    let result;
    if(method==='connect')result={identity:params.expectedIdentity,facts:{actualProfilePath:params.candidate.actualProfilePath,edgeVersion:'synthetic'}};
    else if(method==='preparePurchaseCreate'){prepared=params.id;result={preparedAt:'synthetic',orderIds:[]};}
    else if(method==='uploadPurchaseOnce')result=await uploadWithHostGuard(ui,params,id,channel);
    else if(method==='inspect')result={...counts,pendingGuards:channel.pending.size};
    else throw Error('unexpected synthetic method');
    send({id,result});
  }catch(e){send({id,error:{code:e.code||'SYNTHETIC_ERROR',message:e.message,details:e.details}});}
});
