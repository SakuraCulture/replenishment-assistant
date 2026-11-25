// Real Edge processes and delivered browser bridge, with a local-only platform fixture.
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import http from 'node:http';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const out=path.resolve(process.argv[2]),components=path.resolve(process.argv[3]);
const held=process.argv.includes('--hold');
const solo=process.argv.includes('--one');
await fs.mkdir(out,{recursive:false});
const load=name=>import(pathToFileURL(path.join(components,name)).href);
const {CatalogService}=await load('jst_catalog/src/service.mjs');
const {AccountSessions}=await load('jst_catalog/src/accounts.mjs');
const {discoverEndpoint}=await load('aoxiang/src/environment.mjs');
const {WebSocket}=await load('aoxiang/node_modules/ws/wrapper.mjs');
const {sha}=await load('jst_catalog/src/common.mjs');
const [sd,hb,ax]=['山东仓','河北仓','翱象'];
const bridgeRoot=path.join(components,'aoxiang'),stateRoot=path.join(out,'jst');
const profiles=new Map(),intervals=[],counts={},queries=[];
const report={passed:false,scope:'真实独立 Edge 实例，受控本机 HTTP 回执，生产平台提交 0',started:new Date().toISOString(),intervals,counts,queries};
const delay=ms=>new Promise(resolve=>setTimeout(resolve,ms));
const server=http.createServer(async(request,response)=>{
  const url=new URL(request.url,'http://127.0.0.1');
  if(url.pathname!=='/query')return response.writeHead(404).end();
  const lane=url.searchParams.get('lane'),name=url.searchParams.get('operation');
  if(![sd,hb,ax].includes(lane)||!['initial','following','wrong_account'].includes(name))return response.writeHead(400).end();
  const row={lane,name,from_ms:Date.now()};intervals.push(row);
  if(held&&name==='initial'){
    if(intervals.filter(item=>item.name==='initial').length===(solo?1:3))
      await fs.writeFile(path.join(out,'active.json'),JSON.stringify(intervals));
    let released=false;
    for(let i=0;i<2400&&!released;i++){
      released=await fs.access(path.join(out,'release')).then(()=>true,()=>false);
      if(!released)await delay(100);
    }
    if(!released)return response.writeHead(408).end();
  }
  setTimeout(()=>{
    row.to_ms=Date.now();response.writeHead(200,{'Content-Type':'text/html; charset=utf-8'}).end('<title>isolation fixture</title>');
  },name==='initial'?(lane===hb?4100:3200):150);
});
await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
const origin=`http://127.0.0.1:${server.address().port}`;
const locks=path.join(out,'browser-locks');
const axSessions=new AccountSessions({stateRoot:path.join(out,'ax'),bridgeRoot,lockRoot:locks});
let service;
const label=new Map([[sd,'initial'],[hb,'initial']]);
const operation=(warehouse,operation_id)=>({action:'sync.start',purpose:'default_catalog',operation_id,
  items:[{warehouse,expected_catalog_digest:null}],scoped:true,scope:[warehouse],lease_id:operation_id});
async function until(predicate,timeout=30000){
  const end=Date.now()+timeout;
  while(!predicate()){
    if(Date.now()>end)throw Error('controlled-browser wait timed out');
    await delay(30);
  }
}
async function navigate(page,lane,name){
  const url=new URL('/query',origin);url.searchParams.set('lane',lane);url.searchParams.set('operation',name);
  await page.goto(url.href,{waitUntil:'domcontentloaded',timeout:held?255000:20000});
  assert.equal(await page.title(),'isolation fixture');
}
async function finish(bid,c){
  const end=Date.now()+(held?260000:30000);
  while(Date.now()<end){
    const state=service.status(bid),batch=state.batches[0];
    if(!state.running)return batch;
    for(const item of batch.items)if(item.stage==='WAIT_HOST'&&!item.observation){
      await service.command({...c,action:'sync.provide_observation',batch_id:bid,item_id:item.id,token:item.observationRequest,
        observation:{warehouse:item.account.warehouse,state:null,included:[]}});
    }
    await delay(30);
  }
  throw Error('original batch did not settle');
}
async function closeProfile(binding){
  const endpoint=await discoverEndpoint(binding.userDataRoot).catch(()=>null);
  if(endpoint)await new Promise((resolve,reject)=>{
    const socket=new WebSocket(endpoint),timer=setTimeout(()=>{socket.terminate();reject(Error('QA browser did not close'));},10000);
    socket.once('open',()=>socket.send(JSON.stringify({id:1,method:'Browser.close'})));
    socket.once('close',()=>{clearTimeout(timer);resolve();});socket.once('error',reject);
  });
}

