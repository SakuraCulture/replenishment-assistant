import path from 'node:path';
import {discover} from './environment.mjs';
import {BrowserClient} from './worker-client.mjs';
import {fail,sameIdentity,now,writeJson,canonical} from './common.mjs';

// Uses the component's existing browser worker and selectors. Never opens a new profile.
export class BoundBrowser{
  constructor({state,config,binding,signal,onProgress=()=>{},Client=BrowserClient,discoverEnvironment=discover}){
    Object.assign(this,{state,config,binding,signal,onProgress,Client,discoverEnvironment});
    this.client=null;this.adapter={onReceipt:null};
    for(const method of ['verifyIdentity','querySingle','warehouseOptions','prepareExport','submitOnce','findTask','downloadPurchaseTemplate','preparePurchaseCreate','uploadPurchaseOnce','purchaseImportTask','queryCreateOrders','queryPurchase','preparePurchase','submitPurchase','purchaseTask','downloadPurchase']){
      this.adapter[method]=async(...args)=>{
        this.check();await this.ensure();this.check();return this.client[method](...args);
      };
    }
  }
  check(){if(this.signal?.aborted)fail('PAUSED','已请求暂停；未开始新的平台动作');}
  async ensure(){
    if(this.client&&!this.client.closed&&this.connected)return;
    if(!this.binding?.enabled)fail('SETUP_REQUIRED','请先配置本机翱象连接');
    const env=await this.discoverEnvironment({extraDataRoot:this.binding.userDataRoot});this.check();
    const profile=env.profiles.find(p=>p.actualProfilePath.toLowerCase()===this.binding.actualProfilePath.toLowerCase());
    const edge=env.installations.find(e=>e.executable.toLowerCase()===this.binding.edgeExecutable.toLowerCase());
    if(!profile||!edge)fail('MACHINE_REBIND_REQUIRED','本机浏览器或已有用户配置变化，请重新绑定');
    if(!this.client||this.client.closed)this.client=new this.Client({
      onPhase:stage=>this.onProgress({stage}),
      onAccepted:receipt=>{if(!this.state.recordReceipt(receipt))this.state.recordPurchaseReceipt(receipt);this.adapter.onReceipt?.(receipt);},
      onImportReceipt:receipt=>this.adapter.onImportReceipt?.(receipt)
    });
    const connected=await this.client.connect({candidate:{...profile,edgeExecutable:edge.executable},
      expectedIdentity:this.config.tenantBinding,stateRoot:this.state.root,
      timeout:this.config.resourceLimits.connectTimeoutMs,pageTimeoutMs:this.config.resourceLimits.pageTimeoutMs,
      mode:this.config.browserInteraction?.mode||'background_tab',source:'D13'});
    if(!sameIdentity(connected.identity,this.config.tenantBinding))fail('WRONG_IDENTITY','浏览器身份与命令绑定不同');
    this.connected=connected;
    this.binding={...this.binding,edgeVersion:connected.facts.edgeVersion,actualProfilePath:connected.facts.actualProfilePath,identityVerifiedAt:now()};
    await writeJson(path.join(this.state.root,'config/machine.binding.json'),this.binding);
  }
  async close(){
    // Called only after the awaited operation settles. A pause does not kill an in-flight submission.
    await this.client?.stop();this.client=null;this.connected=null;
  }
}

export class BoundBrowserPool{
  constructor(Browser=BoundBrowser){this.Browser=Browser;this.browser=null;this.key=null;}
  async acquire(args){
    const {binding,config}=args;
    const key=canonical({identity:config.tenantBinding,profile:binding.actualProfilePath,
      edge:binding.edgeExecutable,userDataRoot:binding.userDataRoot,profileDirectory:binding.profileDirectory,
      mode:config.browserInteraction?.mode||'background_tab',
      pageTimeoutMs:config.resourceLimits.pageTimeoutMs,connectTimeoutMs:config.resourceLimits.connectTimeoutMs});
    if(this.browser&&this.key!==key)await this.close();
    if(!this.browser){this.browser=new this.Browser(args);this.key=key;}
    else Object.assign(this.browser,args);
    return this.browser;
  }
  async close(){await this.browser?.close();this.browser=null;this.key=null;}
}
