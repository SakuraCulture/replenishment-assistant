import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createRequire} from 'node:module';
import {ARCHIVE,archiveDownloadName,archiveLogRow,exportArchive,recoverArchive,validateArchiveRequest,verifyArchiveConditions} from '../src/archive-export.mjs';

const title='商品主题分析_全商品档案';
const stamp='20261122232831', task='211513959';
const filename=title+'_'+stamp+'_'+task+'_1.xlsx';
const company='优团河北石家庄仓', partner='优团山东临沂仓';
const order='订单状态@=WAITCONFIRM,WAITFCONFIRM,WAITDELIVER,DELIVERING,SENT,QUESTION,WAITOUTERSENT,CANCELLED';
const condition=warehouse=>order+';&nbsp;其他=排除发货前取消商品'+(warehouse?';&nbsp;仓库='+warehouse:'');
const filters=id=>JSON.stringify([
  {k:'A.status',v:order.split('=')[1],c:'@=',t:''},
  {k:'C.sent_flag',v:'1',c:'@=',t:''},
  ...(id?[{k:'A.wms_co_id',v:id,c:'@=',t:''}]:[]),
]);
const reportColumns=JSON.stringify([{id:'first_io_date',text:'首次入库时间',visible:true}]);
const accountDefault={kind:ARCHIVE,reportScope:'account_default'};
const oldSelection={kind:ARCHIVE,reportWarehouse:company,reportWarehouseId:'6'};
const nativeFile=new URL('https://jstzbstatic.erp321.com/export/abcdef.xlsx');
nativeFile.searchParams.set('response-content-disposition','attachment;filename='+encodeURIComponent(filename));
const base='https://bi.erp321.com/app/report/subject/itemskuanalysis/';
const lurl='https://www.erp321.com/app/common/export/export_log.aspx';
const {chromium}=createRequire(new URL('../../aoxiang/package.json',import.meta.url))('playwright-core');
const edge=process.env.JST_TEST_EDGE||path.join(process.env['ProgramFiles(x86)']||'/missing','Microsoft/Edge/Application/msedge.exe');

test('full-format native metadata rejects other warehouses, filters, receipts, or partial fragments',()=>{
  assert.deepEqual(archiveDownloadName(filename),{stamp,task});
  assert.throws(()=>archiveDownloadName(filename.replace('_1.xlsx','_2.xlsx')),/单片/);
  assert.equal(archiveLogRow([1,task,title+'_'+stamp,'4,512','2026-11-22 23:28:31',title,'100%','1','下载完成'],task).expectedRows,4512);
  assert.throws(()=>archiveLogRow([1,task,title+'_'+stamp,'4512','2026-11-22 23:28:31','商品资料','100%','1','下载完成'],task),/类型/);
  assert.equal(verifyArchiveConditions('提示 !\n'+condition(company)+'\n关闭',oldSelection),true);
  assert.equal(verifyArchiveConditions(condition(partner),oldSelection),false);
  assert.equal(verifyArchiveConditions(condition(company)+';是否排除数据为0的商品=是',oldSelection),false);
  assert.equal(verifyArchiveConditions(condition(company)+';包含商品包含XXX',oldSelection),false);
  const scope={...oldSelection,expectedRows:4512};
  const args={page_count:'4512',export_remark:condition(company),rpt_config:reportColumns,search:filters('6')};
  assert.equal(validateArchiveRequest([JSON.stringify(args)],scope),true);
  assert.equal(validateArchiveRequest([JSON.stringify({...args,page_count:4219})],scope),false);
  assert.equal(validateArchiveRequest([JSON.stringify({...args,export_remark:condition(partner)})],scope),false);
  assert.equal(validateArchiveRequest([JSON.stringify({...args,search:filters('3')})],scope),false);
});

