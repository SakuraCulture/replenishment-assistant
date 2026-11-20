// Exercise the real sale-out adapter with a private Chromium context and no outbound requests.
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {createRequire} from 'node:module';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {SaleoutPages} from '../src/saleout-allocation.mjs';
import {AllocationFlow} from '../src/order-allocation.mjs';

const require=createRequire(new URL('../../aoxiang/package.json',import.meta.url));
const {chromium}=require('playwright-core');
const edge=process.env.JST_TEST_EDGE||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe');
const origin='https://www.erp321.com';
const saleout='/app/wms/saleout/saleout.aspx';
const file='/app/wms/saleout/ExportSaleoutV2.aspx';
const binding={warehouse:'河北仓',company:'合成企业',coid:'8',uid:'5',version:1,shop_id:'7',shop:'合成店'};
const spec={warehouse:binding.warehouse,shop:binding.shop,internal:'6001',online:'BH'+'A'.repeat(24),
  binding,contact:{buyer:'synthetic'}};
const account={warehouse:binding.warehouse,expectedCoid:binding.coid,expectedUid:binding.uid,
  expectedCompany:binding.company,bindingVersion:binding.version};
const id='1'.repeat(32);
const facts={found:true,internal:spec.internal,online:spec.online,shop:spec.shop,shop_id:binding.shop_id,
  coid:binding.coid,native_status:'已发货',amount:'33.00',freight:'0',observed:'2026-11-20T08:00:00Z',
  items:[{sku:'0001',qty:'12',price:'2.75',amount:'33'}]};
const documents=['9001','9002','9003'].map((io_id,i)=>({io_id,o_id:spec.internal,so_id:spec.online,
  co_id:binding.coid,shop_id:binding.shop_id,shop_name:spec.shop,status:i===1?'出库前作废':'已出库'}));
const selection=documents.filter(r=>r.status==='已出库').map(r=>r.io_id);
const bytes=Buffer.from('PK synthetic fixture body');

function html({wrongCompany=false,checkout=false,invalidPost=false}={}){
  const rows=documents.map(doc=>'<div class="_jt_row"><div class="_jt_cell_io_id"><a class="io_id_click_bind">'+doc.io_id+
    '</a></div><div class="_jt_cell_checked"><input type="checkbox" class="_jt_cbx"></div></div>').join('');
  return '<span id="partnerChange">'+(wrongCompany?'另一企业':binding.company)+'</span>'+
    '<div id="ul_search"><button onclick="SearchReset()">重置</button></div><input id="id" onkeydown="lookup(event)">'+
    '<div id="results"></div><div id="ExportBtn_Btn"><span class="_db_item" onclick="exportRows()">导出勾选的单据(导出明细)</span></div>'+
    '<script>'+
    'const jTable={Filter:[]};const $={cbx:()=>'+checkout+',escape:encodeURIComponent};'+
    'function __AppendCoid(value){return value+"&owner_co_id='+binding.coid+'";}'+
    'function SearchReset(){jTable.Filter=[];id.value="";results.textContent="";}'+
    'function GetCheckIoIds(){return Array.from(document.querySelectorAll("input._jt_cbx:checked"),e=>e.closest("._jt_row").querySelector("a").textContent);}'+
    'async function call(Method,Args){const response=await fetch(location.href,{method:"POST",body:new URLSearchParams({__CALLBACKPARAM:JSON.stringify({Method,Args})})});'+
    'return JSON.parse((await response.text()).split("|")[1]).ReturnValue;}'+
    'async function lookup(e){if(e.key!=="Enter")return;jTable.Filter=[{k:"o_id",v:id.value,c:"@="}];'+
    'await call("LoadDataToJSON",[0,JSON.stringify(jTable.Filter)]);results.innerHTML='+JSON.stringify(rows)+';}'+
    'async function exportRows(){const token=await call("ExportSaleOut",[JSON.stringify({Filter:JSON.stringify(jTable.Filter),'+
    'CheckIoIds:GetCheckIoIds(),Flag:1,CheckoutAfterSend:'+invalidPost+',IsExportNoTask:true,IsExportSonInouts:false})]);'+
    'window.open(__AppendCoid("ExportSaleoutV2.aspx?s="+token));}</script>';
}

