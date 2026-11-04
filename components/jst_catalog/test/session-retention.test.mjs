import test from 'node:test';
import assert from 'node:assert/strict';
import {AccountSessions} from '../src/accounts.mjs';
import {CatalogService} from '../src/service.mjs';
import {sha,canonical} from '../src/common.mjs';
import {JobStore} from '../src/job-store.mjs';

function fixture(){
  const s=new AccountSessions({bridgeRoot:'unused',stateRoot:'isolated',lockRoot:'isolated'}),events=[];
  const accounts=['山东仓','河北仓'].map((warehouse,i)=>({warehouse,binding:{actualProfilePath:'isolated/'+i+'/Default',userDataRoot:'isolated/'+i,edgeExecutable:'fake'}}));
  let count=0;
  class Bridge{
    constructor(){this.id=++count;this.connected=true;this.closed=false;this.page={isClosed:()=>this.closed};this.browser={isConnected:()=>this.connected};}
    async bind(){events.push('bind:'+this.id);}
    async close(){events.push('close:'+this.id);this.closed=true;this.connected=false;}
  }
  s.components={EdgeBridge:Bridge,ExclusiveLock:{acquire:async()=>({release:()=>events.push('release')})}};
  s.binding=async input=>accounts.find(a=>a.binding.actualProfilePath===input.profilePath).binding;
  s.ensureBrowser=async binding=>'endpoint:'+binding.actualProfilePath;
  return {s,accounts,events};
}
test('two warehouses retain independent bridges across repeated operations',async()=>{
  const {s,accounts,events}=fixture();const a=await s.connect(accounts[0]),b=await s.connect(accounts[1]);
  for(let n=0;n<5;n++){assert.equal(await s.connect(accounts[0]),a);assert.equal(await s.connect(accounts[1]),b);}
  assert.deepEqual(events,['bind:1','bind:2']);assert(s.status(accounts).every(r=>r.state==='CONNECTED'));
  await s.close();assert.deepEqual(events,['bind:1','bind:2','close:1','close:2','release','release']);
});
test('explicit reconnect affects only selected warehouse and not task store',async()=>{
  const {s,accounts,events}=fixture();await s.connect(accounts[0]);const b=await s.connect(accounts[1]);
  await s.disconnect(accounts[0].binding);assert.equal(s.status(accounts)[0].state,'DISCONNECTED');
  assert.equal(await s.connect(accounts[1]),b);await s.connect(accounts[0]);assert.equal(events.filter(x=>x.startsWith('bind')).length,3);await s.close();
});
test('disconnected socket is not advertised or reused as healthy',async()=>{
  const {s,accounts}=fixture();const old=await s.connect(accounts[0]);old.connected=false;
  assert.equal(s.status(accounts)[0].state,'DISCONNECTED');assert.notEqual(await s.connect(accounts[0]),old);await s.close();
});
test('closing owned page recreates only its own session',async()=>{
  const {s,accounts}=fixture();const a=await s.connect(accounts[0]),b=await s.connect(accounts[1]);a.closed=true;
  assert.notEqual(await s.connect(accounts[0]),a);assert.equal(await s.connect(accounts[1]),b);await s.close();
});
test('service release aborts current operation but retains browser pool and task records',async()=>{
  const data={accounts:{},batches:{original:{id:'original',nativeTaskRef:'123'}}};
  const s=new CatalogService({store:{read:()=>data},driver:{close:()=>{throw Error('must not close')}}});
  const controller=new AbortController();s.active={controller};const before=canonical(data);
  assert.deepEqual(await s.command({action:'session.release'}),{idle:false});assert(controller.signal.aborted);
  s.active=null;assert.deepEqual(await s.command({action:'session.release'}),{idle:true});assert.equal(canonical(data),before);
});
test('reset is refused during active work; cannot touch another platform',async()=>{
  const s=new CatalogService({store:{read:()=>({accounts:{}})},driver:{}});s.active={controller:new AbortController()};
  await assert.rejects(s.command({action:'session.reset',warehouse:'山东仓'}),{code:'BUSY'});
  s.active=null;await assert.rejects(s.command({action:'session.reset',warehouse:'翱象'}),{code:'ACCOUNT_UNVERIFIED'});
});

test('lightweight health does not read full jobs or queue behind mutations',async()=>{
  const data={accounts:{sd:{warehouse:'山东仓',binding:{}}}};
  Object.defineProperty(data,'batches',{get:()=>{throw Error('must not read jobs');},enumerable:true});
  const store=new JobStore('unused',data,null),s=new CatalogService({store,driver:{}});
  s.sessions={status:accounts=>accounts.map(a=>({warehouse:a.warehouse,state:'CONNECTED'}))};
  s.controlTail=new Promise(()=>{});
  const reply=await s.command({action:'session.health'});
  assert.deepEqual(reply,{connections:[{warehouse:'山东仓',state:'CONNECTED'}]});
  assert.equal(Object.keys(reply).length,1);
});
