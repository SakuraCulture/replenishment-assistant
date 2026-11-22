import {CatalogError,check,sleep} from './common.mjs';
import {activateControl,displayedCompany,downloadResponseUrl,nativeTaskId,observe,openHistory,receiveKnownExport} from './product-export.mjs';
import {receiveArchiveFile} from './archive-download.mjs';

export const ARCHIVE='jst_full_archive';
const TITLE='商品主题分析';
const TYPE=TITLE+'_全商品档案';
const BI='https://bi.erp321.com';
const REPORT_ROOT='/app/report/subject/itemskuanalysis/';
const HISTORY='/app/common/export/export_log.aspx';
const EXPORT=REPORT_ROOT+'allsku.aspx';
const STATIC='https://jstzbstatic.erp321.com/export/**';
const WAREHOUSE_LABELS={
  '河北仓': ['优团河北石家庄仓','河北仓'],
  '山东仓': ['优团山东临沂仓','山东仓'],
};
const ORDER_STATES='WAITCONFIRM,WAITFCONFIRM,WAITDELIVER,DELIVERING,SENT,QUESTION,WAITOUTERSENT,CANCELLED';
const QUERY={sales:'unlimited',enabled:'all',excludeZero:false,excludeCancelledBeforeShipment:true};
const tidy=value=>String(value??'').replace(/\s+/g,'');
const scopeError=detail=>new CatalogError('REPORT_SCOPE',detail+'，未采用全商品档案');

export function resolveReportWarehouse(entries,warehouse,company){
  const aliases=WAREHOUSE_LABELS[warehouse];
  if(!aliases)throw scopeError('未识别目标仓');
  const hits=entries.filter(x=>/^mw_[1-9]\d*$/.test(x.id)&&aliases.includes(tidy(x.label)));
  const exact=hits.filter(x=>tidy(x.label)===tidy(company));
  if(exact.length===1)return exact[0];
  if(hits.length===1)return hits[0];
  throw scopeError('报表没有唯一的'+warehouse+'发货仓选项');
}

export function archiveDownloadName(name){
  const m=new RegExp('^'+TYPE+'_(\\d{14})_([1-9]\\d*)_1\\.xlsx$').exec(name);
  if(!m)throw new CatalogError('FILE_IDENTITY','全商品档案未返回可核验的单片文件名，保留导出待核');
  return {stamp:m[1],task:nativeTaskId(m[2])};
}

export function verifyArchiveConditions(text,warehouse){
  const parts=String(text).replace(/&nbsp;/g,' ').split(/[;；\n]/).map(x=>x.trim()).filter(Boolean);
  const conditions=parts.filter(x=>!['提示 !','关闭'].includes(x));
  const expected=new Set(['订单状态@='+ORDER_STATES,'其他=排除发货前取消商品','仓库='+warehouse]);
  return conditions.length===expected.size&&conditions.every(x=>expected.delete(x))&&!expected.size;
}

export function archiveLogRow(cells,recordId){
  // The native grid retains a status code just before the visible status.
  const [,id,name,count,created,type,,,state]=cells.map(x=>String(x).trim());
  if(id!==recordId||type!==TYPE||!new RegExp('^'+TYPE+'_\\d{14}$').test(name||''))
    throw new CatalogError('FILE_IDENTITY','原任务号或导出类型与全商品档案不一致');
  if(/失败|已取消/.test(state))throw new CatalogError('PLATFORM_FAILED','平台显示原报表导出失败');
  if(state!=='下载完成')return null;
  const expectedRows=Number(String(count).replaceAll(',',''));
  const matched=/^(\d{4}-\d\d-\d\d) (\d\d:\d\d:\d\d)$/.exec(created||'');
  if(!Number.isSafeInteger(expectedRows)||expectedRows<1||!matched)
    throw new CatalogError('FILE_IDENTITY','平台导出总行数或时间无法核验');
  const stamp=(matched[1]+matched[2]).replace(/[-:]/g,'');
  if(name!==TYPE+'_'+stamp)throw new CatalogError('FILE_IDENTITY','原生任务的名称与创建时间不一致');
  return {expectedRows,exportedAt:matched[1]+'T'+matched[2]+'+08:00',stamp};
}

function nativeCallback(request,frame,pathname,method){
  try{
    const url=new URL(request.url());
    if(url.origin!==BI||url.pathname!==pathname||request.method()!=='POST'||request.frame()!==frame)return null;
    const form=new URLSearchParams(request.postData()||'');
    const callback=JSON.parse(form.get('__CALLBACKPARAM'));
    if(form.get('__CALLBACKID')==='ACall1'&&callback?.CallControl==='{page}'&&callback.Method===method)
      return Array.isArray(callback.Args)?callback.Args:[];
  }catch{}
  return null;
}

