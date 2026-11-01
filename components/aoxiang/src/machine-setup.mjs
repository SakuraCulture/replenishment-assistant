import fs from 'node:fs/promises';
import path from 'node:path';
import {discover} from './environment.mjs';
import {BrowserClient} from './worker-client.mjs';
import {freshConfig,validateConfig} from './config.mjs';
import {canonical,sha256,now,uid,exists,readJson,writeJson,fail,sameIdentity} from './common.mjs';
import {checkOutput} from './preflight.mjs';
import {mappingVersion} from './machine-commands.mjs';
import {errorInfo} from './machine-runtime.mjs';

const configFiles={config:'config/portable.config.json',binding:'config/machine.binding.json'};
const journalName='recovery/configuration.pending.json';
export async function readConfiguration(root){
  const pair={};for(const [key,file] of Object.entries(configFiles))pair[key]=await exists(path.join(root,file))?await readJson(path.join(root,file)):null;
  return pair;
}
export const configurationHash=pair=>sha256(canonical(pair));
export async function recoverConfiguration(root){
  const file=path.join(root,journalName);if(!await exists(file))return false;
  const plan=await readJson(file);
  if(plan.schema!==1||!plan.previous||!plan.desired)fail('CONFIG_RECOVERY_REQUIRED','配置恢复记录不完整，不能继续执行');
  await validateConfig('portable.config',plan.desired.config);await validateConfig('machine.binding',plan.desired.binding);
  const current=await readConfiguration(root);
  for(const key of Object.keys(configFiles))if(![canonical(plan.previous[key]),canonical(plan.desired[key])].includes(canonical(current[key])))fail('CONFIG_RECOVERY_CONFLICT','待恢复配置已被其他操作改变，请保留备份并核对');
  for(const [key,name] of Object.entries(configFiles))await writeJson(path.join(root,name),plan.desired[key]);
  await fs.unlink(file);return true;
}
export async function saveConfiguration(root,expectedHash,desired){
  if(await exists(path.join(root,journalName)))fail('CONFIG_RECOVERY_REQUIRED','存在未完成的配置保存，请先恢复');
  const previous=await readConfiguration(root);
  if(configurationHash(previous)!==expectedHash)fail('STALE_CONFIGURATION','配置已被更新，请重新核验后保存');
  await validateConfig('portable.config',desired.config);await validateConfig('machine.binding',desired.binding);
  const backupId=uid();await writeJson(path.join(root,'recovery/config-before-'+backupId+'.json'),{schema:1,saved_at:now(),...previous});
  await writeJson(path.join(root,journalName),{schema:1,previous,desired,backup_id:backupId});
  await recoverConfiguration(root);
}