test('exact sale-out DOM exports only valid documents; restart consumes the accepted reference',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'jst-saleout-fixture-'));
  const browser=await chromium.launch({executablePath:edge,headless:true,args:['--disable-background-networking']});
  t.after(async()=>{
    await browser.close();
    assert.equal(path.dirname(await fs.realpath(root)),await fs.realpath(os.tmpdir()));
    assert.match(path.basename(root),/^jst-saleout-fixture-/);
    await fs.rm(root,{recursive:true,force:true});
  });
  const context=await browser.newContext({acceptDownloads:true}),page=await context.newPage();
  let opts={},queries=0,exports=0,downloads=0,state={};
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!==origin)return route.abort();
    if(url.pathname===file){
      assert.equal(url.searchParams.get('s'),'synthetic-receipt');
      assert.equal(url.searchParams.get('owner_co_id'),binding.coid);
      downloads++;
      return route.fulfill({headers:{'Content-Type':'application/octet-stream','Content-Disposition':'attachment; filename="source.xlsx"'},body:bytes});
    }
    if(url.pathname!==saleout)return route.abort();
    if(route.request().method()==='GET')return route.fulfill({contentType:'text/html; charset=utf-8',body:html(opts)});
    const form=new URLSearchParams(route.request().postData()),{Method,Args}=JSON.parse(form.get('__CALLBACKPARAM'));
    let ReturnValue;
    if(Method==='LoadDataToJSON'){
      queries++;
      assert.deepEqual(JSON.parse(Args[1]),[{k:'o_id',v:spec.internal,c:'@='}]);
      ReturnValue=JSON.stringify({datas:documents,dp:{PageIndex:1,IsLast:true}});
    }else{
      assert.equal(Method,'ExportSaleOut');
      const payload=JSON.parse(Args[0]);
      assert.deepEqual(payload.CheckIoIds,selection);
      assert.equal(payload.CheckoutAfterSend,false);
      exports++;
      ReturnValue='synthetic-receipt';
    }
    return route.fulfill({body:'1|'+JSON.stringify({IsSuccess:true,ReturnValue})});
  });
  const driver={page:async()=>page,query:async()=>({scope:binding})};
  const pages=new SaleoutPages();
  pages.orders={lookup:async(p,s,signal,pending,native)=>{
    assert.equal(p,page);assert.equal(s,spec);assert.equal(pending,false);assert.equal(native,true);
    return facts;
  }};
  const store={read:()=>structuredClone(state),write:async update=>{update(state);}};
  const flow=new AllocationFlow(driver,{store,root,pages});
  opts={wrongCompany:true};
  await assert.rejects(flow.run({spec,operation_id:id},account),{code:'ALLOCATION_SOURCE'});
  opts={checkout:true};
  await assert.rejects(flow.run({spec,operation_id:id},account),{code:'ALLOCATION_SOURCE'});
  assert.equal(exports,0);
  assert.deepEqual(state,{});
  opts={};
  const read=pages.readDownload.bind(pages);
  pages.readDownload=async()=>{throw Error('synthetic disconnect after accepted export');};
  await assert.rejects(flow.run({spec,operation_id:id},account),/synthetic disconnect/);
  assert.equal(exports,1);
  assert.equal(state.allocationOperations[id].state,'DISPATCHING');
  assert.ok(state.allocationOperations[id].native.url.endsWith('s=synthetic-receipt&owner_co_id=8'));
  pages.readDownload=read;
  const restart=new AllocationFlow(driver,{store,root,pages});
  const result=await restart.run({spec,operation_id:'2'.repeat(32)},account);
  assert.equal(result.operation_id,id);
  assert.equal(result.reused,true);
  assert.equal(result.source_kind,'jst_saleout_detail_v1');
  assert.deepEqual(result.outbounds.map(row=>row.io_id),selection);
  assert.deepEqual(await fs.readFile(result.path),bytes);
  assert.equal(state.allocationOperations[id].native,undefined);
  assert.equal(exports,1);
  assert.equal(queries,2);
  const count=downloads;
  assert.ok(count>0);
  assert.equal((await restart.run({spec,operation_id:'3'.repeat(32)},account)).reused,true);
  assert.equal(downloads,count);
});