export function validateArchiveRequest(args,scope){
  try{
    if(args.length!==1)return false;
    const data=JSON.parse(args[0]),filters=JSON.parse(data.search);
    const expected=new Map([['A.status',ORDER_STATES],['C.sent_flag','1'],
      ['A.wms_co_id',scope.reportWarehouseId]]);
    return String(data.page_count)===String(scope.expectedRows)&&Boolean(data.rpt_config)
      &&verifyArchiveConditions(data.export_remark,scope.reportWarehouse)
      &&Array.isArray(filters)&&filters.length===expected.size
      &&filters.every(f=>f.c==='@='&&!f.t&&expected.get(f.k)===f.v&&expected.delete(f.k))
      &&!expected.size;
  }catch{return false;}
}

function acceptedArchive(body){
  let envelope;
  try{if(body.startsWith('0|'))envelope=JSON.parse(body.slice(2));}catch{}
  if(envelope?.IsSuccess!==true||envelope.GotoLogin||envelope.ExceptionMessage||envelope.ReturnValue?.Code!=='Success')
    throw new CatalogError('SUBMISSION_UNKNOWN','未取得可核验的报表受理回执；保留原任务');
  return nativeTaskId(envelope.ReturnValue.Data);
}

function verifyArchiveAddress(url,record){
  const disposition=url.searchParams.get('response-content-disposition')||'';
  const token=/^attachment;\s*filename="?([^";]+)"?$/i.exec(disposition);
  if(!token)throw new CatalogError('FILE_IDENTITY','原任务链接缺少全商品档案文件名');
  const name=archiveDownloadName(decodeURIComponent(token[1]));
  if(name.task!==record.scope.taskId||name.stamp!==record.receipt.stamp)
    throw new CatalogError('FILE_IDENTITY','原文件名与全商品档案任务号或时间不一致');
}

function fromFrame(frame,leaf){
  try{const u=new URL(frame.url());return u.origin===BI&&u.pathname===REPORT_ROOT+leaf;}
  catch{return false;}
}

async function reportFrame(page,leaf,parent,signal){
  const until=Date.now()+40000;
  while(Date.now()<until){
    check(signal);
    const frames=page.frames().filter(f=>fromFrame(f,leaf)&&(!parent||f.parentFrame()===parent));
    if(frames.length>1)throw scopeError('业务报表页不唯一');
    if(frames.length===1)return frames[0];
    await sleep(150);
  }
  throw scopeError('未取得平台报表页');
}

async function readFilters(filter){
  return filter.evaluate(()=>{
    const val=id=>document.getElementById(id);
    const selected=id=>val(id)?.selectedOptions?.[0]?.textContent.trim();
    const criteria=['sku_id','i_id','sku_code','name','short_name','properties_value','remark',
      'labels_search','vc_name','no_sku_id','no_i_id','no_labels_search','item_type'];
    return {sales:selected('sale_qty'),enabled:selected('enabled'),sync:selected('stock_disabled'),
      excludeZero:val('is_filter_empty')?.checked,excludeCancelled:val('sent_flag')?.checked,
      excludeGifts:val('is_gift')?.checked,criteria:criteria.filter(key=>String(val(key)?.value||'').trim()),
      warehouses:[...document.querySelectorAll('input[id^="mw_"]')].map(x=>({
        id:x.id,label:Array.from(x.labels||[]).map(y=>y.innerText.trim()).join(''),checked:x.checked}))};
  });
}

async function setFilterCheckbox(filter,id,value){
  // The platform styles these as labels and can hide the backing input.
  const control=filter.locator('#'+id);
  if(await control.count()!==1||!await control.evaluate((el,wanted)=>{
    if(!(el instanceof HTMLInputElement)||el.type!=='checkbox'||el.matches(':disabled'))return false;
    if(el.checked!==wanted)el.click();
    return el.checked===wanted;
  },value))throw scopeError('报表筛选控件未采用预期值');
}

