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
    '<script>window.statusValue="";window.itemPopoverAvailable=true;window.importCount=0;window.auditCount=0;window.otherAction=0;'+
    'function selectTab(list){listing.hidden=!list;upload.hidden=list;listTab.setAttribute("aria-selected",String(list));'+
    'importTab.setAttribute("aria-selected",String(!list));}</script>',
  '/app/order/import/import.aspx': '<form id="form1" action="import_show.aspx" method="post" enctype="multipart/form-data">'+
    '<label><input id="importbyshop" type="radio" name="mode" checked>按店铺导入</label>'+formShop+
    '<label><input id="MultiCommodity" name="import_formart" value="MultiCommodity" type="radio">多商品</label>'+
    '<label><input id="SingleCommodity" name="import_formart" value="SingleCommodity" type="radio">单商品</label>'+
    '<label><input id="sku_price" type="checkbox">取基本售价</label>'+
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
    'function _ACP(method,cb,oid,full){if(method!=="ReloadOrdersV2"||oid!=="9001"||!full)throw Error("mock callback");'+
    'fetch("/order-facts?status="+parent.statusValue).then(r=>r.json()).then(row=>cb([JSON.stringify({data:row})]));}'+
    'async function findOrder(online,status){'+
    'const filters=[{k:"so_id",c:"@=",v:online}];if(status)filters.push({k:"status",c:"@=",v:status});'+
    'const response=await fetch(location.pathname,{method:"POST",body:new URLSearchParams({__CALLBACKID:"JTable1",'+
    '__CALLBACKPARAM:JSON.stringify({Method:"LoadDataToJSON",Args:["1",JSON.stringify(filters),""]}),fixtureState:parent.statusValue})});'+
    'const callback=JSON.parse((await response.text()).slice(2));const count=JSON.parse(callback.ReturnValue).datas.length;'+
    'results.innerHTML=count?'+JSON.stringify(
      '<div class="_jt_row"><div class="_jt_cell_o_id"><div class="o_id">9001</div></div>'+
      '<div class="_jt_cell_so_id">'+online+'</div><div class="_jt_cell_shop_buyer_id"><span>'+contact.buyer+'</span>'+
      '<div class="shopname">'+shop+'</div></div><div class="_jt_cell_pay_amount">20.00</div>'+
      '<div class="_jt_cell_items" onclick="if(parent.itemPopoverAvailable)details.hidden=false">明细</div>'+
      '<div class="_jt_cell_checked"><input type="checkbox" id="selected"></div></div>')+':"";'+
    '_jt_pagebar.textContent=count?"共 "+count+"条 1/1页":"总共 1 页 0 条记录";details.hidden=true;}'+
    'function audit(){if(document.querySelectorAll("._jt_row input:checked").length===1&&selected.checked){'+
    'parent.auditCount++;parent.statusValue="waitfconfirm";}}</script>',
};

