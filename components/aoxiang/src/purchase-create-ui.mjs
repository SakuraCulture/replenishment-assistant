import fs from 'node:fs/promises';
import {portablePath} from './portable-assets.mjs';
import path from 'node:path';
import {PurchaseUI} from './purchase-ui.mjs';
import {IMPORT_CODE,taskPattern} from './purchase-create-contract.mjs';
import {fail,sha256,sameIdentity,now,businessDate} from './common.mjs';

const allowed=u=>['saas-retail.ele.me','tc-supply-chain-portal.ele.me','sc-gei-tc-supply-chain-portal.ele.me'].includes(u.hostname);
const importPath=u=>/\/(?:gei\/import\/task|(?:ebai\/)?gei\/create\/import\/task)(?:\/|$)/.test(u.pathname);
const otherWrite=u=>/\/(?:purchase|merchant|warehouse|inventory)\/.*(?:save|update|disable|enable|delete|cancel|settle|confirm|inbound|outbound|create|submit|audit)(?:\/|$)/i.test(u.pathname);
const knownReads=new Set(['/account/session/getLoginInfo','/ebai/gei/task/list','/saas/chain/plan/supply/purchase/order/pageQuery']);

// Kept for the lifetime of this owned worker page, including after a timed-out upload.
// Playwright dispatches the later one-shot guard first; its verified request alone continues.
export async function installPurchaseWriteFence(page){
  await page.route('**/*',async route=>{
    const r=route.request(),u=new URL(r.url());
    if(allowed(u)&&!['GET','HEAD','OPTIONS'].includes(r.method())&&(importPath(u)||otherWrite(u)))return route.abort();
    return route.continue();
  });
}

// Installed only around one native file-input action. No direct create API is invoked.
// The parent has committed SUBMITTING before entering here. Every exit is non-replayable.
export class NativeImportGuard{
  constructor(page,p,bytes,onReceipt=()=>{}){
    Object.assign(this,{page,p,bytes,onReceipt});this.requests=0;this.blocked=[];this.matched=new Map();this.pending=new Set();this.receipts=[];
    this.route=async route=>{
      const r=route.request(),u=new URL(r.url()),write=!['GET','HEAD','OPTIONS'].includes(r.method());
      if(!allowed(u)||!write)return route.continue();
      if(importPath(u)){
        const body=r.postDataBuffer();
        if(this.requests||r.method()!=='PUT'||u.hostname!=='sc-gei-tc-supply-chain-portal.ele.me'||u.pathname!=='/gei/import/task/'+IMPORT_CODE||!body?.includes(this.bytes)||body.indexOf(this.bytes)!==body.lastIndexOf(this.bytes)){
          this.blocked.push({kind:'unexpected_or_repeated_import',method:r.method(),host:u.hostname,path:u.pathname});return route.abort();
        }
        this.requests++;this.matched.set(r,{requestSha256:sha256(body),method:r.method()});
      }else if(r.method()!=='POST'||!knownReads.has(u.pathname)){this.blocked.push({kind:'unapproved_request',method:r.method(),host:u.hostname,path:u.pathname});return route.abort();}
      return route.continue();
    };
    this.listener=response=>{const work=this.observe(response);this.pending.add(work);void work.finally(()=>this.pending.delete(work));};
  }
  async install(){await this.page.route('**/*',this.route);this.page.on('response',this.listener);}
  async observe(response){
    try{
      const proof=this.matched.get(response.request());if(!proof)return;
      if(response.status()<200||response.status()>=300)return;
      const body=await response.text(),ids=[...new Set(body.match(new RegExp('GEI@'+IMPORT_CODE+'@\\d{12}@\\d+','g'))||[])];
      if(ids.length!==1)return;
      const receipt={intentId:this.p.id,taskId:ids[0],code:IMPORT_CODE,fileSha256:this.p.file.sha256,requestSha256:proof.requestSha256,exactSourceBytesPresent:true,importRequests:this.requests,acceptedAt:now()};
      this.receipts.push(receipt);await this.onReceipt(receipt);
    }catch(e){this.error=e;}
  }
  async result(){
    await Promise.all([...this.pending]);
    if(this.error)throw this.error;
    if(this.blocked.length)fail('IMPORT_SIDE_EFFECT_BLOCKED','额外请求已阻断；已有回执时只核对原任务，不重传',
      {blocked:this.blocked,acceptedTaskId:this.receipts.length===1?this.receipts[0].taskId:null});
    if(this.requests!==1||this.receipts.length!==1)fail('SUBMISSION_UNKNOWN','未取得本次唯一导入任务回执，只能核对原任务，不允许重传');
    return this.receipts[0];
  }
  async dispose(){await this.page.unroute('**/*',this.route);this.page.off('response',this.listener);await Promise.all([...this.pending]);}
}

