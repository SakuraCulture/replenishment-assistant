import {SITE,EXPORT_CODE,parseResponse,idValue,countValue,fail,wait,sameIdentity,bounded} from './common.mjs';
import platformProfile from '../assets/platform-profile.json' with {type:'json'};

const LOGIN='/account/session/getLoginInfo';
const QUERY='/saas/chain/plan/replenish/advice/query';
const TASKS='/ebai/gei/task/list';
const CREATE='/ebai/gei/create/export/task';
export async function guardedClick(locator,timeout=15000){
  try{
  let matches=await locator.count();
  if(matches>1)fail('AMBIGUOUS_CONTROL','页面控件未唯一定位，停止操作',{control:locator.toString(),matches});
  await locator.waitFor({state:'visible',timeout});
  matches=await locator.count();
  if(matches!==1)fail('AMBIGUOUS_CONTROL','页面控件未唯一定位，停止操作',{control:locator.toString(),matches});
  if(!await locator.isEnabled())fail('CONTROL_DISABLED','页面控件暂不可用');
  // UI.activate keeps only the tool-owned target's rendering active. A failed
  // click is never automatically replayed: the coordinator preserves UNKNOWN.
  await locator.click({timeout});
  }catch(e){if(e.name==='TimeoutError')fail('UI_ACTION_TIMEOUT','页面操作等待超时；原作业已保留，请处理页面弹窗后继续原作业',{control:locator.toString(),diagnostic:e.message});throw e;}
}
export function identityFrom(j) {
  if(j.status!==true||j.success!==true||!j.data?.account||!j.data?.user)fail('WAIT_LOGIN','请在所选Edge配置中完成翱象登录');
  const a=j.data.account,u=j.data.user;
  const accountId=idValue(a.accountId,'账号ID');
  if(accountId!==idValue(u.accountId,'用户账号ID'))fail('IDENTITY_INCONSISTENT','平台返回的账号隔离域不一致');
  return {accountId,userId:idValue(u.userId,'用户ID'),merchantCode:idValue(u.merchantCode,'商家编码'),displayName:String(u.merchantName||a.accountName||u.merchantCode)};
}
export function safeTask(t) {
  let fileObject=null;try {const u=new URL(t.fileUrl);fileObject=u.origin+u.pathname;}catch{}
  let downloadHost=null;try{downloadHost=new URL(t.fileUrl).host;}catch{}
  return {taskId:String(t.taskId),taskStatus:t.taskStatus,successCount:t.successCount??null,failCount:t.failCount??null,startTime:t.startTime??null,doneTime:t.doneTime??null,code:t.code??t.taskCode??null,downloadHost,fileObject};
}
export class AoxiangUI {
  constructor(page,{timeoutMs=30000,focusSession=null}={}){
    this.page=page;this.identity=null;this.lastIdentityError=null;this.taskCache=new Map();this.lastQuery=null;this.observations=[];this.receipts=[];this.spentIntents=new Set();this.requestIntents=new WeakMap();
    this.timeoutMs=timeoutMs;page.setDefaultTimeout(timeoutMs);page.setDefaultNavigationTimeout(timeoutMs);
    this.focusSession=focusSession;
    this.exportNotice=page.getByRole('dialog').filter({hasText:/正在导出数据|请点击\s*历史记录\s*查看结果/});
    this.noticeDismissals=0;
    this.requestListener=r=>{try{const u=new URL(r.url());if(platformProfile.businessOrigins.includes(u.origin)&&u.pathname.endsWith(CREATE))this.requestIntents.set(r,this.activeIntentId||null);}catch{}};
    page.on('request',this.requestListener);
    this.listener=async r=>{
      try {
        const u=new URL(r.url());
        for(const p of [LOGIN,QUERY,TASKS,CREATE])if(u.pathname.endsWith(p))this.observations.push({host:u.host,path:u.pathname,status:r.status()});
        if(this.isBusinessResponse(r,CREATE)){
          const response=parseResponse(await r.text()),request=parseResponse(r.request().postData()||'{}');
          if(response.success===true&&response.data?.taskId){
            let scope;try{scope=typeof request.query==='string'?parseResponse(request.query):request.query;}catch{}
            const safeScope={buyerWarehouseCodeList:Array.isArray(scope?.buyerWarehouseCodeList)?scope.buyerWarehouseCodeList:[],extraKeys:Object.keys(scope||{}).filter(k=>k!=='buyerWarehouseCodeList')};
            const receipt={intentId:this.requestIntents.get(r.request())||null,taskId:String(response.data.taskId),code:request.code,scope:safeScope,acceptedAt:new Date().toISOString()};
            this.receipts.push(receipt);this.onReceipt?.(receipt);
          }
        }
        if(this.isBusinessResponse(r,QUERY)){
          const j=parseResponse(await r.text());this.lastObservedQuery={request:parseResponse(r.request().postData()||'{}'),status:j.status??j.success,total:j.total};
        }
        if(!this.isBusinessResponse(r,LOGIN))return;
        try{this.identity=identityFrom(parseResponse(await r.text()));this.lastIdentityError=null;}catch(e){this.identity=null;this.lastIdentityError=e;}
      }catch{}
    };page.on('response',this.listener);
  }
  isBusinessResponse(r,p){try{const u=new URL(r.url());return platformProfile.businessOrigins.includes(u.origin)&&u.pathname.endsWith(p);}catch{return false;}}
  waitResponse(p,timeout=this.timeoutMs){const w=this.page.waitForResponse(r=>this.isBusinessResponse(r,p),{timeout});w.catch(()=>{});return w;}
  async identityAfter(action){
    const sent=this.page.waitForRequest(r=>this.isBusinessResponse(r,LOGIN),{timeout:this.timeoutMs});sent.catch(()=>{});
    // Consume the exact observed response immediately, before the navigation/action
    // completes: Chromium can discard its body on a subsequent redirect/reload.
    const identity=sent.then(async request=>{
      const response=await bounded(request.response(),this.timeoutMs,'IDENTITY_RESPONSE_TIMEOUT','本次身份查询没有取得响应');
      if(!response)fail('IDENTITY_RESPONSE_TIMEOUT','本次身份查询失败');
      return identityFrom(parseResponse(await bounded(response.text(),this.timeoutMs,'IDENTITY_RESPONSE_TIMEOUT','本次身份响应未读取完成')));
    });identity.catch(()=>{});
    await action();return identity;
  }
  async open(expected){
    await this.activate();
    this.identity=null;this.lastIdentityError=null;
    try{this.identity=await this.identityAfter(()=>this.page.goto(SITE+'/#/replenish/list',{waitUntil:'domcontentloaded',timeout:this.timeoutMs}));}catch(e){if(e.code)throw e;fail('WAIT_LOGIN','未取得有效翱象身份，请完成登录后再继续');}
    if(expected&&!sameIdentity(this.identity,expected))fail('WRONG_IDENTITY','当前Edge登录的翱象账号/商家与配置不符');
    await this.page.locator('#buyerWarehouseCodeList').waitFor({state:'visible',timeout:this.timeoutMs});
    return this.identity;
  }
  async verifyIdentity(expected=this.identity){
    await this.activate();
    const known=expected;
    this.identity=await this.identityAfter(()=>this.page.reload({waitUntil:'domcontentloaded',timeout:this.timeoutMs}));
    if(known&&!sameIdentity(this.identity,known))fail('WRONG_IDENTITY','翱象账号/商家发生变化');
    await this.page.locator('#buyerWarehouseCodeList').waitFor({state:'visible',timeout:this.timeoutMs});
    return this.identity;
  }
  async inspect(){
    return this.page.evaluate(()=>{
      const container=document.querySelector('#suggestTable');
      return {route:location.hash,ready:document.readyState,
        inputs:[...document.querySelectorAll('input')].filter(e=>e.getBoundingClientRect().width).map(e=>({id:e.id,type:e.type,placeholder:e.placeholder,value:/^buyerWarehouse/.test(e.id)?e.value:'[未采集]',checked:e.checked})),
        buttons:container?[...container.querySelectorAll('button')].map(e=>({text:e.innerText.trim(),disabled:e.disabled})):[],
        tabs:container?[...container.querySelectorAll('.ant-tabs-tab')].map(e=>({text:e.innerText.trim(),active:e.classList.contains('ant-tabs-tab-active')})):[],
        dialogs:[...document.querySelectorAll('[role=dialog]')].filter(e=>e.getBoundingClientRect().width).map(e=>e.getAttribute('aria-label')||e.querySelector('.ant-modal-title')?.textContent||''),
        headers:container?[...container.querySelectorAll('thead th')].map(e=>e.innerText.trim()):[]};
    });
  }
  async history({refresh=false}={}){
    await this.activate();
    if(!this.identity)fail('WAIT_LOGIN','尚未核验翱象身份');
    const modal=this.page.getByRole('dialog',{name:'历史记录',exact:true});
    const response=this.waitResponse(TASKS);
    if(await modal.isVisible()){
      // Refresh retains the current page. A prior missing-task search may have
      // left the dialog at its last page, hiding newly accepted tasks on page 1.
      const first=modal.locator('.ant-pagination-item-1:not(.ant-pagination-item-active)');
      const count=await first.count();
      if(count>1)fail('AMBIGUOUS_CONTROL','历史记录第一页入口不唯一，未继续');
      await this.click(count===1?first:modal.getByRole('button',{name:'刷新列表',exact:true}));
    }
    else await this.click(this.page.locator('#suggestTable').getByRole('button',{name:'历史记录',exact:true}));
    const raw=await response;
    const j=parseResponse(await raw.text());
    if(j.success===false||j.status===false||!Array.isArray(j.data))fail('TASK_LIST_FAILED','未取得有效导出历史');
    await modal.waitFor({state:'visible',timeout:15000});
    for(const t of j.data)this.taskCache.set(String(t.taskId),t);
    return {data:j.data,total:countValue(j.total,'历史任务数'),request:parseResponse(raw.request().postData()||'{}')};
  }
  async findTask(taskId){
    idValue(taskId,'任务ID');
    let {data,total}=await this.history();let seen=data.length,pages=1;
    let found=data.find(t=>String(t.taskId)===taskId);
    const modal=this.page.getByRole('dialog',{name:'历史记录',exact:true});
    while(!found&&seen<total&&pages<100){
      const next=modal.locator('.ant-pagination-next:not(.ant-pagination-disabled)');if(await next.count()!==1)break;
      const r=this.waitResponse(TASKS);await this.click(next);const j=parseResponse(await(await r).text());
      if(!Array.isArray(j.data)||!j.data.length)break;
      pages++;seen+=j.data.length;for(const t of j.data)this.taskCache.set(String(t.taskId),t);
      found=j.data.find(t=>String(t.taskId)===taskId);
    }
    if(!found)fail('TASK_NOT_FOUND','历史记录中尚未定位原任务；不能据此重新提交',{taskId,pages,seen,total});
    return found;
  }
  async click(locator){await this.activate();return guardedClick(locator,this.timeoutMs);}
  async closeHistory(){const modal=this.page.getByRole('dialog',{name:'历史记录',exact:true});if(await modal.isVisible()){await this.click(modal.getByRole('button',{name:'Close',exact:true}));await modal.waitFor({state:'hidden',timeout:15000});}}
  async activate(){
    if(!this.focusSession){
      this.focusSession=await this.page.context().newCDPSession(this.page);
      await this.focusSession.send('Emulation.setFocusEmulationEnabled',{enabled:true});
    }
    if(!this.noticeGuardInstalled){
      // A receipt can arrive before React mounts its post-export notice. Handle
      // that known notice during actionability checks, including late overlays.
      // Export confirmations and unrelated dialogs never match this locator.
      await this.page.addLocatorHandler(this.exportNotice,async notice=>{
        await guardedClick(notice.getByRole('button',{name:/^我\s*知\s*道\s*了$/}),this.timeoutMs);
        this.noticeDismissals++;
      });
      this.noticeGuardInstalled=true;
    }
  }
  async querySingle(code,name){
    if(code!==null)idValue(code,'接收仓编码');if(typeof name!=='string'||!name)fail('TARGET_NAME_MISSING','请指定已核对的仓店名称');
    await this.closeHistory();await this.verifyIdentity();
    const reset=this.page.getByRole('button',{name:/^重\s*置$/});if(await reset.count()===1){
      await this.click(reset);
      await this.page.waitForFunction(()=>{const e=document.querySelector('#buyerWarehouseCodeList');return e&&[...e.closest('.ant-select').querySelectorAll('.ant-select-selection-item')].filter(n=>!n.closest('.ant-select-selection-overflow-item-rest')).length===0;},{},{timeout:this.timeoutMs});
    }
    const input=this.page.locator('#buyerWarehouseCodeList');await input.waitFor({state:'visible'});await this.click(input);
    // On a cold entry the selector's directory and the reset render can lag
    // behind the input. Wait for real native choices before applying the search.
    await input.fill('');await this.page.locator('.ant-select-tree-treenode').filter({visible:true}).first().waitFor({state:'visible',timeout:this.timeoutMs});
    const group=[...name.matchAll(/[（(]([^（）()]+)[）)]/g)].at(-1)?.[1];
    const searchTerm=group?.replace(/(?:门店|店|仓)$/,'')||name;await input.fill(searchTerm);
    const escape=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
    const title=this.page.locator('.ant-select-tree-title').filter({hasText:new RegExp('^'+escape(name)+'$')});
    await title.first().waitFor({state:'visible',timeout:this.timeoutMs});
    if(await title.count()!==1)fail('AMBIGUOUS_TARGET','仓店名称未唯一匹配，不能选择第一个候选');
    const node=title.locator('xpath=ancestor::*[contains(@class,"ant-select-tree-treenode")][1]');
    const check=node.locator('.ant-select-tree-checkbox');
    if(!(await check.getAttribute('class')||'').includes('checkbox-checked'))await this.click(check);
    await input.press('Escape');
    await this.page.waitForFunction(name=>[...document.querySelectorAll('.ant-select-selection-item')].some(e=>e.textContent.includes(name)),name,{timeout:10000});
    const all=this.page.getByRole('tab',{name:'全部商品',exact:true});if(await all.count()===1)await this.click(all);
    // Bind to a request issued after the explicit query action. A response-only
    // wait can accidentally consume a late reset/previous-warehouse response.
    const sent=this.page.waitForRequest(r=>{if(!platformProfile.businessOrigins.includes(new URL(r.url()).origin)||!new URL(r.url()).pathname.endsWith(QUERY))return false;try{const a=parseResponse(r.postData()||'{}').buyerWarehouseCodeList;return Array.isArray(a)&&a.length===1&&(code===null||JSON.stringify(a)===JSON.stringify([code]));}catch{return false;}},{timeout:this.timeoutMs});sent.catch(()=>{});
    await this.click(this.page.getByRole('button',{name:/^查\s*询$/}));
    const nativeRequest=await sent,r=await bounded(nativeRequest.response(),this.timeoutMs,'QUERY_TIMEOUT','本次查询响应超时，未提交导出');if(!r)fail('QUERY_FAILED','本次查询请求没有取得响应');
    const j=parseResponse(await r.text()),request=parseResponse(nativeRequest.postData()||'{}');
    if(j.status!==true&&j.success!==true)fail('QUERY_FAILED','补货建议查询没有成功');
    const extras=Object.entries(request).filter(([k,v])=>!['buyerWarehouseCodeList','pageIndex','pageSize'].includes(k)&&v!==null&&v!==''&&v!==undefined&&!(Array.isArray(v)&&!v.length));
    if(code===null&&Array.isArray(request.buyerWarehouseCodeList)&&request.buyerWarehouseCodeList.length===1)code=idValue(request.buyerWarehouseCodeList[0]);
    if(JSON.stringify(request.buyerWarehouseCodeList)!==JSON.stringify([code])||extras.length)fail('QUERY_SCOPE_MISMATCH','页面实际查询范围不是约定的单仓全部商品',{request,extras});
    const tabs=await this.page.locator('#suggestTable .ant-tabs-tab-active').allTextContents();
    if(!tabs.some(t=>t.trim()==='全部商品'))fail('QUERY_SCOPE_MISMATCH','未处于全部商品页签');
    const total=countValue(j.total,'补货建议行数');
    this.lastQuery={warehouseCode:code,name,total,request,filterState:await this.readFilterState(),observedAt:new Date().toISOString(),identity:{...this.identity}};
    return this.lastQuery;
  }
  async warehouseOptions(term){
    await this.closeHistory();await this.activate();const input=this.page.locator('#buyerWarehouseCodeList');await this.click(input);await input.fill(term);
    await wait(400);
    return this.page.locator('.ant-select-tree-treenode').evaluateAll(nodes=>nodes.filter(n=>n.getBoundingClientRect().height&&n.querySelector('.ant-select-tree-checkbox')).map(n=>({name:n.querySelector('.ant-select-tree-title')?.textContent?.trim()||n.innerText.trim(),level:n.getAttribute('aria-level')||null,expandable:!!n.querySelector('.ant-select-tree-switcher:not(.ant-select-tree-switcher-noop)')})));
  }
  async preflightScope(expected){
    if(!this.lastQuery||this.lastQuery.warehouseCode!==expected.warehouseCode)fail('QUERY_REQUIRED','必须先核对本项查询范围');
    if(!sameIdentity(this.identity,expected.identity||this.lastQuery.identity))fail('WRONG_IDENTITY','身份不一致');
    const tags=await this.page.locator('#buyerWarehouseCodeList').evaluate(e=>[...e.closest('.ant-select').querySelectorAll('.ant-select-selection-item')].filter(n=>!n.closest('.ant-select-selection-overflow-item-rest')).map(n=>n.textContent));
    if(tags.length!==1||!tags[0].includes(expected.name))fail('QUERY_SCOPE_MISMATCH','页面仓点选择已变化，停止提交');
    if(expected.filterState&&JSON.stringify(await this.readFilterState())!==JSON.stringify(expected.filterState))fail('QUERY_SCOPE_MISMATCH','查询后筛选表单发生变化，停止提交');
    const tabs=await this.page.locator('#suggestTable .ant-tabs-tab-active').allTextContents();
    if(!tabs.some(t=>t.trim()==='全部商品'))fail('QUERY_SCOPE_MISMATCH','全部商品页签已变化');
  }
  async readFilterState(){return this.page.locator('#buyerWarehouseCodeList').evaluate(e=>{
    const form=e.closest('form');if(!form)throw Error('补货建议筛选表单结构发生变化');
    return {inputs:[...form.querySelectorAll('input[id],textarea[id]')].map(n=>({id:n.id,type:n.type,value:n.value,checked:n.type==='checkbox'?n.checked:false})),selections:[...form.querySelectorAll('.ant-select-selection-item')].filter(n=>!n.closest('.ant-select-selection-overflow-item-rest')).map(n=>n.textContent.trim())};
  });}
  async prepareExport(expected){
    await this.preflightScope(expected);
    const baseline=await this.history();await this.closeHistory();
    await this.page.locator('#suggestTable').getByRole('button',{name:/^导出/}).hover({timeout:10000});
    const option=this.page.getByText('全部信息导出',{exact:true}).filter({visible:true});
    await option.waitFor({state:'visible',timeout:10000});
    return {taskIds:baseline.data.map(t=>String(t.taskId)),total:baseline.total,observedAt:new Date().toISOString(),option:'全部信息导出'};
  }
  async submitOnce({intentId,expected}){
    idValue(intentId,'提交意图ID');if(this.spentIntents.has(intentId))fail('INTENT_ALREADY_DISPATCHED','同一意图已经执行，不能重复点击');
    await this.preflightScope(expected);this.spentIntents.add(intentId);this.activeIntentId=intentId;
    try{
    const response=this.waitResponse(CREATE);
    const receiptCount=this.receipts.length;
    await this.click(this.page.getByText('全部信息导出',{exact:true}).filter({visible:true}));
    // Current UI may either accept at the menu item or show a confirmation.
    // Keep one response listener and click a confirmation only if it actually exists.
    const confirm=this.page.locator('.ant-modal-confirm').filter({visible:true}).filter({hasText:'确认现在导出吗？'}).getByRole('button',{name:/^确\s*认$/});
    const outcome=await Promise.race([response.then(r=>({response:r})),confirm.waitFor({state:'visible',timeout:10000}).then(()=>({confirm:true})).catch(()=>({none:true}))]);
    let raw=outcome.response;
    if(!raw){
      if(!outcome.confirm)fail('SUBMISSION_UNKNOWN','未观察到明确受理或确认界面，保留未决状态');
      await this.click(confirm);raw=await response;
    }
    const j=parseResponse(await raw.text()),request=parseResponse(raw.request().postData()||'{}');
    const taskId=j.data?.taskId?String(j.data.taskId):null;
    if(j.success!==true||!taskId)fail('SUBMISSION_UNKNOWN','未取得明确受理ID',{taskId});
    let scope;try{scope=typeof request.query==='string'?parseResponse(request.query):request.query;}catch{}
    const ok=platformProfile.exportCodes.includes(request.code)&&JSON.stringify(scope)===JSON.stringify({buyerWarehouseCodeList:[expected.warehouseCode]});
    if(!ok)fail('WRONG_SCOPE_ACCEPTED','平台已接受不同范围任务，保存真实任务ID并停止',{taskId,code:request.code,actualScope:{buyerWarehouseCodeList:scope?.buyerWarehouseCodeList,extraKeys:Object.keys(scope||{}).filter(k=>k!=='buyerWarehouseCodeList')}});
    // The notice may not exist yet. The registered guard closes it before the
    // next actionable history click; do not treat an immediate isVisible=false
    // as proof that the modal will not appear.
    return {taskId,code:request.code,scope,acceptedAt:new Date().toISOString(),receiptCountBefore:receiptCount};
    }finally{this.activeIntentId=null;}
  }
  async dispose(){this.page.off('request',this.requestListener);this.page.off('response',this.listener);if(this.noticeGuardInstalled){try{await this.page.removeLocatorHandler(this.exportNotice);}catch{}this.noticeGuardInstalled=false;}if(this.focusSession){try{await this.focusSession.detach();}catch{}this.focusSession=null;}}
}
