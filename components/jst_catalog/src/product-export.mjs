import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
import {CatalogError,check,sleep,atomic} from './common.mjs';
const execFileAsync=promisify(execFile);

export const COMPANY=null;
const label='商品及库存管理(普通商品资料)';
const clean=s=>String(s).replace(/\s+/g,'');
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export async function openProducts(page,{reload=false,expectedCompany=COMPANY}={}){
  if(reload||!page.url().startsWith('https://www.erp321.com/epaas'))await page.goto('https://www.erp321.com/epaas',{waitUntil:'domcontentloaded',timeout:60000});
  await page.locator('a[class^="menuItem"]').filter({hasText:/^商品$/}).waitFor({timeout:60000});
  await page.waitForFunction(expected=>document.querySelector('#root')?.innerText.replace(/\s+/g,'').includes(expected),clean(expectedCompany),{timeout:30000});
  const body=clean(await page.locator('#root').innerText());
  if(!body.includes(clean(expectedCompany)))throw Error('聚水潭公司与配置不一致，未切换账号或执行导出');
  const tabs=page.getByRole('tab').filter({hasText:label});
  if(await tabs.count()===1)await tabs.click();
  else{
    await page.locator('a[class^="menuItem"]').filter({hasText:/^商品$/}).click();
    const entries=page.locator('div[class^="title"]').filter({hasText:new RegExp('^'+label.replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'$')}).filter({visible:true});
    const n=await entries.count();if(n!==2)throw Error('商品原生入口结构发生变化');
    await entries.nth(1).click();
  }
  await page.locator('#iframe-10').waitFor({state:'visible',timeout:60000});
  let frame;for(let n=0;n<150;n++){
    frame=page.frames().find(f=>f.url().includes('/erp-scm-goods/goodsInventoryManagement'));
    if(frame&&await frame.getByPlaceholder('商品编码',{exact:true}).count())break;
    await wait(200);
  }
  if(!frame)throw Error('商品业务页未就绪');
  await frame.getByPlaceholder('商品编码',{exact:true}).waitFor({timeout:60000});
  await page.getByRole('tab').filter({hasText:label}).click();
  await page.waitForFunction(()=>document.querySelector('#iframe-10')?.closest('[class*="tabContent"]')?.className.includes('current'),null,{timeout:15000});
  return frame;
}
function countsIn(json){
  const found=[];const walk=(x,depth=0)=>{if(!x||typeof x!=='object'||Array.isArray(x)||depth>5)return;
    for(const [key,value] of Object.entries(x)){
      if(/^(count|total|totalCount|total_count|recordCount|recordsCount)$/i.test(key)&&/^\d+$/.test(String(value)))found.push(Number(value));
      else walk(value,depth+1);
    }
  };walk(json);return [...new Set(found)];
}
export async function queryAll(page,options={}){
  const frame=await openProducts(page,options);
  await frame.getByText('重 置',{exact:true}).click();
  let selected=(await frame.getByPlaceholder('商品状态',{exact:true}).inputValue()).split(',').sort();
  if(JSON.stringify(selected)!==JSON.stringify(['启用','备用','禁用'].sort())){
    await frame.getByPlaceholder('商品状态',{exact:true}).click();
    await frame.getByText('全选',{exact:true}).click();
  }
  await frame.waitForFunction(()=>JSON.stringify(document.querySelector('input[placeholder="商品状态"]')?.value.split(',').map(x=>x.trim()).sort())===JSON.stringify(['启用','备用','禁用'].sort()),null,{timeout:10000});
  selected=(await frame.getByPlaceholder('商品状态',{exact:true}).inputValue()).split(',').map(x=>x.trim()).sort();
  if(JSON.stringify(selected)!==JSON.stringify(['启用','备用','禁用'].sort()))throw Error('未选中全部三种商品状态，未查询或导出');
  const responsePending=page.waitForResponse(r=>{
    if(r.request().method()!=='POST'||new URL(r.url()).pathname!=='/webapi/ItemApi/ItemSku/GetPageListV2')return false;
    try{const request=r.request().postDataJSON();return request.page?.pageAction===2&&String(request.data?.enabled).split(',').sort().join(',')==='-1,0,1';}catch{return false;}
  },{timeout:60000});
  // If the click fails or the page closes first, its pending response must not
  // become an unhandled rejection that terminates an otherwise recoverable service.
  responsePending.catch(()=>{});
  await frame.getByRole('button',{name:'搜 索',exact:true}).click();
  const response=await responsePending,json=await response.json(),request=response.request().postDataJSON();
  if(!response.ok()||json.code!==0||json.act!==0)throw Error('全量商品查询未获原生成功响应');
  const counts=countsIn(json);if(counts.length!==1||counts[0]<1){
    const shape=(x,depth=0)=>Array.isArray(x)?{arrayLength:x.length}:x&&typeof x==='object'&&depth<3?Object.fromEntries(Object.entries(x).slice(0,35).map(([k,v])=>[k,shape(v,depth+1)])):typeof x==='number'||typeof x==='boolean'?x:typeof x==='string'?{type:'string',length:x.length,number:/^\d+$/.test(x)?x:undefined}:typeof x;
    throw Error('未唯一识别全量商品计数 '+JSON.stringify({counts,shape:shape(json)}));
  }
  const expectedRows=counts[0];
  await frame.getByText(new RegExp('共\\s*'+expectedRows+'条')).waitFor({timeout:30000});
  const scope={company:options.expectedCompany||COMPANY,coid:String(request.coid),uid:String(request.uid),statuses:['启用','备用','禁用'],expectedRows,query:request.data,queriedAt:new Date().toISOString()};
  if(options.expectedCoid&&scope.coid!==String(options.expectedCoid))throw Error('聚水潭公司ID与绑定不符');
  if(options.expectedUid&&scope.uid!==String(options.expectedUid))throw Error('聚水潭用户ID与账号绑定不符，未提交导出');
  return {frame,scope};
}
export async function openHistory(page){
  await page.locator('div[class^="item"]:has(use[*|href="#icon-download-line"])').click();
  let frame;for(let n=0;n<100;n++){
    frame=page.frames().find(f=>f.url().includes('/app/common/export/export_log.aspx'));
    if(frame&&await frame.locator('#_jt_body_list').count())break;
    await wait(200);
  }
  if(!frame)throw Error('未取得原生下载结果页');
  await frame.locator('#_jt_body_list').waitFor({timeout:30000});return frame;
}
export function nativeTaskId(value){
  if(value&&typeof value==='object'&&!Array.isArray(value)){
    const ids=['taskId','task_id','recordId','id'].filter(k=>value[k]!==undefined).map(k=>nativeTaskId(value[k]));
    if(new Set(ids).size!==1)throw new CatalogError('TASK_ID_UNKNOWN','未取得唯一原生任务号，不能重新提交');
    return ids[0];
  }
  if(!/^[1-9]\d*$/.test(String(value))||(typeof value==='number'&&!Number.isSafeInteger(value)))
    throw new CatalogError('TASK_ID_UNKNOWN','平台未返回规范任务号，保留原提交待核');
  return String(value);
}
export function nativeFileUrl(value){
  try{const u=new URL(value);if(u.protocol==='https:'&&u.hostname==='jstzbstatic.erp321.com'&&/^\/export\/[a-f0-9]+\.xlsx$/.test(u.pathname))return u;}catch{}
  throw new CatalogError('FILE_IDENTITY','任务未提供可核验的原生文件地址，未改取其他文件');
}
export async function receiveKnownExport(page,{recordId,expectedRows,output,expectedPath,signal,onProgress=()=>{},timeout=120000}){
  recordId=nativeTaskId(recordId);if(!Number.isSafeInteger(expectedRows)||expectedRows<1)throw Error('原任务缺少已核对行数');
  check(signal);const history=await openHistory(page);const until=Date.now()+timeout;let row;
  for(;;){
    check(signal);row=history.locator('div._jt_row').filter({has:history.getByText(recordId,{exact:true})});
    const count=await row.count();if(count>1)throw Error('同任务号出现多条记录，未下载');
    if(count===1){
      const text=await row.innerText();
      if(!text.includes('商品资料')||!text.split(/\s+/).includes(String(expectedRows)))throw Error('原任务的类型或条数不符');
      if(text.includes('下载完成'))break;
      if(/失败|已取消/.test(text))throw new CatalogError('PLATFORM_FAILED','原导出任务失败，请核对平台记录');
    }
    if(Date.now()>=until)throw new CatalogError('TASK_PENDING','原导出任务尚未完成，请继续原任务；未重新提交');
    onProgress('WAIT_EXPORT','等待原任务 '+recordId+' 完成');await sleep(1500);check(signal);
    await history.goto(history.url(),{waitUntil:'domcontentloaded',timeout:30000});
  }
  // Bind the address to this exact task row BEFORE observing any download.
  // A same-sized background download must never become our input.
  const attributes=await row.locator('[href],[onclick],[data-url]').evaluateAll(nodes=>nodes.flatMap(e=>['href','onclick','data-url'].map(k=>e.getAttribute(k)||'')));
  const urls=[...new Set(attributes.flatMap(s=>s.match(/https:\/\/jstzbstatic\.erp321\.com\/export\/[a-f0-9]+\.xlsx[^\s"'<>)]*/g)||[]))];
  if(urls.length!==1)throw new CatalogError('FILE_IDENTITY','原任务行未提供唯一文件链接，保留任务待核；未使用最近下载');
  const url=nativeFileUrl(urls[0].replaceAll('&amp;','&'));
  if(expectedPath&&url.pathname!==expectedPath)throw new CatalogError('FILE_IDENTITY','原任务文件身份已变化');
  check(signal);onProgress('DOWNLOADING','正在接收原任务文件');
  const response=await page.context().request.get(url.href,{timeout:60000,maxRedirects:0});
  if(!response.ok())throw Error('原生文件接收 HTTP '+response.status());
  const bytes=await response.body();check(signal);
  if(bytes.length<100||bytes.length>60*1024*1024||bytes.subarray(0,4).toString('hex')!=='504b0304')throw Error('下载不是受支持的XLSX');
  const sha256=crypto.createHash('sha256').update(bytes).digest('hex');await fs.mkdir(output,{recursive:true});
  const file=path.join(output,'聚水潭商品资料_'+recordId+'_'+sha256.slice(0,12)+'.xlsx');
  await fs.writeFile(file,bytes,{flag:'wx'}).catch(async e=>{if(e.code!=='EEXIST')throw e;if(crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex')!==sha256)throw Error('已有文件内容冲突');});
  return {file,recordId,expectedRows,bytes:bytes.length,sha256,nativePath:url.origin+url.pathname,sourceBytesPreserved:true};
}
function observe(page,event,predicate,timeout){
  let listener,timer,finish;
  const promise=new Promise((resolve,reject)=>{
    finish=(error,value)=>{clearTimeout(timer);page.off(event,listener);error?reject(error):resolve(value);};
    listener=value=>{try{if(predicate(value))finish(null,value);}catch(e){finish(e);}};
    page.on(event,listener);timer=setTimeout(()=>finish(Error('等待原生 '+event+' 超时，未重新提交导出')),timeout);
  });
  promise.catch(()=>{});return {promise,dispose(){clearTimeout(timer);page.off(event,listener);}};
}
export function exportScopeValid(body,scope){
  const data=body?.data,s=data?.searchCondition;
  if(String(body?.coid)!==scope.coid||String(body?.uid)!==scope.uid||!s||data.type!==3||data.filterType!==2)return false;
  if(Object.keys(data).some(k=>!['type','searchCondition','filterType'].includes(k)))return false;
  if(String(s.enabled).split(',').sort().join(',')!=='-1,0,1'||s.sku_type!==1)return false;
  const ignore=new Set(['sku_type','enabled','queryFlds','orderBy']);
  return Object.entries(s).every(([k,v])=>ignore.has(k)||v===null||v===undefined||v===''||(Array.isArray(v)&&!v.length));
}
export async function validateProductFile(file,{validator,expectedRows,warehouse,output,signal}){
  check(signal);if(!validator?.executable||!Array.isArray(validator.prefix))throw Error('缺少包内文件校验入口');
  const id=crypto.randomUUID(),request=path.join(output,'validate-'+id+'.request.json'),result=path.join(output,'validate-'+id+'.result.json');
  const sha256=crypto.createHash('sha256').update(await fs.readFile(file)).digest('hex');
  await atomic(request,{schema:1,file,sha256,warehouse,expected_rows:expectedRows,candidate:path.join(output,'candidate-'+id+'.json')});
  let failed;
  try{await execFileAsync(validator.executable,[...validator.prefix,'--request',request,'--result',result],{windowsHide:true,timeout:120000,maxBuffer:65536,signal});}catch(e){failed=e;}
  check(signal);const receipt=await fs.readFile(result,'utf8').then(JSON.parse).catch(()=>null);
  if(failed||receipt?.status!=='VALIDATED')throw new CatalogError('VALIDATION_FAILED',receipt?.error||'包内校验未成功，未采用文件');
  return receipt;
}
export async function exportAll(page,{output,onManifest,signal,onProgress=()=>{},queried,...options}){
  if(!output)throw Error('缺少本次归档目录');
  await fs.mkdir(output,{recursive:true});
  let manifest={schema:1,stage:'QUEUED',submissions:0,createdAt:new Date().toISOString()};
  const save=async patch=>{manifest={...manifest,...patch};await onManifest(structuredClone(manifest));};
  const endpoint='**/erp/webapi/ItemApi/Export/ItemSkuAndInventory';let scope,installed=false,observation;
  const guard=async route=>{
    const request=route.request();let body;try{body=request.postDataJSON();}catch{}
    if(signal?.aborted){await save({stage:'CANCELLED_BEFORE_SUBMIT',submissions:0});await route.abort('blockedbyclient');return;}
    if(manifest.submissions||request.method()!=='POST'||!exportScopeValid(body,scope)){
      await save({stage:manifest.submissions?'SUBMISSION_UNKNOWN':'NOT_SUBMITTED_SCOPE_MISMATCH'});await route.abort('blockedbyclient');return;
    }
    await save({stage:'SUBMITTING',submissions:1,nativeExportScope:body.data});
    // The durable intent precedes dispatch. Stop is acknowledged but never kills this boundary.
    await route.continue();
  };
  try{
    check(signal);const found=queried||await queryAll(page,options);scope=found.scope;
    await save({stage:'INTENT_RECORDED',scope});check(signal);
    observation=observe(page,'response',r=>new URL(r.url()).pathname==='/erp/webapi/ItemApi/Export/ItemSkuAndInventory'&&r.request().method()==='POST',60000);
    await page.route(endpoint,guard);installed=true;
    onProgress('SUBMITTING','正在提交一次导出；此时停止将等待受理结果');
    await found.frame.getByText('导出',{exact:true}).click();check(signal);
    await found.frame.getByText('导出商品',{exact:true}).click();
    const response=await observation.promise,json=await response.json();
    if(!response.ok()||json.code!==0||json.act!==0){await save({stage:'FAILED_CONFIRMED'});throw new CatalogError('PLATFORM_REJECTED','平台明确拒绝本次导出');}
    const task=nativeTaskId(json.data);await save({stage:'ACCEPTED',nativeTaskRef:task,acceptedAt:new Date().toISOString()});check(signal);
    const received=await receiveKnownExport(page,{recordId:task,expectedRows:scope.expectedRows,output,signal,onProgress});
    await save({stage:'DOWNLOADED',...received,nativeFilePath:received.nativePath,completedAt:new Date().toISOString()});return manifest;
  }catch(e){
    if(manifest.stage==='SUBMITTING')await save({stage:'SUBMISSION_UNKNOWN'});
    throw e;
  }finally{observation?.dispose();if(installed)await page.unroute(endpoint,guard);}
}