try{
  service=await CatalogService.open({stateRoot,bridgeRoot,lockRoot:locks});
  const axBinding=await axSessions.automaticBinding(sd,[]);profiles.set(ax,axBinding);
  const axBridge=await axSessions.connect({binding:axBinding});
  if(solo){
    report.instances={[ax]:axBridge.facts};
    await navigate(axBridge.page,ax,'initial');
    report.common_ms=intervals[0].to_ms-intervals[0].from_ms;
  }else{
  for(const warehouse of [sd,hb]){
    const binding=await service.sessions.automaticBinding(warehouse,[...profiles.values()].map(binding=>({binding})));
    profiles.set(warehouse,binding);
  }
  const accounts=Object.fromEntries([sd,hb].map((warehouse,index)=>[warehouse,{id:index?'hb':'sd',warehouse,
    expectedCompany:'QA-'+index,expectedCoid:String(index+100),expectedUid:String(index+101),binding:profiles.get(warehouse),
    enabled:true,mappingVerified:true,bindingVersion:1}]));
  await service.store.write(data=>{data.accounts=structuredClone(accounts);});
  const jstPages=new Map();
  for(const account of Object.values(accounts))jstPages.set(account.warehouse,await service.sessions.connect(account));
  report.instances=Object.fromEntries([...jstPages,[ax,axBridge]].map(([key,bridge])=>[key,bridge.facts]));
  const endpoints=await Promise.all([...profiles.values()].map(p=>discoverEndpoint(p.userDataRoot)));
  assert.equal(new Set(endpoints).size,3);
  const scope=a=>({company:a.expectedCompany,coid:a.expectedCoid,uid:a.expectedUid,expectedRows:1,statuses:['启用','备用','禁用']});
  // Keep the actual transport, account connections, slot check and durable JobStore.
  delete service.driver.exportArchive;delete service.driver.recoverArchive;
  service.driver.page=async a=>(await service.sessions.connect(a)).page;
  service.driver.query=async(page,a)=>{
    const name=label.get(a.warehouse);queries.push({warehouse:a.warehouse,name});
    await navigate(page,a.warehouse,name);
    return {scope:{...scope(a),...(name==='wrong_account'?{uid:'9999'}:{})}};
  };
  service.driver.export=async(page,o)=>{
    counts[o.warehouse]=(counts[o.warehouse]||0)+1;
    const task=String((o.warehouse===sd?100:200)+counts[o.warehouse]);
    const data={scope:o.queried.scope,submissions:1,stage:'SUBMITTING'};
    await o.onManifest(data);
    await o.onManifest({...data,stage:'ACCEPTED',nativeTaskRef:task});
    await fs.mkdir(o.output,{recursive:true});
    const file=path.join(o.output,'controlled.xlsx'),bytes=Buffer.from(o.warehouse+' / '+task);
    await fs.writeFile(file,bytes);
    await o.onManifest({...data,stage:'DOWNLOADED',nativeTaskRef:task,file,sha256:sha(bytes)});
  };
  service.driver.validate=async(file,o)=>{
    const candidate=path.join(o.output,'candidate.json');await fs.writeFile(candidate,'{}');
    return {source_sha256:sha(await fs.readFile(file)),warehouse:o.warehouse,candidate,candidate_sha256:sha('{}'),rows:1};
  };
  const first=operation(sd,'sd_initial'),second=operation(hb,'hb_initial'),next=operation(sd,'sd_following');
  const a=await service.command(first),aWait=finish(a.batch_id,first);
  await until(()=>intervals.some(r=>r.lane===sd));
  await assert.rejects(service.command(next),{code:'BUSY'});
  const alias=path.join(out,'alias-of-sd');await fs.symlink(profiles.get(sd).userDataRoot,alias,'junction');
  await service.store.write(data=>{data.accounts[hb].binding.userDataRoot=alias;});
  await assert.rejects(service.command(second),{code:'ACCOUNT_ISOLATION'});
  await service.store.write(data=>{data.accounts[hb]=accounts[hb];});
  const b=await service.command(second),bWait=finish(b.batch_id,second);
  const cWait=navigate(axBridge.page,ax,'initial');
  const [doneA,doneB]=await Promise.all([aWait,bWait,cWait]);
  assert.equal(doneA.items[0].native.nativeTaskRef,'101');assert.equal(doneB.items[0].native.nativeTaskRef,'201');
  label.set(sd,'following');const d=await service.command(next);
  const doneD=await finish(d.batch_id,next);assert.equal(doneD.items[0].native.nativeTaskRef,'102');
  label.set(hb,'wrong_account');const wrong=operation(hb,'hb_wrong_account');const e=await service.command(wrong);
  assert.equal((await finish(e.batch_id,wrong)).items[0].error.code,'WRONG_ACCOUNT');
  assert.equal(counts[hb],1);
  const firstIntervals=intervals.filter(r=>r.name==='initial');
  assert.equal(firstIntervals.length,3);
  report.common_ms=Math.min(...firstIntervals.map(r=>r.to_ms))-Math.max(...firstIntervals.map(r=>r.from_ms));
  assert(report.common_ms>0);
  for(const lane of [sd,hb,ax]){
    const rows=intervals.filter(r=>r.lane===lane);
    assert(rows.every((r,i)=>i===0||r.from_ms>=rows[i-1].to_ms));
  }
  report.batches=[doneA,doneB,doneD].map(b=>({id:b.id,operation:b.operation_id,warehouse:b.items[0].account.warehouse,task:b.items[0].native.nativeTaskRef}));
  report.same_scope_refused_until_release=true;report.alias_refused_before_page=true;report.wrong_account_submitted=0;
  }
  report.passed=true;
}catch(error){report.error=error.stack;}
finally{
  await service?.close().catch(error=>{report.passed=false;report.cleanup_error=String(error);});
  await axSessions.close();
  await Promise.allSettled([...profiles.values()].map(closeProfile)).then(results=>{
    report.browser_cleanup=results.map(r=>r.status);if(results.some(r=>r.status==='rejected'))report.passed=false;
  });
  server.closeAllConnections();await new Promise(resolve=>server.close(resolve));
  report.completed=new Date().toISOString();
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));
}
console.log(JSON.stringify({passed:report.passed,common_ms:report.common_ms,counts,error:report.error}));
process.exitCode=report.passed?0:1;
