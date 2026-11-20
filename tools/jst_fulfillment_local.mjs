// Isolated flow acceptance. Never navigates to ERP or uses a user profile.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
import {JobStore} from '../components/jst_catalog/src/job-store.mjs';
import {AllocationFlow,AllocationPages} from '../components/jst_catalog/src/order-allocation.mjs';

const inputFile=process.argv[2],outputFile=process.argv[3],resume=process.argv.includes('--resume');
const input=JSON.parse(await fs.readFile(inputFile,'utf8'));
if(input.synthetic_only!==true)throw Error('This helper accepts synthetic cases only');
const require=createRequire(fileURLToPath(new URL('../components/aoxiang/package.json',import.meta.url)));
const {chromium}=require('playwright-core');
const edge=path.join(process.env['ProgramFiles(x86)'],'Microsoft/Edge/Application/msedge.exe');
const raw=await fs.readFile(input.native_file),{spec,account}=input;
const store=await JobStore.open(input.state_root,{});
let browser,server,result,downloads=0,queries=0;
try {
  const unavailable={page:async()=>{throw Error('A resumed receipt must not open a browser');}};
  if(resume) {
    result=await new AllocationFlow(unavailable,{store,root:input.state_root}).run(input,account);
  } else {
    server=http.createServer(async(req,res)=>{
      let body='';for await(const block of req)body+=block;
      if(req.url==='/export'&&req.method==='POST') {
        queries++;
        const form=new URLSearchParams(body),callback=JSON.parse(form.get('__CALLBACKPARAM'));
        const filters=JSON.parse(callback.Args[1]);
        if(JSON.stringify(filters)!==JSON.stringify([{k:'o_id',v:spec.internal,c:'@='},{k:'so_id',v:spec.online,c:'@='}]))
          throw Error('Synthetic export query widened');
        res.end('1|'+JSON.stringify({IsSuccess:true,ReturnValue:JSON.stringify({datas:[{}],dp:{PageIndex:1,IsLast:true}})}));return;
      }
      if(req.url==='/file.xlsx') {
        downloads++;res.setHeader('Content-Disposition','attachment; filename="synthetic-allocation.xlsx"');
        res.setHeader('Content-Type','application/octet-stream');res.end(raw);return;
      }
      res.setHeader('Content-Type','text/html; charset=utf-8');
      if(req.url==='/')res.end('<div id="root"></div><iframe id="listing" src="/list"></iframe><iframe id="exporting" src="/empty" hidden></iframe><script>document.getElementById("root").textContent='+JSON.stringify(account.expectedCompany)+';</script>');
      else if(req.url==='/list')res.end('<span id="ExportOrder" onclick="parent.exporting.src=\'/export\';parent.exporting.hidden=false;parent.listing.hidden=true">导出订单</span>');
      else if(req.url==='/filters')res.end('<input id="o_id"><input id="so_id"><span onclick="ClearSearch()">清空</span><span class="btn_search" onclick="FullSearch()">组合查询</span>'+
        '<script>function ClearSearch(){o_id.value="";so_id.value="";}function FullSearch(){parent.findOrder(o_id.value,so_id.value);}</script>');
      else if(req.url==='/export')res.end('<iframe id="s_filter_frame" src="/filters"></iframe><div id="results"></div><div id="_jt_pagebar">总共 1 页 0 条记录</div>'+
        '<span id="Tool_New_Btn"><span class="ding_db_txt" onclick="location.href=\'/file.xlsx\'">导出所有符合条件的订单</span></span>'+
        '<script>async function findOrder(internal,online){await fetch("/export",{method:"POST",body:new URLSearchParams({__CALLBACKID:"JTable1",__CALLBACKPARAM:JSON.stringify({Method:"LoadDataToJSON",Args:[0,JSON.stringify([{k:"o_id",v:internal,c:"@="},{k:"so_id",v:online,c:"@="}])]})})});results.innerHTML='+JSON.stringify('<div class="_jt_row"><div class="_jt_cell_o_id">'+spec.internal+'</div><div class="_jt_cell_so_id">'+spec.online+'</div><div class="_jt_cell_shop_name">'+spec.shop+'</div><div class="_jt_cell_status">已发货</div></div>')+';_jt_pagebar.textContent="总共 1 页 1 条记录";}</script>');
      else res.end('<div>synthetic</div>');
    });
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
    const origin='http://127.0.0.1:'+server.address().port;
    browser=await chromium.launch({executablePath:edge,headless:true});
    const context=await browser.newContext({acceptDownloads:true});
    await context.route('**/*',route=>route.request().url().startsWith(origin+'/')?route.continue():route.abort());
    const page=await context.newPage();
    await page.goto(origin);
    const driver={page:async()=>page,query:async()=>({scope:{company:account.expectedCompany,coid:account.expectedCoid,uid:account.expectedUid}})};
    const pages=new AllocationPages(undefined,url=>url.startsWith(origin+'/'));
    // Explicitly local fixture verification; the production adapter remains gated.
    pages.liveContractVerified=true;
    pages.lookup=async()=>{
      const frame=page.frame({url:origin+'/list'});
      await frame.locator('#ExportOrder').waitFor();
      return {found:true,frame,internal:spec.internal,online:spec.online,shop:spec.shop,native_status:'已发货'};
    };
    result=await new AllocationFlow(driver,{store,root:input.state_root,pages}).run(input,account);
  }
  const report={synthetic_only:true,real_platform_requests:0,downloads,queries,result};
  await fs.writeFile(outputFile,JSON.stringify(report,null,2),{flag:'wx'});
  process.stdout.write(JSON.stringify({state:result.state,reused:!!result.reused,downloads,queries})+'\n');
} finally {
  if(browser)await browser.close();
  if(server)await new Promise(resolve=>server.close(resolve));
  await store.close();
}
