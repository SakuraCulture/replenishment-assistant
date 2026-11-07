import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {JobStore} from './job-store.mjs';
import {AccountSessions,accountId,validateIdentity} from './accounts.mjs';
import * as native from './product-export.mjs';
import {CatalogError,check,now,sha,canonical,safeError,WAREHOUSES} from './common.mjs';

const businessAccount=a=>({id:a.id,warehouse:a.warehouse,expectedCompany:a.expectedCompany,expectedCoid:a.expectedCoid,expectedUid:a.expectedUid,mappingVerified:a.mappingVerified,bindingVersion:a.bindingVersion,binding:a.binding});
const unresolved=i=>(i.native?.submissions>0||i.native?.nativeTaskRef)&&!['FAILED_CONFIRMED','DOWNLOADED'].includes(i.native.stage)&&!i.adoption;

export class CatalogService {
  static async open(options){return new CatalogService({...options,store:await JobStore.open(options.stateRoot)});}
  constructor({store,stateRoot,bridgeRoot,lockRoot,forbiddenRoots,validator,driver}){
    Object.assign(this,{store,stateRoot,validator});this.active=null;this.shuttingDown=false;this.sequence=0;this.controlTail=Promise.resolve();
    this.sessions=driver?null:new AccountSessions({stateRoot,bridgeRoot,lockRoot,forbiddenRoots,phase:(phase,message)=>{if(this.active)this.active.phase={phase,message,at:now()};}});
    this.driver=driver||{
      binding:(...a)=>this.sessions.binding(...a),
      automaticBinding:(...a)=>this.sessions.automaticBinding(...a),
      inspect:(...a)=>this.sessions.inspect(...a),
      page:(...a)=>this.sessions.page(...a),
      query:(page,a)=>native.queryAll(page,a),
      export:(page,o)=>native.exportAll(page,o),
      recover:(page,o)=>native.receiveKnownExport(page,o),
      validate:(file,o)=>native.validateProductFile(file,{...o,validator}),
      close:()=>this.sessions.close(),
    };
  }
  async patch(batchId,itemId,change){
    await this.store.write(data=>{
      const b=data.batches[batchId],i=b?.items.find(i=>i.id===itemId);if(!i)throw Error('原任务不存在');
      Object.assign(i,change,{updatedAt:now()});b.updatedAt=now();b.sequence=(b.sequence||0)+1;
    });
  }
  current(batch,item){return this.store.batch(batch).items.find(i=>i.id===item);}
  status(batchId){
    const data=this.store.read();return {protocol:'jst.catalog.v1',running:!!this.active,
      active:this.active?{id:this.active.id,kind:this.active.kind,stopRequested:this.active.controller.signal.aborted,phase:this.active.phase}:null,
      accounts:Object.values(data.accounts||{}),operations:data.operations||{},
      connections:this.sessions?.status(Object.values(data.accounts||{}))||[],
      batches:batchId?[this.store.batch(batchId)]:Object.values(data.batches).slice(-50).reverse()};
  }
  command(c){
    if(['capabilities','sync.status','session.health','sync.stop','shutdown'].includes(c.action))return this.dispatch(c);
    const op=this.controlTail.catch(()=>{}).then(()=>this.dispatch(c));this.controlTail=op;return op;
  }
  async dispatch(c){
    const action=c.action;
    if(action==='capabilities')return {protocol:'jst.catalog.v1',component_version:'0.31.0',dedicated_browser:true,password_login:false};
    if(action==='sync.status')return this.status(c.batch_id);
    if(action==='session.health')return {connections:this.sessions?.status(this.store.accounts())||[]};
    if(action==='session.release'){
      this.active?.controller.abort();
      return {idle:!this.active};
    }
    if(action==='sync.stop'||action==='shutdown'){
      if(action==='shutdown')this.shuttingDown=true;
      if(this.active&&(!c.batch_id||c.batch_id===this.active.id))this.active.controller.abort();
      return {stop_requested:true,still_running:!!this.active};
    }
    if(action==='sync.provide_observation'){
      const i=this.current(c.batch_id,c.item_id);
      if(!i||i.observationRequest!==c.token||i.stage!=='WAIT_HOST')throw new CatalogError('STALE_OBSERVATION','查询依据请求已变化');
      if(i.observation&&canonical(i.observation)!==canonical(c.observation))throw Error('同一查询不能替换库存观察');
      await this.patch(c.batch_id,c.item_id,{observation:c.observation});return {accepted:true};
    }
    if(action==='sync.record_adoption_error'){
      const i=this.current(c.batch_id,c.item_id);if(!i?.receipt||i.adoption)throw Error('该项不可记录采用错误');
      await this.patch(c.batch_id,c.item_id,{adoption_error:String(c.message).slice(0,2000)});return {accepted:true};
    }
    if(action==='sync.acknowledge_adoption'){
      const i=this.current(c.batch_id,c.item_id);
      if(!i?.receipt||i.receipt.source_sha256!==c.receipt?.source_sha256||i.account.warehouse!==c.receipt?.warehouse)throw Error('采用回执与原文件/仓不符');
      if(i.adoption&&canonical(i.adoption)!==canonical(c.receipt))throw Error('原采用回执不能覆盖');
      await this.patch(c.batch_id,c.item_id,{adoption:c.receipt,stage:'APPLIED',error:null,adoption_error:null});return {accepted:true};
    }
    if(this.shuttingDown)throw new CatalogError('CLOSING','采集服务正在安全关闭');
    if(action==='accounts.bind'||action==='accounts.set_enabled'){
      if(this.active)throw new CatalogError('BUSY','请先结束当前采集或核验');
      return this.configure(c);
    }
    if(action==='sync.start')return this.start(c);
    if(this.active)throw new CatalogError('BUSY','已有采集或账号核验在运行，可查看状态或停止');
    if(action==='session.reset'){
      const a=this.store.read().accounts?.[c.warehouse];
      if(!a||!WAREHOUSES.includes(c.warehouse))throw new CatalogError('ACCOUNT_UNVERIFIED','请先保存本仓账号配置');
      if(!this.sessions)throw new CatalogError('SESSION_UNAVAILABLE','当前连接不支持主动重连');
      await this.sessions.disconnect(a.binding);
      return {disconnected:true,warehouse:c.warehouse};
    }
    if(['accounts.auto','accounts.probe','accounts.verify','accounts.open_login'].includes(action))return this.accountOperation(c);
    if(action==='sync.resume'||action==='sync.retry_unsubmitted'){
      const b=this.store.batch(c.batch_id),ids=c.item_ids||b.items.filter(i=>!i.adoption).map(i=>i.id);
      if(!ids.length||new Set(ids).size!==ids.length||ids.some(id=>!b.items.some(i=>i.id===id)))throw Error('恢复项无效');
      if(action==='sync.retry_unsubmitted'&&ids.some(id=>b.items.find(i=>i.id===id).native?.submissions>0))throw Error('该项已经提交，只能恢复原任务');
      this.launch(b.id,'batch',signal=>this.run(b.id,ids,signal));return {batch_id:b.id};
    }
    throw new CatalogError('UNKNOWN_ACTION','未知商品资料操作');
  }
  async configure(c,verified=null){
    const data=this.store.read(),old=data.accounts?.[c.warehouse];if(!WAREHOUSES.includes(c.warehouse))throw Error('未知仓库');
    if((old?.bindingVersion||0)!==c.expected_version)throw new CatalogError('CONFIG_CONFLICT','账号设置已由其他窗口更新，请重新核对');
    if(Object.values(data.batches).some(b=>b.items.some(i=>i.account.warehouse===c.warehouse&&(unresolved(i)||!i.adoption&&i.receipt))))
      throw new CatalogError('PENDING_TASK','该仓有原任务或待采用文件，请先处理原记录再改账号设置');
    let next;
    if(c.action==='accounts.set_enabled'){
      if(!old||typeof c.enabled!=='boolean')throw Error('账号或启用状态无效');next={...old,enabled:c.enabled};
    }else{
      const op=verified||data.operations?.[c.probe_id];
      if(op?.state!=='SUCCEEDED'||!['accounts.probe','accounts.auto'].includes(op.action)||op.warehouse!==c.warehouse||c.mapping_verified!==true)throw Error('请完成本仓账号核验并确认库存范围');
      const r=op.result;next={id:accountId(c.warehouse),warehouse:c.warehouse,expectedCompany:r.scope.company,
        expectedCoid:r.scope.coid,expectedUid:r.scope.uid,binding:r.binding,mappingVerified:true,enabled:true,
        mappingEvidence:op.action==='accounts.auto'?'用户选择'+c.warehouse+'专用浏览器自动配置，登录账号核验后保存':'业务在本机确认该账号资料对应'+c.warehouse,verifiedAt:op.finishedAt};
      for(const a of Object.values(data.accounts||{}))if(a.warehouse!==c.warehouse&&(
          a.expectedCoid===next.expectedCoid&&a.expectedUid===next.expectedUid||canonical(a.binding)===canonical(next.binding)))throw Error('两个仓不能使用同一账号或浏览器绑定');
    }
    next.bindingVersion=(old?.bindingVersion||0)+1;
    if(this.sessions&&old?.binding&&canonical(old.binding)!==canonical(next.binding))await this.sessions.disconnect(old.binding);
    await this.store.write(d=>{d.accounts||={};d.accounts[c.warehouse]=next;});return next;
  }
  launch(id,kind,fn){
    if(this.shuttingDown)throw new CatalogError('CLOSING','正在安全关闭，未开始新动作');
    const active={id,kind,controller:new AbortController(),phase:{phase:'STARTING',message:'准备中',at:now()}};
    this.active=active;active.promise=Promise.resolve().then(()=>fn(active.controller.signal)).catch(async e=>{
      await this.store.write(d=>{if(kind==='batch'){d.batches[id].error=safeError(e);d.batches[id].state='NEEDS_ATTENTION';}else if(d.operations?.[id])Object.assign(d.operations[id],{state:'FAILED',error:safeError(e)});});
    }).finally(()=>{if(this.active===active)this.active=null;});
  }
  async accountOperation(c){
    if(!WAREHOUSES.includes(c.warehouse))throw Error('未知仓库');
    if(c.action==='accounts.auto'&&(this.store.read().accounts?.[c.warehouse]?.bindingVersion||0)!==c.expected_version)
      throw new CatalogError('CONFIG_CONFLICT','账号设置已更新，请刷新后重试');
    const id=crypto.randomUUID();await this.store.write(d=>{d.operations||={};d.operations[id]={id,action:c.action,warehouse:c.warehouse,state:'RUNNING',createdAt:now()};});
    this.launch(id,'account',async signal=>{
      const data=this.store.read(),a=data.accounts?.[c.warehouse];let binding,result;
      try{
        if(c.action==='accounts.auto')binding=await this.driver.automaticBinding(c.warehouse,Object.values(data.accounts||{}).filter(x=>x.warehouse!==c.warehouse),signal);
        else if(c.action==='accounts.probe')binding=await this.driver.binding(c,Object.values(data.accounts||{}).filter(x=>x.warehouse!==c.warehouse));
        else{
          const probe=data.operations?.[c.probe_id];
          if(c.action==='accounts.open_login'&&probe?.warehouse===c.warehouse&&probe.binding)binding=probe.binding;
          else{if(!a)throw Error('该仓尚未绑定，请先核验专用浏览器');binding=a.binding;}
        }
        if(c.action==='accounts.open_login'){await this.driver.page({binding},signal,{loginOnly:true});result={opened:true};}
        else{
          const scope=await this.driver.inspect(binding,c.company||a?.expectedCompany,signal,{waitForLogin:c.action==='accounts.auto'});
          if(a&&['accounts.verify','accounts.auto'].includes(c.action)&&(scope.coid!==a.expectedCoid||scope.uid!==a.expectedUid))throw new CatalogError('WRONG_ACCOUNT','当前登录账号与该仓绑定不同');
          result={scope,binding};
        }
        check(signal);
        if(c.action==='accounts.auto'){
          // An unchanged verified binding must not invalidate pending batch versions.
          if(a&&canonical(a.binding)===canonical(binding)){
            result.account=a;
          }else{
            result.account=await this.configure({action:'accounts.bind',warehouse:c.warehouse,expected_version:c.expected_version,mapping_verified:true},
              {state:'SUCCEEDED',action:c.action,warehouse:c.warehouse,result,finishedAt:now()});
          }
        }
        await this.store.write(d=>Object.assign(d.operations[id],{state:'SUCCEEDED',result,finishedAt:now()}));
      }catch(e){
        await this.store.write(d=>Object.assign(d.operations[id],{state:e.code==='LOGIN_REQUIRED'?'LOGIN_REQUIRED':signal.aborted?'CANCELLED':'FAILED',error:safeError(e),binding,finishedAt:now()}));
      }
    });return {operation_id:id};
  }
  async start(c){
    if(!/^[a-zA-Z0-9_-]{8,100}$/.test(c.operation_id||'')||!['default_catalog','allocation_baseline','scheduled_replenishment','stockout_analysis'].includes(c.purpose))throw Error('同步意图缺少稳定编号或用途');
    if(!Array.isArray(c.items)||!c.items.length||c.items.length>2||new Set(c.items.map(i=>i.warehouse)).size!==c.items.length)throw Error('请选择一仓或两仓，不可重复');
    const data=this.store.read(),fingerprint=sha(canonical({operation_id:c.operation_id,purpose:c.purpose,items:c.items}));
    const existing=Object.values(data.batches).find(b=>b.operation_id===c.operation_id);
    if(existing){if(existing.fingerprint!==fingerprint)throw Error('同一同步编号对应不同范围');return {batch_id:existing.id,reused:true};}
    if(this.active)throw new CatalogError('BUSY','已有采集或账号核验在运行，可查看状态或停止');
    const items=c.items.map((i,index)=>{
      const a=data.accounts?.[i.warehouse];if(!a?.enabled)throw Error(i.warehouse+'尚未启用自动同步');validateIdentity(a);
      if(Object.values(data.batches).some(b=>b.items.some(x=>x.account.id===a.id&&unresolved(x))))throw new CatalogError('PENDING_TASK',i.warehouse+'有已受理未决任务，请继续原批次');
      return {id:crypto.randomUUID(),index,account:businessAccount(a),expected_catalog_digest:i.expected_catalog_digest??null,stage:'QUEUED',native:null,receipt:null,adoption:null,observation:null,error:null};
    });
    const id=crypto.randomUUID();await this.store.write(d=>{d.batches[id]={id,operation_id:c.operation_id,fingerprint,purpose:c.purpose,state:'QUEUED',createdAt:now(),items};});
    this.launch(id,'batch',signal=>this.run(id,items.map(i=>i.id),signal));return {batch_id:id};
  }
  async observation(batchId,itemId,signal){
    const token=crypto.randomUUID();await this.patch(batchId,itemId,{stage:'WAIT_HOST',observationRequest:token,observation:null});
    const until=Date.now()+30000;
    while(!this.current(batchId,itemId).observation){check(signal);if(Date.now()>until)throw new CatalogError('HOST_TIMEOUT','主应用未提供查询前库存依据，未提交导出');await new Promise(r=>setTimeout(r,50));}
  }
  async run(batchId,ids,signal){
    await this.store.write(d=>{d.batches[batchId].state='RUNNING';d.batches[batchId].error=null;});
    for(const initial of this.store.batch(batchId).items){
      if(!ids.includes(initial.id)||initial.adoption)continue;
      if(signal.aborted)break;
      const id=initial.id,a=initial.account;let item=this.current(batchId,id);
      const phase=async(stage,message)=>{this.active.phase={phase:stage,message,warehouse:a.warehouse,at:now()};};
      try{
        const latest=this.store.read().accounts?.[a.warehouse];
        if(!latest||!latest.enabled||canonical(businessAccount(latest))!==canonical(a))throw new CatalogError('CONFIG_CHANGED','原任务账号配置已变化，未使用新账号执行');
        if(['SUBMITTING','SUBMISSION_UNKNOWN'].includes(item.native?.stage))throw new CatalogError('SUBMISSION_UNKNOWN','原导出受理不明，请核对原任务，不自动重提');
        let validFile=false;
        if(item.native?.file)validFile=await fs.readFile(item.native.file).then(b=>sha(b)===item.native.sha256,()=>false);
        if(!validFile){
          await phase('CONNECTING','连接'+a.warehouse+'专用浏览器');const page=await this.driver.page(a,signal);check(signal);
          await this.patch(batchId,id,{connection_state:'READY',error:null});
          if(item.native?.nativeTaskRef){
            await this.driver.query(page,a);check(signal);
            await phase('RECOVERING','恢复原任务 '+item.native.nativeTaskRef);
            const received=await this.driver.recover(page,{recordId:item.native.nativeTaskRef,expectedRows:item.native.scope.expectedRows,output:path.join(this.stateRoot,'downloads',batchId,id),expectedPath:item.native.nativeFilePath?new URL(item.native.nativeFilePath).pathname:undefined,signal,onProgress:phase});
            await this.patch(batchId,id,{native:{...item.native,...received,stage:'DOWNLOADED',nativeFilePath:received.nativePath},stage:'DOWNLOADED'});
          }else{
            if(item.native?.submissions>0)throw new CatalogError('SUBMISSION_UNKNOWN','缺少原任务号，禁止再次提交');
            await this.observation(batchId,id,signal);check(signal);
            await phase('QUERYING','查询'+a.warehouse+'全部商品');const queried=await this.driver.query(page,a);check(signal);
            if(queried.scope.coid!==a.expectedCoid||queried.scope.uid!==a.expectedUid)throw new CatalogError('WRONG_ACCOUNT','实际查询账号与仓绑定不同');
            await this.driver.export(page,{...a,queried,output:path.join(this.stateRoot,'downloads',batchId,id),signal,onProgress:phase,
              onManifest:async native=>{await this.patch(batchId,id,{native,stage:native.stage});}});
          }
        }
        item=this.current(batchId,id);check(signal);
        if(!item.native?.file||!item.native.scope?.expectedRows)throw Error('缺少原文件或平台计数证据');
        let receipt=item.receipt;
        if(!receipt||await fs.readFile(receipt.candidate).then(b=>sha(b)!==receipt.candidate_sha256,()=>true)){
          await phase('VALIDATING','校验'+a.warehouse+'文件与商品字段');
          receipt=await this.driver.validate(item.native.file,{expectedRows:item.native.scope.expectedRows,warehouse:a.warehouse,output:path.dirname(item.native.file),signal});
        }
        if(receipt.source_sha256!==item.native.sha256||receipt.warehouse!==a.warehouse)throw Error('校验回执与原任务不一致');
        await this.patch(batchId,id,{receipt,stage:'READY_TO_ADOPT',error:null});
      }catch(e){
        item=this.current(batchId,id);const stage=item.native?.stage==='SUBMITTING'?'SUBMISSION_UNKNOWN':item.native?.stage||'QUEUED';
        await this.patch(batchId,id,{stage,connection_state:e.code==='LOGIN_REQUIRED'?'LOGIN_REQUIRED':item.connection_state,
          error:safeError(e),stopped:signal.aborted});
      }
    }
    await this.store.write(d=>{const b=d.batches[batchId];b.state=signal.aborted?'STOPPED':b.items.every(i=>i.receipt||i.adoption)?'FILES_READY':'NEEDS_ATTENTION';b.finishedAt=now();});
  }
  async close(){
    this.shuttingDown=true;this.active?.controller.abort();await this.controlTail.catch(()=>{});this.active?.controller.abort();await this.active?.promise;await this.driver.close();await this.store.close();
  }
}
