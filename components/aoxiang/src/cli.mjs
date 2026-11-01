import fs from 'node:fs/promises';
import path from 'node:path';
import readline from 'node:readline/promises';
import {discover,defaultStateRoot} from './environment.mjs';
import {APP_VERSION,redact,uid,now,readJson,writeJson,exists,fail,sameIdentity,businessDate} from './common.mjs';
import {loadConfig,saveConfig,freshConfig,validateConfig} from './config.mjs';
import {ExclusiveLock,StateStore} from './state.mjs';
import {BrowserClient} from './worker-client.mjs';
import {Coordinator} from './coordinator.mjs';
import {PurchaseCoordinator} from './purchase-coordinator.mjs';
import {PurchaseTemplateService} from './purchase-template.mjs';
import {purchaseDays,previousPurchaseDay} from './purchase-contract.mjs';
import {EventLog} from './event-log.mjs';
import {checkOutput,assertHandoff} from './preflight.mjs';
import {exportMigration,previewMigration,applyMigration,importMigrationAssets} from './migration.mjs';
import {verifyInstallation} from './integrity.mjs';

function parseArgs(tokens){
  const options={},words=[];
  for(let i=0;i<tokens.length;i++){
    const s=tokens[i];if(!s.startsWith('--')){words.push(s);continue;}
    const key=s.slice(2),booleans=['json','help','include-results','snapshot','accept-config-conflict','source-stopped','choose-profile','foreground','rerun'];
    if(![...booleans,'profile-directory','user-data-root','output-root','warehouse-name','warehouse-code','batch','run','imported','file','from','to','date-basis','order-id'].includes(key))fail('ARGUMENT','未知参数：'+s);
    const v=booleans.includes(key)?true:tokens[++i];if(v===undefined||String(v).startsWith('--'))fail('ARGUMENT','参数缺少值：'+s);
    if(key==='warehouse-name')options[key]=[...(options[key]||[]),v];else options[key]=v;
  }return {words,options};
}
let parsed;try{parsed=parseArgs(process.argv.slice(2));if(parsed.options.batch&&parsed.options['warehouse-code'])fail('ARGUMENT','--batch与--warehouse-code不能同时指定');}catch(e){console.error(e.code+'：'+e.message);process.exit(2);}
const {words,options}=parsed,command=options.help?'help':words[0]||'menu';
const flags={help:[],menu:['json','foreground'],doctor:['json','user-data-root'],setup:['json','profile-directory','user-data-root','output-root','warehouse-name','choose-profile','foreground'],targets:['json','warehouse-name','foreground'],batches:['json'],run:['json','batch','warehouse-code','foreground'],rerun:['json','batch','warehouse-code','foreground'],resume:['json','run','imported','foreground'],results:['json']};
const migrationFlags={export:['json','file','include-results','snapshot'],preview:['json','file'],import:['json','file','accept-config-conflict','profile-directory','user-data-root','output-root','choose-profile'],assets:['json'],'confirm-handoff':['json','source-stopped']};
const purchaseFlags={export:['json','foreground','warehouse-code','warehouse-name','from','to','date-basis','order-id','rerun'],resume:['json','foreground','run'],list:['json'],template:['json','foreground']};
try{
  if(!options.help){const allowed=command==='migration'?migrationFlags[words[1]]:command==='purchase'?purchaseFlags[words[1]||'export']:flags[command];if(!allowed)fail('ARGUMENT','命令无效；使用help查看支持的命令');for(const key of Object.keys(options))if(!allowed.includes(key))fail('ARGUMENT','该命令不接受参数：--'+key);if(words.length>(['migration','purchase'].includes(command)?2:1))fail('ARGUMENT','命令含多余的位置参数');}
  if(options.run&&options.imported)fail('ARGUMENT','--run与--imported不能同时指定');
}catch(e){console.error(e.code+'：'+e.message);process.exit(2);}
const stateRoot=defaultStateRoot(),json=!!options.json,interactive=!!process.stdin.isTTY;
let state,lock,client,connected=null,config=null,binding=null,rl=null,log=null,active=null,shuttingDown=false;
const text=value=>console.log(typeof value==='string'?value:JSON.stringify(redact(value),null,2));
const stages={RUNNING:'开始作业',INTENT_RECORDED:'已记录提交意图',ACCEPTED:'平台已受理',WAIT_TASK:'等待原任务完成',REUSED:'复用当日文件',SAVED:'文件验收并保存',COMPLETE:'作业完成',PURCHASE_RUNNING:'采购单作业',PURCHASE_EMPTY:'当日无可见采购单',PURCHASE_COMPLETE:'主单和商品明细已验收'};
async function emit(event){await log?.write(event);if(json)text(event);else if(event.stage){const details=[event.warehouseCode,event.day,event.sourceId==='D16'?'采购主单':event.sourceId==='D17'?'商品明细':null,event.taskId&&'任务'+event.taskId,event.rows!==undefined&&event.rows+'行',event.file].filter(Boolean).join(' · ');text((stages[event.stage]||event.stage)+(details?'：'+details:''));}}
async function ask(question,defaultValue=''){
  if(!interactive)fail('INPUT_REQUIRED',question+'；请在交互向导中操作，或提供对应命令参数');
  rl??=readline.createInterface({input:process.stdin,output:process.stdout});const answer=(await rl.question(question+(defaultValue?' ['+defaultValue+']':'')+'：')).trim();return answer||defaultValue;
}
async function choose(question,items,label){
  if(items.length===1)return items[0];
  if(!items.length)fail('NO_CANDIDATE',question+'：没有可选项');
  items.forEach((x,i)=>text(`${i+1}. ${label(x)}`));const n=Number(await ask(question));if(!Number.isInteger(n)||n<1||n>items.length)fail('INVALID_CHOICE','所选序号无效');return items[n-1];
}
async function readSettings(){
  const c=path.join(stateRoot,'config/portable.config.json'),b=path.join(stateRoot,'config/machine.binding.json');
  config=await exists(c)?await loadConfig(stateRoot):null;
  binding=await exists(b)?await validateConfig('machine.binding',await readJson(b)):null;
}
async function stopClient(){await client?.stop();client=null;connected=null;}
const adapter={onReceipt:null};
for(const method of ['verifyIdentity','querySingle','warehouseOptions','prepareExport','submitOnce','findTask'])adapter[method]=async(...args)=>{await ensureBrowser();return client[method](...args);};
for(const method of ['queryPurchase','preparePurchase','submitPurchase','purchaseTask','downloadPurchase','downloadPurchaseTemplate'])adapter[method]=async(...args)=>{await ensureBrowser();return client[method](...args);};
async function connectCandidate(candidate,expectedIdentity,timeout=60000){
  const mode=options.foreground?'visible_tab':config?.browserInteraction?.mode||'background_tab';
  if(client&&(connected?.actualProfilePath.toLowerCase()!==candidate.actualProfilePath.toLowerCase()||connected?.mode!==mode))await stopClient();
  if(!client||client.closed){client=new BrowserClient({onPhase:phase=>{if(!json)text(({CONNECTING:'正在连接Edge；若浏览器提示远程调试连接，请允许当前这次连接。',PROFILE_VERIFIED:'已核对实际Edge用户配置。'})[phase]||phase);void log?.write({stage:phase});},onAccepted:receipt=>{if(!state.recordPurchaseReceipt(receipt))state.recordReceipt(receipt);adapter.onReceipt?.(receipt);}});connected={actualProfilePath:candidate.actualProfilePath,mode};}
  let result;
  for(;;){
    try{result=await client.connect({candidate,expectedIdentity,stateRoot,timeout,mode,source:command==='purchase'?'purchase':'D13',pageTimeoutMs:config?.resourceLimits.pageTimeoutMs||30000});break;}
    catch(e){if(!['WAIT_LOGIN','IDENTITY_RESPONSE_TIMEOUT'].includes(e.code)||!interactive)throw e;text(e.message);const answer=await ask('请确认所选Edge已正常登录、页面已加载后按回车继续，输入q结束连接');if(answer.toLowerCase()==='q')throw e;}
  }
  connected={...candidate,identity:result.identity,facts:result.facts,mode};if(!json)text(mode==='background_tab'?'后台标签已就绪，可切换其他页面继续工作。':'当前为显示页面的诊断模式。');return result;
}
async function ensureBrowser(){
  if(client&&!client.closed&&connected?.identity){
    if(config&&!sameIdentity(connected.identity,config.tenantBinding))fail('WRONG_IDENTITY','已连接商家与业务配置不同');return;
  }
  if(!config||!binding?.enabled)fail('SETUP_REQUIRED','请先运行连接与配置向导');
  const env=await discover({extraDataRoot:binding.userDataRoot});
  const profile=env.profiles.find(p=>p.actualProfilePath.toLowerCase()===binding.actualProfilePath.toLowerCase());
  const edge=env.installations.find(e=>e.executable.toLowerCase()===binding.edgeExecutable.toLowerCase());
  if(!profile||!edge)fail('MACHINE_REBIND_REQUIRED','本机Edge或已有用户配置路径发生变化，请重新运行setup自动发现');
  const r=await connectCandidate({...profile,edgeExecutable:edge.executable},config.tenantBinding,config.resourceLimits.connectTimeoutMs);
  binding={...binding,edgeVersion:r.facts.edgeVersion,actualProfilePath:r.facts.actualProfilePath,identityVerifiedAt:now()};await writeJson(path.join(stateRoot,'config/machine.binding.json'),binding);
}
async function targetFromName(name){
  const q=await client.querySingle(null,name);text(json?{type:'TARGET_VERIFIED',...q}:{仓店:q.name,接收仓编码:q.warehouseCode,当前商品行数:q.total});
  return {buyerWarehouseCode:q.warehouseCode,displayName:q.name,operatingStores:[],mappingSource:'补货建议原生选择器与本次单仓查询请求',mappingVerifiedAt:q.observedAt};
}
async function pickTarget(){
  const term=await ask('输入仓店名称关键词');if(!term)fail('TARGET_REQUIRED','名称关键词不能为空');
  const options=await client.warehouseOptions(term),leaves=options.filter(t=>!t.expandable);
  const found=await choose('选择接收仓店点',leaves,t=>t.name);return targetFromName(found.name);
}
async function setup(){
  const env=await discover({extraDataRoot:options['user-data-root']||binding?.userDataRoot});
  if(env.arch!=='x64')fail('ARCH_UNVERIFIED','当前发行仅验收Windows x64');
  let edge=env.installations.find(e=>e.executable===binding?.edgeExecutable)||await choose('选择Edge安装',env.installations,e=>e.executable+' · '+e.version);
  let profiles=env.profiles;
  if(options['profile-directory'])profiles=profiles.filter(p=>p.profileDirectory===options['profile-directory']);
  if(options['user-data-root'])profiles=profiles.filter(p=>path.resolve(p.userDataRoot).toLowerCase()===path.resolve(options['user-data-root']).toLowerCase());
  let profile=!options['choose-profile']&&profiles.find(p=>p.actualProfilePath===binding?.actualProfilePath);
  profile??=await choose('选择已有Edge用户配置',profiles,p=>p.displayName+' · '+p.profileDirectory+' · '+p.actualProfilePath);
  text('使用已有配置：'+profile.displayName+'（'+profile.profileDirectory+'）');
  const result=await connectCandidate({...profile,edgeExecutable:edge.executable},config?.tenantBinding,config?.resourceLimits.connectTimeoutMs||60000);
  text({商家:result.identity.displayName,商家编码:result.identity.merchantCode,账号ID:result.identity.accountId,实际配置路径:result.facts.actualProfilePath});
  const proposed=options['output-root']||binding?.outputRoot||path.join(env.documents,'翱象补货建议');
  const output=await checkOutput(proposed);
  binding={schemaVersion:'2.0',exampleOnly:false,enabled:true,machineBindingId:binding?.machineBindingId||uid(),edgeChannel:'stable',edgeExecutable:edge.executable,edgeVersion:result.facts.edgeVersion,userDataRoot:profile.userDataRoot,profileDirectory:profile.profileDirectory,actualProfilePath:result.facts.actualProfilePath,outputRoot:output.root,identityVerifiedAt:now(),compatibilityResult:'PASSED'};
  await validateConfig('machine.binding',binding);await writeJson(path.join(stateRoot,'config/machine.binding.json'),binding);
  let targets=config?.targets||[];
  if(options['warehouse-name']?.length){targets=[...targets];for(const name of options['warehouse-name']){const target=await targetFromName(name);targets=[...targets.filter(t=>t.buyerWarehouseCode!==target.buyerWarehouseCode),target];}}
  if(!targets.length)targets=[await pickTarget()];
  config=config?{...config,targets}:freshConfig(result.identity,targets);
  // Source V2 is an explicit compatibility upgrade. Old attempts retain their
  // frozen version and block their warehouse until resolved.
  config.exportPolicy.semanticVersion='D13-all-info-2';
  config.batches[0].targetWarehouseCodes=[...new Set([...config.batches[0].targetWarehouseCodes,...targets.map(t=>t.buyerWarehouseCode)])];
  await saveConfig(stateRoot,config);
  const mp=path.join(stateRoot,'recovery/migration-status.json');
  if(await exists(mp)){const status=await readJson(mp);if(sameIdentity(status.tenantBinding,config.tenantBinding))await writeJson(mp,{...status,needsMachineSetup:false});}
  await log.write({stage:'SETUP_SAVED',merchantCode:config.tenantBinding.merchantCode,profileDirectory:binding.profileDirectory,outputRoot:binding.outputRoot});
  text('配置已保存。只完成查询和绑定，尚未创建导出任务。');
}
async function manageTargets(){
  if(!config||!binding?.enabled){await setup();return;}else await ensureBrowser();
  if(options['warehouse-name']?.length){for(const name of options['warehouse-name']){const target=await targetFromName(name);config.targets=[...config.targets.filter(t=>t.buyerWarehouseCode!==target.buyerWarehouseCode),target];}}
  else{const target=await pickTarget();config.targets=[...config.targets.filter(t=>t.buyerWarehouseCode!==target.buyerWarehouseCode),target];}
  config.batches[0].targetWarehouseCodes=config.targets.map(t=>t.buyerWarehouseCode);await saveConfig(stateRoot,config);text('仓店目录和默认批次已更新。相同仓码只保留一个导出项。');
}
async function manageBatches(){
  if(!config)fail('SETUP_REQUIRED','请先完成配置');config.targets.forEach((t,i)=>text(`${i+1}. ${t.displayName} · ${t.buyerWarehouseCode}`));
  const name=await ask('批次名称','默认补货批次'),id=await ask('批次ID（字母数字）','default');if(!/^[A-Za-z0-9_-]+$/.test(id))fail('INVALID_BATCH_ID','批次ID格式无效');
  const indexes=(await ask('选择仓店序号，用逗号分隔')).split(/[,，\s]+/).map(Number);
  if(!indexes.length||indexes.some(i=>!Number.isInteger(i)||i<1||i>config.targets.length))fail('INVALID_CHOICE','所选序号无效');
  const batch={id,name,targetWarehouseCodes:[...new Set(indexes.map(i=>config.targets[i-1].buyerWarehouseCode))]};config.batches=[...config.batches.filter(b=>b.id!==id),batch];await saveConfig(stateRoot,config);text('批次已保存。');
}
async function run(mode='normal',resume=false){
  if(!config||!binding?.enabled)fail('SETUP_REQUIRED','请先运行setup配置本机');
  await assertHandoff(stateRoot,config.tenantBinding);
  let runId=options.run;
  if(resume&&options.imported){const imported=state.adoptImported(options.imported,config);runId=imported.id;}
  if(resume&&!runId){
    const candidates=state.rows('runs').filter(r=>sameIdentity(r.identity,config.tenantBinding)&&!['SUCCEEDED','CANCELLED_BEFORE_SUBMIT'].includes(r.status));
    const selected=await choose('选择未完成作业',candidates,r=>r.id+' · '+r.day+' · '+r.status+(r.errorCode?' · '+r.errorCode:''));runId=selected.id;
  }
  let batchId=options.batch||'default';
  if(!resume&&interactive&&!options.batch&&!options['warehouse-code'])batchId=(await choose('选择批次',config.batches,b=>b.name+' · '+b.targetWarehouseCodes.length+'个仓点')).id;
  active=new AbortController();
  try{
    let request=resume?{runId}:{mode,batchId,warehouseCode:options['warehouse-code']},currentRun=runId;
    for(;;){
      try{const engine=new Coordinator({state,config,binding,adapter,onProgress:async e=>{if(e.stage==='RUNNING')currentRun=e.runId;await emit(e);},signal:active.signal});const result=await engine.run(request);text(json?result:'作业结果：'+path.join(stateRoot,'reports',result.id+'.csv'));break;}
      catch(e){
        if(!interactive||active.signal.aborted||!currentRun)throw e;
        await showError(e);
        if(client&&!client.closed)try{await log.write({stage:'UI_DIAGNOSTIC',runId:currentRun,...await client.inspect()});}catch{}
        const answer=await ask('连接保留。按回车继续原作业，输入q退出（受理未知时仍禁止重提）');
        if(answer.toLowerCase()==='q')throw e;request={runId:currentRun};
      }
    }
  }
  finally{active=null;adapter.onReceipt=null;}
}
async function finishAssets(){
  const file=path.join(stateRoot,'recovery/migration-status.json');if(!await exists(file))fail('NO_MIGRATION','没有待完成的迁入记录');
  const status=await readJson(file);if(!config||!binding?.enabled||status.needsMachineSetup||!sameIdentity(status.tenantBinding,config.tenantBinding))fail('MACHINE_REBIND_REQUIRED','请先在本机setup核验迁入商家');
  if(!status.assetsPending){text('该迁移包没有待导入的结果文件。');return;}
  const result=await importMigrationAssets(status.packageFile,{state,outputRoot:binding.outputRoot,expectedIdentity:config.tenantBinding});await writeJson(file,{...status,assetsPending:result.conflicts.length>0,assetResult:result});text({结果文件已导入:result.saved,冲突数:result.conflicts.length});
}
async function purchases(action='export'){
  if(!config||!binding?.enabled)fail('SETUP_REQUIRED','请先运行setup配置本机');
  const rows=state.rows('purchase_runs').filter(r=>sameIdentity(r.identity,config.tenantBinding));
  if(action==='list'){text(json?rows:rows.map(r=>`${r.id} · ${r.target.displayName} · ${r.from} · ${r.dateBasis==='created'?'制单':'更新'} · ${r.status}`).join('\n')||'尚无采购导出作业');return;}
  active=new AbortController();
  try{
    if(action==='template'){
      const result=await new PurchaseTemplateService({adapter,outputRoot:binding.outputRoot,identity:config.tenantBinding,signal:active.signal}).fetchLatest();
      await log?.write({stage:'PURCHASE_TEMPLATE_SAVED',file:result.file,sha256:result.sha256,checkedAt:result.checkedAt,contractVersion:result.contractVersion});
      text(json?result:`最新采购导入模板已获取并核对：${result.file}\n核对时间：${result.checkedAt}；${result.schema.sheets.find(s=>s.state==='visible').headers.length}列${result.uiProductLimit?'；平台页面商品数量上限 '+result.uiProductLimit:''}`);return;
    }
    const engine=new PurchaseCoordinator({state,config,binding,adapter,signal:active.signal,onProgress:emit});
    if(action==='resume'){
      const runId=options.run||(await choose('选择采购作业',rows.filter(r=>!['SUCCEEDED','EMPTY'].includes(r.status)),r=>`${r.target.displayName} · ${r.from} · ${r.status} · ${r.id}`)).id;
      const result=await engine.run({runId});text(json?result:'采购作业已完成：'+result.id);return;
    }
    if(action!=='export')fail('ARGUMENT','采购支持export、resume、list、template');
    let target=options['warehouse-code']?config.targets.find(t=>t.buyerWarehouseCode===options['warehouse-code']):null;
    if(options['warehouse-name']?.length>1)fail('ARGUMENT','一次采购作业只指定一个仓店名称');
    if(options['warehouse-code']&&options['warehouse-name']?.length)target={buyerWarehouseCode:options['warehouse-code'],displayName:options['warehouse-name'][0]};
    if(!target){if(options['warehouse-code']||options['warehouse-name'])fail('TARGET_REQUIRED','未配置的采购仓店必须同时提供实际仓码和精确全名');target=await choose('选择采购仓店',config.targets,t=>t.displayName+' · '+t.buyerWarehouseCode);}
    const from=options.from||await ask('采购开始日期 YYYY-MM-DD',previousPurchaseDay()),to=options.to||await ask('采购结束日期 YYYY-MM-DD',from),dateBasis=options['date-basis']||'created';
    const days=purchaseDays(from,to),completed=[];
    for(const day of days){if(active.signal.aborted)fail('PAUSED','采购作业已暂停');const result=await engine.run({target,from:day,to:day,dateBasis,orderId:options['order-id']||null,rerun:!!options.rerun});completed.push({id:result.id,day,status:result.status,report:result.reportRelativePath});}
    text(json?{purchaseRuns:completed,outputRoot:binding.outputRoot}:`采购导出完成：${completed.length}个日切片；输出目录 ${binding.outputRoot}`);
  }finally{active=null;}
}
async function migration(action){
  if(action==='preview'||action==='import'){
    const file=options.file||await ask('输入迁移ZIP完整路径'),preview=await previewMigration(file,{currentConfig:config});text(preview.summary);
    if(action==='preview')return;
    let accept=!!options['accept-config-conflict'];
    if(preview.configConflict&&!accept){text('迁入配置与本机配置不同；确认后会备份本机配置再采用迁入配置，原作业记录保留。');accept=(await ask('输入“采用迁入配置”继续'))==='采用迁入配置';if(!accept)fail('MIGRATION_CONFIG_CONFLICT','未采用迁入配置');}
    const result=await applyMigration(preview,{state,stateRoot,acceptConfigConflict:accept});text(result);await readSettings();
    if(result.needsMachineSetup){await stopClient();binding=null;await setup();await finishAssets();}
    return;
  }
  if(action==='assets'){await finishAssets();return;}
  if(action==='confirm-handoff'){
    if(!config)fail('SETUP_REQUIRED','尚无业务配置');
    const confirmed=options['source-stopped']||(await ask('确认其他电脑已停止同商家同批次作业后，输入“本机执行”'))==='本机执行';
    if(!confirmed)fail('HANDOFF_CONFIRMATION_REQUIRED','尚未确认执行电脑');
    for(const name of ['migration-status.json','source-handoff.json']){const p=path.join(stateRoot,'recovery',name);if(await exists(p)){const status=await readJson(p);if(sameIdentity(status.tenantBinding,config.tenantBinding))await writeJson(p,{...status,runBlocked:false,handoffConfirmedAt:now()});}}
    await log.write({stage:'HANDOFF_CONFIRMED',merchantCode:config.tenantBinding.merchantCode});text('已记录本机执行约定；未决任务仍逐仓阻断。');return;
  }
  if(action!=='export')fail('MIGRATION_COMMAND','迁移支持 export、preview、import、assets、confirm-handoff');
  if(!config)fail('SETUP_REQUIRED','尚无可迁移业务配置');await stopClient();
  const runner=await ExclusiveLock.acquire(path.join(stateRoot,'runner.lock.sqlite'));
  try{
    const output=options.file||path.join(binding?.outputRoot||stateRoot,'翱象配置迁移_'+businessDate()+'_'+uid().slice(0,8)+'.zip');
    if(!options.snapshot)await writeJson(path.join(stateRoot,'recovery/source-handoff.json'),{tenantBinding:config.tenantBinding,runBlocked:true,stoppedAt:now(),phase:'EXPORTING_HANDOFF_PACKAGE'});
    const result=await exportMigration({state,config,output,sourceStopped:!options.snapshot,includeResults:!!options['include-results'],outputRoot:binding?.outputRoot});
    if(!options.snapshot)await writeJson(path.join(stateRoot,'recovery/source-handoff.json'),{tenantBinding:config.tenantBinding,migrationId:result.migrationId,runBlocked:true,stoppedAt:now()});text(result);
    if(!options.snapshot)text('本机已进入交接状态。若继续在本机执行，请使用迁移菜单确认执行电脑。');
  }finally{runner.release();}
}
function results(){
  const runs=state.rows('runs').filter(r=>!config||sameIdentity(r.identity,config.tenantBinding));
  const assets=state.rows('assets').filter(a=>!config||sameIdentity(a,config.tenantBinding));
  const pending=state.rows('imported_pending').filter(p=>!config||sameIdentity(p,config.tenantBinding));
  const purchaseRuns=state.rows('purchase_runs').filter(r=>!config||sameIdentity(r.identity,config.tenantBinding));
  if(json)text({stateRoot,outputRoot:binding?.outputRoot,runs,assets,importedPending:pending,purchaseRuns,conflicts:state.rows('conflicts')});
  else{for(const r of runs.slice(-20))text(`${r.id} · ${r.day} · ${r.status}${r.errorCode?' · '+r.errorCode:''}`);for(const a of assets)text(`${a.acquisitionDate} · ${a.buyerWarehouseCode} · ${a.rows}行 · ${path.join(binding?.outputRoot||'',a.relativePath)}`);for(const p of pending)text(`迁入待恢复 ${p.recordId} · ${p.buyerWarehouseCode} · ${p.state} · ${p.taskId||'受理未知'}`);text('结果和日志：'+stateRoot);}
  if(!json)for(const r of purchaseRuns.slice(-20))text(`采购 ${r.id} · ${r.target.displayName} · ${r.from} · ${r.status}`);
}
async function menu(){
  for(;!shuttingDown;){
    text('\n翱象补货建议助手 '+APP_VERSION+' · 受控测试版');
    text('1 连接与配置  2 添加仓店  3 编辑批次  4 开始运行\n5 继续未完成  6 重新导出今日文件  7 迁移  8 查看结果\n9 采购单（导出/获取导入模板）  0 退出');
    const action=await ask('选择操作');if(action==='0')return;
    try{
      if(action==='1')await setup();else if(action==='2')await manageTargets();else if(action==='3')await manageBatches();else if(action==='4')await run();else if(action==='5')await run('normal',true);else if(action==='6')await run('rerun');
      else if(action==='7'){const a=await ask('迁移操作 export/preview/import/assets/confirm-handoff','export');await migration(a);}
      else if(action==='8')results();else if(action==='9')await purchases(await ask('采购操作 export导出/resume继续/list记录/template最新导入模板','export'));else text('请选择菜单中的操作。');
    }catch(e){await showError(e);}
  }
}
async function showError(e){const event={stage:'ERROR',code:e.code||'FAILED',message:e.message,details:e.details};try{await log?.write(event);}catch{}text(json?event:`${event.code}：${event.message}`);if(!json&&e.code==='WAIT_BROWSER_SETUP')text('在所选Edge打开 edge://inspect/#remote-debugging，启用当前实例远程调试，再手动继续。');}
const help='翱象补货建议助手\n默认使用后台标签；查看页面诊断可加 --foreground\n启动.cmd doctor [--json]\n启动.cmd setup [--profile-directory "Profile 1"] [--output-root "D:\\补货建议"] [--warehouse-name "仓店全名"]\n启动.cmd targets --warehouse-name "仓店全名"\n启动.cmd batches\n启动.cmd run [--batch default | --warehouse-code 编码] [--foreground]\n启动.cmd resume --run 作业ID [--foreground]\n启动.cmd resume --imported 迁入记录ID\n启动.cmd rerun [--batch default | --warehouse-code 编码]\n启动.cmd results [--json]\n启动.cmd purchase export --warehouse-code 仓码 [--warehouse-name "精确仓店全名"] --from 2026-10-01 --to 2026-10-31 [--date-basis created|updated] [--order-id 采购单号] [--rerun]\n启动.cmd purchase resume --run 采购作业ID\n启动.cmd purchase list [--json]\n启动.cmd purchase template [--json] [--foreground]\n启动.cmd migration export [--file 完整路径] [--include-results] [--snapshot]\n启动.cmd migration preview --file 迁移包.zip\n启动.cmd migration import --file 迁移包.zip\n启动.cmd migration assets\n启动.cmd migration confirm-handoff --source-stopped\nCtrl+C暂停当前作业；不会关闭整个Edge。';
process.on('SIGINT',()=>{if(shuttingDown)return;active?.abort();void stopClient();if(!active){shuttingDown=true;rl?.close();process.exitCode=130;}});
try{
  await verifyInstallation();
  if(command==='help'){text(help);}
  else if(command==='doctor'){
    const e=await discover({extraDataRoot:options['user-data-root']});if(json)text(e);else{text(`系统：${e.platform}/${e.arch}；随包Node：${e.node}`);text(`Edge安装：${e.installations.length}；已有用户配置：${e.profiles.length}`);e.profiles.forEach((p,i)=>text(`${i+1}. ${p.displayName} · ${p.profileDirectory} · ${p.actualProfilePath}`));text('发现配置不代表登录有效；setup会核对实际路径和翱象身份。');}
  }else{
    lock=await ExclusiveLock.acquire(path.join(stateRoot,'coordinator.lock.sqlite'));state=await StateStore.open(stateRoot);log=new EventLog(stateRoot);await readSettings();
    if(command==='menu')await menu();else if(command==='setup')await setup();else if(command==='targets')await manageTargets();else if(command==='batches')await manageBatches();else if(command==='run')await run();else if(command==='rerun')await run('rerun');else if(command==='resume')await run('normal',true);else if(command==='results')results();else if(command==='migration')await migration(words[1]);else if(command==='purchase')await purchases(words[1]);else fail('UNKNOWN_COMMAND',help);
  }
}catch(e){await showError(e);process.exitCode=1;}
finally{shuttingDown=true;active?.abort();await stopClient();rl?.close();try{await log?.flush();}catch{}state?.close();lock?.release();}
