// Exercise the order adapter against local DOM pages, without a real account.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {OrderFlow, OrderPages} from '../src/order-import.mjs';
import {sha} from '../src/common.mjs';

const root=fileURLToPath(new URL('../../aoxiang/package.json',import.meta.url));
const require=createRequire(root);
const {chromium}=require('playwright-core');
const edge=process.env.JST_TEST_EDGE||path.join(process.env['ProgramFiles(x86)']||'/missing','Microsoft/Edge/Application/msedge.exe');
const online='BH'+'A'.repeat(24), wh='河北仓', shop='河北合成店铺';
const file=Buffer.from('isolated file fixture');
const contact={buyer:'00010',recipient:'合成收货人',phone:'00000000000',province:'河北省',city:'石家庄市',district:'桥西区',address:'测试路1号'};
const spec={warehouse:wh,shop,online,file:file.toString('base64'),sha:sha(file),contact,lines:[['00001','10']],
  preview_names:{'00001':'合成_x00B0_商品'}};
const formShop = '<span id="shopOrDrpFroms"><input id="shopOrDrpFrom_v" type="hidden"><input id="shopOrDrpFrom" onclick="shops.hidden=false">'+
  '<div id="shops" class="_cbb_items" hidden><div class="_cbb_item"><label class="_cbb_label" value="103">'+shop+'分店</label></div>'+
  '<div class="_cbb_item"><label class="_cbb_label" value="102" onclick="selectShop(this)">'+shop+'</label></div></div></span>';