async function openFixture(t,{large=[],split=false,shortShop=false,listBuyer=contact.buyer,
  detailBuyer=contact.buyer,displayBuyer=contact.buyer,detailOverrides={}}={}){
  const submittedFormats=[];
  const detailReads=[];
  const nativeItem=(sku,qty)=>({sku_id:sku,qty,price:'2',amount:String(Number(qty)*2)});
  const nativeItems=large.length ? large.map(([sku,qty])=>nativeItem(sku,qty)) : [nativeItem('00001','10')];
  const nativeOrder=state=>({o_id:'9001',so_id:online,shop_name:shop,co_id:'55',shop_id:'102',
    status:{waitconfirm:'已付款待审核',waitfconfirm:'已客审待财审',delivering:'发货中',sent:'已发货',cancelled:'已取消'}[state],
    item_actual_count:nativeItems.length,shop_buyer_id:listBuyer,pay_amount:'20.00',freight:'0'});
  const server=http.createServer(async (req,res)=>{
    if(req.method==='POST' && req.url===listPath) {
      const chunks=[];
      for await(const part of req) chunks.push(part);
      const data=new URLSearchParams(Buffer.concat(chunks).toString());
      const filters=JSON.parse(JSON.parse(data.get('__CALLBACKPARAM')).Args[1]);
      const status=filters.find(x=>x.k==='status')?.v;
      const state=data.get('fixtureState');
      const datas=state && (!status || status===state) && filters.some(x=>x.v===online) ? [nativeOrder(state)] : [];
      res.end('0|'+JSON.stringify({IsSuccess:true,ReturnValue:JSON.stringify({dp:{PageIndex:1,IsLast:true},datas})}));
      return;
    }
    const url=new URL(req.url,'http://localhost');
    if(url.pathname==='/order-facts') {
      detailReads.push(url.searchParams.get('status'));
      res.writeHead(200,{'Content-Type':'application/json'});
      res.end(JSON.stringify({...nativeOrder(url.searchParams.get('status')),items:nativeItems,shop_buyer_id:detailBuyer,...detailOverrides}));
      return;
    }
    let html=pages[url.pathname];
    if (shortShop&&url.pathname===listPath) html=html.replace(shop,'合成简称');
    if (url.pathname===listPath) html=html.replace('<span>'+contact.buyer+'</span>','<span>'+displayBuyer+'</span>');
    if (req.method==='POST') {
      const chunks=[];
      for await(const part of req) chunks.push(part);
      submittedFormats.push(Buffer.concat(chunks).toString().match(/name="import_formart"\r\n\r\n(\w+)/)?.[1]);
    }
    if(large.length&&url.pathname.endsWith('/import_show.aspx')) {
      const cells=[...fields];
      cells[13]=large.map(([sku,qty])=>'<span>商品'+sku+' * '+qty+'</span><br>').join('');
      const row='<tr>'+cells.map(value=>'<td>'+value+'</td>').join('')+'</tr>';
      html=html.replace(/<table>.*<\/table>/,'<table>'+row+(split?row:'')+'</table>');
    }
    res.writeHead(html?200:404,{'Content-Type':'text/html; charset=utf-8'});
    res.end(html||'');
  }).listen(0,'127.0.0.1');
  await new Promise(resolve=>server.once('listening',resolve));
  t.after(()=>new Promise(resolve=>{
    server.close(resolve);
    server.closeAllConnections();
  }));
  const base='http://127.0.0.1:'+server.address().port;
  const browser=await chromium.launch({executablePath:edge,headless:true});
  t.after(()=>browser.close());
  const page=await browser.newPage();
  await page.goto(base);
  const account={warehouse:wh,expectedCoid:'55',expectedUid:'65',expectedCompany:'合成企业',bindingVersion:1};
  const driver={page:async()=>page,query:async()=>({scope:{coid:'55',uid:'65',company:'合成企业'}})};
  const orderPages=new OrderPages({allowed:url=>url.startsWith(base+'/')&&url!==base+'/'});
  return {page,account,submittedFormats,detailReads,flow:new OrderFlow(driver,{pages:orderPages})};
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
  const payment=await flow.run('payment_detail',{spec:exact},account);
  assert.equal(payment.detail_count,1);
  assert.equal(payment.native_status,'已客审待财审');
  assert.deepEqual(payment.items.map(({sku,qty,price,amount})=>[sku,qty,price,amount]),[['00001','10','2','20']]);
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

test('native single-SKU rows preserve a 2000-item order and reopen a stale preview without importing',
  {skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const lines=Array.from({length:2000},(_,i)=>[String(i).padStart(20,'0'),String(i%3+1)]);
  const local={...spec,import_format:'SingleCommodity',lines,
    preview_names:Object.fromEntries(lines.map(([sku])=>[sku,'商品'+sku]))};
  const {page,account,flow,submittedFormats}=await openFixture(t,{large:lines});
  await page.frameLocator('#upload').locator('form').waitFor();
  await page.locator('#upload').evaluate(e=>e.src='/app/order/import/import_show.aspx');
  await page.frameLocator('#upload').locator('#txt_exception_datas').waitFor({state:'attached'});
  const preview=await flow.run('preview',{spec:local},account);
  assert.deepEqual(preview.lines,lines);
  assert.equal(preview.order_count,1);
  assert.equal(preview.import_format,'SingleCommodity');
  assert.deepEqual(submittedFormats,['SingleCommodity']);
  const staged=await flow.run('stage_import',{spec:{...local,binding:preview.binding}},account);
  await page.frameLocator('#upload').locator('table tr td').nth(17).evaluate(e=>e.textContent='￥21.00 ￥0.00');
  await assert.rejects(flow.run('import',{warehouse:wh,permit:staged.permit,online},account),{code:'PREVIEW_CHANGED'});
  const checked=await flow.run('stage_import',{spec:{...local,binding:preview.binding}},account);
  await flow.run('import',{warehouse:wh,permit:checked.permit,online},account);
  const found=await flow.run('lookup',{spec:{...local,binding:preview.binding}},account);
  assert.equal(found.internal,'9001');
  assert.deepEqual(found.lines.map(row=>row.slice(0,2)),lines);
  const audit=await flow.run('stage_audit',{spec:{...local,binding:preview.binding,internal:found.internal}},account);
  await flow.run('audit',{warehouse:wh,permit:audit.permit,online,internal:found.internal},account);
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[1,1,0]);
});

