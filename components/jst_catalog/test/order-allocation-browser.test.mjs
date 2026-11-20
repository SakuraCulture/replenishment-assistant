// Local DOM fixture only: no business account or production browser profile.
import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import test from 'node:test';
import assert from 'node:assert/strict';
import {fileURLToPath} from 'node:url';
import {AllocationPages} from '../src/order-allocation.mjs';

const require=createRequire(fileURLToPath(new URL('../../aoxiang/package.json',import.meta.url)));
const {chromium}=require('playwright-core');
const edge=process.env.JST_TEST_EDGE||path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe');
const spec={online:'BH'+'A'.repeat(24),internal:'478074',shop:'合成店'};
const bytes=Buffer.from('PK synthetic native original');

test('native DOM filters one original, receives direct file and blocks a changed shop',async t=>{
  assert.ok(fs.existsSync(edge),'Installed Edge required for local DOM acceptance');
  let exports=0,wrongShop=false;
  const server=http.createServer(async(req,res)=>{
    let body='';for await (const part of req) body+=part;
    if(req.url==='/export'&&req.method==='POST'){
      const form=new URLSearchParams(body),cb=JSON.parse(form.get('__CALLBACKPARAM'));
      assert.deepEqual(JSON.parse(cb.Args[1]),[{k:'o_id',v:spec.internal,c:'@='},{k:'so_id',v:spec.online,c:'@='}]);
      res.end('1|'+JSON.stringify({IsSuccess:true,ReturnValue:JSON.stringify({datas:[{}],dp:{PageIndex:1,IsLast:true}})}));return;
    }
    if(req.url==='/file.xlsx'){
      exports++;res.setHeader('Content-Disposition','attachment; filename="allocation.xlsx"');
      res.setHeader('Content-Type','application/octet-stream');res.end(bytes);return;
    }
    res.setHeader('Content-Type','text/html; charset=utf-8');
    if(req.url==='/')res.end('<iframe id="listing" src="/list"></iframe><iframe id="exporting" hidden src="/empty"></iframe>');
    else if(req.url==='/list')res.end('<span id="ExportOrder" onclick="parent.exporting.src=\'/export\';parent.exporting.hidden=false;parent.listing.hidden=true">导出订单</span>');
    else if(req.url==='/filters')res.end('<input id="o_id"><input id="so_id">'+
      '<span onclick="ClearSearch()">清空</span><span class="btn_search" onclick="FullSearch()">组合查询</span>'+
      '<script>function ClearSearch(){o_id.value="";so_id.value="";}function FullSearch(){parent.findOrder(o_id.value,so_id.value);}</script>');
    else if(req.url==='/export')res.end('<iframe id="s_filter_frame" src="/filters"></iframe><div id="results"></div>'+
      '<div id="_jt_pagebar">总共 1 页 0 条记录</div><span id="Tool_New_Btn"><span class="ding_db_txt" onclick="location.href=\'/file.xlsx\'">导出所有符合条件的订单</span></span>'+
      '<script>async function findOrder(internal,online){await fetch("/export",{method:"POST",body:new URLSearchParams({__CALLBACKID:"JTable1",__CALLBACKPARAM:JSON.stringify({Method:"LoadDataToJSON",Args:[0,JSON.stringify([{k:"o_id",v:internal,c:"@="},{k:"so_id",v:online,c:"@="}])]})})});'+
      'results.innerHTML='+JSON.stringify('<div class="_jt_row"><div class="_jt_cell_o_id">'+spec.internal+'</div><div class="_jt_cell_so_id">'+spec.online+'</div><div class="_jt_cell_shop_name">'+(wrongShop?'另一店':spec.shop)+'</div><div class="_jt_cell_status">已发货</div></div>')+';_jt_pagebar.textContent="总共 1 页 1 条记录";}</script>');
    else res.end('<div>empty</div>');
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  t.after(()=>new Promise(resolve=>server.close(resolve)));
  const browser=await chromium.launch({executablePath:edge,headless:true});
  t.after(()=>browser.close());
  const context=await browser.newContext({acceptDownloads:true}),page=await context.newPage();
  const origin='http://127.0.0.1:'+server.address().port;
  const adapter=new AllocationPages(undefined,url=>url.startsWith(origin));
  async function load(){
    await page.goto(origin);
    const frame=page.frame({url:origin+'/list'});
    await frame.locator('#ExportOrder').waitFor();
    return {frame};
  }
  let intents=0;
  const raw=await adapter.download(page,await load(),spec,undefined,async()=>{intents++;});
  assert.deepEqual(raw,bytes);assert.equal(exports,1);assert.equal(intents,1);
  wrongShop=true;
  await assert.rejects(adapter.download(page,await load(),spec,undefined,async()=>{intents++;}),{code:'ALLOCATION_SCOPE'});
  assert.equal(exports,1);assert.equal(intents,1);
});
