import fs from 'node:fs/promises';
import path from 'node:path';
import {fail,now,sha256,uid} from './common.mjs';

const ORIGIN='https://sc-gei-tc-supply-chain-portal.ele.me';
const PREFIX='/gei/import/template/';
export function purchaseTemplateSource(value){
  const u=new URL(value),code=decodeURIComponent(u.pathname.slice(PREFIX.length));
  if(u.origin!==ORIGIN||!u.pathname.startsWith(PREFIX)||u.search||u.hash||u.username||u.password||
    !/^[A-Z0-9_-]+$/.test(code)||!code.includes('PURCHASE-ORDER')||!code.includes('IMPORT'))
    fail('PURCHASE_TEMPLATE_SOURCE','平台模板入口发生变化，需要复核下载来源');
  return {origin:u.origin,path:u.pathname,taskCode:code};
}

// Only the native template button is used. No file is selected and no import
// confirmation is clicked. The observed GET is saved with this context's auth;
// Edge's global download behaviour and existing login are never changed.
export async function downloadNativePurchaseTemplate(ui,{directory,expectedIdentity,timeoutMs=60000}={}){
  if(!directory||!expectedIdentity?.accountId||!expectedIdentity?.merchantCode)fail('PURCHASE_TEMPLATE_INPUT','获取模板需要输出目录及已核对的商家身份');
  const page=ui.page,hasFile=()=>page.locator('input[type=file]').evaluateAll(ns=>ns.some(n=>n.files?.length));
  if(await hasFile())fail('IMPORT_FILE_PENDING','当前作业页已有待导入文件，停止获取模板以保留现场');
  let modal,blocked=0;
  const guard=async route=>{
    const r=route.request(),p=new URL(r.url()).pathname;
    if((/\/gei\/import\/task(?:\/|$)/.test(p)&&!['GET','HEAD','OPTIONS'].includes(r.method()))||/\/gei\/create\/import\/task(?:\/|$)/.test(p)){
      blocked++;await route.abort('blockedbyclient');
    }else await route.fallback();
  };
  await page.route('**/*',guard);
  try{
    await ui.open(expectedIdentity);
    await ui.click(page.getByRole('button',{name:'Excel批量导入创建',exact:true}).filter({visible:true}));
    modal=page.locator('.next-dialog').filter({hasText:'下载导入模板'}).filter({visible:true});
    await modal.waitFor({state:'visible',timeout:ui.timeoutMs});
    if(await modal.count()!==1||await hasFile())fail('PURCHASE_TEMPLATE_DIALOG','导入模板窗口不唯一或已有文件被选中');
    const description=await modal.innerText(),limit=/每次添加的商品数量不能超过\s*([\d,]+)\s*个/.exec(description);
    const sent=page.waitForRequest(r=>{
      try{const u=new URL(r.url());return r.method()==='GET'&&u.origin===ORIGIN&&u.pathname.startsWith(PREFIX);}catch{return false;}
    },{timeout:ui.timeoutMs});sent.catch(()=>{});
    await ui.click(modal.getByRole('button',{name:'下载导入模板',exact:true}));
    const request=await sent,source=purchaseTemplateSource(request.url());
    const response=await page.context().request.get(request.url(),{maxRedirects:0,timeout:timeoutMs});
    try{
      if(response.status()!==200)fail('PURCHASE_TEMPLATE_HTTP','最新采购导入模板下载失败，HTTP '+response.status());
      const maxBytes=10485760,declared=response.headers()['content-length'];
      if(declared&&(!/^\d+$/.test(declared)||Number(declared)>maxBytes))fail('PURCHASE_TEMPLATE_SIZE','模板超过10MiB工程保护值');
      const body=await response.body();
      if(body.length<100||body.length>maxBytes||body.subarray(0,4).toString('hex')!=='504b0304')fail('PURCHASE_TEMPLATE_NOT_XLSX','平台未返回有效XLSX模板；请核对登录及权限');
      if(declared&&Number(declared)!==body.length)fail('PURCHASE_TEMPLATE_TRUNCATED','模板下载字节数不完整');
      if(blocked||await hasFile())fail('PURCHASE_TEMPLATE_SIDE_EFFECT','取模板期间出现导入动作或文件选择，停止后续准备');
      await fs.mkdir(directory,{recursive:true});
      const file=path.join(directory,uid()+'.xlsx'),h=await fs.open(file,'wx');
      try{await h.writeFile(body);await h.sync();}finally{await h.close();}
      return {file,bytes:body.length,sha256:sha256(body),identity:{accountId:ui.identity.accountId,merchantCode:ui.identity.merchantCode},source,
        uiProductLimit:limit?Number(limit[1].replaceAll(',','')):null,checkedAt:now(),transport:'native_template_button_observed_get_browser_context',importSubmissions:0};
    }finally{await response.dispose();}
  }finally{
    // Never dismiss a user-selected file. Cancel is safe only for this empty
    // native dialog; do not touch a notice or another same-named button.
    try{if(modal&&await modal.count()&&!await hasFile()){await ui.click(modal.getByRole('button',{name:'取消',exact:true}));await modal.waitFor({state:'hidden',timeout:5000});}}catch{}
    await page.unroute('**/*',guard);
  }
}