test('single-SKU rows split by the platform are blocked before import',
  {skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const lines=[['00001','10'],['00002','20'],['00003','30'],['00004','40']];
  const {page,account,flow}=await openFixture(t,{large:lines,split:true});
  await assert.rejects(flow.run('stage_import',{spec:{...spec,import_format:'SingleCommodity',lines}},account),
    /识别到 2 张订单/);
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,0,0]);
});

test('recover and audit an existing 232-item order without a product popover or reimport',
  {skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const lines=Array.from({length:232},(_,i)=>[String(i+1).padStart(5,'0'),i<165?'21':'20']);
  const {page,account,flow}=await openFixture(t,{large:lines});
  await page.evaluate(()=>{statusValue='waitconfirm';itemPopoverAvailable=false;});
  // No import_format is stored for the existing multi-product task.
  const bound={...spec,lines,phase:'IMPORT_UNKNOWN',binding:{warehouse:wh,shop,coid:account.expectedCoid,
    uid:account.expectedUid,company:account.expectedCompany,version:account.bindingVersion,shop_id:'102'}};
  const found=await flow.run('lookup',{spec:bound},account);
  assert.equal(found.internal,'9001');
  assert.deepEqual(found.lines.map(row=>row.slice(0,2)),lines);
  assert.equal(found.status,'awaiting');
  const exact={...bound,internal:found.internal};
  const detailUrl='**/order-facts?*';
  await page.route(detailUrl,async route=>{
    const response=await route.fetch();
    const order=await response.json();
    order.items.pop();
    await route.fulfill({response,json:order});
  });
  await assert.rejects(flow.run('stage_audit',{spec:exact},account),/原生整单明细没有完整商品范围/);
  await page.unroute(detailUrl);
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

test('exact import and full payment read work before audit under the frozen native shop',
  {skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow}=await openFixture(t,{shortShop:true});
  const preview=await flow.run('stage_import',{spec},account);
  await flow.run('import',{warehouse:wh,permit:preview.permit,online},account);
  const bound={...spec,phase:'IMPORT_UNKNOWN',binding:preview.binding};
  const result=await flow.run('lookup',{spec:bound,identity_only:true},account);
  assert.equal(result.internal,'9001');
  assert.equal(result.native_status,'已付款待审核');
  assert.equal(result.contact_ok,true);
  const exact={...bound,internal:result.internal};
  const awaiting=await flow.run('payment_detail',{spec:exact},account);
  assert.equal(awaiting.native_status,'已付款待审核');
  assert.deepEqual(awaiting.items.map(({sku,qty,price,amount})=>[sku,qty,price,amount]),[['00001','10','2','20']]);
  await page.evaluate(()=>{statusValue='sent';});
  const shipped=await flow.run('payment_detail',{spec:exact},account);
  assert.equal(shipped.native_status,'已发货');
  assert.deepEqual(shipped.items,awaiting.items);
  await assert.rejects(flow.run('payment_detail',{spec:{...exact,internal:'9002'}},account),/内部单号与原单不同/);
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[1,0,0]);
});

