import {fork} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {uid,AppError} from './common.mjs';
import {CONNECTION_STATES} from './browser-connection.mjs';

export class BrowserClient{
  constructor({onPhase=()=>{},onAccepted=()=>{},onImportReceipt=()=>{}}={}){
    this.connectionState='NOT_CONNECTED';
    this.onImportReceipt=onImportReceipt;
    this.pending=new Map();this.onPhase=onPhase;this.onReceipt=onAccepted;this.child=fork(fileURLToPath(new URL('./browser-worker.mjs',import.meta.url)),[],{stdio:['ignore','ignore','pipe','ipc'],windowsHide:true,execPath:process.execPath});
    this.child.stderr.on('data',()=>{});
    this.child.on('message',m=>{
      if(m.event==='connection'){this.connectionState=CONNECTION_STATES.has(m.state)?m.state:'UNKNOWN';return;}
      if(m.event==='login'){this.loginState=m.state;this.loginError=m.error;return;}
      if(m.event==='phase'){this.onPhase(m.phase);return;}
      if(m.event==='purchase_import_receipt'){
        try{this.onImportReceipt?.(m.receipt);}catch(e){for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(new AppError('IMPORT_RECEIPT_WRITE_FAILED','采购受理记录保存失败；保留上传意图，禁止重传'));}this.pending.clear();void this.stop();}return;
      }
      if(m.event==='accepted'){
        try{this.onReceipt?.(m.receipt);}catch(e){for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(new AppError('RECEIPT_STATE_WRITE_FAILED','受理信息写入失败；保留未决状态并停止连接'));}this.pending.clear();void this.stop();}return;
      }
      const p=this.pending.get(m.id);if(!p)return;this.pending.delete(m.id);clearTimeout(p.timer);
      if(m.error)p.reject(new AppError(m.error.code,m.error.message,m.error.details));else p.resolve(m.result);
    });
    this.child.on('exit',()=>{for(const p of this.pending.values()){clearTimeout(p.timer);p.reject(new AppError('BROWSER_DISCONNECTED','浏览器执行器已断开；已知任务和未决记录保持'));}this.pending.clear();this.closed=true;});
  }
  call(method,params={},timeout=90000){
    if(this.closed)return Promise.reject(new AppError('BROWSER_DISCONNECTED','浏览器执行器已结束'));
    const id=uid();return new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{this.pending.delete(id);reject(new AppError('WORKER_RESPONSE_UNKNOWN','操作应答超时，不能据此重放提交'));void this.stop();},timeout);
      this.pending.set(id,{resolve,reject,timer});this.child.send({id,method,params});
    });
  }
  connect(params){this.pageTimeoutMs=params.pageTimeoutMs||30000;return this.call('connect',params,(params.timeout||60000)+this.pageTimeoutMs*2);}
  recheckLogin(expectedIdentity){return this.call('recheckLogin',{expectedIdentity});}
  verifyIdentity(expected){return this.call('verifyIdentity',{expected});}
  querySingle(code,name){return this.call('querySingle',{code,name},Math.max(90000,(this.pageTimeoutMs||30000)*6));}
  warehouseOptions(term){return this.call('warehouseOptions',{term});}
  inspect(){return this.call('inspect');}
  prepareExport(expected){return this.call('prepareExport',{expected});}
  submitOnce(params){return this.call('submitOnce',params);}
  findTask(taskId){return this.call('findTask',{taskId},120000);}
  queryPurchase(params){return this.call('queryPurchase',params,Math.max(180000,(this.pageTimeoutMs||30000)*12));}
  preparePurchase(query,sourceId){return this.call('preparePurchase',{query,sourceId},120000);}
  submitPurchase(params){return this.call('submitPurchase',params,120000);}
  purchaseTask(taskId,sourceId,query){return this.call('purchaseTask',{taskId,sourceId,query},180000);}
  downloadPurchase(params){return this.call('downloadPurchase',params,(params.timeoutMs||600000)+120000);}
  downloadPurchaseTemplate(params){return this.call('downloadPurchaseTemplate',params,(params.timeoutMs||60000)+Math.max(120000,(this.pageTimeoutMs||30000)*4));}
  preparePurchaseCreate(params){return this.call('preparePurchaseCreate',params,Math.max(180000,(this.pageTimeoutMs||30000)*12));}
  uploadPurchaseOnce(params){return this.call('uploadPurchaseOnce',params,Math.max(120000,(this.pageTimeoutMs||30000)*4));}
  purchaseImportTask(params){return this.call('purchaseImportTask',params,180000);}
  queryCreateOrders(params){return this.call('queryCreateOrders',params,Math.max(180000,(this.pageTimeoutMs||30000)*12));}
  async stop(){
    if(this.closed)return;
    await new Promise(resolve=>{
      const timer=setTimeout(()=>{this.child.kill();resolve();},5000);
      this.child.once('exit',()=>{clearTimeout(timer);resolve();});
      if(this.child.connected)this.child.send({method:'stop'});else{this.child.kill();clearTimeout(timer);resolve();}
    });
  }
}
