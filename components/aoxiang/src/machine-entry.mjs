import path from 'node:path';
import {ExclusiveLock,StateStore} from './state.mjs';
import {MachineCommands,mappingVersion} from './machine-commands.mjs';
import {MachineRuntime,errorInfo} from './machine-runtime.mjs';
import {readFrames,writeFrame,validateRequest,publicRecord,correlationId} from './machine-protocol.mjs';
import {Coordinator} from './coordinator.mjs';
import {BoundBrowserPool} from './bound-browser.mjs';
import {freezeRunAsset} from './machine-assets.mjs';
import {loadConfig,validateConfig} from './config.mjs';
import {readJson,fail,sameIdentity,exists} from './common.mjs';
import {verifyInstallation} from './integrity.mjs';
import {assertHandoff} from './preflight.mjs';
import {MachineSetup,recoverConfiguration,readConfiguration} from './machine-setup.mjs';
import {MachineTemplate} from './machine-template.mjs';
import {PurchaseCreates,purchasePublic} from './purchase-create-ledger.mjs';
import {MachinePurchaseCreate} from './machine-purchase-create.mjs';
import {PurchaseCreateService} from './purchase-create-service.mjs';
import {readPurchasePackage,checkPurchaseConfiguration,checkPurchaseInput,freezePurchaseInput} from './purchase-create-artifacts.mjs';
import {PurchaseReceipts} from './purchase-receipt-ledger.mjs';
import {PurchaseReceiptService} from './purchase-receipt-service.mjs';
import {MachinePurchaseReceipt} from './machine-purchase-receipt.mjs';
import {PurchaseBindingService,MachinePurchaseBinding} from './purchase-binding.mjs';
import {releaseSession} from './machine-session.mjs';
import {CollectionMaintenance} from './machine-maintenance.mjs';
import {clientConnectionState} from './browser-connection.mjs';