const fields = Array(20).fill('');
fields[2]=online;
fields[5]=contact.buyer;
fields[7]='已付款待审核';
fields[11]=contact.recipient+' ['+contact.phone+'] '+contact.province+contact.city+contact.district+contact.address;
fields[12]='现场取货';
fields[13]='<span>合成°商品 * 10</span><br>';
fields[17]='￥20.00 ￥0.00';
const confirmation='<span class="btnImport" onclick="Confirm()">确定导入</span>';
const listPath='/app/order/order/list.aspx';
const pages = {
  '/': '<button id="listTab" role="tab" onclick="selectTab(true)" aria-selected="false">订单</button>'+
    '<button id="importTab" role="tab" onclick="selectTab(false)" aria-selected="true">导入订单</button>'+
    '<iframe id="listing" src="'+listPath+'" hidden></iframe>'+
    '<iframe id="upload" src="/app/order/import/import.aspx"></iframe><iframe id="epaasDialogFrame" src="/empty" hidden></iframe>'+
    '<script>window.statusValue="";window.importCount=0;window.auditCount=0;window.otherAction=0;'+
    'function selectTab(list){listing.hidden=!list;upload.hidden=list;listTab.setAttribute("aria-selected",String(list));'+
    'importTab.setAttribute("aria-selected",String(!list));'+
    'if(!list&&upload.contentWindow.location.pathname.endsWith("/import_show.aspx"))upload.src="/app/order/import/import.aspx";}</script>',
  '/app/order/import/import.aspx': '<form id="form1" action="import_show.aspx" method="post" enctype="multipart/form-data">'+
    '<label><input id="importbyshop" type="radio" name="mode" checked>按店铺导入</label>'+formShop+
    '<label><input id="MultiCommodity" type="radio">多商品</label><label><input id="sku_price" type="checkbox">取基本售价</label>'+
    '<label><input id="default_pay_date" type="checkbox">填付款时间</label><input type="file" id="order" name="order">'+
    '<span id="btnImport" onclick="form1.submit()">确认导入订单</span></form>'+
    '<script>function selectShop(e){shopOrDrpFrom.value=e.textContent;shopOrDrpFrom_v.value=e.getAttribute("value");shops.hidden=true;}</script>',
  '/app/order/import/import_show.aspx': '<form id="form1" action="./import_show.aspx" method="post">'+
    '<input type="hidden" id="txt_exception_datas" value="">'+confirmation+
    '<table><tr>'+fields.map(value=>'<td>'+value+'</td>').join('')+'</tr></table>'+confirmation+'</form>'+
    '<script>function Confirm(){parent.importCount++;parent.statusValue="waitconfirm";'+
    'parent.epaasDialogFrame.src="/receipt";parent.epaasDialogFrame.hidden=false;}</script>',
  '/receipt': '<div>成功条数：1，失败条数：0</div><a href="#" onclick="parent.epaasDialogFrame.hidden=true">关闭</a>',
  '/empty': '<div></div>',
  '/filters': '<input id="so_id">'+
    '<input type="checkbox" id="status_waitconfirm" hidden><label for="status_waitconfirm">已付款待审核</label>'+
    '<input type="checkbox" id="status_waitfconfirm" hidden><label for="status_waitfconfirm">已客审待财审</label>'+
    '<input type="checkbox" id="status_delivering" hidden><label for="status_delivering">发货中(打单拣货)</label>'+
    '<input type="checkbox" id="status_sent" hidden><label for="status_sent">已发货</label>'+
    '<span onclick="ClearSearch()">清空</span><span class="btn_search" onclick="FullSearch()">组合查询</span>'+
    '<script>function ClearSearch(){so_id.value="";document.querySelectorAll("input:checked").forEach(e=>e.checked=false);}'+
    'function FullSearch(){const status=document.querySelector("input[id^=status_]:checked");'+
    'parent.findOrder(so_id.value,status?status.id.slice(7):"");}</script>',
  [listPath]: '<iframe id="s_filter_frame"></iframe><div id="results"></div>'+
    '<div id="_jt_pagebar">总共 1 页 0 条记录</div>'+
    '<div id="ToolConfirm_Btn"><span class="ding_db_txt" onclick="audit()">审核</span>'+
    '<span onclick="parent.otherAction++">强制审核</span></div>'+
    '<div class="full_item_table" id="details" hidden><div class="jt_item_row"><span class="sku_id">00001</span>'+
    '<span title="数量">x10</span><span title="单价">￥2.00</span></div></div>'+
    '<script>setTimeout(()=>{s_filter_frame.src="/filters";},250);'+
    'async function findOrder(online,status){'+
    'const filters=[{k:"so_id",c:"@=",v:online}];if(status)filters.push({k:"status",c:"@=",v:status});'+
    'const response=await fetch(location.pathname,{method:"POST",body:new URLSearchParams({__CALLBACKID:"JTable1",'+
    '__CALLBACKPARAM:JSON.stringify({Method:"LoadDataToJSON",Args:["1",JSON.stringify(filters),""]}),fixtureState:parent.statusValue})});'+
    'const callback=JSON.parse((await response.text()).slice(2));const count=JSON.parse(callback.ReturnValue).datas.length;'+
    'results.innerHTML=count?'+JSON.stringify(
      '<div class="_jt_row"><div class="_jt_cell_o_id"><div class="o_id">9001</div></div>'+
      '<div class="_jt_cell_so_id">'+online+'</div><div class="_jt_cell_shop_buyer_id"><span>'+contact.buyer+'</span>'+
      '<div class="shopname">'+shop+'</div></div><div class="_jt_cell_pay_amount">20.00</div>'+
      '<div class="_jt_cell_items" onclick="details.hidden=false">明细</div>'+
      '<div class="_jt_cell_checked"><input type="checkbox" id="selected"></div></div>')+':"";'+
    '_jt_pagebar.textContent=count?"共 "+count+"条 1/1页":"总共 1 页 0 条记录";details.hidden=true;}'+
    'function audit(){if(document.querySelectorAll("._jt_row input:checked").length===1&&selected.checked){'+
    'parent.auditCount++;parent.statusValue="waitfconfirm";}}</script>',
};

async function openFixture(t){
  const server=http.createServer(async (req,res)=>{
    if(req.method==='POST' && req.url===listPath) {
      const chunks=[];
      for await(const part of req) chunks.push(part);
      const data=new URLSearchParams(Buffer.concat(chunks).toString());
      const filters=JSON.parse(JSON.parse(data.get('__CALLBACKPARAM')).Args[1]);
      const status=filters.find(x=>x.k==='status')?.v;
      const state=data.get('fixtureState');
      const datas=state && (!status || status===state) && filters.some(x=>x.v===online) ? [{}] : [];
      res.end('0|'+JSON.stringify({IsSuccess:true,ReturnValue:JSON.stringify({dp:{PageIndex:1,IsLast:true},datas})}));
      return;
    }
    if (req.method==='POST') for await(const _ of req) {/* consume uploaded fixture */}
    const html=pages[req.url];
    res.writeHead(html?200:404,{'Content-Type':'text/html; charset=utf-8'});
    res.end(html||'');
  }).listen(0,'127.0.0.1');
  await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const base='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({executablePath:edge,headless:true});
  t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.goto(base);
  const account={warehouse:wh,expectedCoid:'55',expectedUid:'65',expectedCompany:'合成企业',bindingVersion:1};
  const driver={page:async()=>page,query:async()=>({scope:{coid:'55',uid:'65',company:'合成企业'}})};
  const orderPages=new OrderPages({allowed:url=>url.startsWith(base+'/')&&url!==base+'/'});
  return {page,account,flow:new OrderFlow(driver,{pages:orderPages})};
}