export class MachineSetup{
  constructor({root,Client=BrowserClient,discoverEnvironment=discover,checkOutputRoot=checkOutput}){
    Object.assign(this,{root,Client,discoverEnvironment,checkOutputRoot});this.job=null;this.promise=null;this.client=null;
  }
  get busy(){return !!this.promise||['READY','COMMITTING'].includes(this.job?.state);}
  async discover(){
    if(this.busy)fail('SETUP_BUSY','正在核验连接或等待保存，请先完成当前配置');
    const {installations,profiles}=await this.discoverEnvironment();
    return {installations,profiles};
  }
  begin(params){
    if(this.busy)fail('SETUP_BUSY','已有待完成的连接核验');
    this.control=new AbortController();this.preview=null;
    this.job={id:uid(),state:'RUNNING',stage:'发现浏览器',created_at:now(),updated_at:now(),current:0,total:params.targets.length,
      targets:params.targets.map((item,index)=>({...item,index,state:'PENDING',error:null})),identity:null,error:null};
    this.promise=this.run(structuredClone(params)).catch(error=>{
      this.job={...this.job,state:this.control.signal.aborted?'CANCELLED':'FAILED',error:errorInfo(error),updated_at:now()};
    }).finally(async()=>{
      try{await this.closeClient();}
      catch(error){this.job={...this.job,state:'FAILED',error:errorInfo(error),updated_at:now()};}
      finally{this.promise=null;}
    });
    return this.status(this.job.id);
  }
  status(id){if(this.job?.id!==id)fail('SETUP_NOT_FOUND','当前连接核验已失效，请重新核验');return structuredClone(this.job);}
  check(){if(this.control.signal.aborted)fail('PAUSED','配置核验已停止，未保存新配置');}
  async closeClient(){await this.client?.stop();this.client=null;}
  async cancel(id){
    this.status(id);this.control.abort();this.job.cancel_requested=true;this.job.updated_at=now();
    if(this.job.state==='READY'){this.preview=null;this.job.state='CANCELLED';}
    return this.status(id);
  }
  async close(){if(this.job)this.control.abort();await this.promise;await this.closeClient();this.preview=null;if(this.job?.state==='READY')this.job.state='CANCELLED';}
  async run(params){
    const old=await readConfiguration(this.root),env=await this.discoverEnvironment();this.check();
    const profile=env.profiles.find(p=>p.actualProfilePath.toLowerCase()===params.candidate.actual_profile_path.toLowerCase());
    const edge=env.installations.find(e=>e.executable.toLowerCase()===params.candidate.edge_executable.toLowerCase());
    if(!profile||!edge)fail('MACHINE_REBIND_REQUIRED','所选浏览器或已有用户配置未在本机发现，不能执行任意路径');
    const output=await this.checkOutputRoot(params.output_root);this.check();
    this.client=new this.Client({onPhase:stage=>{this.job.stage=stage;this.job.updated_at=now();}});
    this.job.stage='核验登录与商家';
    const result=await this.client.connect({candidate:{...profile,edgeExecutable:edge.executable},expectedIdentity:old.config?.tenantBinding,
      stateRoot:this.root,timeout:old.config?.resourceLimits.connectTimeoutMs||60000,pageTimeoutMs:old.config?.resourceLimits.pageTimeoutMs||30000,
      mode:'background_tab',source:'D13'});this.check();
    if(old.config&&!sameIdentity(old.config.tenantBinding,result.identity))fail('WRONG_IDENTITY','当前商家与已保存账号不同；原配置保持不变');
    this.job.identity={account_id:result.identity.accountId,merchant_code:result.identity.merchantCode,display_name:result.identity.displayName};
    const verified=[];
    for(const item of this.job.targets){
      this.check();item.state='RUNNING';this.job.stage='核对门店 '+(item.index+1)+'/'+this.job.total+' · '+item.name;
      try{
        const query=await this.client.querySingle(item.code,item.name);this.check();
        if(!sameIdentity(query.identity,result.identity))fail('WRONG_IDENTITY','核验期间商家身份变化');
        if(query.warehouseCode!==item.code)fail('TARGET_CODE_MISMATCH','查询中的实际接收编码与目录不一致');
        item.state='VERIFIED';item.rows=query.total;
        verified.push({buyerWarehouseCode:item.code,displayName:item.name,operatingStores:[],mappingSource:'门店目录与补货建议原生单店查询精确编码核对',mappingVerifiedAt:query.observedAt});
      }catch(error){
        item.state='FAILED';item.error=errorInfo(error);
        if(this.control.signal.aborted||['WRONG_IDENTITY','WAIT_LOGIN','BROWSER_DISCONNECTED','WORKER_RESPONSE_UNKNOWN'].includes(error.code))throw error;
      }
      this.job.current=item.index+1;this.job.updated_at=now();
    }
    this.check();if(!verified.length)fail('NO_VERIFIED_TARGET','没有门店通过编码核验，请检查目录和当前账号权限');
    const config=old.config?structuredClone(old.config):freshConfig(result.identity,verified);
    const byCode=new Map(verified.map(t=>[t.buyerWarehouseCode,t]));
    config.targets=config.targets.map(t=>byCode.get(t.buyerWarehouseCode)||t);
    for(const target of verified)if(!config.targets.some(t=>t.buyerWarehouseCode===target.buyerWarehouseCode))config.targets.push(target);
    config.batches[0].targetWarehouseCodes=[...new Set([...config.batches[0].targetWarehouseCodes,...verified.map(t=>t.buyerWarehouseCode)])];
    config.exportPolicy.semanticVersion='D13-all-info-2';
    const binding={schemaVersion:'2.0',exampleOnly:false,enabled:true,machineBindingId:old.binding?.machineBindingId||uid(),edgeChannel:'stable',
      edgeExecutable:edge.executable,edgeVersion:result.facts.edgeVersion,userDataRoot:profile.userDataRoot,profileDirectory:profile.profileDirectory,
      actualProfilePath:result.facts.actualProfilePath,outputRoot:output.root,identityVerifiedAt:now(),compatibilityResult:'PASSED'};
    await validateConfig('portable.config',config);await validateConfig('machine.binding',binding);
    this.preview={expectedHash:configurationHash(old),desired:{config,binding}};
    this.job={...this.job,state:'READY',stage:'核验完成，等待保存',verified:verified.length,failed:this.job.targets.length-verified.length,
      store_mapping_version:mappingVersion(config),output_root:binding.outputRoot,updated_at:now()};
  }
  async commit(id){
    this.status(id);if(this.job.state!=='READY'||!this.preview)fail('SETUP_NOT_READY','请先完成连接和门店核验');
    await this.promise;this.job.state='COMMITTING';
    try{await saveConfiguration(this.root,this.preview.expectedHash,this.preview.desired);this.job.state='SAVED';this.job.stage='连接与已核验门店已保存';this.preview=null;}
    catch(error){this.job.state='FAILED';this.job.error=errorInfo(error);throw error;}
    return this.status(id);
  }
}
