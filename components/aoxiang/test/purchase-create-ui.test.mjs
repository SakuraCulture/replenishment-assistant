import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {NativeImportGuard,installPurchaseWriteFence} from '../src/purchase-create-ui.mjs';
import {IMPORT_CODE} from '../src/purchase-create-contract.mjs';
import {sha256} from '../src/common.mjs';
import {PurchaseCreateUI} from '../src/purchase-create-ui.mjs';
import {isolatedEdge} from './edge-fixture.mjs';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
class Page extends EventEmitter{
  constructor(){super();this.routes=[];this.sent=[];this.blocked=[];}
  async route(_,f){this.routes.push(f);}
  async unroute(_,f){this.routes=this.routes.filter(x=>x!==f);}
  async request(r){await this.routes.at(-1)({request:()=>r,continue:async()=>this.sent.push(r),abort:async()=>this.blocked.push(r)});}
}
const bytes=Buffer.from('PK-frozen-xlsx-bytes'),p={id:'p1',file:{sha256:sha256(bytes)}};
const task='GEI@'+IMPORT_CODE+'@261101173932@7114';
const request=(body=Buffer.concat([Buffer.from('multipart-'),bytes]),method='PUT',endpoint='/gei/import/task/'+IMPORT_CODE)=>({url:()=> 'https://sc-gei-tc-supply-chain-portal.ele.me'+endpoint,method:()=>method,postDataBuffer:()=>body});
const response=(r,body=JSON.stringify({data:{taskId:task}}),status=200)=>({request:()=>r,status:()=>status,text:async()=>body});

test('native import sends exact bytes at most once and binds only the exact response request',async()=>{
  const page=new Page(),saved=[];await installPurchaseWriteFence(page);const guard=new NativeImportGuard(page,p,bytes,r=>saved.push(r));await guard.install();
  const r=request();await page.request(r);page.emit('response',response(request()));await Promise.resolve();assert.equal(saved.length,0);
  page.emit('response',response(r));const result=await guard.result();assert.equal(saved.length,1);assert.equal(result.taskId,task);assert.equal(result.fileSha256,sha256(bytes));
  await page.request(request());await assert.rejects(()=>guard.result(),{code:'IMPORT_SIDE_EFFECT_BLOCKED'});assert.equal(page.sent.length,1);
  await guard.dispose();await page.request(request());assert.equal(page.sent.length,1);assert.equal(page.blocked.length,2); // late native retry is still fenced
});
test('wrong bytes, more than one payload, create POST, and unrelated business mutations are blocked',async()=>{
  for(const r of [request(Buffer.from('wrong-file')),request(Buffer.concat([bytes,bytes])),request(undefined,'POST'),request(undefined,'PUT','/gei/import/task/WRONG-IMPORT-OPERATION'),request(undefined,'POST','/unknown/newBusinessAction'),request(undefined,'POST','/saas/chain/plan/supply/purchase/order/submit')]){
    const page=new Page(),guard=new NativeImportGuard(page,p,bytes);await guard.install();await page.request(r);assert.equal(page.sent.length,0);assert.equal(page.blocked.length,1);await assert.rejects(()=>guard.result());await guard.dispose();
  }
});
test('no task, conflicting tasks and HTTP failure never become a successful receipt',async()=>{
  for(const [body,status] of [['{}',200],[task+' '+task.replace('@7114','@7115'),200],[task,500]]){
    const page=new Page(),guard=new NativeImportGuard(page,p,bytes);await guard.install();const r=request();await page.request(r);page.emit('response',response(r,body,status));await assert.rejects(()=>guard.result(),{code:'SUBMISSION_UNKNOWN'});await guard.dispose();
  }
});