test('native controls preview, import, reconcile and ordinary audit exactly one order',{skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow}=await openFixture(t);
  const preview=await flow.run('stage_import',{spec},account);
  assert.deepEqual(preview.lines,[['00001','10']]);
  assert.equal(preview.contact_ok,true);
  assert.equal(await page.evaluate(()=>importCount),0);
  await page.frameLocator('#upload').locator('table tr td').nth(17).evaluate(e=>e.textContent='￥25.00 ￥0.00');
  await flow.run('import',{warehouse:wh,permit:preview.permit,online},account);
  const bound={...spec,binding:preview.binding};
  const found=await flow.run('lookup',{spec:bound},account);
  assert.equal(found.internal,'9001');
  assert.equal(found.status,'awaiting');
  const exact={...bound,internal:found.internal};
  const next=await flow.run('stage_audit',{spec:exact},account);
  await flow.run('audit',{warehouse:wh,permit:next.permit,online,internal:found.internal},account);
  const verified=await flow.run('lookup',{spec:{...exact,phase:'AUDIT_UNKNOWN'}},account);
  assert.equal(verified.status,'audited');
  assert.equal(verified.native_status,'已客审待财审');
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[1,1,0]);
  await assert.rejects(flow.run('stage_import',{spec:bound},account),{code:'ORDER_EXISTS'});
});

test('changed preview cannot release the order',{skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow}=await openFixture(t);
  const preview=await flow.run('stage_import',{spec},account);
  await page.frameLocator('#upload').locator('table tr td').nth(13).locator('span').evaluate(e=>e.textContent='合成°商品 * 9');
  await assert.rejects(flow.run('import',{warehouse:wh,permit:preview.permit,online},account),{code:'PREVIEW_CHANGED'});
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,0,0]);
});

test('recover and audit an existing platform order without importing',{skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow}=await openFixture(t);
  await page.evaluate(()=>{statusValue='waitconfirm';});
  const bound={...spec,phase:'IMPORT_UNKNOWN',binding:{warehouse:wh,shop,coid:account.expectedCoid,
    uid:account.expectedUid,company:account.expectedCompany,version:account.bindingVersion,shop_id:'102'}};
  const found=await flow.run('lookup',{spec:bound},account);
  assert.equal(found.internal,'9001');
  assert.deepEqual(found.lines,[['00001','10','2.00']]);
  assert.equal(found.status,'awaiting');
  const exact={...bound,internal:found.internal};
  const next=await flow.run('stage_audit',{spec:exact},account);
  await flow.run('audit',{warehouse:wh,permit:next.permit,online,internal:exact.internal},account);
  const audited=await flow.run('lookup',{spec:{...exact,phase:'AUDIT_UNKNOWN'}},account);
  assert.equal(audited.internal,exact.internal);
  assert.equal(audited.status,'audited');
  assert.equal(audited.native_status,'已客审待财审');
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,1,0]);
});

test('shipping status is recorded for review without importing or auditing',{skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow}=await openFixture(t);
  const bound={...spec,phase:'AUDIT_UNKNOWN',internal:'9001',binding:{warehouse:wh,shop,coid:account.expectedCoid,
    uid:account.expectedUid,company:account.expectedCompany,version:account.bindingVersion,shop_id:'102'}};
  for (const [state,native] of [['delivering','发货中(打单拣货)'],['sent','已发货']]) {
    await page.evaluate(value=>{statusValue=value;},state);
    const found=await flow.run('lookup',{spec:bound},account);
    assert.equal(found.internal,bound.internal);
    assert.equal(found.status,'review');
    assert.equal(found.native_status,native);
  }
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,0,0]);
});
