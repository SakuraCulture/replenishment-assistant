import {OrderPages} from './order-import.mjs';
import {activateControl} from './product-export.mjs';
import {CatalogError,check} from './common.mjs';
const PATH='/app/wms/saleout/saleout.aspx';
const ORIGIN='https://www.erp321.com';
const DOWNLOAD_PATH='/app/wms/saleout/ExportSaleoutV2.aspx';
const fail=message=>{throw new CatalogError('ALLOCATION_SOURCE',message);};
const same=(a,b)=>JSON.stringify(a)===JSON.stringify(b);
const sorted=values=>[...values].sort();
function callback(request,method){
  try{
    const url=new URL(request.url()),form=new URLSearchParams(request.postData());
    const value=JSON.parse(form.get('__CALLBACKPARAM'));
    if(request.method()==='POST'&&url.origin===ORIGIN&&url.pathname===PATH&&value.Method===method)return value;
  }catch{}
  return null;
}
export function saleoutQuery(request,spec){
  const value=callback(request,'LoadDataToJSON');
  try{const filters=JSON.parse(value.Args[1]);return filters.length===1&&filters[0].k==='o_id'
    &&String(filters[0].v)===spec.internal&&filters[0].c==='@=';}catch{return false;}
}
export function saleoutExport(request,spec,ids){
  const value=callback(request,'ExportSaleOut');
  try{const p=JSON.parse(value.Args[0]),filters=JSON.parse(p.Filter);
    return p.Flag===1&&p.IsExportNoTask===true&&p.CheckoutAfterSend===false&&p.IsExportSonInouts===false
      &&same(sorted(p.CheckIoIds.map(String)),sorted(ids))&&filters.length===1
      &&filters[0].k==='o_id'&&String(filters[0].v)===spec.internal&&filters[0].c==='@=';
  }catch{return false;}
}
async function decoded(response){
  if(!response.ok())fail('原生出库回执未成功');
  const body=await response.text(),separator=body.indexOf('|');
  if(separator<1||!/^\d+$/.test(body.slice(0,separator)))fail('原生出库回执格式变化');
  const value=JSON.parse(body.slice(separator+1));
  if(value.IsSuccess!==true||value.GotoLogin||value.ExceptionMessage)fail('原生出库查询或导出未获成功回执');
  return value.ReturnValue;
}
export class SaleoutPages{
  constructor(){this.orders=new OrderPages();this.liveContractVerified=true;}
  lookup(page,spec,signal){return this.orders.lookup(page,spec,signal,false,true);}
  async query(page,spec,signal){
    check(signal);
    await page.goto(ORIGIN+PATH+'?_c=jst-epaas&epaas=true',{waitUntil:'domcontentloaded',timeout:30000});
    await page.locator('#id').waitFor({state:'visible',timeout:15000});
    const company=(await page.locator('#partnerChange').innerText()).replace(/\s+/g,'');
    if(company!==spec.binding.company.replace(/\s+/g,''))fail('出库页面企业与原仓不同');
    await activateControl(page.locator('#ul_search [onclick="SearchReset()"]'));
    await page.locator('#id').fill(spec.internal);
    const reply=page.waitForResponse(r=>saleoutQuery(r.request(),spec),{timeout:20000});reply.catch(()=>{});
    // Native Enter intentionally removes the default date and other filters.
    await page.locator('#id').press('Enter');
    const data=JSON.parse(await decoded(await reply.catch(()=>fail('未收到仅按原内部单号的完整出库查询'))));
    if(!Array.isArray(data.datas)||data.dp?.PageIndex!==1||data.dp?.IsLast!==true)fail('出库查询未覆盖全部分页');
    const all=data.datas.map(r=>({io_id:String(r.io_id),internal:String(r.o_id),online:r.so_id,
      shop:r.shop_name,shop_id:String(r.shop_id),coid:String(r.co_id),status:r.status}));
    if(new Set(all.map(r=>r.io_id)).size!==all.length||all.some(r=>!/^[1-9]\d*$/.test(r.io_id)||r.internal!==spec.internal
      ||r.online!==spec.online||r.shop!==spec.shop||r.shop_id!==spec.binding.shop_id||r.coid!==spec.binding.coid))
      fail('原单出库清单存在错单、重复、错店或错企业');
    if(all.some(r=>!['已出库','出库前作废'].includes(r.status)))fail('原单仍有未完成、出库后取消或状态不明的出库单');
    const documents=all.filter(r=>r.status==='已出库');
    if(!documents.length)fail('原单没有有效的已出库单；作废单不作实配');
    await page.waitForFunction(ids=>{
      const actual=[...document.querySelectorAll('._jt_row')].map(r=>r.querySelector('._jt_cell_io_id a')?.textContent.trim()).sort();
      return JSON.stringify(actual)===JSON.stringify([...ids].sort());
    },all.map(r=>r.io_id),{polling:100,timeout:10000});
    return documents;
  }
  async receive(page,native,signal){
    const url=new URL(native.url);
    if(url.origin!==ORIGIN||url.pathname!==DOWNLOAD_PATH||url.username||url.password||!url.searchParams.get('s'))
      fail('原生下载回执的目标地址不符');
    check(signal);
    const pending=page.waitForEvent('download',{predicate:d=>d.url()===url.href,timeout:45000});pending.catch(()=>{});
    // Follow the accepted export reference. Do not click Export a second time.
    await page.goto(url.href,{waitUntil:'domcontentloaded',timeout:45000}).catch(()=>{});
    const download=await pending.catch(()=>{throw new CatalogError('ALLOCATION_DOWNLOAD_UNKNOWN','原导出回执已保留，文件尚未收到；未重新提交导出');});
    return this.readDownload(download,native.evidence,signal);
  }
  async readDownload(download,evidence,signal){
    const stream=await download.createReadStream();
    if(!stream)fail('原导出没有可读取的文件流');
    const chunks=[];let size=0;
    for await(const chunk of stream){check(signal);size+=chunk.length;if(size>32*1024*1024){stream.destroy();fail('原单文件超过32MB');}chunks.push(chunk);}
    return {raw:Buffer.concat(chunks),evidence};
  }
  async download(page,observed,spec,signal,beforeDispatch){
    const documents=await this.query(page,spec,signal),ids=documents.map(r=>r.io_id);
    for(const doc of documents){
      const row=page.locator('._jt_row').filter({has:page.locator('a.io_id_click_bind').filter({hasText:new RegExp('^'+doc.io_id+'$')})});
      if(await row.count()!==1)fail('出库明细选择行不唯一');
      await row.locator('._jt_cell_checked input._jt_cbx').check({force:true,timeout:8000});
    }
    const state=await page.evaluate(()=>{
      if(typeof GetCheckIoIds!=='function'||typeof $==='undefined'||typeof $.cbx!=='function'||typeof jTable==='undefined')return null;
      return {ids:GetCheckIoIds().map(String),filters:jTable.Filter,checkout:$.cbx('checkoutAfterSend'),
        company:document.querySelector('#partnerChange')?.innerText};
    });
    if(!state||!same(sorted(state.ids),sorted(ids))||state.checkout!==false
        ||state.company?.replace(/\s+/g,'')!==spec.binding.company.replace(/\s+/g,'')
        ||state.filters?.length!==1||state.filters[0].k!=='o_id'||String(state.filters[0].v)!==spec.internal||state.filters[0].c!=='@=')
      fail('导出前企业、勾选范围或出库导出选项不符');
    const control=page.locator('#ExportBtn_Btn ._db_item').filter({hasText:/^导出勾选的单据\(导出明细\)$/});
    if(await control.count()!==1)fail('未找到唯一原生出库明细出口');
    const exportPage=url=>url.origin===ORIGIN&&url.pathname===PATH;
    let authorized=false,dispatched=false;
    const enforce=async route=>{
      const request=route.request();
      if(!callback(request,'ExportSaleOut')) return route.fallback();
      if(!authorized||dispatched||signal?.aborted||!saleoutExport(request,spec,ids)) return route.abort();
      dispatched=true;
      return route.fallback();
    };
    await page.route(exportPage,enforce);
    const downloads=[];
    const collect=download=>downloads.push(download);
    page.on('download',collect);
    try {
      // The native dropdown is hover-driven; only this exact menu item is dispatched.
      await beforeDispatch();check(signal);
      const pending=page.waitForResponse(r=>saleoutExport(r.request(),spec,ids),{timeout:25000});pending.catch(()=>{});
      authorized=true;
      await control.dispatchEvent('click',{bubbles:true,cancelable:true});
      const reply=await pending.catch(()=>{throw new CatalogError('ALLOCATION_DOWNLOAD_UNKNOWN','导出受理状态待核，保留单次意图；未重复导出');});
      return await this.accept(page,await decoded(reply),observed,spec,documents,downloads,beforeDispatch,signal);
    } finally {
      page.off('download',collect);
      await page.unroute(exportPage,enforce);
    }
  }
  async accept(page,condition,observed,spec,documents,downloads,beforeDispatch,signal){
    if(typeof condition!=='string'||!condition||condition.length>2048)fail('原生导出结果不是已验证的同步文件回执');
    const href=await page.evaluate(condition=>{
      const thirdParty=document.querySelector('#is_tpw_wms')?.value==='1'?'&is_tpw_wms=1':'';
      return __AppendCoid('ExportSaleoutV2.aspx?s='+$.escape(condition)+thirdParty);
    },condition);
    const url=new URL(href,page.url());
    if(url.origin!==ORIGIN||url.pathname!==DOWNLOAD_PATH
        ||['owner_co_id','authorize_co_id'].some(k=>url.searchParams.has(k)&&url.searchParams.get(k)!==spec.binding.coid))
      fail('原生下载回执的企业范围与原仓不同');
    const native={url:url.href,evidence:{source_kind:'jst_saleout_detail_v1',outbounds:documents,
      order_facts:{internal:observed.internal,online:observed.online,shop:observed.shop,shop_id:observed.shop_id,
        coid:observed.coid,native_status:observed.native_status,amount:observed.amount,freight:observed.freight,
        items:observed.items,observed:observed.observed}}};
    await beforeDispatch.acknowledge(native);
    const existing=downloads.find(d=>d.url()===url.href)
      ||await page.waitForEvent('download',{predicate:d=>d.url()===url.href,timeout:3000}).catch(()=>null);
    return existing?this.readDownload(existing,native.evidence,signal):this.receive(page,native,signal);
  }
}