test('guard permits exact identity read, keeps writes blocked and records only safe request metadata',async()=>{
  const page=new Page(),guard=new NativeImportGuard(page,p,bytes);await guard.install();
  await page.request(request(undefined,'POST','/account/session/getLoginInfo'));assert.equal(page.sent.length,1);
  const r=request();await page.request(r);page.emit('response',response(r));await guard.result();
  await page.request(request(undefined,'POST','/unknown/newBusinessAction?token=must-not-log'));
  await assert.rejects(()=>guard.result(),e=>{
    assert.equal(e.code,'IMPORT_SIDE_EFFECT_BLOCKED');assert.equal(e.details.acceptedTaskId,task);
    assert.deepEqual(e.details.blocked,[{kind:'unapproved_request',method:'POST',host:'sc-gei-tc-supply-chain-portal.ele.me',path:'/unknown/newBusinessAction'}]);
    assert.ok(!JSON.stringify(e.details).includes('must-not-log'));return true;
  });await guard.dispose();
});
test('late body is awaited, receipt persistence failure is surfaced, read-only queries pass fence',async()=>{
  const page=new Page();await installPurchaseWriteFence(page);await page.request(request(undefined,'POST','/saas/chain/plan/supply/purchase/order/pageQuery'));assert.equal(page.sent.length,1);
  const guard=new NativeImportGuard(page,p,bytes,()=>{throw Error('disk unavailable');});await guard.install();const r=request();await page.request(r);
  let release;page.emit('response',{...response(r),text:()=>new Promise(resolve=>release=resolve)});
  const pending=guard.result();release(task);await assert.rejects(()=>pending,/disk unavailable/);await guard.dispose();
});

test('isolated real Edge native file input/confirm: one exact multipart body; late retry fenced; no external network', {skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge(),directory=await fs.mkdtemp(path.join(os.tmpdir(),'ax-import-ui-'));
  let ui;
  try{
    const real=fixture.browser.contexts()[0].pages()[0];
    // Context and default page handlers deny all traffic. The proxy fulfills only this
    // test's permitted route locally; no route.continue() reaches any external host.
    await real.context().route('**/*',r=>r.abort());
    await real.route('**/*',r=>r.abort());
    let accepted=0;const handlers=new Map(),received=[];
    const page=new Proxy(real,{get(target,key){
      if(key==='route')return async(pattern,handler)=>{const wrapped=route=>handler({request:()=>route.request(),abort:()=>route.abort(),continue:async()=>{
        const req=route.request();if(req.method()==='OPTIONS')return route.fulfill({status:204,headers:{'access-control-allow-origin':'*','access-control-allow-methods':'PUT,OPTIONS','access-control-allow-headers':'content-type'}});
        assert.equal(req.method(),'PUT');assert.ok(req.url().endsWith('/gei/import/task/'+IMPORT_CODE));accepted++;received.push(req.postDataBuffer());
        await route.fulfill({headers:{'access-control-allow-origin':'*','content-type':'application/json'},body:JSON.stringify({success:true,data:{taskId:task}})});
      }});handlers.set(handler,wrapped);await target.route(pattern,wrapped);};
      if(key==='unroute')return (pattern,handler)=>target.unroute(pattern,handlers.get(handler));
      const v=target[key];return typeof v==='function'?v.bind(target):v;
    }});
    const file=path.join(directory,'synthetic.xlsx');await fs.writeFile(file,bytes);
    for(const automatic of [false,true]){
      await real.setContent(`<form><input id="storeCodeList"></form><div class="next-dialog">请按照规定格式上传文件<input type="file" onchange="${automatic?'sendFile()':''}"><button onclick="sendFile()">确定</button></div><script>async function sendFile(){const f=document.querySelector('input[type=file]').files[0];const data=new FormData();data.append('file',f);try{await fetch('https://sc-gei-tc-supply-chain-portal.ele.me/gei/import/task/${IMPORT_CODE}',{method:'PUT',body:data});}catch{} }</script>`);
      await installPurchaseWriteFence(page);ui=new PurchaseCreateUI(page,{timeoutMs:1500});ui.identity={accountId:'A',merchantCode:'M'};
      const p2={...p,id:'p-'+automatic,identity:ui.identity,file:{path:file,sha256:sha256(bytes),bytes:bytes.length}};
      const query={identity:ui.identity,orderIds:[],request:{storeCodeList:['T']},filterState:await ui.readFilterState()};
      ui.lastQuery=query;const baseline={id:p2.id,query,preparedAt:'local-test'};ui.createPrepared=baseline;
      const before=accepted,r=await ui.uploadPurchaseOnce({intent:p2,baseline});assert.equal(r.taskId,task);assert.equal(accepted-before,1);assert.ok(received.at(-1).includes(bytes));
      await assert.rejects(()=>ui.uploadPurchaseOnce({intent:p2,baseline}),{code:'IMPORT_NOT_PREPARED'});
      await real.evaluate(()=>sendFile());assert.equal(accepted-before,1);
      await ui.dispose();ui=null;
    }
  }finally{await ui?.dispose();await fixture.close();await fs.rm(directory,{recursive:true,force:true});}
});
