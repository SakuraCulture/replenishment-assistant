import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {StateStore} from '../src/state.mjs';
import {Coordinator} from '../src/coordinator.mjs';
import {freshConfig,fieldContract} from '../src/config.mjs';
import {createArchive} from '../src/zip.mjs';
import {sha256,AppError} from '../src/common.mjs';

const identity={accountId:'100',merchantCode:'M',displayName:'测试商家'};
const target={buyerWarehouseCode:'WARE01',displayName:'测试独立仓点',operatingStores:[],mappingSource:'native-selector',mappingVerifiedAt:new Date().toISOString()};
const letter=i=>{let r='';for(i++;i;i=Math.floor((i-1)/26))r=String.fromCharCode(65+(i-1)%26)+r;return r;};
async function context(){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-engine-'));const state=await StateStore.open(path.join(root,'state'));
  const headers=(await fieldContract()).columns.map(c=>c.rawHeader),values=Array(54).fill('');values[1]='WARE01';values[7]='SKU001';
  const esc=x=>String(x).replaceAll('&','&amp;').replaceAll('<','&lt;');
  const rows=[headers,values].map((row,i)=>`<row r="${i+1}">${row.map((v,k)=>`<c r="${letter(k)}${i+1}" t="inlineStr"><is><t>${esc(v)}</t></is></c>`).join('')}</row>`).join('');
  const file=path.join(root,'native.xlsx');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:'<workbook xmlns:r="urn:r"><sheets><sheet name="商品补货建议V2" r:id="r1"/></sheets></workbook>'},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+rows+'</sheetData></worksheet>'}]);
  const bytes=await fs.readFile(file);let submits=0,downloads=0;
  const adapter={
    onReceipt:null,
    async verifyIdentity(){return identity;},
    async querySingle(){return {warehouseCode:'WARE01',name:target.displayName,total:1,observedAt:new Date().toISOString(),identity};},
    async prepareExport(){return {taskIds:[],total:0};},
    async submitOnce({intentId}){submits++;this.onReceipt?.({intentId,taskId:'task123',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:['WARE01'],extraKeys:[]}});return {taskId:'task123',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2'};},
    async findTask(){return {taskId:'task123',taskStatus:'FINISHED',successCount:'1',failCount:'0',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',fileUrl:'https://tc-open-gateway-supply-deploy.oss-cn-zhangjiakou.aliyuncs.com/export/task123/file.xlsx?signature=not-a-real-signature'};},
  };
  const args={state,adapter,config:freshConfig(identity,[target]),binding:{outputRoot:path.join(root,'output')},download:async()=>{downloads++;return {file,sha256:sha256(bytes),bytes:bytes.length,taskId:'task123'};}};
  return {root,state,adapter,args,counts:()=>({submits,downloads})};
}
test('明确未派发的菜单失败可继续原作业，原意图留档且不变成受理未知',async t=>{
  const c=await context();t.after(()=>c.state.close());let calls=0;
  const original=c.adapter.submitOnce;
  c.adapter.submitOnce=async function(p){calls++;if(calls===1)throw new AppError('EXPORT_NOT_DISPATCHED','菜单收起，未派发');return original.call(this,p);};
  await assert.rejects(()=>new Coordinator(c.args).run(),{code:'EXPORT_NOT_DISPATCHED'});
  const run=c.state.rows('runs')[0],old=c.state.rows('attempts')[0];
  assert.equal(old.stage,'CANCELLED_BEFORE_SUBMIT');assert.equal(c.state.pending(identity,'WARE01').length,0);
  const done=await new Coordinator(c.args).run({runId:run.id});
  assert.equal(done.status,'SUCCEEDED');assert.equal(c.counts().submits,1);
  assert.equal(c.state.getAttempt(old.id).stage,'CANCELLED_BEFORE_SUBMIT');
});
test('完整作业只提交一次；当日再次正常运行复用合格资产',async()=>{
  const c=await context();const first=await new Coordinator(c.args).run();assert.equal(first.status,'SUCCEEDED');assert.deepEqual(c.counts(),{submits:1,downloads:1});
  const second=await new Coordinator(c.args).run();assert.equal(second.status,'SUCCEEDED');assert.deepEqual(c.counts(),{submits:1,downloads:1});assert.equal(c.state.getItems(second.id)[0].stage,'REUSED');c.state.close();
});

test('提交阶段不等待或下载，轮询未完成立即返回；恢复只取原任务且文件字节一致',async()=>{
  const c=await context(),coordinator=new Coordinator(c.args);let ready=false,lookups=0;
  const original=c.adapter.findTask;
  c.adapter.findTask=async id=>{lookups++;assert.equal(id,'task123');const t=await original();return {...t,taskStatus:ready?'FINISHED':'RUNNING'};};
  const submitted=await coordinator.run({phase:'submit'});
  assert.equal(submitted.pending,true);assert.equal(lookups,0);assert.deepEqual(c.counts(),{submits:1,downloads:0});
  const polled=await coordinator.run({runId:submitted.id,phase:'collect'});
  assert.equal(polled.pending,true);assert.equal(lookups,1);
  await coordinator.run({runId:submitted.id,phase:'submit'});assert.deepEqual(c.counts(),{submits:1,downloads:0});
  ready=true;const done=await coordinator.run({runId:submitted.id,phase:'collect'});
  assert.equal(done.status,'SUCCEEDED');assert.deepEqual(c.counts(),{submits:1,downloads:1});
  const asset=c.state.rows('assets')[0];assert.deepEqual(await fs.readFile(path.join(c.args.binding.outputRoot,asset.relativePath)),await fs.readFile(path.join(c.root,'native.xlsx')));c.state.close();
});
test('提交结果未知不可重放；正面受理已记录时可恢复同任务',async()=>{
  const c=await context();let calls=0;
  c.adapter.submitOnce=async()=>{calls++;throw new AppError('WORKER_RESPONSE_UNKNOWN','模拟已发送但响应丢失');};
  await assert.rejects(()=>new Coordinator(c.args).run(),{code:'WORKER_RESPONSE_UNKNOWN'});
  const unknownRun=c.state.rows('runs')[0];assert.equal(c.state.rows('attempts')[0].stage,'SUBMISSION_UNKNOWN');
  await assert.rejects(()=>new Coordinator(c.args).run({runId:unknownRun.id}),{code:'SUBMISSION_UNKNOWN'});assert.equal(calls,1);c.state.close();
  const d=await context();const normal=d.adapter.submitOnce;
  d.adapter.submitOnce=async function(p){await normal.call(this,p);throw new AppError('UI_ACK_TIMEOUT','受理已观察到，界面应答超时');};
  await assert.rejects(()=>new Coordinator(d.args).run(),{code:'UI_ACK_TIMEOUT'});
  const known=d.state.rows('runs')[0];assert.equal(d.state.rows('attempts')[0].stage,'ACCEPTED');
  await new Coordinator(d.args).run({runId:known.id});assert.deepEqual(d.counts(),{submits:1,downloads:1});d.state.close();
});
test('已下载后本地保存失败，恢复不再访问浏览器或重新下载',async()=>{
  const c=await context();
  await assert.rejects(()=>new Coordinator({...c.args,publish:async()=>{throw new AppError('SAVE_BLOCKED','模拟文件占用');}}).run(),{code:'SAVE_BLOCKED'});
  const run=c.state.rows('runs')[0];
  const offlineAdapter=new Proxy({},{get(){throw Error('本地恢复不得访问浏览器');}});
  await new Coordinator({...c.args,adapter:offlineAdapter,download:async()=>{throw Error('不得重新下载');}}).run({runId:run.id});assert.deepEqual(c.counts(),{submits:1,downloads:1});c.state.close();
});
test('已验收文件丢失时下载原任务；当前查询变动不改写原快照',async()=>{
  const c=await context();await new Coordinator(c.args).run();const a=c.state.rows('assets')[0];await fs.unlink(path.join(c.args.binding.outputRoot,a.relativePath));
  const original=c.adapter.querySingle;c.adapter.querySingle=async()=>({...await original(),total:2});
  await new Coordinator(c.args).run();assert.deepEqual(c.counts(),{submits:1,downloads:2});assert.equal(c.state.rows('assets')[0].rows,1);assert.equal(c.state.rows('assets')[0].collectedAt,a.collectedAt);c.state.close();
});
test('错误范围的受理记录立即阻断，即使工作进程随后失去应答',async()=>{
  const c=await context();c.adapter.submitOnce=async function(p){this.onReceipt({intentId:p.intentId,taskId:'wrong123',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:['WARE01'],extraKeys:['skuCodeList']}});throw new AppError('WORKER_RESPONSE_UNKNOWN','模拟错误范围受理后断开');};
  await assert.rejects(()=>new Coordinator(c.args).run());assert.equal(c.state.rows('attempts')[0].stage,'WRONG_SCOPE_ACCEPTED');const r=c.state.rows('runs')[0];await assert.rejects(()=>new Coordinator(c.args).run({runId:r.id}),{code:'SUBMISSION_UNKNOWN'});c.state.close();
});
test('派发前明确暂停可继续；未把未发送意图当成已提交任务重放',async()=>{
  const c=await context(),control=new AbortController();await assert.rejects(()=>new Coordinator({...c.args,signal:control.signal,onProgress:e=>{if(e.stage==='INTENT_RECORDED')control.abort();}}).run(),{code:'PAUSED'});
  assert.equal(c.state.rows('attempts')[0].stage,'CANCELLED_BEFORE_SUBMIT');assert.deepEqual(c.counts(),{submits:0,downloads:0});
  const r=c.state.rows('runs')[0];await new Coordinator(c.args).run({runId:r.id});assert.deepEqual(c.counts(),{submits:1,downloads:1});c.state.close();
});
