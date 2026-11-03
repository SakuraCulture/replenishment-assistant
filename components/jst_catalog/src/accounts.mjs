import fs from 'node:fs/promises';
import path from 'node:path';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {CatalogError,check,sleep,sha,canonical,inside,samePath} from './common.mjs';
import {queryAll} from './product-export.mjs';

export const accountId=wh=>({'山东仓':'sd','河北仓':'hb'})[wh];
export function validateIdentity(a){
  if(!accountId(a.warehouse)||a.id!==accountId(a.warehouse)||!a.expectedCompany||
      !/^[1-9]\d*$/.test(a.expectedCoid)||!/^[1-9]\d*$/.test(a.expectedUid)||a.mappingVerified!==true)
    throw new CatalogError('ACCOUNT_UNVERIFIED','该仓账号和库存范围尚未核验');
  return a;
}

export class AccountSessions {
  constructor({bridgeRoot,stateRoot,lockRoot,forbiddenRoots=[],phase=()=>{}}){
    Object.assign(this,{bridgeRoot,stateRoot,lockRoot,forbiddenRoots,phase});
    this.sessions=new Map();this.instanceLocks=new Map();this.closed=false;
  }
  async initialize(){
    if(this.components)return;
    const load=name=>import(pathToFileURL(path.join(this.bridgeRoot,'src',name)).href);
    const [{EdgeBridge},env,{ExclusiveLock}]=await Promise.all([load('edge-bridge.mjs'),load('environment.mjs'),load('state.mjs')]);
    this.components={EdgeBridge,env,ExclusiveLock};
  }
  async binding(input,others=[]){
    await this.initialize();const {env}=this.components;
    const profile=await fs.realpath(input.profilePath);
    const directory=path.basename(profile),root=await fs.realpath(path.dirname(profile));
    if(!/^(Default|Profile \d+)$/.test(directory))throw new CatalogError('PROFILE_INVALID','请选择专用浏览器的Default或Profile目录');
    for(const forbidden of [env.defaultEdgeRoot(),...this.forbiddenRoots]){
      const f=await fs.realpath(forbidden).catch(()=>path.resolve(forbidden));
      if(inside(f.toLowerCase(),root.toLowerCase())||inside(root.toLowerCase(),f.toLowerCase()))
        throw new CatalogError('BROWSER_ISOLATION','此目录属于正常浏览器；聚水潭必须使用专用独立浏览器，未连接翱象');
    }
    for(const a of others)if(a.binding&&samePath(a.binding.userDataRoot,root))
      throw new CatalogError('ACCOUNT_ISOLATION','两仓不能绑定同一个浏览器资料根');
    const found=await env.discover({extraDataRoot:root});
    const p=found.profiles.find(p=>samePath(p.actualProfilePath,profile));
    const exe=input.edgeExecutable?found.installations.find(e=>samePath(e.executable,input.edgeExecutable)):found.installations[0];
    if(!p||!exe)throw new CatalogError('BROWSER_MISSING','未找到所选专用配置或已安装Edge，请修复本机绑定');
    return {userDataRoot:root,actualProfilePath:profile,profileDirectory:directory,edgeExecutable:exe.executable,backend:'jst_dedicated'};
  }
  async ensureBrowser(binding,signal){
    const {env}=this.components;check(signal);
    let endpoint=await env.discoverEndpoint(binding.userDataRoot).catch(()=>null);
    if(!endpoint){
      this.phase('OPENING_BROWSER','正在打开该仓专用浏览器');
      // Reuse its normal persisted startup settings. Never add a fresh login profile or AX flags.
      const child=spawn(binding.edgeExecutable,[`--user-data-dir=${binding.userDataRoot}`,`--profile-directory=${binding.profileDirectory}`,'about:blank'],{shell:false,windowsHide:true,stdio:'ignore'});
      let failure;child.once('error',e=>{failure=e;});child.unref();
      const until=Date.now()+20000;
      while(!endpoint&&Date.now()<until){check(signal);if(failure)throw failure;await sleep(300);endpoint=await env.discoverEndpoint(binding.userDataRoot).catch(()=>null);}
      if(!endpoint)throw new CatalogError('BROWSER_SETUP','专用浏览器已打开，但调试连接未就绪；请在该实例允许远程调试后继续');
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
    if(cached&&!cached.page.isClosed())return cached;
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
      this.phase('CONNECTING','正在连接专用浏览器；如有提示请允许调试');
      try{await bridge.bind(binding,{timeout:60000,mode:'background_tab',onPhase:()=>check(signal)});check(signal);}
      catch(e){await bridge.close().catch(()=>{});throw e;}
      bridge.catalogEndpoint=endpoint;this.sessions.set(key,bridge);return bridge;
    }catch(e){if(!this.sessions.has(key)){this.instanceLocks.get(key)?.release();this.instanceLocks.delete(key);}throw e;}
  }
  async page(account,signal,{loginOnly=false}={}){
    const bridge=await this.connect(account,signal);check(signal);
    await bridge.page.goto('https://www.erp321.com/epaas',{waitUntil:'domcontentloaded',timeout:60000});check(signal);
    if(loginOnly){await bridge.page.bringToFront();return bridge.page;}
    const login=await bridge.page.evaluate(()=>[...document.querySelectorAll('input[type=password]')].some(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0;})||/扫码登录|密码登录|账号登录/.test(document.body?.innerText||''));
    if(login)throw new CatalogError('LOGIN_REQUIRED','该仓需要正常登录；登录完成后继续本仓，另一仓可以继续');
    return bridge.page;
  }
  async inspect(binding,company,signal){
    const page=await this.page({binding},signal);check(signal);
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
