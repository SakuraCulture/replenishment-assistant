import fs from 'node:fs/promises';
import path from 'node:path';
import {AoxiangUI,identityFrom} from './aoxiang-ui.mjs';
import {SITE,parseResponse,fail,bounded,countValue,idValue,canonical,sameIdentity,now,uid} from './common.mjs';
import {PURCHASE_CODES,checkPurchaseQuery,checkPurchaseReceipt,exportScope,dateBounds} from './purchase-contract.mjs';
import {fileHash} from './workbook-validator.mjs';
import {downloadNativePurchaseTemplate} from './purchase-template-download.mjs';

const QUERY='/saas/chain/plan/supply/purchase/order/pageQuery',CREATE='/ebai/gei/create/export/task',TASKS='/ebai/gei/task/list';
const RESULT_ORIGIN='https://sc-gei-tc-supply-chain-portal.ele.me';
export class PurchaseUI extends AoxiangUI{
  downloadPurchaseTemplate(params){return downloadNativePurchaseTemplate(this,params);}
  constructor(page,options={}){
    super(page,options);page.off('response',this.listener);
    this.exportNotice=page.locator('.next-dialog').filter({hasText:/正在导出数据|请点击\s*历史记录\s*查看结果/}).filter({visible:true});
    this.listener=async r=>{try{
      if(!this.isBusinessResponse(r,CREATE))return;
      const j=parseResponse(await r.text()),request=parseResponse(r.request().postData()||'{}');if(j.success!==true||!j.data?.taskId||!Object.values(PURCHASE_CODES).includes(request.code))return;
      const receipt={intentId:this.requestIntents.get(r.request())||null,taskId:String(j.data.taskId),code:request.code,scope:typeof request.query==='string'?parseResponse(request.query):request.query,acceptedAt:now()};
      this.receipts.push(receipt);this.onReceipt?.(receipt);
    }catch{}};page.on('response',this.listener);
  }
  async open(expected){
    await this.activate();
    const url=SITE+'/#/chain/order/purchase';
    // Hash-only routing does not necessarily request loginInfo again. Move to
    // the route first, then reload under the fresh identity observer.
    if(this.page.url().split('#')[0]===SITE+'/'&&this.page.url()!==url)await this.page.goto(url,{waitUntil:'domcontentloaded'});
    this.identity=await this.identityAfter(()=>this.page.url()===url?this.page.reload({waitUntil:'domcontentloaded'}):this.page.goto(url,{waitUntil:'domcontentloaded'}));
    if(expected&&!sameIdentity(this.identity,expected))fail('WRONG_IDENTITY','采购页账号或商家与配置不同');
    await this.page.locator('#storeCodeList').waitFor({state:'visible'});return this.identity;
  }
  async verifyIdentity(expected=this.identity){return this.open(expected);}
  historyModal(){return this.page.locator('.next-dialog').filter({has:this.page.locator('.next-dialog-header').filter({hasText:/^历史记录$/})}).filter({visible:true});}
  async closeHistory(){const m=this.historyModal();if(await m.count()){await this.click(m.locator('.next-dialog-close'));await m.waitFor({state:'hidden'});}}
  async dismissNotice(){if(await this.exportNotice.count())await this.click(this.exportNotice.getByRole('button',{name:/^我\s*知\s*道\s*了$/}));}
  async readFilterState(){return this.page.locator('#storeCodeList').evaluate(e=>{
    const f=e.closest('form');if(!f)throw Error('采购筛选表单结构已变化');return {inputs:[...f.querySelectorAll('input,textarea')].map(n=>({id:n.id,placeholder:n.placeholder,value:n.value,checked:n.checked||false})),tags:[...f.querySelectorAll('.next-tag-body')].map(n=>n.textContent),flags:[...document.querySelectorAll('input[type=checkbox]')].filter(n=>!n.closest('table')).map(n=>({checked:n.checked,disabled:n.disabled}))};
  });}
  async queryAction(action){
    const sent=this.page.waitForRequest(r=>this.isBusinessResponse(r,QUERY),{timeout:this.timeoutMs});sent.catch(()=>{});await action();
    const request=await sent,response=await bounded(request.response(),this.timeoutMs,'PURCHASE_QUERY_TIMEOUT','采购查询未取得响应');if(!response)fail('PURCHASE_QUERY_FAILED','采购查询失败');
    const j=parseResponse(await bounded(response.text(),this.timeoutMs,'PURCHASE_QUERY_TIMEOUT','采购查询响应读取超时'));
    // Live AX uses null/null for a successful empty purchase list. Do not turn
    // arbitrary missing values into zero: require both success flags, HTTP and
    // the corresponding visible empty table + zero total before accepting it.
    if(j.success===true&&j.status===true&&j.data===null&&j.total===null&&response.status()===200){
      const empty=this.page.getByText('暂无数据',{exact:true}).filter({visible:true});
      const zero=this.page.getByText(/^共\s*0\s*条$/).filter({visible:true});
      await empty.waitFor({state:'visible',timeout:this.timeoutMs});
      await zero.waitFor({state:'visible',timeout:this.timeoutMs});
      if(await empty.count()!==1||await zero.count()!==1)fail('PURCHASE_QUERY_FAILED','空采购列表与页面计数未唯一对应');
      return {request:parseResponse(request.postData()||'{}'),rows:[],total:0,emptyProof:'success_null_pair_and_visible_empty_zero'};
    }
    if((j.success!==true&&j.status!==true)||!Array.isArray(j.data))fail('PURCHASE_QUERY_FAILED','采购页未返回有效单据列表');
    return {request:parseResponse(request.postData()||'{}'),rows:j.data,total:countValue(j.total)};
  }
  async setDates(input){
    if(!await this.page.locator('#date').isVisible())await this.click(this.page.getByText('更多',{exact:true}).filter({visible:true}));
    const id=input.dateBasis==='updated'?'modifiedDate':'date';await this.click(this.page.locator('#'+id).getByPlaceholder('起始日期'));
    const fields=this.page.getByPlaceholder('YYYY-MM-DD',{exact:true}).filter({visible:true});await fields.nth(1).fill(input.to);await fields.nth(0).fill(input.from);await fields.nth(0).press('Enter');
    const popup=fields.first().locator('xpath=ancestor::*[contains(@class,"next-overlay-wrapper")][1]');await this.click(popup.getByRole('button',{name:'确定',exact:true}));
    if(canonical(await this.page.locator('#'+id+' input').evaluateAll(ns=>ns.map(n=>n.value)))!==canonical([input.from,input.to]))fail('PURCHASE_DATE_NOT_APPLIED','采购时间控件未正确应用日期');
  }
  async queryPurchase(input){
    dateBounds(input.from,input.to);await this.open(input.identity||this.identity);
    await this.click(this.page.getByRole('button',{name:'重置',exact:true}));
    const control=this.page.locator('#storeCodeList');await this.click(control);const group=[...input.name.matchAll(/[（(]([^（）()]+)[）)]/g)].at(-1)?.[1];await control.fill(group?.replace(/(?:门店|店|仓)$/,'')||input.name);
    const escaped=input.name.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),leaf=this.page.locator('.next-tree-node-label').filter({hasText:new RegExp('^'+escaped+'$')}).filter({visible:true});await leaf.first().waitFor({state:'visible'});if(await leaf.count()!==1)fail('AMBIGUOUS_TARGET','采购仓店名称未唯一匹配');await this.click(leaf);await control.press('Escape');
    await this.setDates(input);if(input.orderId)await this.page.locator('#supplyOrderId').fill(input.orderId);
    const first=await this.queryAction(()=>this.click(this.page.getByRole('button',{name:'查询',exact:true})));checkPurchaseQuery(first.request,input);
    if(first.total>3000)fail('PURCHASE_SLICE_TOO_LARGE','单日主单超过3000张工程保护值，请指定采购单号进一步缩小；此值不是平台上限');
    const orderIds=[],orderSkuCounts={};let current=first,pages=0;const seenPages=new Set();
    for(;;){
      checkPurchaseQuery(current.request,input);if(current.total!==first.total)fail('PURCHASE_QUERY_CHANGED','翻页期间采购单计数变化，请重新查询后再导出');
      const pageIndex=String(current.request.pageIndex);if(seenPages.has(pageIndex))fail('PURCHASE_PAGINATION','采购列表未推进到下一页');seenPages.add(pageIndex);pages++;
      for(const row of current.rows){const orderId=idValue(row.supplyOrderId,'采购单号');if(row.buyerWarehouseCode!==input.buyerWarehouseCode||orderIds.includes(orderId)||(input.orderId&&orderId!==input.orderId))fail('PURCHASE_QUERY_CHANGED','采购分页存在跨仓、重复或非指定主单');orderIds.push(orderId);orderSkuCounts[orderId]=countValue(row.totalSku,'主单商品种数');}
      if(orderIds.length===first.total)break;if(!current.rows.length||orderIds.length>first.total||pages>=1000)fail('PURCHASE_PAGINATION','采购列表分页覆盖不完整');
      current=await this.queryAction(()=>this.click(this.page.getByRole('button',{name:/^下一页/}).filter({visible:true})));
    }
    this.lastQuery={warehouseCode:input.buyerWarehouseCode,name:input.name,total:first.total,pages,request:first.request,orderIds,orderSkuCounts,filterState:await this.readFilterState(),observedAt:now(),identity:{...this.identity},...(first.emptyProof?{emptyProof:first.emptyProof}:{})};return this.lastQuery;
  }
  async preflightScope(query){
    if(!sameIdentity(query.identity,this.identity)||!this.lastQuery||canonical(this.lastQuery.orderIds)!==canonical(query.orderIds)||canonical(exportScope(this.lastQuery.request))!==canonical(exportScope(query.request)))fail('PURCHASE_QUERY_REQUIRED','采购导出必须使用当前页面已核对的查询');
    if(canonical(await this.readFilterState())!==canonical(query.filterState))fail('PURCHASE_SCOPE_MISMATCH','查询后采购筛选发生变化，停止导出');
    if(await this.page.locator('tbody input[type=checkbox]:checked').count())fail('PURCHASE_SELECTION_CHANGED','采购列表存在勾选单据，停止导出以免混用范围');
  }
  async preparePurchase(query,sourceId){
    if(!PURCHASE_CODES[sourceId])fail('PURCHASE_SOURCE','未知采购导出类型');await this.preflightScope(query);const baseline=await this.history();await this.closeHistory();return {taskIds:baseline.data.map(t=>String(t.taskId)),total:baseline.total,observedAt:now()};
  }
  async submitPurchase({intentId,query,sourceId}){
    idValue(intentId);if(this.spentIntents.has(intentId))fail('INTENT_ALREADY_DISPATCHED','同一采购提交意图不能重放');await this.preflightScope(query);this.spentIntents.add(intentId);this.activeIntentId=intentId;
    try{
      const response=this.waitResponse(CREATE);await this.click(this.page.getByRole('button',{name:/^导出/}).filter({visible:true}));
      await this.click(this.page.getByText(sourceId==='D16'?'导出主单':'导出子单明细',{exact:true}).filter({visible:true}));
      const confirm=this.page.locator('.next-dialog-quick').filter({visible:true,hasText:'确认现在导出吗？'}).getByRole('button',{name:'确定',exact:true});
      const first=await Promise.race([response.then(raw=>({raw})),confirm.waitFor({state:'visible',timeout:this.timeoutMs}).then(()=>({confirm:true}))]);
      if(first.confirm)await this.click(confirm);const raw=first.raw||await response,j=parseResponse(await raw.text()),request=parseResponse(raw.request().postData()||'{}');
      if(j.success!==true||!j.data?.taskId)fail('SUBMISSION_UNKNOWN','采购导出没有返回明确受理ID');
      const receipt={intentId,taskId:String(j.data.taskId),code:request.code,scope:typeof request.query==='string'?parseResponse(request.query):request.query,acceptedAt:now()};
      if(!checkPurchaseReceipt(receipt,sourceId,query))fail('WRONG_SCOPE_ACCEPTED','采购导出已受理，但实际范围不同',{taskId:receipt.taskId});return receipt;
    }finally{this.activeIntentId=null;}
  }
  async history(){
    await this.activate();const modal=this.historyModal(),open=await modal.count();const sent=this.page.waitForRequest(r=>this.isBusinessResponse(r,TASKS),{timeout:this.timeoutMs});sent.catch(()=>{});
    if(open)await this.click(modal.getByRole('button',{name:'刷新列表',exact:true}));else await this.click(this.page.locator('button:not(.next-dialog button)').filter({hasText:/^历史记录$/}).filter({visible:true}));
    const request=await sent,response=await request.response();if(!response)fail('TASK_LIST_FAILED','采购历史查询失败');const j=parseResponse(await response.text());
    if(!Array.isArray(j.data)||j.success===false||j.status===false)fail('TASK_LIST_FAILED','采购历史记录响应无效');await this.historyModal().waitFor({state:'visible'});
    for(const t of j.data)this.taskCache.set(String(t.taskId),t);return {data:j.data,total:countValue(j.total)};
  }
  async findTask(taskId){
    idValue(taskId);let {data,total}=await this.history(),found=data.find(t=>t.taskId===taskId),seen=data.length,pages=1;
    while(!found&&seen<total&&pages<1000){const sent=this.waitResponse(TASKS);await this.click(this.historyModal().getByRole('button',{name:/^下一页/}));const j=parseResponse(await(await sent).text());if(!Array.isArray(j.data)||!j.data.length)break;data=j.data;pages++;seen+=data.length;for(const t of data)this.taskCache.set(t.taskId,t);found=data.find(t=>t.taskId===taskId);}
    if(!found)fail('TASK_NOT_FOUND','采购历史30天范围内未定位原任务；不能据此重提',{taskId});return found;
  }
  async purchaseTask(taskId,sourceId,query){
    await this.open(query.identity);const task=await this.findTask(taskId);let params;try{params=typeof task.params==='string'?parseResponse(task.params):task.params;}catch{}
    if((task.taskCode||task.code)!==PURCHASE_CODES[sourceId]||canonical(params)!==canonical(exportScope(query.request)))fail('PURCHASE_TASK_SCOPE','采购任务类型或已保存筛选不匹配，停止下载');return task;
  }
  async downloadPurchase({taskId,sourceId,query,directory,maxBytes=67108864,timeoutMs=600000}){
    const task=await this.purchaseTask(taskId,sourceId,query);if(task.taskStatus!=='FINISHED'||countValue(task.failCount)!==0)fail('PURCHASE_TASK_PENDING','采购原任务未成功完成');
    // Native legacy GEI download: task-list fileUrl is a filename, not an OSS
    // URL. Use only the observed exact known-task GET with browser-context auth.
    if(task.downloadFileUrlType!=='default'||task.fileUrl!==taskId+'.xlsx'||task.url)fail('PURCHASE_DOWNLOAD_CHANGED','采购原生下载机制已变化，需要复核后兼容');
    const response=await this.page.context().request.get(RESULT_ORIGIN+'/gei/import/task/errorFile/'+encodeURIComponent(taskId),{maxRedirects:0,timeout:timeoutMs});
    try{
      if(response.status()!==200)fail('PURCHASE_DOWNLOAD_HTTP','采购任务文件下载失败，HTTP '+response.status());
      const declared=response.headers()['content-length'];if(declared&&(!/^\d+$/.test(declared)||Number(declared)>maxBytes))fail('PURCHASE_FILE_SIZE','采购原文件超过本版本64MiB缓冲保护值；原任务保留');
      const body=await response.body();if(body.length<100||body.length>maxBytes||body.subarray(0,4).toString('hex')!=='504b0304')fail('PURCHASE_NOT_XLSX','采购下载不是允许大小的XLSX，原任务保留');
      if(declared&&Number(declared)!==body.length)fail('DOWNLOAD_TRUNCATED','采购文件字节数不完整');await fs.mkdir(directory,{recursive:true});const file=path.join(directory,uid()+'.xlsx'),h=await fs.open(file,'wx');try{await h.writeFile(body);await h.sync();}finally{await h.close();}
      return {file,taskId,bytes:body.length,sha256:await fileHash(file),transport:'known_legacy_gei_task_browser_context'};
    }finally{await response.dispose();}
  }
}
