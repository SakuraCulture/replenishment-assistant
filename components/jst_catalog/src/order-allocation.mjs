import fs from 'node:fs/promises';
import path from 'node:path';
import {CatalogError, canonical, check, now, sha, sleep, atomic, inside} from './common.mjs';
import {activateControl} from './product-export.mjs';
import {OrderPages} from './order-import.mjs';

const fail=(code,message)=>{throw new CatalogError(code,message);};
const same=(a,b)=>canonical(a)===canonical(b);

export function allocationDisposition(status) {
  if (status==='已取消'||status==='取消') return ['CANCELLED','聚水潭原单已取消，不能作为实配或付款依据'];
  if (status==='被合并'||status==='被拆分') return ['SOURCE_UNVERIFIED','原单已合并或拆分，需先核实完整子单，未拼接付款'];
  if (status!=='已发货') return ['WAIT_ALLOCATION','原单尚未提供最终实发明细，保留原单等待配货完成'];
  return null;
}

export function exportQueryMatches(request, frameUrl, spec) {
  try {
    const actual=new URL(request.url()),expected=new URL(frameUrl);
    if (request.method()!=='POST'||actual.origin!==expected.origin||actual.pathname!==expected.pathname) return false;
    const form=new URLSearchParams(request.postData());
    const callback=JSON.parse(form.get('__CALLBACKPARAM'));
    const filters=JSON.parse(callback.Args[1]);
    return form.get('__CALLBACKID')==='JTable1'&&callback.Method==='LoadDataToJSON'
      &&Array.isArray(filters)&&filters.length===2
      &&filters.some(f=>f.k==='o_id'&&f.v===spec.internal&&f.c==='@=')
      &&filters.some(f=>f.k==='so_id'&&f.v===spec.online&&f.c==='@=');
  } catch {return false;}
}

export async function completeExportReply(response) {
  if (!response.ok()) fail('ALLOCATION_SCOPE','未取得本次原单的成功查询响应');
  try {
    const body=await response.text(),separator=body.indexOf('|');
    if (separator<1||!/^\d+$/.test(body.slice(0,separator))) throw Error();
    const reply=JSON.parse(body.slice(separator+1)),result=JSON.parse(reply.ReturnValue);
    if (reply.IsSuccess!==true||reply.GotoLogin||!Array.isArray(result.datas)||result.datas.length!==1
        ||result.dp?.PageIndex!==1||result.dp?.IsLast!==true) throw Error();
  } catch {fail('ALLOCATION_SCOPE','原单导出查询不是完整唯一结果，未点击导出');}
}