export class PurchaseCreateUI extends PurchaseUI{
  constructor(page,options){super(page,options);this.createRows=[];this.usedImports=new Set();}
  async queryAction(action){const r=await super.queryAction(action);this.createRows.push(...r.rows.map(v=>Object.fromEntries(['supplyOrderId','buyerWarehouseCode','sellerMerchantCode','buyerMessage','status','totalSku','count','totalAmount','gmtCreate','outOrderId','executionMainOrderId'].map(k=>[k,v[k]??null]))));return r;}
  async queryCreateOrders(input){this.createRows=[];const query=await this.queryPurchase(input);return {query,rows:this.createRows};}
  async preparePurchaseCreate(p){
    const day=businessDate(),result=await this.queryCreateOrders({identity:p.identity,buyerWarehouseCode:p.basis.store,name:p.target.displayName,from:day,to:day,dateBasis:'created'});
    if(result.rows.some(r=>r.buyerMessage===p.note))fail('PURCHASE_MARKER_ALREADY_EXISTS','本次标记已存在于平台采购单，请核对原单，不重复导入');
    await this.click(this.page.getByRole('button',{name:/^Excel批量导入创建/}).filter({visible:true}));
    const modal=this.importModal();await modal.waitFor({state:'visible'});
    if(await modal.locator('input[type=file]').count()!==1)fail('IMPORT_VIEW','未唯一定位采购上传控件');
    this.createPrepared={id:p.id,query:result.query,from:day,to:day,orderIds:result.query.orderIds,preparedAt:now()};
    return this.createPrepared;
  }
  importModal(){return this.page.locator('.next-dialog').filter({hasText:'请按照规定格式上传文件'}).filter({visible:true});}
  async uploadPurchaseOnce({intent:p,baseline}){
    if(this.usedImports.has(p.id)||this.createPrepared?.id!==p.id||this.createPrepared.preparedAt!==baseline.preparedAt||!sameIdentity(this.identity,p.identity))fail('IMPORT_NOT_PREPARED','采购上传未在当前会话准备或已使用');
    await this.preflightScope(baseline.query);
    const bytes=await fs.readFile(await portablePath(p.file.path));if(bytes.length!==p.file.bytes||sha256(bytes)!==p.file.sha256)fail('PURCHASE_FILE_CHANGED','采购文件变化，停止上传');
    const modal=this.importModal(),input=modal.locator('input[type=file]');if(await input.count()!==1)fail('IMPORT_VIEW','上传窗口已变化');
    const guard=new NativeImportGuard(this.page,p,bytes,r=>this.onImportReceipt?.(r));
    this.usedImports.add(p.id);await guard.install();
    try{
      // In-memory bytes prevent a file-path change between the hash check and native upload.
      await input.setInputFiles({name:path.basename(p.file.path),mimeType:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',buffer:bytes});
      if(!guard.requests){
        const button=modal.getByRole('button',{name:'确定',exact:true});
        await button.waitFor({state:'visible',timeout:this.timeoutMs});
        if(!await button.isEnabled())fail('IMPORT_NOT_READY','上传尚未就绪，原意图待核，不重复上传');
        if(!guard.requests)await this.click(button);
      }
      const until=Date.now()+this.timeoutMs;
      while(!guard.receipts.length&&!guard.error&&Date.now()<until)await new Promise(r=>setTimeout(r,50));
      return await guard.result();
    }finally{await guard.dispose();if(guard.blocked.length||guard.error)await guard.result();}
  }
  async purchaseImportTask({taskId,identity}){
    if(!taskPattern.test(taskId||''))fail('IMPORT_TASK_ID','原导入任务编号无效');
    await this.open(identity);const t=await this.findTask(taskId);
    if((t.taskCode||t.code)!==IMPORT_CODE)fail('IMPORT_TASK_TYPE','原任务不是采购批量导入，不按近似任务匹配');
    return {taskId,code:IMPORT_CODE,status:t.taskStatus,successCount:t.successCount,failCount:t.failCount};
  }
}
