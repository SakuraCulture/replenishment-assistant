import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {createArchive,openArchive} from '../src/zip.mjs';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {purchaseContract,purchaseScopeHash,purchaseDays,dateBounds,exportScope,PURCHASE_CODES} from '../src/purchase-contract.mjs';
import {PurchaseCoordinator,validatePurchasePair} from '../src/purchase-coordinator.mjs';
import {validateWorkbook,fileHash} from '../src/workbook-validator.mjs';
import {exportMigration,previewMigration,applyMigration,importMigrationAssets} from '../src/migration.mjs';
import {isolatedEdge} from './edge-fixture.mjs';
import {PurchaseUI} from '../src/purchase-ui.mjs';

const identity={accountId:'100',merchantCode:'merchant',displayName:'测试商家'},target={buyerWarehouseCode:'WH',displayName:'测试仓店',operatingStores:[],mappingSource:'native',mappingVerifiedAt:'2026-11-01T00:00:00Z'};
const input={buyerWarehouseCode:'WH',from:'2026-10-31',to:'2026-10-31',dateBasis:'created',orderId:null},request={storeCodeList:['WH'],orderType:'3',merchantSubBizType:'CONSIGNEE',pageIndex:'1',pageSize:'30',sort:'main_order_gmt_create',...dateBounds(input.from,input.to)};
const query={warehouseCode:'WH',name:target.displayName,total:1,request,orderIds:['PO1'],orderSkuCounts:{PO1:2},identity,filterState:{},observedAt:'2026-11-01T00:00:00Z'};
const escape=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
function column(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
async function fixture(file,sourceId,{orphan=false,missing=false,wrongDate=false,duplicate=false}={}){
  const c=purchaseContract(sourceId),row=()=>Array(c.columns.length).fill(''),values=[];
  if(sourceId==='D16'){const r=row();r[0]='PO1';r[3]='WH';r[6]='2.0';values.push(r);}
  else for(const sku of missing?['900719925474099312345']:['900719925474099312345','SKU2',...(duplicate?['SKU2']:[])]){const r=row();r[0]=orphan?'OTHER':'PO1';r[7]='WH';r[12]=sku;r[40]=wrongDate?'2026-10-30 12:00:00':'2026-10-31 12:00:00';r[41]='2026-11-01 12:00:00';values.push(r);}
  const xml=[c.columns.map(c=>c.rawHeader),...values].map((r,i)=>`<row r="${i+1}">${r.map((v,k)=>`<c r="${column(k)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+xml+'</sheetData></worksheet>'}]);return file;
}
async function setup(){const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-purchase-')),state=await StateStore.open(path.join(root,'state')),config=freshConfig(identity,[target]),binding={outputRoot:path.join(root,'output')};return {root,state,config,binding};}
function adapterFor(ctx,{unknown=false,failDetailOnce=false}={}){
  const stats={submits:0,downloads:0},taskIds={};let fail=failDetailOnce;
  const adapter={queryPurchase:async()=>structuredClone(query),preparePurchase:async()=>({taskIds:[]}),submitPurchase:async({intentId,sourceId})=>{stats.submits++;if(unknown)throw Object.assign(new Error('lost response'),{code:'WORKER_RESPONSE_UNKNOWN'});const taskId='GEI@'+PURCHASE_CODES[sourceId]+'@261101000000@'+stats.submits,receipt={intentId,taskId,code:PURCHASE_CODES[sourceId],scope:exportScope(request)};taskIds[sourceId]=taskId;ctx.state.recordPurchaseReceipt(receipt);return receipt;},purchaseTask:async(taskId,sourceId)=>({taskId,taskCode:PURCHASE_CODES[sourceId],taskStatus:'FINISHED',successCount:sourceId==='D16'?1:2,failCount:0}),downloadPurchase:async({directory,taskId,sourceId})=>{stats.downloads++;if(sourceId==='D17'&&fail){fail=false;throw Object.assign(new Error('network'),{code:'PURCHASE_DOWNLOAD_HTTP'});}await fs.mkdir(directory,{recursive:true});const file=await fixture(path.join(directory,sourceId+'.xlsx'),sourceId);return {file,taskId,sha256:await fileHash(file)};}};return {adapter,stats};
}

test('采购时间按上海自然日，日切片无漏日，查询分页字段不进入导出范围',()=>{
  assert.deepEqual(dateBounds('2026-10-31','2026-10-31'),{start:'1793376000000',end:'1793462399000'});assert.deepEqual(purchaseDays('2026-10-30','2026-11-01'),['2026-10-30','2026-10-31','2026-11-01']);assert.throws(()=>dateBounds('2026-02-30','2026-03-01'),{code:'PURCHASE_DATE'});assert.throws(()=>dateBounds('bad','bad'),{code:'PURCHASE_DATE'});assert.equal(exportScope(request).pageIndex,undefined);assert.equal(exportScope(request).merchantSubBizType,'CONSIGNEE');
});
test('状态升级先备份；采购表丢失不能重建；原补货记录保留',async()=>{
  const ctx=await setup();ctx.state.putRun({id:'existing',status:'SUCCEEDED'});await ctx.state.enablePurchases();assert.ok((await fs.readdir(path.join(ctx.state.root,'recovery'))).some(n=>n.startsWith('state-before-purchase-')));assert.equal(ctx.state.getRun('existing').status,'SUCCEEDED');const root=ctx.state.root;ctx.state.close();const db=new DatabaseSync(path.join(root,'state.sqlite'));db.exec('DROP TABLE purchase_runs');db.close();assert.throws(()=>new StateStore(root),{code:'STATE_CORRUPT'});
});
test('真实XLSX校验采购父子关联；缺SKU、跨主单、日期超界拒绝，重复明细保留',async()=>{
  const ctx=await setup(),main=await validateWorkbook(await fixture(path.join(ctx.root,'main.xlsx'),'D16'),{contract:purchaseContract('D16')}),run={...input,query};
  for(const [variation,code] of [[{missing:true},'PURCHASE_CHILD_COVERAGE'],[{orphan:true},'PURCHASE_CHILD_ORPHAN'],[{wrongDate:true},'PURCHASE_FILE_DATE']]){const detail=await validateWorkbook(await fixture(path.join(ctx.root,code+'.xlsx'),'D17',variation),{contract:purchaseContract('D17')});assert.throws(()=>validatePurchasePair(run,main,detail),{code});}
  const detail=await validateWorkbook(await fixture(path.join(ctx.root,'detail.xlsx'),'D17',{duplicate:true}),{contract:purchaseContract('D17')});assert.equal(validatePurchasePair(run,main,detail).detailRows,3);assert.equal(detail.duplicateWarehouseSkuRows,1);ctx.state.close();
});
test('真实隔离Edge：列表内同名仓店不干扰采购选择器',{skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge();let ui;
  try{
    const page=fixture.browser.contexts()[0].pages()[0];await page.setContent('<form><input id="storeCodeList"><button type="button">重置</button><button type="button">查询</button></form><div class="next-tree-node-label" onclick="window.picked=(window.picked||0)+1">测试仓店</div><table><tbody><tr><td><div onclick="window.wrong=true">测试仓店</div></td></tr></tbody></table>');
    ui=new PurchaseUI(page,{timeoutMs:3000});ui.identity=identity;ui.open=async()=>identity;ui.setDates=async()=>{};ui.queryAction=async action=>{await action();return {request,total:1,rows:[{supplyOrderId:'PO1',buyerWarehouseCode:'WH',totalSku:'2'}]};};
    const q=await ui.queryPurchase({...input,name:target.displayName,identity});assert.equal(q.total,1);assert.equal(await page.evaluate(()=>window.picked),1);assert.equal(await page.evaluate(()=>window.wrong),undefined);
  }finally{await ui?.dispose();await fixture.close();}
});
test('明细下载中断恢复原任务；主单不重导；再次运行校验并复用文件',async()=>{
  const ctx=await setup(),{adapter,stats}=adapterFor(ctx,{failDetailOnce:true}),engine=new PurchaseCoordinator({...ctx,adapter});await assert.rejects(()=>engine.run({target,...input}),{code:'PURCHASE_DOWNLOAD_HTTP'});const pending=ctx.state.rows('purchase_runs')[0],ids=Object.values(pending.exports).map(e=>e.taskId);assert.equal(stats.submits,2);
  const done=await engine.run({runId:pending.id});assert.equal(done.status,'SUCCEEDED');assert.equal(done.errorCode,undefined);assert.equal(done.exports.D17.errorCode,undefined);assert.deepEqual(Object.values(done.exports).map(e=>e.taskId),ids);assert.equal(stats.submits,2);assert.equal(done.pairValidation.mainRows,1);assert.equal(done.pairValidation.detailRows,2);await engine.run({target,...input});assert.equal(stats.submits,2);assert.equal(stats.downloads,3);ctx.state.close();
});
test('采购受理未知持久化并阻断重跑及另一日期，不盲目再点导出',async()=>{
  const ctx=await setup(),{adapter,stats}=adapterFor(ctx,{unknown:true}),engine=new PurchaseCoordinator({...ctx,adapter});await assert.rejects(()=>engine.run({target,...input}),{code:'WORKER_RESPONSE_UNKNOWN'});const id=ctx.state.rows('purchase_runs')[0].id;
  await assert.rejects(()=>engine.run({runId:id}),{code:'SUBMISSION_UNKNOWN'});await assert.rejects(()=>engine.run({target,...input,rerun:true}),{code:'PURCHASE_UNRESOLVED'});await assert.rejects(()=>engine.run({target,...input,from:'2026-11-01',to:'2026-11-01'}),{code:'PURCHASE_UNRESOLVED'});assert.equal(stats.submits,1);ctx.state.close();
});
test('用户改过旧采购文件时正常复用停止；显式重跑另建快照并保留旧文件',async()=>{
  const ctx=await setup(),{adapter,stats}=adapterFor(ctx),engine=new PurchaseCoordinator({...ctx,adapter}),first=await engine.run({target,...input}),file=path.join(ctx.binding.outputRoot,first.exports.D16.asset.relativePath);await fs.appendFile(file,'user-change');const editedHash=await fileHash(file);
  await assert.rejects(()=>engine.run({target,...input}),{code:'PURCHASE_ASSET_CHANGED'});const second=await engine.run({target,...input,rerun:true});assert.equal(second.status,'SUCCEEDED');assert.notEqual(second.id,first.id);assert.equal(ctx.state.getPurchase(first.id).status,'SUPERSEDED');assert.equal(await fileHash(file),editedHash);assert.equal(stats.submits,4);ctx.state.close();
});
test('采购迁移携带原任务和原生文件，新输出目录恢复；未知状态不会被旧包抹掉',async()=>{
  const ctx=await setup(),{adapter}=adapterFor(ctx),engine=new PurchaseCoordinator({...ctx,adapter}),done=await engine.run({target,...input}),file=path.join(ctx.root,'move.zip');await exportMigration({...ctx,output:file,sourceStopped:true,includeResults:true,outputRoot:ctx.binding.outputRoot});const preview=await previewMigration(file);assert.equal(preview.manifest.schemaVersion,'2.1');assert.equal(preview.manifest.purchaseRecordCount,1);assert.equal(preview.manifest.purchaseAssets.length,2);
  const dest=await StateStore.open(path.join(ctx.root,'different','state'));await applyMigration(preview,{state:dest,stateRoot:dest.root});assert.equal(dest.getPurchase(done.id).exports.D17.taskId,done.exports.D17.taskId);const outputRoot=path.join(ctx.root,'new-output');const imported=await importMigrationAssets(file,{state:dest,outputRoot,expectedIdentity:identity});assert.equal(imported.saved,2);for(const e of Object.values(done.exports))assert.equal(await fileHash(path.join(outputRoot,e.asset.relativePath)),e.asset.sha256);
  const unknown=dest.getPurchase(done.id);unknown.exports.D17.stage='SUBMISSION_UNKNOWN';dest.putPurchase(unknown);dest.importPurchases(preview.manifest.purchaseRecords);assert.equal(dest.getPurchase(done.id).exports.D17.stage,'SUBMISSION_UNKNOWN');await fs.writeFile(path.join(dest.root,'recovery/migration-status.json'),JSON.stringify({tenantBinding:identity,needsMachineSetup:false,runBlocked:false}));const afterImport=new PurchaseCoordinator({state:dest,config:ctx.config,binding:{outputRoot},adapter});await assert.rejects(()=>afterImport.run({target,...input,rerun:true}),{code:'PURCHASE_UNRESOLVED'});ctx.state.close();dest.close();
});
test('采购迁移删减主子任务不能用新包解除未知提交',async()=>{
  const ctx=await setup(),{adapter}=adapterFor(ctx,{unknown:true}),engine=new PurchaseCoordinator({...ctx,adapter});await assert.rejects(()=>engine.run({target,...input}));const good=path.join(ctx.root,'good.zip');await exportMigration({...ctx,output:good});const zip=await openArchive(good),members=[];for(const n of zip.entries.keys()){let buffer=await zip.buffer(n);if(n==='migration.manifest.json'){const m=JSON.parse(buffer);m.purchaseRecords=[];buffer=Buffer.from(JSON.stringify(m));}members.push({relativePath:n,buffer});}zip.close();const bad=path.join(ctx.root,'bad.zip');await createArchive(bad,members);await assert.rejects(()=>previewMigration(bad),{code:'MIGRATION_PURCHASE_INCOMPLETE'});ctx.state.close();
});
test('真实隔离Edge：Fusion采购确认框只提交一次，迟到提示不挡历史记录',{skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge();let ui;
  try{
    const page=fixture.browser.contexts()[0].pages()[0],prefix='https://sc-gei-tc-supply-chain-portal.ele.me';let submissions=0;
    await page.route(prefix+'/**',async route=>{if(route.request().url().endsWith('/create/export/task')){submissions++;await route.fulfill({headers:{'access-control-allow-origin':'*'},body:JSON.stringify({success:true,data:{taskId:'GEI@'+PURCHASE_CODES.D16+'@261101000000@1'}})});}else await route.fulfill({headers:{'access-control-allow-origin':'*'},body:'{"success":true,"total":1,"data":[{"taskId":"known"}]}'});});
    await page.setContent(`<form><input id="storeCodeList"></form><button onclick="document.querySelector('#menu').hidden=false">导出 </button><div id="menu" hidden><span onclick="confirmExport()">导出主单</span></div><button onclick="historyOpen()">历史记录</button><script>
    function confirmExport(){const d=document.createElement('div');d.className='next-dialog next-dialog-quick';d.innerHTML='确认现在导出吗？<button onclick="sendExport(this)">确定</button>';document.body.append(d);}
    async function sendExport(b){const r=await fetch('${prefix}/ebai/gei/create/export/task',{method:'POST',body:JSON.stringify({code:'${PURCHASE_CODES.D16}',query:JSON.stringify(${JSON.stringify(exportScope(request))})})});await r.text();b.parentNode.remove();setTimeout(()=>{const d=document.createElement('div');d.className='next-dialog';d.innerHTML='正在导出数据… 请点击历史记录查看结果<button onclick="this.parentNode.remove()">我知道了</button><button>历史记录</button>';document.body.append(d);},20);}
    function historyOpen(){fetch('${prefix}/ebai/gei/task/list',{method:'POST',body:'{}'});const d=document.createElement('div');d.className='next-dialog';d.innerHTML='<div class="next-dialog-header">历史记录</div>';document.body.append(d);}
    </script>`);
    ui=new PurchaseUI(page,{timeoutMs:5000});ui.identity=identity;ui.lastQuery=query;const q={...query,filterState:await ui.readFilterState()},receipt=await ui.submitPurchase({intentId:'one',sourceId:'D16',query:q});assert.equal(receipt.taskId.endsWith('@1'),true);await page.waitForTimeout(50);assert.equal((await ui.history()).data[0].taskId,'known');assert.equal(ui.noticeDismissals,1);await assert.rejects(()=>ui.submitPurchase({intentId:'one',sourceId:'D16',query:q}),{code:'INTENT_ALREADY_DISPATCHED'});assert.equal(submissions,1);
  }finally{await ui?.dispose();await fixture.close();}
});