let lock,state,runtime,setup,template,purchase,receiptSync,bindingCheck,outputFailed=false;
const browsers=new BoundBrowserPool();
const send=value=>writeFrame(process.stdout,{protocol_version:1,...value});
const rootArg=process.argv.slice(2);
// Explicit state root is mandatory: this entry never falls back to a daily user's state.
async function main(){
  if(rootArg.length!==2||rootArg[0]!=='--state-root'||!path.isAbsolute(rootArg[1])||/^\\\\/.test(rootArg[1]))fail('ARGUMENT','机器入口必须指定本机绝对路径 --state-root');
  await verifyInstallation();const root=path.resolve(rootArg[1]);
  lock=await ExclusiveLock.acquire(path.join(root,'coordinator.lock.sqlite'));
  state=await StateStore.open(root);const commands=new MachineCommands(state);
  const configurationRecovered=await recoverConfiguration(root);setup=new MachineSetup({root,browsers,state});
  template=new MachineTemplate({root,state,browsers});
  const purchases=await PurchaseCreates.open(state);purchases.recover();
  const purchaseService=new PurchaseCreateService({root,state,ledger:purchases,browsers,progress:stage=>{
    const active=[bindingCheck,receiptSync,purchase].find(controller=>controller?.busy);active?.progress(stage);
  }});
  purchase=new MachinePurchaseCreate({ledger:purchases,adapter:purchaseService,preflight:p=>purchaseService.preflight(p),onFatal:error=>{void fatal(error);}});
  const receipts=await PurchaseReceipts.open(state);receipts.recover();
  const receiptService=new PurchaseReceiptService({state,creates:purchases,ledger:receipts,creationService:purchaseService,progress:stage=>receiptSync?.progress(stage)});
  receiptSync=new MachinePurchaseReceipt({ledger:receipts,service:receiptService,onFatal:error=>{void fatal(error);}});
  const bindingService=new PurchaseBindingService({state,creates:purchases,creationService:purchaseService,progress:stage=>bindingCheck?.progress(stage)});
  bindingCheck=new MachinePurchaseBinding({creates:purchases,service:bindingService,onFatal:error=>{void fatal(error);}});
  const recovered=commands.recoverInterrupted();
  runtime=new MachineRuntime({commands,pipeline:true,onFatal:error=>{void fatal(error);},
    needsReview:item=>state.getItems(item.run_id).some(i=>['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(state.getAttempt(i.attemptId)?.stage)),
    executeItem:async({record,item,phase,signal,onProgress})=>{
      const current=await loadConfig(root),config=record.config_snapshot;
      if(!current.enabled||!sameIdentity(current.tenantBinding,config.tenantBinding))fail('WRONG_IDENTITY','当前配置身份变化，不能执行原命令');
      await assertHandoff(root,config.tenantBinding);
      if(!await exists(path.join(root,'config/machine.binding.json')))fail('SETUP_REQUIRED','请先完成本机浏览器绑定');
      const binding=await validateConfig('machine.binding',await readJson(path.join(root,'config/machine.binding.json')));
      const browser=await browsers.acquire({state,config,binding,signal,onProgress});
      const result=await new Coordinator({state,config,binding,adapter:browser.adapter,signal,onProgress}).run({runId:item.run_id,phase});
      if(result.pending)return {pending:true};
      return await freezeRunAsset({state,binding,commandId:record.id,item});
    }
  });
  const maintenance=new CollectionMaintenance(state,commands,runtime);
  await send({type:'ready',operations:['replenishment.export'],recovered:recovered.map(r=>({command_id:r.id,state:r.state,sequence:r.sequence})),configuration_recovered:configurationRecovered,configured:await exists(path.join(root,'config/portable.config.json'))});
  for await(const raw of readFrames(process.stdin)){
    let request,result,stop=false,kick=false;
    try{
      request=validateRequest(raw);
      const configurationActions=['discover','setup.start','setup.check_login','setup.verify_saved','setup.commit','template.start'];
      if((configurationActions.includes(request.action)||['reserve','resume','purchase.start','purchase.cancel','receipt.start','binding.start','binding.confirm'].includes(request.action))&&(purchase.busy||receiptSync.busy||bindingCheck.busy))fail('PURCHASE_CREATE_BUSY','当前采购、关联核对或收货动作正在运行或安全停止，请等待后再执行其他作业');
      if(configurationActions.includes(request.action)&&(runtime.active||commands.list().some(r=>r.state==='QUEUED')))fail('COMPONENT_BUSY','先暂停或完成当前取数任务，再修改连接与目录');
      if(configurationActions.includes(request.action)&&template.busy)fail('TEMPLATE_BUSY','先完成或停止当前采购模板获取');
      switch(request.action){
        case 'discover':result=await setup.discover();break;
        case 'setup.start':result=setup.begin(request);break;
        case 'setup.check_login':result=await setup.verifySaved([]);break;
        case 'setup.verify_saved':result=await setup.verifySaved(request.codes);break;
        case 'setup.status':result=setup.status(request.setup_id);break;
        case 'setup.cancel':result=await setup.cancel(request.setup_id);break;
        case 'setup.commit':result=await setup.commit(request.setup_id);break;
        case 'template.start':if(setup.busy)fail('SETUP_BUSY','先保存或取消当前连接核验');result=template.start();break;
        case 'template.status':result=template.status(request.template_id);break;
        case 'template.cancel':result=template.cancel(request.template_id);break;
        case 'purchase.reserve':{
          if(runtime.closing)fail('SHUTTING_DOWN','组件正在安全退出');
          const p=await readPurchasePackage(request.package_file,request.package_sha256);
          const existing=purchases.list().find(r=>r.id===p.id);
          if(existing){if(existing.requestHash!==request.package_sha256)fail('PURCHASE_INTENT_CHANGED','已有采购命令不能替换文件');result={created:false,record:purchase.status(p.id)};break;}
          const config=await loadConfig(root);checkPurchaseConfiguration(p,config);await assertHandoff(root,config.tenantBinding);await checkPurchaseInput(p);
          const accepted=purchases.reserve(await freezePurchaseInput(root,p),request.package_sha256);
          result={created:accepted.created,record:purchasePublic(accepted.record)};break;
        }
        case 'purchase.start':{
          if(runtime.closing||runtime.active||commands.list().some(r=>r.state==='QUEUED')||setup.busy||template.busy)fail('COMPONENT_BUSY','先完成或停止当前取数、模板或连接任务');
          result=purchase.start(request.purchase_id);break;
        }
        case 'purchase.status':result=purchase.status(request.purchase_id);break;
        case 'purchase.pause':result=purchase.pause(request.purchase_id);break;
        case 'purchase.cancel':result=purchase.cancelBeforeUpload(request.purchase_id);break;
        case 'purchase.list':{const all=purchases.list();result={total:all.length,items:all.slice(request.offset||0,(request.offset||0)+(request.limit||50)).map(purchasePublic)};break;}
        case 'receipt.start':{
          if(runtime.closing||runtime.active||commands.list().some(r=>r.state==='QUEUED')||setup.busy||template.busy)fail('COMPONENT_BUSY','先完成或停止当前取数、模板或连接任务');
          result=receiptSync.start(request.purchase_id);break;
        }
        case 'receipt.status':result=receiptSync.status(request.purchase_id);break;
        case 'receipt.host_void_conflict':result=receipts.hostVoidConflict(request.purchase_id,request.reason);break;
        case 'receipt.pause':result=receiptSync.pause(request.purchase_id);break;
        case 'receipt.list':{const all=receipts.list();result={total:all.length,items:all.slice(request.offset||0,(request.offset||0)+(request.limit||50))};break;}
        case 'receipt.synced':result=receipts.synced(request.purchase_id,request.manifest_sha256,request.receipt);break;
        case 'binding.start':{
          if(runtime.closing||runtime.active||commands.list().some(r=>r.state==='QUEUED')||setup.busy||template.busy)fail('COMPONENT_BUSY','先完成或停止当前取数、模板或连接任务');
          result=bindingCheck.start(request.purchase_id,request.binding_request);break;
        }
        case 'binding.status':result=bindingCheck.status(request.purchase_id);break;
        case 'binding.pause':result=bindingCheck.pause(request.purchase_id);break;
        case 'binding.confirm':{
          if(runtime.closing||runtime.active||setup.busy||template.busy)fail('COMPONENT_BUSY','组件当前有未完成作业或正在退出，不能确认关联');
          result=purchasePublic(await bindingService.confirm(request.purchase_id,request.proposal_id,request.sequence,request.manifest_sha256));break;
        }
        case 'configuration':{
          if(!await exists(path.join(root,'config/portable.config.json'))){result={configured:false};break;}
          const config=await loadConfig(root);
          const {binding}=await readConfiguration(root);
          result={configured:true,enabled:config.enabled,account_binding:{account_id:config.tenantBinding.accountId,merchant_code:config.tenantBinding.merchantCode},
            display_name:config.tenantBinding.displayName,store_mapping_version:mappingVersion(config),targets:config.targets,
            browser_binding:binding?{edge_executable:binding.edgeExecutable,edge_version:binding.edgeVersion,
              actual_profile_path:binding.actualProfilePath,profile_directory:binding.profileDirectory,output_root:binding.outputRoot}:null};break;
        }
        case 'session.release':result=await releaseSession({runtime,commands,setup,template,purchase,receiptSync,bindingCheck});break;
        case 'session.status':result={host_pid:process.pid,...browsers.status()};break;
        case 'session.health':{const client=setup.client||browsers.browser?.client;result={state:clientConnectionState(client),login_state:client?.loginState||'UNKNOWN',login_error:client?.loginError||null};break;}
        case 'ping':result={closing:runtime.closing,active_command_id:runtime.active?.id||null,active_purchase_id:purchase.busy?purchase.job.id:null,active_receipt_id:receiptSync.busy?receiptSync.job.id:null,active_binding_id:bindingCheck.busy?bindingCheck.job.id:null};break;
        case 'list':{const records=commands.list(),offset=request.offset||0,limit=request.limit||50;result={total:records.length,items:records.slice(offset,offset+limit).map(publicRecord)};break;}
        case 'reserve':{
          if(setup.busy||template.busy)fail('COMPONENT_BUSY','先完成或停止连接核验/采购模板获取，再开始取数');
          if(runtime.closing)fail('SHUTTING_DOWN','组件正在安全退出');
          const config=await loadConfig(root);await assertHandoff(root,config.tenantBinding);
          const accepted=await commands.reserve(request.command,config);
          result={created:accepted.created,record:publicRecord(accepted.record)};kick=true;break;
        }
        case 'status':result=publicRecord(commands.get(request.command_id));if(!result)fail('COMMAND_NOT_FOUND','未找到自动化作业');break;
        case 'collection.preview':result=maintenance.preview(request.command_id,request.operation);break;
        case 'collection.apply':result=maintenance.apply(request.command_id,request.operation,request.token);break;
        case 'events':result={events:commands.events(request.command_id,request.after||0,request.limit||50)};break;
        case 'pause':result=publicRecord(runtime.pause(request.command_id));break;
        case 'resume':if(setup.busy||template.busy)fail('COMPONENT_BUSY','先完成或停止连接核验/采购模板获取');result=publicRecord(runtime.resume(request.command_id));kick=true;break;
        case 'retry':if(setup.busy||template.busy||purchase.busy||receiptSync.busy||bindingCheck.busy)fail('COMPONENT_BUSY','先完成或停止当前平台动作');result=publicRecord(runtime.retry(request.command_id,request.indices,request.sequence));kick=true;break;
        case 'shutdown':{
          runtime.closing=true;
          if(purchase.busy)purchase.pause(purchase.job.id);
          if(receiptSync.busy)receiptSync.pause(receiptSync.job.id);
          if(bindingCheck.busy)bindingCheck.pause(bindingCheck.job.id);
          for(const record of commands.list())if(['QUEUED','RUNNING'].includes(record.state))runtime.pause(record.id);
          result={closing:true,message:'停止新动作；等待当前操作安全结束，已受理任务保留'};stop=true;break;
        }
      }
      await send({type:'response',request_id:request.request_id,ok:true,result});
      if(kick)runtime.kick();
      if(stop)break;
    }catch(error){
      await send({type:'response',request_id:request?.request_id||correlationId(raw),ok:false,error:errorInfo(error)});
    }
  }
}
async function fatal(error){
  process.exitCode=1;
  if(!outputFailed)try{await send({type:'fatal',error:errorInfo(error)});}catch{outputFailed=true;}
  if(runtime)await runtime.shutdown();
  await purchase?.close();
  await receiptSync?.close();
  await bindingCheck?.close();
  process.stdin.destroy();
}
process.stdout.on('error',error=>{outputFailed=true;void fatal(error);});
process.on('SIGINT',()=>{void runtime?.shutdown();process.stdin.destroy();});
process.on('SIGTERM',()=>{void runtime?.shutdown();process.stdin.destroy();});
try{await main();}catch(error){await fatal(error);}
finally{
  try{await runtime?.shutdown();}catch(error){process.exitCode=1;process.stderr.write(errorInfo(error).code+'\n');}
  await setup?.close();await template?.close();await purchase?.close();await receiptSync?.close();await bindingCheck?.close();await browsers.close();state?.close();lock?.release();
}