async function displayScopedArchive(page,account,signal){
  if(tidy(await displayedCompany(page))!==tidy(account.expectedCompany))throw scopeError('当前企业与目标仓账号不一致');
  const menu=page.locator('a[class^="menuItem"]').filter({hasText:/^报表$/});
  await activateControl(menu);
  const entries=page.locator('div[class^="title"]').filter({hasText:/^商品主题分析$/}).filter({visible:true});
  const count=await entries.count();
  if(count<1||count>2)throw scopeError('商品主题分析菜单结构发生变化');
  await activateControl(entries.last());
  const report=await reportFrame(page,'default.aspx',page.mainFrame(),signal);
  const filters=await reportFrame(page,'filter.aspx',report,signal);
  const initial=await reportFrame(page,'allsku.aspx',report,signal);
  await initial.getByText('点击左侧【生成报表】，查看报表数据!',{exact:true}).waitFor({timeout:20000});
  await filters.locator('#sale_qty').selectOption({label:'无限制'});
  await filters.locator('#enabled').selectOption({label:'-全部-'});
  const all=await readFilters(filters);
  const target=resolveReportWarehouse(all.warehouses,account.warehouse,account.expectedCompany);
  for(const entry of all.warehouses)
    if(entry.checked!==(entry.id===target.id))await setFilterCheckbox(filters,entry.id,entry.id===target.id);
  if(all.excludeZero)await setFilterCheckbox(filters,'is_filter_empty',false);
  const state=await readFilters(filters);
  const checked=state.warehouses.filter(x=>x.checked);
  if(state.sales!=='无限制'||state.enabled!=='-全部-'||state.sync!=='-全部-'
    ||state.excludeZero!==false||state.excludeCancelled!==true||state.excludeGifts!==false
    ||state.criteria.length||checked.length!==1||checked[0].id!==target.id)
    throw scopeError('全状态、无限销量、取消排零或目标仓筛选未生效');
  check(signal);
  const queriedAt=new Date().toISOString();
  await activateControl(filters.getByText('生成报表',{exact:true}));
  const result=await reportFrame(page,'allsku.aspx',report,signal);
  const button=result.locator('span.excel').filter({hasText:/^导出EXCEL$/});
  await button.waitFor({timeout:60000});
  await result.getByText('首次入库时间',{exact:true}).first().waitFor({state:'attached',timeout:25000});
  const expectedRows=Number(await result.locator('#dataPageCount').inputValue());
  if(!Number.isSafeInteger(expectedRows)||expectedRows<1)throw scopeError('报表缺少可核对的全量数据条数');
  const conditions=await report.locator('#right_td').innerText();
  const warehouses=[...conditions.matchAll(/(?:^|[;；\s])仓库=([^;；\r\n]+)/g)].map(x=>x[1].trim());
  if(warehouses.length!==1||warehouses[0]!==target.label||/是否排除数据为0的商品=是/.test(conditions))
    throw scopeError('生成结果未显示唯一目标仓或仍排除了零数据');
  return {button,result,scope:{kind:ARCHIVE,warehouse:account.warehouse,reportWarehouse:target.label,reportWarehouseId:target.id.slice(3),
    company:account.expectedCompany,coid:account.expectedCoid,uid:account.expectedUid,query:{...QUERY},queriedAt,expectedRows}};
}

async function archiveRecord(page,scope,recordId,signal,onProgress){
  const history=await openHistory(page);
  const until=Date.now()+90000;
  const row=history.locator('div._jt_row').filter({has:history.getByText(recordId,{exact:true})});
  for(;;){
    check(signal);
    const count=await row.count();
    if(count>1)throw new CatalogError('FILE_IDENTITY','同一任务号出现多条回执');
    if(count===1){
      const cells=await row.locator('div._jt_cell').allInnerTexts();
      const receipt=archiveLogRow(cells,recordId);
      if(receipt){
        if(scope.expectedRows!==receipt.expectedRows)throw scopeError('原任务总条数与查询结果不同');
        const modal=history.locator('#msg_body'),close=history.locator('#msg_close');
        if(await close.isVisible())await activateControl(close);
        await activateControl(row.getByText('导出条件',{exact:true}));
        await modal.waitFor({timeout:15000});
        const text=await modal.innerText();
        if(await close.isVisible())await activateControl(close);
        if(!verifyArchiveConditions(text,scope.reportWarehouse))throw scopeError('原生回执的全量筛选或所属仓不同');
        return {history,receipt,scope:{...scope,...receipt,taskId:recordId}};
      }
    }
    if(Date.now()>=until)throw new CatalogError('TASK_PENDING','原报表导出尚未完成，请核对原任务');
    onProgress('WAIT_EXPORT','等待全商品档案原任务 '+recordId);
    await sleep(1200);check(signal);
    await history.goto(history.url(),{waitUntil:'domcontentloaded',timeout:30000});
  }
}

