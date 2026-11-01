import fs from 'node:fs/promises';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import {spawn} from 'node:child_process';
import {WebSocket, WebSocketServer} from 'ws';
import {chromium} from 'playwright-core';
import {discoverEndpoint,defaultEdgeRoot} from './environment.mjs';
import {AppError,fail,exists,wait,redact} from './common.mjs';

export async function createGuard(endpoint) {
  const nonce=crypto.randomBytes(24).toString('hex');
  const server=new WebSocketServer({host:'127.0.0.1',port:0,verifyClient:info=>info.req.url==='/'+nonce});
  await new Promise((resolve,reject)=>{server.once('listening',resolve);server.once('error',reject);});
  const sockets=new Set(),ownedTargets=new Set(),blocked=[],stats={upstreamOpen:false,sent:0,received:0,upstreamError:null};
  server.on('connection',down=>{
    const up=new WebSocket(endpoint);sockets.add(up);sockets.add(down);const queue=[];
    up.on('open',()=>{stats.upstreamOpen=true;for(const text of queue)up.send(text);});
    down.on('message',bytes=>{
      let j;try{j=JSON.parse(String(bytes));}catch{return;}
      stats.sent++;
      if(j.method==='Browser.close'||j.method==='Browser.setDownloadBehavior'||j.method==='Page.setDownloadBehavior'||j.method==='BrowserContext.close'||(j.method==='Target.closeTarget'&&!ownedTargets.has(j.params?.targetId))||j.method==='Target.disposeBrowserContext'||j.method==='Target.createBrowserContext'){
        blocked.push(j.method);down.send(JSON.stringify({id:j.id,error:{code:-32000,message:'操作超出已有Edge会话的控制范围'}}));return;
      }
      if(up.readyState===WebSocket.OPEN)up.send(String(bytes));else queue.push(String(bytes));
    });
    up.on('message',bytes=>{stats.received++;if(down.readyState===WebSocket.OPEN)down.send(String(bytes));});
    up.on('error',e=>{stats.upstreamError=redact(String(e.message));down.close();});down.on('error',()=>{});
    down.on('close',()=>{up.close();sockets.delete(down);});up.on('close',()=>{down.close();sockets.delete(up);});
  });
  return {url:`ws://127.0.0.1:${server.address().port}/${nonce}`,ownedTargets,blocked,stats,close:async()=>{for(const s of sockets)s.terminate();await new Promise(r=>server.close(r));}};
}

