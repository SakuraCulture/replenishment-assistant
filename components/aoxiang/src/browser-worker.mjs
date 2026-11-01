import path from 'node:path';
import {EdgeBridge} from './edge-bridge.mjs';
import {AoxiangUI} from './aoxiang-ui.mjs';
import {PurchaseUI} from './purchase-ui.mjs';
import {PurchaseCreateUI,installPurchaseWriteFence} from './purchase-create-ui.mjs';
import {ExclusiveLock} from './state.mjs';
import {redact} from './common.mjs';

const bridge=new EdgeBridge();let ui=null,uiSource='D13',lock=null,busy=false,closing=false;
async function sourceUI(source){if(uiSource===source)return;const identity=ui.identity,options={timeoutMs:ui.timeoutMs};await ui.dispose();const Adapter=source==='purchase_create'?PurchaseCreateUI:source==='purchase'?PurchaseUI:AoxiangUI;ui=new Adapter(bridge.page,options);uiSource=source;ui.onReceipt=receipt=>send({event:'accepted',receipt});ui.onImportReceipt=receipt=>send({event:'purchase_import_receipt',receipt});await ui.open(identity);}
function send(message){if(process.connected)process.send(message);}
async function close(){
  if(closing)return;closing=true;
  try{await ui?.dispose();await bridge.close();}finally{try{lock?.release();}catch{}process.exit(0);}
}
process.on('disconnect',()=>void close());process.on('SIGTERM',()=>void close());process.on('SIGINT',()=>void close());
process.on('message',async message=>{
  const {id,method,params={}}=message||{};
  if(method==='stop'){await close();return;}
  if(!id||busy){send({id,error:{code:'WORKER_BUSY',message:'浏览器执行器正在处理上一操作'}});return;}
  busy=true;
  try{
    let result;
    if(method==='connect'){
      if(ui){await sourceUI(params.source==='purchase'?'purchase':'D13');result={facts:bridge.facts,identity:await ui.open(params.expectedIdentity)};send({id,result});return;}
      if(!lock)lock=await ExclusiveLock.acquire(path.join(params.stateRoot,'runner.lock.sqlite'));
      const facts=await bridge.bind(params.candidate,{timeout:params.timeout||60000,mode:params.mode||'background_tab',onPhase:phase=>send({event:'phase',phase})});
      await installPurchaseWriteFence(bridge.page);
      uiSource=params.source==='purchase'?'purchase':'D13';const Adapter=uiSource==='purchase'?PurchaseUI:AoxiangUI;ui=new Adapter(bridge.page,{timeoutMs:params.pageTimeoutMs||30000,focusSession:bridge.takeFocusSession()});
      ui.onReceipt=receipt=>send({event:'accepted',receipt});
      const identity=await ui.open(params.expectedIdentity);result={facts,identity};
    }else{
      if(!ui)throw Object.assign(new Error('尚未连接浏览器'),{code:'SETUP_REQUIRED'});
      if(['queryPurchase','preparePurchase','submitPurchase','purchaseTask','downloadPurchase','downloadPurchaseTemplate'].includes(method))await sourceUI('purchase');
      else if(['preparePurchaseCreate','uploadPurchaseOnce','purchaseImportTask','queryCreateOrders'].includes(method))await sourceUI('purchase_create');
      else if(['querySingle','warehouseOptions','prepareExport','submitOnce','findTask'].includes(method))await sourceUI('D13');
      if(method==='recheckLogin')result={facts:bridge.facts,identity:await ui.open(params.expectedIdentity)};
      else if(method==='verifyIdentity')result=await ui.verifyIdentity(params.expected);
      else if(method==='querySingle')result=await ui.querySingle(params.code??null,params.name);
      else if(method==='warehouseOptions')result=await ui.warehouseOptions(params.term);
      else if(method==='inspect')result={...await ui.inspect(),selectorTitles:await ui.page.locator('.ant-select-tree-title').allTextContents(),observations:ui.observations.slice(-12)};
      else if(method==='prepareExport')result=await ui.prepareExport(params.expected);
      else if(method==='submitOnce')result=await ui.submitOnce(params);
      else if(method==='queryPurchase')result=await ui.queryPurchase(params);
      else if(method==='preparePurchase')result=await ui.preparePurchase(params.query,params.sourceId);
      else if(method==='submitPurchase')result=await ui.submitPurchase(params);
      else if(method==='purchaseTask'){const t=await ui.purchaseTask(params.taskId,params.sourceId,params.query);result={taskId:t.taskId,taskCode:t.taskCode||t.code,taskStatus:t.taskStatus,successCount:t.successCount,failCount:t.failCount};}
      else if(method==='downloadPurchase')result=await ui.downloadPurchase(params);
      else if(method==='downloadPurchaseTemplate')result=await ui.downloadPurchaseTemplate(params);
      else if(method==='preparePurchaseCreate')result=await ui.preparePurchaseCreate(params);
      else if(method==='uploadPurchaseOnce')result=await ui.uploadPurchaseOnce(params);
      else if(method==='purchaseImportTask')result=await ui.purchaseImportTask(params);
      else if(method==='queryCreateOrders')result=await ui.queryCreateOrders(params);
      else if(method==='findTask'){
        const t=await ui.findTask(params.taskId);result={taskId:String(t.taskId),taskStatus:t.taskStatus,code:t.code||t.taskCode,successCount:t.successCount,failCount:t.failCount,fileUrl:t.fileUrl,startTime:t.startTime,doneTime:t.doneTime};
      }else throw Object.assign(new Error('未允许的浏览器操作'),{code:'UNKNOWN_WORKER_METHOD'});
    }
    send({id,result});
  }catch(e){send({id,error:redact({code:e.code||'BROWSER_OPERATION_FAILED',message:e.message,details:e.details})});}
  finally{busy=false;}
});