export async function exportArchive(page,{output,onManifest,signal,onProgress=()=>{},...account}){
  check(signal);
  const {button,result,scope}=await displayScopedArchive(page,account,signal);
  let manifest={schema:1,stage:'INTENT_RECORDED',sourceFormat:ARCHIVE,scope,submissions:0,createdAt:new Date().toISOString()};
  const save=async change=>{manifest={...manifest,...change};await onManifest(structuredClone(manifest));};
  await save({});
  const submission=url=>url.origin===BI&&url.pathname===EXPORT;
  const args=request=>nativeCallback(request,result,EXPORT,'CreateAsyncExport');
  const attempt=observe(page,'response',response=>args(response.request())!==null,45000);
  const automatic=new Map();
  const capture=response=>{
    const ids=nativeCallback(response.request(),result,HISTORY,'GetReportExportData');
    if(ids?.length===1)try{automatic.set(nativeTaskId(ids[0]),response);}catch{}
  };
  // The existing Edge session disallows a global download-policy change.
  // Receive the original task from its own callback; don't write a browser copy.
  const blockCopy=route=>route.abort('blockedbyclient');
  let transmitted=false;
  const blocked=Promise.withResolvers();
  blocked.promise.catch(()=>{});
  const guard=async route=>{
    const submitted=args(route.request());
    if(!submitted)return route.fallback();
    if(transmitted||manifest.stage!=='SUBMITTING')return route.abort('blockedbyclient');
    if(signal?.aborted||!validateArchiveRequest(submitted,scope)){
      await route.abort('blockedbyclient');
      await save({stage:signal?.aborted?'CANCELLED_BEFORE_SUBMIT':'NOT_SUBMITTED_SCOPE_MISMATCH',submissions:0});
      blocked.reject(scopeError('实际导出请求的条数、筛选或所属仓与已核验查询不同'));
      return;
    }
    transmitted=true;
    return route.fallback();
  };
  page.on('response',capture);
  try{
    await page.route(submission,guard);
    await page.route(STATIC,blockCopy);
    onProgress('SUBMITTING','提交本仓全商品档案；受理未知时保留原任务');
    await activateControl(button,{beforeDispatch:async()=>{
      check(signal);await save({stage:'SUBMITTING',submissions:1});
      if(signal?.aborted){await save({stage:'CANCELLED_BEFORE_SUBMIT',submissions:0});check(signal);}
    }});
    const response=await Promise.race([attempt.promise,blocked.promise]);
    if(!transmitted||!response.ok())throw new CatalogError('SUBMISSION_UNKNOWN','本次报表受理结果待核');
    const task=acceptedArchive(await response.text());
    await save({stage:'ACCEPTED',nativeTaskRef:task,acceptedAt:new Date().toISOString()});
    check(signal);
    const record=await archiveRecord(page,scope,task,signal,onProgress);
    if(tidy(await displayedCompany(page))!==tidy(account.expectedCompany))
      throw scopeError('下载回执企业与本次查询不一致');
    await save({scope:record.scope});
    const options={recordId:task,expectedRows:record.scope.expectedRows,output,signal,onProgress};
    const direct=automatic.get(task);
    let url;
    if(direct?.ok())try{url=downloadResponseUrl(await direct.text());}catch{}
    if(url){
      verifyArchiveAddress(url,record);
      await save({nativeFilePath:url.origin+url.pathname});
    }
    await page.unroute(STATIC,blockCopy);
    const received=url?await receiveArchiveFile(page,url,options):
      await receiveKnownExport(page,{...options,type:TYPE,history:record.history,
        verifyAddress:address=>verifyArchiveAddress(address,record),receiveFile:receiveArchiveFile});
    await save({...received,stage:'DOWNLOADED',nativeFilePath:received.nativePath,completedAt:new Date().toISOString()});
    return manifest;
  }catch(error){
    if(manifest.stage==='SUBMITTING')await save({stage:'SUBMISSION_UNKNOWN'});
    throw error;
  }finally{
    attempt.dispose();page.off('response',capture);
    await page.unroute(submission,guard).catch(()=>{});
    await page.unroute(STATIC,blockCopy).catch(()=>{});
  }
}

export async function recoverArchive(page,{scope,recordId,expectedPath,output,signal,onProgress=()=>{}}){
  if(scope?.kind!==ARCHIVE||!scope.reportWarehouse)throw scopeError('原任务缺少仓库与筛选证据');
  const record=await archiveRecord(page,scope,nativeTaskId(recordId),signal,onProgress);
  const received=await receiveKnownExport(page,{recordId,expectedRows:record.scope.expectedRows,
    expectedPath,output,signal,onProgress,type:TYPE,history:record.history,
    verifyAddress:address=>verifyArchiveAddress(address,record),receiveFile:receiveArchiveFile});
  return {...received,scope:record.scope};
}