export class EdgeBridge {
  constructor(){this.guard=null;this.browser=null;this.page=null;this.marker=null;this.facts=null;this.ownedPages=new Set();this.backgroundFocusSession=null;}
  async bind(candidate,{timeout=60000,onPhase=()=>{},mode='background_tab'}={}) {
    if(this.browser) fail('ALREADY_CONNECTED','本执行器已有浏览器连接');
    if(!['background_tab','visible_tab'].includes(mode))fail('BROWSER_MODE','浏览器模式无效');
    const expected=await fs.realpath(candidate.actualProfilePath);
    if(!await exists(candidate.edgeExecutable)) fail('EDGE_MISSING','未找到所选Edge程序');
    const endpoint=await discoverEndpoint(candidate.userDataRoot);
    const nonce=crypto.randomBytes(24).toString('hex');
    const markerServer=http.createServer((req,res)=>{if(!['/bind/'+nonce,'/job/'+nonce].includes(req.url)){res.writeHead(404).end();return;}res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store','Content-Security-Policy':"default-src 'none'"});res.end('<!doctype html><meta charset="utf-8"><title>翱象补货建议助手 · 配置绑定</title><h2>正在核对所选Edge用户配置</h2><p>本页由本机补货建议助手创建，核对完成后进入翱象。</p>');});
    await new Promise((resolve,reject)=>{markerServer.once('error',reject);markerServer.listen(0,'127.0.0.1',resolve);});
    this.marker=markerServer;
    const markerUrl=`http://127.0.0.1:${markerServer.address().port}/bind/${nonce}`;
    const args=[`--profile-directory=${candidate.profileDirectory}`];
    if(path.resolve(candidate.userDataRoot).toLowerCase()!==path.resolve(defaultEdgeRoot()).toLowerCase())args.push(`--user-data-dir=${candidate.userDataRoot}`);
    args.push(markerUrl);
    const child=spawn(candidate.edgeExecutable,args,{shell:false,detached:false,windowsHide:false,stdio:'ignore'});let spawnError;child.once('error',e=>spawnError=e);child.unref();
    onPhase('CONNECTING');
    try {
      this.guard=await createGuard(endpoint);
      this.browser=await chromium.connectOverCDP(this.guard.url,{noDefaults:true,timeout});
      const bcdp=await this.browser.newBrowserCDPSession();const version=await bcdp.send('Browser.getVersion');await bcdp.detach();
      if(!/^Edg\//.test(version.product||'')) fail('WRONG_BROWSER','当前连接不是所选Microsoft Edge实例');
      let candidates=[];const until=Date.now()+15000;
      do {if(spawnError)throw spawnError;candidates=this.browser.contexts().flatMap(c=>c.pages()).filter(p=>p.url()===markerUrl);if(candidates.length)break;await wait(250);}while(Date.now()<until);
      if(candidates.length!==1)fail('PROFILE_TARGET_NOT_FOUND','无法唯一识别所选Edge配置的绑定标签，请在目标配置中重试');
      this.page=candidates[0];this.ownedPages.add(this.page);const c=await this.page.context().newCDPSession(this.page);const ti=await c.send('Target.getTargetInfo');await c.detach();
      this.guard.ownedTargets.add(ti.targetInfo.targetId);
      await this.page.goto('edge://version/',{waitUntil:'domcontentloaded',timeout:30000});
      const actual=await this.page.evaluate(()=>document.querySelector('#profile_path')?.textContent?.trim()||'');
      if(!actual)fail('PROFILE_PATH_UNAVAILABLE','未取得Edge显示的实际Profile path，不能自动绑定');
      const actualResolved=await fs.realpath(actual);
      if(actualResolved.toLowerCase()!==expected.toLowerCase())fail('WRONG_PROFILE','实际Edge用户配置与所选配置不符');
      let targetId=ti.targetInfo.targetId;
      if(mode==='background_tab')targetId=await this.backgroundJob(markerUrl.replace('/bind/','/job/'),ti.targetInfo,expected);
      this.facts={edgeVersion:version.product,profileDirectory:candidate.profileDirectory,actualProfilePath:actualResolved,profilePathVerified:true,ownedTargetId:targetId,mode};
      onPhase('PROFILE_VERIFIED');
      return this.facts;
    } catch(error) {
      await this.close();
      if(error instanceof AppError)throw error;
      throw new AppError('CONNECTION_NOT_ESTABLISHED','Edge连接未建立；请检查浏览器是否弹出允许连接提示后再继续',{cause:redact(String(error.message)).slice(0,1500)});
    } finally {if(this.marker){this.marker.closeAllConnections();await new Promise(r=>this.marker.close(r));this.marker=null;}}
  }
  async backgroundJob(jobUrl,verifiedTarget,expected){
    const markerPage=this.page,context=markerPage.context(),session=await this.browser.newBrowserCDPSession();
    try{
      const params={url:jobUrl,background:true};if(verifiedTarget.browserContextId)params.browserContextId=verifiedTarget.browserContextId;
      const {targetId}=await session.send('Target.createTarget',params);this.guard.ownedTargets.add(targetId);
      let found=[];const until=Date.now()+15000;
      do{found=this.browser.contexts().flatMap(c=>c.pages()).filter(p=>p.url()===jobUrl);if(found.length)break;await wait(100);}while(Date.now()<until);
      if(found.length!==1)fail('BACKGROUND_TARGET_UNAVAILABLE','未唯一识别后台作业标签，请用--foreground诊断');
      const job=found[0];this.ownedPages.add(job);
      const check=await job.context().newCDPSession(job);let actualTarget;try{actualTarget=(await check.send('Target.getTargetInfo')).targetInfo;}finally{await check.detach();}
      if(actualTarget.targetId!==targetId||job.context()!==context||actualTarget.browserContextId!==verifiedTarget.browserContextId)fail('WRONG_PROFILE','后台作业标签未归属于已核对的用户配置');
      this.backgroundFocusSession=await job.context().newCDPSession(job);
      await this.backgroundFocusSession.send('Emulation.setFocusEmulationEnabled',{enabled:true});
      await job.goto('edge://version/',{waitUntil:'domcontentloaded',timeout:30000});
      await job.waitForFunction(()=>document.querySelector('#profile_path')?.textContent?.trim(),{},{timeout:30000});
      const actual=await job.evaluate(()=>document.querySelector('#profile_path')?.textContent?.trim()||'');
      if(!actual||(await fs.realpath(actual)).toLowerCase()!==expected.toLowerCase())fail('WRONG_PROFILE','后台作业标签的实际Profile path不符');
      this.page=job;
      // Closing only our short-lived binding page lets Edge return to the
      // user's previous tab. Never bring the background job to the foreground.
      await markerPage.close({runBeforeUnload:false});this.ownedPages.delete(markerPage);
      return targetId;
    }finally{await session.detach();}
  }
  takeFocusSession(){const session=this.backgroundFocusSession;this.backgroundFocusSession=null;return session;}
  async close(){
    if(this.backgroundFocusSession){try{await this.backgroundFocusSession.detach();}catch{}this.backgroundFocusSession=null;}
    if(this.page)this.ownedPages.add(this.page);
    for(const page of this.ownedPages)if(!page.isClosed())try{await page.close({runBeforeUnload:false});}catch{}
    this.ownedPages.clear();
    if(this.browser&&this.guard?.ownedTargets?.size)try{const c=await this.browser.newBrowserCDPSession();try{for(const targetId of this.guard.ownedTargets)try{await c.send('Target.closeTarget',{targetId});}catch{}}finally{await c.detach();}}catch{}
    this.page=null;
    if(this.guard)await this.guard.close();this.guard=null;this.browser=null;
    if(this.marker){this.marker.closeAllConnections();await new Promise(r=>this.marker.close(r));this.marker=null;}
  }
}
