import path from 'node:path';
import {ExclusiveLock,StateStore} from './state.mjs';
import {MachineCommands,mappingVersion} from './machine-commands.mjs';
import {MachineRuntime,errorInfo} from './machine-runtime.mjs';
import {readFrames,writeFrame,validateRequest,publicRecord} from './machine-protocol.mjs';
import {Coordinator} from './coordinator.mjs';
import {BoundBrowserPool} from './bound-browser.mjs';
import {freezeRunAsset} from './machine-assets.mjs';
import {loadConfig,validateConfig} from './config.mjs';
import {readJson,fail,sameIdentity,exists} from './common.mjs';
import {verifyInstallation} from './integrity.mjs';
import {assertHandoff} from './preflight.mjs';
import {MachineSetup,recoverConfiguration} from './machine-setup.mjs';

let lock,state,runtime,setup,outputFailed=false;
const browsers=new BoundBrowserPool();
const send=value=>writeFrame(process.stdout,{protocol_version:1,...value});
const rootArg=process.argv.slice(2);
// Explicit state root is mandatory: this entry never falls back to a daily user's state.
async function main(){
  if(rootArg.length!==2||rootArg[0]!=='--state-root'||!path.isAbsolute(rootArg[1])||/^\\\\/.test(rootArg[1]))fail('ARGUMENT','机器入口必须指定本机绝对路径 --state-root');
  await verifyInstallation();const root=path.resolve(rootArg[1]);
  lock=await ExclusiveLock.acquire(path.join(root,'coordinator.lock.sqlite'));
  state=await StateStore.open(root);const commands=new MachineCommands(state);
  const configurationRecovered=await recoverConfiguration(root);setup=new MachineSetup({root});
  const recovered=commands.recoverInterrupted();
  runtime=new MachineRuntime({commands,onFatal:error=>{void fatal(error);},
    needsReview:item=>state.getItems(item.run_id).some(i=>['INTENT_RECORDED','SUBMITTING','SUBMISSION_UNKNOWN','WRONG_SCOPE_ACCEPTED'].includes(state.getAttempt(i.attemptId)?.stage)),
    executeItem:async({record,item,signal,onProgress})=>{
      const current=await loadConfig(root),config=record.config_snapshot;
      if(!current.enabled||!sameIdentity(current.tenantBinding,config.tenantBinding))fail('WRONG_IDENTITY','当前配置身份变化，不能执行原命令');
      await assertHandoff(root,config.tenantBinding);
      if(!await exists(path.join(root,'config/machine.binding.json')))fail('SETUP_REQUIRED','请先完成本机浏览器绑定');
      const binding=await validateConfig('machine.binding',await readJson(path.join(root,'config/machine.binding.json')));
      const browser=await browsers.acquire({state,config,binding,signal,onProgress});
      await new Coordinator({state,config,binding,adapter:browser.adapter,signal,onProgress}).run({runId:item.run_id});
      return await freezeRunAsset({state,binding,commandId:record.id,item});
    }
  });
  await send({type:'ready',operations:['replenishment.export'],recovered:recovered.map(r=>({command_id:r.id,state:r.state,sequence:r.sequence})),configuration_recovered:configurationRecovered,configured:await exists(path.join(root,'config/portable.config.json'))});
  for await(const raw of readFrames(process.stdin)){
    let request,result,stop=false,kick=false;
    try{
      request=validateRequest(raw);
      const configurationActions=['discover','setup.start','setup.commit'];
      if(configurationActions.includes(request.action)&&(runtime.active||commands.list().some(r=>r.state==='QUEUED')))fail('COMPONENT_BUSY','先暂停或完成当前取数任务，再修改连接与目录');
      switch(request.action){
        case 'discover':result=await setup.discover();break;
        case 'setup.start':await browsers.close();result=setup.begin(request);break;
        case 'setup.status':result=setup.status(request.setup_id);break;
        case 'setup.cancel':result=await setup.cancel(request.setup_id);break;
        case 'setup.commit':result=await setup.commit(request.setup_id);break;
        case 'configuration':{
          if(!await exists(path.join(root,'config/portable.config.json'))){result={configured:false};break;}
          const config=await loadConfig(root);
          result={configured:true,enabled:config.enabled,account_binding:{account_id:config.tenantBinding.accountId,merchant_code:config.tenantBinding.merchantCode},
            display_name:config.tenantBinding.displayName,store_mapping_version:mappingVersion(config),targets:config.targets};break;
        }
        case 'ping':result={closing:runtime.closing,active_command_id:runtime.active?.id||null};break;
        case 'list':{const records=commands.list(),offset=request.offset||0,limit=request.limit||50;result={total:records.length,items:records.slice(offset,offset+limit).map(publicRecord)};break;}
        case 'reserve':{
          if(setup.busy)fail('SETUP_BUSY','先保存或取消当前连接核验，再开始取数');
          if(runtime.closing)fail('SHUTTING_DOWN','组件正在安全退出');
          const config=await loadConfig(root);await assertHandoff(root,config.tenantBinding);
          const accepted=await commands.reserve(request.command,config);
          result={created:accepted.created,record:publicRecord(accepted.record)};kick=true;break;
        }
        case 'status':result=publicRecord(commands.get(request.command_id));if(!result)fail('COMMAND_NOT_FOUND','未找到自动化作业');break;
        case 'events':result={events:commands.events(request.command_id,request.after||0,request.limit||50)};break;
        case 'pause':result=publicRecord(runtime.pause(request.command_id));break;
        case 'resume':if(setup.busy)fail('SETUP_BUSY','先保存或取消当前连接核验');result=publicRecord(runtime.resume(request.command_id));kick=true;break;
        case 'shutdown':{
          runtime.closing=true;
          for(const record of commands.list())if(['QUEUED','RUNNING'].includes(record.state))runtime.pause(record.id);
          result={closing:true,message:'停止新动作；等待当前操作安全结束，已受理任务保留'};stop=true;break;
        }
      }
      await send({type:'response',request_id:request.request_id,ok:true,result});
      if(kick)runtime.kick();
      if(stop)break;
    }catch(error){
      await send({type:'response',request_id:request?.request_id||null,ok:false,error:errorInfo(error)});
    }
  }
}
async function fatal(error){
  process.exitCode=1;
  if(!outputFailed)try{await send({type:'fatal',error:errorInfo(error)});}catch{outputFailed=true;}
  if(runtime)await runtime.shutdown();
  process.stdin.destroy();
}
process.stdout.on('error',error=>{outputFailed=true;void fatal(error);});
process.on('SIGINT',()=>{void runtime?.shutdown();process.stdin.destroy();});
process.on('SIGTERM',()=>{void runtime?.shutdown();process.stdin.destroy();});
try{await main();}catch(error){await fatal(error);}
finally{
  try{await runtime?.shutdown();}catch(error){process.exitCode=1;process.stderr.write(errorInfo(error).code+'\n');}
  await setup?.close();await browsers.close();state?.close();lock?.release();
}