test('account default requires positive scope evidence and forbids any warehouse predicate',()=>{
  const scope={...accountDefault,expectedRows:2};
  const args={page_count:'2',export_remark:condition(),rpt_config:reportColumns,search:filters()};
  assert.equal(validateArchiveRequest([JSON.stringify(args)],scope),true);
  assert.equal(validateArchiveRequest([JSON.stringify(args)],{expectedRows:2}),false);
  assert.equal(validateArchiveRequest([JSON.stringify(args)],{...scope,reportWarehouseId:'6'}),false);
  assert.equal(validateArchiveRequest([JSON.stringify({...args,rpt_config:'[]'})],scope),false);
  assert.equal(validateArchiveRequest([JSON.stringify({...args,search:filters('6')})],scope),false);
  assert.equal(verifyArchiveConditions('提示 !\n'+condition()+'\n关闭',scope),true);
  assert.equal(verifyArchiveConditions(condition(company),scope),false);
});

function fixturePage(url,warehouse,scenario){
  if(url===base+'default.aspx')return '<iframe id="f" src="'+base+'filter.aspx"></iframe><div id="right_td"></div><iframe id="g" src="'+base+'allsku.aspx"></iframe>'
    +'<script>addEventListener("message", e=>{right_td.textContent="当前查询条件==> "+e.data+";";g.src="'
    +base+'allsku.aspx?generated=1";});</script>';
  if(url===base+'filter.aspx')return '<select id="sale_qty"><option>30天有销量</option><option>无限制</option></select>'
    +'<select id="enabled"><option>启用</option><option>-全部-</option></select><select id="stock_disabled"><option>-全部-</option></select>'
    +'<input id="is_gift" type="checkbox"><input id="sent_flag" type="checkbox" checked><input id="is_filter_empty" type="checkbox" checked>'
    +'<input id="sku_id">'+(scenario==='no_warehouses'?'':
      '<label><input id="mw_3" type="checkbox" style="display:none" checked>'+partner+'</label>'
      +'<label><input id="mw_6" type="checkbox">'+company+'</label>')+'<span id="run" onclick="generate()">生成报表</span>'
    +'<script>function generate(){const wh=[...document.querySelectorAll(\'input[id^="mw_"]:checked\')];'
    +'parent.postMessage((wh.length?"仓库="+wh[0].labels[0].innerText:"")+(is_filter_empty.checked?";是否排除数据为0的商品=是":""),"*");}</script>';
  if(url===base+'allsku.aspx')return '点击左侧【生成报表】，查看报表数据!';
  if(url===base+'allsku.aspx?generated=1')return '<table><tr><td>首次入库时间</td></tr></table>'
    +'<input type="hidden" id="dataPageCount" value="2"><span class="excel" onclick="native()">导出EXCEL</span>'
    +'<script>function send(url,Method,Args){return fetch(url,{method:"POST",body:new URLSearchParams('
    +'{__CALLBACKID:"ACall1",__CALLBACKPARAM:JSON.stringify({Method,Args,CallControl:"{page}"})})}).then(r=>r.text());}'
    +'async function native(){const ack=await send(location.href,"CreateAsyncExport",[JSON.stringify('
    +JSON.stringify({page_count:'2',rpt_config:scenario==='hidden_date'?'[]':reportColumns,
      export_remark:condition(),search:filters(scenario==='wrong_request'?'3':null)})
    +')]);const task=JSON.parse(ack.slice(2)).ReturnValue.Data;'
    +'await send("/app/common/export/export_log.aspx","GetReportExportData",[task]);}</script>';
  if(url===lurl)return '<div id="_jt_body_list"><div class="_jt_row">'
    +['1',task,title+'_'+stamp,'2','2026-11-22 23:28:31',title,'100%','1','下载完成'].map(s=>'<div class="_jt_cell">'+s+'</div>').join('')
    +'<div class="_jt_cell"><span onclick="msg_body.style.display=\'block\'">导出条件</span>'
    +'<span data-url="'+nativeFile.href.replaceAll('&','&amp;')+'">下载</span></div></div></div>'
    +'<div id="msg_body" style="display:none">提示 !<br>'+condition(scenario==='wrong_receipt'?partner:scenario==='legacy_resume'?warehouse:null)
    +'<br><span id="msg_close" onclick="msg_body.style.display=\'none\'">关闭</span></div>';
  return '<div id="root"> '+company+' [测试员]<br><a class="menuItem" onclick="m.hidden=false">报表</a>'
    +'<div id="m" hidden><div class="title1" onclick="report.src=\''+base+'default.aspx\'">商品主题分析</div></div>'
    +'<div class="item" title="下载" onclick="historyFrame.src=\''+lurl+'\'"><svg width="24" height="24">'
    +'<symbol id="icon-download-line"><rect width="22" height="22"/></symbol><use href="#icon-download-line"/></svg></div>'
    +'<iframe id="report" width="800" height="420"></iframe><iframe id="historyFrame" width="800" height="400"></iframe></div>';
}