export class AllocationPages {
  constructor(orderPages=new OrderPages(),allowed=url=>{
    try {const loc=new URL(url);return loc.protocol==='https:'&&/(^|\.)erp321\.com$/.test(loc.hostname);} catch {return false;}
  }) {this.orders=orderPages;this.allowed=allowed;this.liveContractVerified=false;}
  async lookup(page,spec,signal) {return this.orders.lookup(page,spec,signal,false);}
  async exportFrame(page,observed,signal) {
    await activateControl(observed.frame.locator('#ExportOrder'));
    const end=Date.now()+30000;
    while (Date.now()<end) {
      check(signal);
      const found=[];
      for (const frame of page.frames()) {
        try {
          if (!this.allowed(frame.url())) continue;
          if (await frame.locator('#Tool_New_Btn > .ding_db_txt').count()===1
              &&await frame.locator('#s_filter_frame').count()===1
              &&await (await frame.frameElement()).isVisible()) found.push(frame);
        } catch (error) {if (!frame.isDetached()) throw error;}
      }
      if (found.length>1) fail('ALLOCATION_SCOPE','存在多个原生导出页面，停止操作');
      if (found.length===1) return found[0];
      await sleep(100);
    }
    fail('ALLOCATION_SCOPE','尚未找到原生订单导出入口');
  }
  async assertExport(frame,spec,signal,page) {
    check(signal);
    if (spec.binding?.company&&page) {
      const banner=(await page.locator('#root').innerText()).replace(/\s+/g,'');
      if (!banner.includes(spec.binding.company.replace(/\s+/g,'')))
        fail('ACCOUNT_CHANGED','实配导出期间企业已变化，停止导出');
    }
    const identity=await frame.locator('._jt_row').evaluateAll(rows=>rows.map(row=>{
      const text=selector=>(row.querySelector(selector)?.innerText||'').trim();
      return {internal:text('._jt_cell_o_id'),online:text('._jt_cell_so_id'),
        shop:text('._jt_cell_shop_name'),status:text('._jt_cell_status')};
    }));
    const pager=await frame.locator('#_jt_pagebar').innerText();
    if (!/总共\s*1\s*页\s*1\s*条记录/.test(pager)||identity.length!==1
        ||!same(identity[0],{internal:spec.internal,online:spec.online,shop:spec.shop,status:'已发货'})
        ||await frame.locator('input[type=checkbox]:checked').count()!==0)
      fail('ALLOCATION_SCOPE','导出前的原单、店铺、已发货状态或勾选范围变化，未导出');
  }
  async download(page,observed,spec,signal,beforeDispatch) {
    const frame=await this.exportFrame(page,observed,signal),filters=frame.frameLocator('#s_filter_frame');
    await activateControl(filters.locator('span[onclick="ClearSearch()"]').filter({hasText:/^清空/}));
    await filters.locator('input#o_id').fill(spec.internal);
    await filters.locator('input#so_id').fill(spec.online);
    const response=page.waitForResponse(r=>exportQueryMatches(r.request(),frame.url(),spec),{timeout:25000});
    response.catch(()=>{});
    await activateControl(filters.locator('span.btn_search[onclick="FullSearch()"]'),{beforeDispatch:async()=>{
      if (await filters.locator('input#o_id').inputValue()!==spec.internal
          ||await filters.locator('input#so_id').inputValue()!==spec.online)
        fail('ALLOCATION_SCOPE','原生导出筛选已变化');
      check(signal);
    }});
    await completeExportReply(await response.catch(()=>fail('ALLOCATION_SCOPE','未收到原单导出查询响应')));
    await frame.waitForFunction(spec=>{
      const rows=[...document.querySelectorAll('._jt_row')];
      return rows.length===1&&rows[0].querySelector('._jt_cell_o_id')?.innerText.trim()===spec.internal
        &&rows[0].querySelector('._jt_cell_so_id')?.innerText.trim()===spec.online;
    },spec,{timeout:10000,polling:100});
    await this.assertExport(frame,spec,signal,page);
    const download=page.waitForEvent('download',{timeout:60000});
    download.catch(()=>{});
    await activateControl(frame.locator('#Tool_New_Btn > .ding_db_txt').filter({hasText:/^导出所有符合条件的订单$/}),{
      beforeDispatch:async()=>{await this.assertExport(frame,spec,signal,page);await beforeDispatch();check(signal);},
    });
    const result=await download.catch(()=>fail('ALLOCATION_DOWNLOAD_UNKNOWN','已记录本次原生导出意图，下载结果待核；不重发导出'));
    const stream=await result.createReadStream();
    if (!stream) fail('ALLOCATION_DOWNLOAD_UNKNOWN','原生导出未取得文件流，请核对原导出');
    const parts=[];let bytes=0;
    for await (const part of stream) {
      check(signal);bytes+=part.length;
      if (bytes>32*1024*1024) {stream.destroy();fail('ALLOCATION_FILE','原单实配文件超过32MB，停止整单回流');}
      parts.push(part);
    }
    await this.assertExport(frame,spec,signal,page);
    return Buffer.concat(parts);
  }
}