test('cancelled original with a masked buyer and unbound internal number returns complete quantities',
  {skip:!fs.existsSync(edge),timeout:60000},async t=>{
  const {page,account,flow,detailReads}=await openFixture(t,{listBuyer:'0***0',detailBuyer:'',
    detailOverrides:{items:[{sku_id:'00001',qty:'10',price:null,amount:null,item_status:'Cancelled'}]}});
  await page.evaluate(()=>{statusValue='cancelled';});
  const bound={...spec,phase:'IMPORT_UNKNOWN',binding:{warehouse:wh,shop,company:account.expectedCompany,
    coid:account.expectedCoid,uid:account.expectedUid,version:account.bindingVersion,shop_id:'102'}};
  const result=await flow.run('lookup',{spec:bound,identity_only:true},account);
  assert.equal(result.internal,'9001');
  assert.equal(result.native_status,'已取消');
  assert.equal(result.buyer_check,'unavailable');
  assert.equal(result.detail_count,1);
  assert.deepEqual(result.items.map(({sku,qty})=>[sku,qty]),[['00001','10']]);
  assert.equal(detailReads.length,1);
  assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,0,0]);
});

test('masked, missing and conflicting buyers on the exact original order',
  {skip:!fs.existsSync(edge),timeout:120000},async t=>{
  const listMask='0***0';
  const swappedBuyer='00020';
  const large=Array.from({length:582},(_,i)=>[String(i+1).padStart(5,'0'),i===581?'45':'18']);
  const scenarios=[
    ['masked list with complete original detail', {listBuyer:listMask,large},'verified','verified',2],
    ['empty list with complete original detail', {listBuyer:null},'verified','verified',2],
    ['masked list and empty detail', {listBuyer:listMask,detailBuyer:''},'unavailable','unavailable',2],
    ['masked detail has conflicting visible digits', {listBuyer:listMask,detailBuyer:'9***0'},'masked_conflict','masked_conflict',2],
    ['masked list and different full detail account', {listBuyer:listMask,detailBuyer:swappedBuyer},'mismatch','mismatch',2],
    ['different full list account', {listBuyer:swappedBuyer},'mismatch','mismatch',0],
    ['complete list account can verify its masked detail', {detailBuyer:listMask},'verified','verified',1],
    ['different full detail account still blocks payment', {detailBuyer:swappedBuyer},'verified','mismatch',1],
    ['detail with a different internal order cannot verify the buyer', {listBuyer:null,detailOverrides:{o_id:'9002'}},'unavailable','unavailable',2],
  ];
  for (const [name,options,lookupBuyer,paymentBuyer,reads] of scenarios) {
    await t.test(name,async sub=>{
      const {page,account,flow,detailReads}=await openFixture(sub,{displayBuyer:listMask,...options});
      await page.evaluate(()=>{statusValue='waitfconfirm';});
      const bound={...spec,phase:'IMPORT_UNKNOWN',binding:{warehouse:wh,shop,company:account.expectedCompany,
        coid:account.expectedCoid,uid:account.expectedUid,version:account.bindingVersion,shop_id:'102'}};
      const found=await flow.run('lookup',{spec:bound,identity_only:true},account);
      assert.equal(found.internal,'9001');
      assert.equal(found.native_status,'已客审待财审');
      assert.equal(found.buyer_check,lookupBuyer);
      assert.equal(found.contact_ok,lookupBuyer==='verified');
      const payment=await flow.run('payment_detail',{spec:{...bound,internal:found.internal}},account);
      assert.equal(payment.buyer_check,paymentBuyer);
      assert.equal(payment.contact_ok,paymentBuyer==='verified');
      if (lookupBuyer==='unavailable'&&!options.detailOverrides) {
        assert.equal(found.detail_count,1);
        assert.deepEqual(found.items,payment.items);
        assert.equal(found.amount,'20.00');
        assert.equal(found.buyer,undefined);
      }
      assert.equal(detailReads.length,reads);
      assert.deepEqual(await page.evaluate(()=>[importCount,auditCount,otherAction]),[0,0,0]);
      if (options.large) {
        assert.equal(payment.detail_count,582);
        assert.equal(payment.items.reduce((n,item)=>n+Number(item.qty),0),10503);
      }
    });
  }
});