test('native callbacks identify one submission and one file in the owned browser page',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'archive-report-'));
  const browser=await chromium.launch({executablePath:edge,headless:true});
  t.after(async()=>{await browser.close();await fs.rm(root,{recursive:true,force:true});});
  for(const scenario of ['matched','no_warehouses','wrong_request','wrong_receipt','hidden_date','legacy_resume','default_resume']){
    const context=await browser.newContext({acceptDownloads:true});
    let sent=0,downloaded=0;
    const bytes=Buffer.concat([Buffer.from('504b0304','hex'),Buffer.alloc(200,2)]);
    await context.route('**/*',route=>{
      const request=route.request(),url=request.url();
      if(url===nativeFile.href){
        downloaded++;
        return route.fulfill({headers:{'content-disposition':"attachment; filename*=UTF-8''"+encodeURIComponent(filename)},body:bytes});
      }
      if(request.method()==='POST'){
        if(url===base+'allsku.aspx?generated=1'){
          sent++;
          return route.fulfill({body:'0|'+JSON.stringify({IsSuccess:true,ReturnValue:{Code:'Success',Data:Number(task)}})});
        }
        assert.equal(url,'https://bi.erp321.com/app/common/export/export_log.aspx');
        return route.fulfill({body:'0|'+JSON.stringify({IsSuccess:true,ReturnValue:nativeFile.href})});
      }
      return route.fulfill({contentType:'text/html; charset=utf-8',body:fixturePage(url,company,scenario)});
    });
    const page=await context.newPage();
    await page.goto('https://www.erp321.com/epaas');
    if(scenario.endsWith('_resume')){
      const scope={...(scenario==='legacy_resume'?oldSelection:accountDefault),expectedRows:2};
      const result=await recoverArchive(page,{scope,recordId:task,output:root});
      assert.equal(result.scope.taskId,task);
      assert.deepEqual(await fs.readFile(result.file),bytes);
      assert.equal(sent,0);
      assert.equal(downloaded,1);
      await context.close();
      continue;
    }
    const manifests=[];
    const promise=exportArchive(page,{warehouse:'河北仓',expectedCompany:company,expectedCoid:'61',expectedUid:'62',
      output:root,onManifest:async receipt=>manifests.push(receipt)});
    if(scenario==='wrong_receipt')await assert.rejects(promise,/回执的全量筛选或所属仓不同/);
    else if(scenario==='wrong_request'||scenario==='hidden_date')await assert.rejects(promise,/实际导出请求/);
    else{
      const result=await promise;
      assert.equal(result.scope.expectedRows,2);
      assert.equal(result.scope.reportScope,'account_default');
      assert.equal(result.scope.reportWarehouse,undefined);
      assert.equal(result.scope.kind,ARCHIVE);
      assert.equal(result.scope.taskId,task);
      assert.deepEqual(await fs.readFile(result.file),bytes);
    }
    assert.equal(sent,['wrong_request','hidden_date'].includes(scenario)?0:1);
    assert.equal(downloaded,['matched','no_warehouses'].includes(scenario)?1:0);
    assert.equal(manifests.find(x=>x.stage==='SUBMITTING').submissions,1);
    if(sent)assert.equal(manifests.find(x=>x.stage==='ACCEPTED').nativeTaskRef,task);
    else assert.equal(manifests.at(-1).submissions,0);
    await context.close();
  }
});
