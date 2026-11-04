// Opt-in live, exact-PO read/export verification. Never submit or receive a PO.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {EdgeBridge} from '../components/aoxiang/src/edge-bridge.mjs';
import {PurchaseUI} from '../components/aoxiang/src/purchase-ui.mjs';
import {installPurchaseWriteFence} from '../components/aoxiang/src/purchase-create-ui.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const [outArg,po]=process.argv.slice(2);
if(!outArg||!/^PO\d+$/.test(po||''))throw Error('Usage: node verify_aoxiang_completed.mjs NEW_QA_DIRECTORY PO...');
const out=path.resolve(outArg);if(!out.startsWith(path.join(root,'qa')+path.sep))throw Error('Isolated QA directory required');
await fs.mkdir(out,{recursive:false});
const configRoot=path.join(process.env.LOCALAPPDATA,'YoutuanTools/补货助手/settings/aoxiang/component-state/config');
const binding=JSON.parse(await fs.readFile(path.join(configRoot,'machine.binding.json'),'utf8'));
const config=JSON.parse(await fs.readFile(path.join(configRoot,'portable.config.json'),'utf8'));
const save=(name,value)=>fs.writeFile(path.join(out,name),JSON.stringify(value,null,2));
const bridge=new EdgeBridge();let ui;
try{
 await save('status.json',{phase:'connecting',po});
 await bridge.bind(binding,{timeout:180000,mode:'background_tab',onPhase:p=>console.log(p)});
 await installPurchaseWriteFence(bridge.page);
 ui=new PurchaseUI(bridge.page,{timeoutMs:config.resourceLimits.pageTimeoutMs,focusSession:bridge.takeFocusSession()});
 const identity=await ui.open(config.tenantBinding);
 await ui.click(bridge.page.getByRole('button',{name:'重置',exact:true}));
 await ui.setDates({from:'2026-09-01',to:'2026-11-04',dateBasis:'created'});
 await bridge.page.locator('#supplyOrderId').fill(po);
 const q=await ui.queryAction(()=>ui.click(bridge.page.getByRole('button',{name:'查询',exact:true})));
 if(q.total!==1||q.rows.length!==1||q.rows[0].supplyOrderId!==po)throw Error('Exact PO not uniquely visible');
 await save('query.json',{identity,...q});await save('status.json',{phase:'query_complete',po});console.log('Exact PO query saved');
 // Keep this verified connection alive while the operator/QA inspects the
 // observed store and date. No export is submitted before that instruction.
 for(let n=0;n<1200;n++){
  const command=await fs.readFile(path.join(out,'command.json'),'utf8').then(JSON.parse,()=>null);
  if(command?.action==='stop')break;
  if(command?.action==='export'){
   if(command.orderId!==po||command.target.buyerWarehouseCode!==q.rows[0].buyerWarehouseCode)throw Error('Command scope differs');
   const {StateStore}=await import('../components/aoxiang/src/state.mjs');
   const {PurchaseCoordinator}=await import('../components/aoxiang/src/purchase-coordinator.mjs');
   const state=await StateStore.open(path.join(out,'state'));
   try{
    const coordinator=new PurchaseCoordinator({state,config,binding:{...binding,outputRoot:path.join(out,'downloads')},adapter:ui,onProgress:async p=>{await save('status.json',p);console.log(p.stage);}});
    const result=await coordinator.run({target:command.target,from:command.day,to:command.day,dateBasis:'created',orderId:po});
    await save('run.json',result);await save('status.json',{phase:'complete',runId:result.id,status:result.status});
   }finally{state.close();}
   break;
  }
  await new Promise(r=>setTimeout(r,1000));
 }
}catch(e){await save('failure.json',{code:e.code||'QA_ERROR',message:e.message});console.error(e.code||'QA_ERROR',e.message);process.exitCode=1;}
finally{ui?.dispose();await bridge.close();}