export class AllocationFlow {
  constructor(driver,{store,root,pages=new AllocationPages()}={}) {Object.assign(this,{driver,store,root,pages});}
  async accountPage(account,expected,signal,retainedOnly) {
    const page=await this.driver.page(account,signal,{retainedOnly}),{scope:identity}=await this.driver.query(page,account);
    if (!same([identity.company,identity.coid,identity.uid],[expected.company,expected.coid,expected.uid]))
      fail('ACCOUNT_CHANGED','实配查询中的登录身份与原仓账号不同');
    return page;
  }
  async ready(receipt,id,scope,expected,spec) {
    const file=path.join(this.root,receipt.relative||'');
    if (receipt.scope!==scope||!inside(path.join(this.root,'allocation',id),file)
        ||!same(receipt.result?.binding,expected)||receipt.result?.online!==spec.online||receipt.result?.internal!==spec.internal)
      fail('ALLOCATION_FILE','原导出完成凭据的订单或归档范围不一致');
    if (sha(await fs.readFile(file))!==receipt.result.sha) fail('ALLOCATION_FILE','原导出原件摘要变化，未重发导出');
    return {...receipt.result,path:file,reused:true};
  }
  async markReady(id,receipt) {
    await this.store.write(data=>{
      const op=data.allocationOperations[id];
      Object.assign(op,receipt,{state:'FILE_READY'});
      delete op.native;
    });
  }
  async finish(id,scope,common,received,signal) {
    check(signal);
    const raw=Buffer.isBuffer(received)?received:received.raw;
    if (!raw.length) fail('ALLOCATION_DOWNLOAD_UNKNOWN','原生文件为空，保留原导出意图');
    const relative=path.join('allocation',id,raw.subarray(0,2).toString()==='PK'?'原生实配.xlsx':'原生实配.xls');
    const file=path.join(this.root,relative);
    await fs.mkdir(path.dirname(file),{recursive:true});
    // The file can be recreated from the exact accepted reference until the receipt is durable.
    const handle=await fs.open(file,'w');
    try {await handle.writeFile(raw);await handle.sync();} finally {await handle.close();}
    const result={...received.evidence,...common,state:'FILE_READY',complete:true,sha:sha(raw),operation_id:id};
    const receipt={scope,relative,result};
    await atomic(path.join(this.root,'allocation',id,'receipt.json'),receipt);
    await this.markReady(id,receipt);
    return {...result,path:file};
  }
  async run(input,account,signal) {
    check(signal);
    const spec=input.spec,id=input.operation_id;
    const retainedOnly=input.retained_only===true;
    if (!/^[a-f0-9]{32}$/.test(id||'')||!/^BH[A-F0-9]{24}$/.test(spec?.online||'')
        ||!/^\d+$/.test(spec?.internal||'')||spec.warehouse!==account.warehouse||!spec.contact
        ||!spec.binding||spec.binding.shop!==spec.shop)
      fail('ALLOCATION_SCOPE','实配回流缺少原单及冻结的仓店关联');
    const expected={warehouse:account.warehouse,shop:spec.shop,company:account.expectedCompany,
      coid:account.expectedCoid,uid:account.expectedUid,version:account.bindingVersion,shop_id:spec.binding.shop_id};
    if (!same(spec.binding,expected)) fail('ACCOUNT_CHANGED','原单企业、用户或仓账号绑定已变化');
    const scope=sha(canonical([expected,spec.online,spec.internal]));
    const orderKey=sha(canonical([expected.warehouse,expected.coid,expected.uid,expected.shop_id,spec.online,spec.internal]));
    const operations=this.store.read().allocationOperations||{};
    const original=operations[id];
    if (original&&original.scope!==scope) fail('ALLOCATION_SCOPE','原回流编号的订单范围已变化');
    const matching=Object.entries(operations).filter(([,op])=>op.scope===scope);
    const completed=matching.find(([,op])=>op.state==='FILE_READY');
    if (completed) return this.ready(completed[1],completed[0],scope,expected,spec);
    const pending=matching.filter(([,op])=>op.state==='DISPATCHING');
    if (pending.length>1) fail('ALLOCATION_DOWNLOAD_UNKNOWN','该原单有多份未决导出，需核对原导出范围');
    if (pending.length===1) {
      const [key,op]=pending[0];
      let receipt;
      try {receipt=JSON.parse(await fs.readFile(path.join(this.root,'allocation',key,'receipt.json'),'utf8'));}
      catch(error) {if(error.code!=='ENOENT')throw error;}
      if(receipt) {
        const result=await this.ready(receipt,key,scope,expected,spec);
        await this.markReady(key,receipt);
        return result;
      }
      if (op.native&&this.pages.receive) {
        const page=await this.accountPage(account,expected,signal,retainedOnly);
        const received=await this.pages.receive(page,op.native,signal);
        const common={online:spec.online,internal:spec.internal,binding:expected,native_status:'已发货'};
        return {...await this.finish(key,scope,common,received,signal),reused:true};
      }
    }
    if (pending.length||Object.values(operations).some(op=>op.orderKey===orderKey&&op.state==='DISPATCHING'))
      fail('ALLOCATION_DOWNLOAD_UNKNOWN','该原单已有未决导出，必须先核对原导出；未重发');
    const page=await this.accountPage(account,expected,signal,retainedOnly);
    const observed=await this.pages.lookup(page,spec,signal);
    if (!observed.found||observed.internal!==spec.internal||observed.online!==spec.online||observed.shop!==spec.shop)
      fail('ALLOCATION_SCOPE','没有查到企业及店铺内唯一原订单');
    const common={online:spec.online,internal:spec.internal,binding:expected,native_status:observed.native_status};
    const disposition=allocationDisposition(observed.native_status);
    if (disposition) return {...common,state:disposition[0],message:disposition[1]};
    if (this.pages.liveContractVerified!==true)
      return {...common,state:'SOURCE_UNVERIFIED',message:'实配原生出口及数量口径尚未现场验通，当前仅保留原单状态，未提交实配导出'};
    const beforeDispatch=async()=>{
      check(signal);
      await this.store.write(data=>{
        data.allocationOperations||={};
        data.allocationOperations[id]={scope,orderKey,state:'DISPATCHING',online:spec.online,internal:spec.internal,at:now()};
      });
    };
    beforeDispatch.acknowledge=async native=>{
      await this.store.write(data=>{data.allocationOperations[id].native=native;});
    };
    const received=await this.pages.download(page,observed,spec,signal,beforeDispatch);
    return this.finish(id,scope,common,received,signal);
  }
}
