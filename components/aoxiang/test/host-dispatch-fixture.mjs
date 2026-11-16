// Offline fixture only: no browser is constructed and no HTTP request is sent.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {DatabaseSync} from 'node:sqlite';
import {fileURLToPath,pathToFileURL} from 'node:url';
const component=process.argv[4]||path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const load=name=>import(pathToFileURL(path.join(component,'src',name)).href);
const [{StateStore},{PurchaseCreates},{PurchaseCreateService},{MachinePurchaseCreate},{NativeImportGuard},{checkHostDispatch,hostFacts},{IMPORT_CODE}]=await Promise.all(
  ['state.mjs','purchase-create-ledger.mjs','purchase-create-service.mjs','machine-purchase-create.mjs','purchase-create-ui.mjs','purchase-host-guard.mjs','purchase-create-contract.mjs'].map(load));

const input=JSON.parse(await fs.readFile(process.argv[2],'utf8')),mode=process.argv[3]||'check';
assert.equal(input.synthetic,true);assert(path.resolve(input.business_path).startsWith(path.resolve(input.fixture_root)+path.sep));
process.env.REPLENISHMENT_BUSINESS_DB=input.business_path;
const p=JSON.parse(await fs.readFile(input.package.package_file,'utf8'));
if(mode==='check'||mode==='consume'){
  let result;
  try{checkHostDispatch(p,{consume:mode==='consume',requestHash:'a'.repeat(64)});result={ok:true};}
  catch(e){result={ok:false,code:e.code,message:e.message};}
  const db=new DatabaseSync(input.business_path,{readOnly:true});
  try{result.facts=hostFacts(db,p.basis);result.guard=db.prepare('SELECT state FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id)?.state;}
  finally{db.close();}
  console.log(JSON.stringify(result));
}else{
  const binding={schemaVersion:'2.0',exampleOnly:false,enabled:true,machineBindingId:'synthetic',edgeChannel:'stable',edgeExecutable:'synthetic-edge.exe',edgeVersion:'synthetic',userDataRoot:input.fixture_root,profileDirectory:'Default',actualProfilePath:path.join(input.fixture_root,'Default'),outputRoot:input.component,identityVerifiedAt:'2026-11-16T00:00:00Z',compatibilityResult:'PASSED'};
  await fs.writeFile(path.join(input.component,'config/machine.binding.json'),JSON.stringify(binding));
  const state=await StateStore.open(input.component);await state.enablePurchases();const ledger=await PurchaseCreates.open(state);
  ledger.reserve(p,input.package.package_sha256);
  let changed=false,acquisitions=0,preflights=0,released=0,aborted=0,controller;
  async function mutate(){
    if(changed)return;
    if(input.barrier){
      await fs.writeFile(input.barrier.ready,JSON.stringify({mode,synthetic:true}));
      while(true){try{const v=JSON.parse(await fs.readFile(input.barrier.go,'utf8'));if(v.cancel)throw Error('synthetic acceptance cancelled before release');break;}catch(e){if(e.code!=='ENOENT')throw e;await new Promise(r=>setTimeout(r,20));}}
    }else execFileSync(input.python,['-c',"import sys,json;sys.path.insert(0,sys.argv[1]);import order_lifecycle as lc;v=json.load(open(sys.argv[2],encoding='utf-8'));lc.commit(v['business_path'],v['new_payment_plan'])",input.source,input.input_file],{encoding:'utf8'});
    changed=true;
  }
  const adapter={downloadPurchase:async()=>{throw Error('unexpected download');},preparePurchaseCreate:async()=>({from:'2026-11-16',preparedAt:'offline',orderIds:[]}),
    uploadPurchaseOnce:async()=>{
      if(mode==='request')await mutate();
      const bytes=await fs.readFile(p.file.path),body=Buffer.concat([Buffer.from('multipart-prefix'),bytes,Buffer.from('multipart-suffix')]);
      const guard=new NativeImportGuard({},p,bytes,()=>{},adapter.beforePurchaseImport);
      const request={url:()=>`https://sc-gei-tc-supply-chain-portal.ele.me/gei/import/task/${IMPORT_CODE}`,method:()=> 'PUT',postDataBuffer:()=>body};
      await guard.route({request:()=>request,abort:async()=>{aborted++;},continue:async()=>{released++;if(mode==='after_boundary')await mutate();if(mode==='unknown_release')throw Error('synthetic network outcome unknown');}});
      if(guard.error)throw guard.error;
      assert.equal(guard.requests,1);
      return {intentId:p.id,taskId:`GEI@${IMPORT_CODE}@261116000000@7114`,code:IMPORT_CODE,fileSha256:p.file.sha256,requestSha256:guard.matched.get(request).requestSha256,exactSourceBytesPresent:true,importRequests:1};
    }};
  const service=new PurchaseCreateService({root:input.component,state,ledger,browsers:{acquire:async()=>{
    acquisitions++;if(mode==='prepare'&&acquisitions===1||mode==='connect'&&acquisitions===2)await mutate();
    if(mode==='cancel'&&acquisitions===1)controller.pause(p.id);
    return {adapter};
  }}});
  const update=ledger.update.bind(ledger);
  if(mode==='mark_failure'||mode==='mark_failure_after_write')ledger.update=(...args)=>{
    if(args[2]==='SUBMITTING'){
      if(mode==='mark_failure_after_write')update(...args);
      throw Error('synthetic component boundary save failure');
    }
    return update(...args);
  };
  controller=new MachinePurchaseCreate({ledger,adapter:service,preflight:async value=>{preflights++;return service.preflight(value);}});
  try{
    controller.start(p.id,{submitOnly:true});await controller.pending;
    const r=ledger.get(p.id);let restart=null;
    if(r.uploadAttempted&&!r.receipt){try{controller.start(p.id);}catch(e){restart=e.code;}}
    const db=new DatabaseSync(input.business_path,{readOnly:true});let persisted;
    try{persisted=db.prepare('SELECT state FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id).state;}finally{db.close();}
    console.log(JSON.stringify({state:r.state,uploadAttempted:r.uploadAttempted,error:r.error,taskId:r.receipt?.taskId,guard:persisted,preflights,acquisitions,released,aborted,changed,restart,quantity:p.rows[0].quantity,live_platform:false}));
  }finally{await controller.close();state.close();}
}
