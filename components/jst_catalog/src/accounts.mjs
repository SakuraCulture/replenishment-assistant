import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {CatalogError,check,sleep,sha,canonical,inside,samePath} from './common.mjs';
import {queryAll} from './product-export.mjs';

export const accountId=wh=>({'山东仓':'sd','河北仓':'hb'})[wh];
export function dedicatedLaunchArgs(binding){
  if(binding.backend!=='jst_dedicated')throw new CatalogError('BROWSER_ISOLATION','只能启动已核验的聚水潭专用浏览器');
  return [`--user-data-dir=${binding.userDataRoot}`,`--profile-directory=${binding.profileDirectory}`,
    '--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--no-first-run','--no-default-browser-check','about:blank'];
}
export function validateIdentity(a){
  if(!accountId(a.warehouse)||a.id!==accountId(a.warehouse)||!a.expectedCompany||
      !/^[1-9]\d*$/.test(a.expectedCoid)||!/^[1-9]\d*$/.test(a.expectedUid)||a.mappingVerified!==true)
    throw new CatalogError('ACCOUNT_UNVERIFIED','该仓账号和库存范围尚未核验');
  return a;
}

export class AccountSessions {
  constructor({bridgeRoot,stateRoot,lockRoot,forbiddenRoots=[],phase=()=>{}}){
    Object.assign(this,{bridgeRoot,stateRoot,lockRoot,forbiddenRoots,phase});
    this.sessions=new Map();this.instanceLocks=new Map();this.connectionStates=new Map();this.closed=false;
  }
  async initialize(){
    if(this.components)return;
    const load=name=>import(pathToFileURL(path.join(this.bridgeRoot,'src',name)).href);
    const [{EdgeBridge},env,{ExclusiveLock}]=await Promise.all([load('edge-bridge.mjs'),load('environment.mjs'),load('state.mjs')]);
    this.components={EdgeBridge,env,ExclusiveLock};
  }
  async binding(input,others=[]){
    await this.initialize();const {env}=this.components;
    const profile=await fs.realpath(input.profilePath).catch(()=>{throw new CatalogError('PROFILE_MISSING','专用浏览器目录不存在，请在连接设置中重新选择；未连接其他浏览器');});
    const directory=path.basename(profile),root=await fs.realpath(path.dirname(profile));
    if(!/^(Default|Profile \d+)$/.test(directory))throw new CatalogError('PROFILE_INVALID','请选择专用浏览器的Default或Profile目录');
    await this.assertIsolated(root,others);
    const found=await env.discover({extraDataRoot:root});
    const p=found.profiles.find(p=>samePath(p.actualProfilePath,profile));
    // A fresh managed profile is not always in Local State until Edge exits.
    // The bridge still verifies the live edge://version profile path before use.
    const managed=['sd','hb'].some(id=>samePath(profile,path.join(this.stateRoot,'browsers',id,'User Data','Default')));
    const exe=input.edgeExecutable?found.installations.find(e=>samePath(e.executable,input.edgeExecutable)):found.installations[0];
    if((!p&&!managed)||!exe)throw new CatalogError('BROWSER_MISSING','未找到所选专用配置或已安装Edge，请修复本机绑定');
    return {userDataRoot:root,actualProfilePath:profile,profileDirectory:directory,edgeExecutable:exe.executable,backend:'jst_dedicated'};
  }
  async assertIsolated(root,others=[]){
    const {env}=this.components;
    for(const forbidden of [env.defaultEdgeRoot(),...this.forbiddenRoots]){
      const f=await fs.realpath(forbidden).catch(()=>path.resolve(forbidden));
      if(inside(f.toLowerCase(),root.toLowerCase())||inside(root.toLowerCase(),f.toLowerCase()))
        throw new CatalogError('BROWSER_ISOLATION','此目录属于正常浏览器；聚水潭必须使用专用独立浏览器，未连接翱象');
    }
    for(const a of others)if(a.binding&&samePath(a.binding.userDataRoot,root))
      throw new CatalogError('ACCOUNT_ISOLATION','两仓不能绑定同一个浏览器资料根');
  }
  async automaticBinding(warehouse,others,signal){
    const id=accountId(warehouse);
    if(!id)throw new CatalogError('ACCOUNT_UNVERIFIED','未知仓库');
    await this.initialize();check(signal);
    const root=path.join(this.stateRoot,'browsers',id,'User Data');
    const profile=path.join(root,'Default');
    await fs.mkdir(profile,{recursive:true});
    const discovered=await this.components.env.discover({extraDataRoot:root});
    const executable=discovered.installations[0]?.executable;
    if(!executable)throw new CatalogError('BROWSER_MISSING','未找到已安装的 Microsoft Edge');
    const binding={userDataRoot:await fs.realpath(root),actualProfilePath:await fs.realpath(profile),
      profileDirectory:'Default',edgeExecutable:executable,backend:'jst_dedicated'};
    await this.assertIsolated(binding.userDataRoot,others);
    if(!samePath(path.dirname(binding.actualProfilePath),binding.userDataRoot))
      throw new CatalogError('BROWSER_ISOLATION','专用配置目录链接到其他浏览器，未启动');
    await this.ensureBrowser(binding,signal);
    return this.binding({profilePath:profile,edgeExecutable:executable},others);
  }
  async ensureBrowser(binding,signal){
    const {env}=this.components;check(signal);
    let endpoint=await env.discoverEndpoint(binding.userDataRoot).catch(()=>null);
    if(!endpoint){
      this.phase('OPENING_BROWSER','正在打开该仓专用浏览器');
      // Reopen only this previously verified dedicated profile, retaining login.
      // Publish an ephemeral loopback endpoint again after browser restart.
      // Existing AX/default-browser processes and their startup flags are untouched.
      const child=spawn(binding.edgeExecutable,dedicatedLaunchArgs(binding),{shell:false,windowsHide:true,stdio:'ignore'});
      let failure;child.once('error',e=>{failure=e;});child.unref();
      const until=Date.now()+20000;
      while(!endpoint&&Date.now()<until){check(signal);if(failure)throw failure;await sleep(300);endpoint=await env.discoverEndpoint(binding.userDataRoot).catch(()=>null);}
      if(!endpoint)throw new CatalogError('BROWSER_SETUP','专用浏览器自动连接未就绪；如已手动打开该专用实例，请正常关闭后重试自动配置');
    }
    for(const root of [env.defaultEdgeRoot(),...this.forbiddenRoots]){
      const other=await env.discoverEndpoint(root).catch(()=>null);
      if(other===endpoint)throw new CatalogError('BROWSER_ISOLATION','专用浏览器端点与正常浏览器相同，未继续');
    }
    return endpoint;
  }
  async connect(account,signal){
    if(this.closed)throw new CatalogError('SESSION_CLOSED','采集服务已关闭');
    await this.initialize();check(signal);
    const binding=await this.binding({profilePath:account.binding.actualProfilePath,edgeExecutable:account.binding.edgeExecutable});
    if(canonical(binding)!==canonical(account.binding))throw new CatalogError('BINDING_CHANGED','本机专用浏览器绑定已变化，请重新核验');
    const key=sha(canonical(binding));let cached=this.sessions.get(key);
    if(cached&&this.healthy(cached))return cached;
    if(cached){await cached.close().catch(()=>{});this.sessions.delete(key);}
    if(!this.instanceLocks.has(key)){
      const file=path.join(this.lockRoot,sha(binding.userDataRoot.toLowerCase())+'.sqlite3');
      try{this.instanceLocks.set(key,await this.components.ExclusiveLock.acquire(file));}
      catch{throw new CatalogError('BROWSER_BUSY','该聚水潭专用浏览器正由另一采集服务使用，请先完成原任务');}
    }
    try{
      const endpoint=await this.ensureBrowser(binding,signal);
      for(const [otherKey,b] of this.sessions)if(otherKey!==key&&b.catalogEndpoint===endpoint)
        throw new CatalogError('ACCOUNT_ISOLATION','两仓连接到了相同浏览器实例');
      const bridge=new this.components.EdgeBridge();
      this.connectionStates.set(key,{state:'CONNECTING',reason:cached?'原连接已断开，正在重新连接':'首次连接或已主动重连'});
      this.phase('CONNECTING','正在自动连接本仓专用浏览器');
      try{await bridge.bind(binding,{timeout:60000,mode:'background_tab',onPhase:()=>check(signal)});check(signal);}
      catch(e){await bridge.close().catch(()=>{});this.connectionStates.set(key,{state:'DISCONNECTED',reason:'连接未建立，请重试自动配置或检查专用浏览器状态'});throw e;}
      bridge.catalogEndpoint=endpoint;this.sessions.set(key,bridge);this.connectionStates.set(key,{state:'CONNECTED',reason:'关闭业务窗口后继续保留'});return bridge;
    }catch(e){if(!this.sessions.has(key)){this.instanceLocks.get(key)?.release();this.instanceLocks.delete(key);}throw e;}
  }
  healthy(bridge){return !!(bridge.page&&!bridge.page.isClosed()&&bridge.browser?.isConnected());}
  status(accounts){
    return accounts.map(a=>{
      const key=sha(canonical(a.binding)),bridge=this.sessions.get(key),last=this.connectionStates.get(key);
      const state=bridge?(this.healthy(bridge)?'CONNECTED':'DISCONNECTED'):(last?.state||'NOT_CONNECTED');
      return {warehouse:a.warehouse,state,reason:bridge&&!this.healthy(bridge)?'浏览器连接或作业标签已关闭':last?.reason||''};
    });
  }
  async disconnect(binding){
    const key=sha(canonical(binding)),bridge=this.sessions.get(key);
    if(bridge)await bridge.close();
    this.sessions.delete(key);this.instanceLocks.get(key)?.release();this.instanceLocks.delete(key);
    this.connectionStates.set(key,{state:'DISCONNECTED',reason:'已主动断开本仓；下次连接可能需要重新允许'});
  }
  async page(account,signal,{loginOnly=false,waitForLogin=false}={}){
    const bridge=await this.connect(account,signal);check(signal);
    await bridge.page.goto('https://www.erp321.com/epaas',{waitUntil:'domcontentloaded',timeout:60000});check(signal);
    if(loginOnly){await bridge.page.bringToFront();return bridge.page;}
    await bridge.page.waitForFunction(()=>document.querySelector('a[class^=menuItem]')||document.querySelector('input[type=password]')||/扫码登录|密码登录|账号登录/.test(document.body?.innerText||''),null,{timeout:30000});check(signal);
    const login=await bridge.page.evaluate(()=>[...document.querySelectorAll('input[type=password]')].some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})||/扫码登录|密码登录|账号登录/.test(document.body?.innerText||''));
    if(login&&waitForLogin){
      this.phase('WAIT_LOGIN','请在本仓专用浏览器登录对应仓库账号，完成后将自动核验并保存');
      await bridge.page.bringToFront();
      const until=Date.now()+180000;
      while(!await bridge.page.locator('a[class^=menuItem]').count()){
        check(signal);
        if(Date.now()>until)throw new CatalogError('LOGIN_REQUIRED','等待登录超时，请点击自动配置继续；专用浏览器已保留');
        await sleep(300);
      }
      check(signal);
    }else if(login){
      await bridge.page.bringToFront();
      throw new CatalogError('LOGIN_REQUIRED','该仓需要正常登录；登录完成后继续本仓，另一仓可以继续');
    }
    return bridge.page;
  }
  async inspect(binding,company,signal,options={}){
    const page=await this.page({binding},signal,options);check(signal);
    const detected=company||await page.evaluate(()=>{
      const lines=(document.querySelector('#root')?.innerText||'').split('\n').map(x=>x.trim()).filter(x=>/^.{2,150}\[[^\]]+\]$/.test(x));
      return lines.length===1?lines[0].replace(/\[[^\]]+\]$/,'').trim():null;
    });
    if(!detected)throw new CatalogError('COMPANY_REQUIRED','未唯一识别公司名称，请填写页面顶部公司名再核验');
    const {scope}=await queryAll(page,{expectedCompany:detected});check(signal);return scope;
  }
  async close(){
    this.closed=true;
    for(const bridge of this.sessions.values())await bridge.close().catch(()=>{});
    this.sessions.clear();for(const lock of this.instanceLocks.values())lock.release();this.instanceLocks.clear();
  }
}
