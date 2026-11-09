import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {spawn} from 'node:child_process';
import {once} from 'node:events';
import {StateStore} from '../src/state.mjs';
import {PurchaseCreates} from '../src/purchase-create-ledger.mjs';
import {OrderRetirement,captureOwnedFiles,requireHostOrder} from '../src/order-retirement.mjs';
import {sha256} from '../src/common.mjs';
const h='a'.repeat(64),op='d'.repeat(32);
function intent(id,oid,number=oid){return {schema:1,operation:'purchase.create',id,identity:{accountId:'A',merchantCode:'M'},mappingVersion:h,
 basis:{oid,number,warehouse:'河北仓',store:'001',planFingerprint:h,paymentVersion:1,paymentDigest:h},
 target:{buyerWarehouseCode:'001',displayName:'隔离合成店'},supplierCode:'2078246',marker:'AX_CREATE:'+id,note:'AX_CREATE:'+id+' 本地单:'+number,
 createExternalOrder:false,postCreationState:'待提交',file:{path:path.resolve('synthetic.xlsx'),sha256:h,bytes:300},template:{recordFile:path.resolve('template.json'),sha256:h,attemptId:'get-one'},
 rows:[{sku:'0001',name:'合成商品',quantity:'2',amount:'1.00'}],totalQuantity:'2',totalAmount:'1.00'};}
async function fixture(t){
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-retirement-')),main=new DatabaseSync(path.join(root,'main.sqlite'));
 main.exec('CREATE TABLE normal_orders(oid TEXT PRIMARY KEY); CREATE TABLE deleted_order_tokens(oid TEXT PRIMARY KEY,operation_id TEXT);');
 for(const id of ['gone','keep','new'])main.prepare('INSERT INTO normal_orders VALUES(?)').run(id);
 const old=process.env.REPLENISHMENT_BUSINESS_DB;process.env.REPLENISHMENT_BUSINESS_DB=path.join(root,'main.sqlite');
 const state=await StateStore.open(path.join(root,'component-state')),ledger=await PurchaseCreates.open(state),retirement=new OrderRetirement(state);await state.enablePurchases();
 t.after(async()=>{state.close();main.close();if(old===undefined)delete process.env.REPLENISHMENT_BUSINESS_DB;else process.env.REPLENISHMENT_BUSINESS_DB=old;await fs.rm(root,{recursive:true,force:true});});
 return {root,state,main,ledger,retirement,del(id='gone'){main.prepare('DELETE FROM normal_orders WHERE oid=?').run(id);main.prepare('INSERT INTO deleted_order_tokens VALUES(?,?)').run(id,op);}};
}
function reserve(f,id,oid,number){const p=intent(id,oid,number);f.ledger.reserve(p,sha256(JSON.stringify(p)));return p;}
test('retirement requires a committed matching main deletion and never retires live orders',async t=>{
 const f=await fixture(t);reserve(f,'p1','gone');
 await assert.rejects(f.retirement.apply(op,['gone']),{code:'RETIREMENT_NOT_CONFIRMED'});
 assert.equal(f.ledger.get('p1').state,'RESERVED');f.del();
 await assert.rejects(f.retirement.apply('e'.repeat(32),['gone']),{code:'RETIREMENT_NOT_CONFIRMED'});
 assert.equal((await f.retirement.apply(op,['gone'])).pending_files,0);
});
test('delete frees active reservation but old command and old oid cannot replay; new oid independent',async t=>{
 const f=await fixture(t),p=reserve(f,'p1','gone','BH-20261109'),kept=reserve(f,'p2','keep','BH-keep');
 let r=f.ledger.get('p1');for(const s of ['PREPARING','READY','SUBMITTING'])r=f.ledger.update(r.id,r.sequence,s,s==='SUBMITTING'?{uploadAttempted:true}:{});
 f.del();await f.retirement.apply(op,['gone']);
 assert.equal(f.ledger.get('p1').state,'RETIRED');assert.equal(f.ledger.get('p1').uploadAttempted,true);
 assert.deepEqual(f.ledger.get('p1').intent,undefined);assert.equal(f.ledger.events('p1').length,0);assert.equal(f.ledger.list().length,1);
 assert.throws(()=>f.ledger.reserve(p,h),{code:'SOURCE_ORDER_DELETED'});
 assert.throws(()=>reserve(f,'another-command','gone'),{code:'SOURCE_ORDER_DELETED'});
 assert.throws(()=>requireHostOrder('gone'),{code:'SOURCE_ORDER_DELETED'});
 reserve(f,'new-command','new','BH-20261109');assert.equal(f.ledger.list().length,2);
 assert.deepEqual(f.ledger.get('p2').intent,kept);
 const late=f.ledger.accepted({intentId:'p1',taskId:'native-late-task'});
 assert.deepEqual(late.taskIds,['native-late-task']);assert.equal(late.state,'RETIRED');
 assert.throws(()=>f.state.putPurchase({id:'late-run',hostOrderId:'gone',rows:[{sku:'must-not-return'}]}),{code:'SOURCE_ORDER_DELETED'});
 assert.throws(()=>f.state.importPurchases([{id:'old-migration',hostOrderId:'gone'}]),{code:'SOURCE_ORDER_DELETED'});
 assert.equal(f.state.rows('purchase_runs').length,0);
});
test('owned files removed, shared and external original files retained; later files retry locally',async t=>{
 const f=await fixture(t);reserve(f,'p1','gone');reserve(f,'p2','keep');
 const own=path.join(f.state.root,'owned.xlsx'),shared=path.join(f.state.root,'shared.xlsx'),outside=path.join(f.root,'original.xlsx');
 for(const p of [own,shared,outside])await fs.writeFile(p,'synthetic bytes');
 captureOwnedFiles(f.state,'gone',{own,shared,outside});captureOwnedFiles(f.state,'keep',{shared});
 f.del();assert.equal((await f.retirement.apply(op,['gone'])).pending_files,0);
 await assert.rejects(fs.stat(own),{code:'ENOENT'});assert.equal((await fs.readFile(shared)).toString(),'synthetic bytes');await fs.stat(outside);
 // A download that was already in flight can complete after the first cleanup.
 const late=path.join(f.state.root,'late.xlsx');await fs.writeFile(late,'late');captureOwnedFiles(f.state,'gone',{late});
 assert.equal(f.state.db.prepare('SELECT COUNT(*) AS n FROM retirement_files').get().n,1);
 await f.retirement.apply(op,['gone']);await assert.rejects(fs.stat(late),{code:'ENOENT'});
});
test('shared exact native evidence remains while deleted host ownership is removed',async t=>{
 const f=await fixture(t);reserve(f,'p1','gone');reserve(f,'p2','keep');
 const exports=Object.fromEntries(['D16','D17'].map(s=>[s,{asset:{sha256:h,path:path.join(f.state.root,s+'.xlsx')}}]));
 for(const id of ['p1','p2']){const r=f.ledger.get(id);r.binding={orderId:'PO-shared',files:['D16','D17'].map(source=>({source,sha256:h}))};f.state.db.prepare('UPDATE purchase_creates SET payload=? WHERE id=?').run(JSON.stringify(r),id);}
 f.state.putPurchase({id:'shared-native-run',hostOrderId:'gone',hostPurchaseId:'p1',identity:{accountId:'A',merchantCode:'M'},orderId:'PO-shared',exports});
 f.del();await f.retirement.apply(op,['gone']);
 const r=f.state.rows('purchase_runs')[0];assert.equal(r.id,'shared-native-run');assert.equal(r.hostOrderId,undefined);assert.deepEqual(r.hostOrderIds,['keep']);
 assert.equal(f.ledger.get('p2').state,'RESERVED');
});
test('retirement markers survive reopen and a missing marker table fails closed',async t=>{
 const f=await fixture(t);reserve(f,'p1','gone');f.del();await f.retirement.apply(op,['gone']);
 const reopened=await StateStore.open(f.state.root);
 try{const ledger=await PurchaseCreates.open(reopened);assert.equal(ledger.get('p1').state,'RETIRED');assert.throws(()=>reserve({...f,ledger},'replay','gone'),{code:'SOURCE_ORDER_DELETED'});}finally{reopened.close();}
 f.state.db.exec('DROP TABLE retired_host_orders');
 assert.throws(()=>new OrderRetirement(f.state),{code:'RETIREMENT_STATE_CORRUPT'});
});
test('Windows exclusive file lock leaves a recoverable pending cleanup after business retirement',{skip:process.platform!=='win32'},async t=>{
 const f=await fixture(t);reserve(f,'p1','gone');const file=path.join(f.state.root,'locked.xlsx');await fs.writeFile(file,'locked synthetic');captureOwnedFiles(f.state,'gone',{file});
 const child=spawn('powershell.exe',['-NoProfile','-NonInteractive','-Command',"$stream=[System.IO.File]::Open($env:RETIREMENT_TEST_FILE,'Open','ReadWrite','None');[Console]::WriteLine('READY');[Console]::ReadLine() | Out-Null;$stream.Dispose()"],{windowsHide:true,env:{...process.env,RETIREMENT_TEST_FILE:file},stdio:['pipe','pipe','pipe']});
 let stderr='';child.stderr.on('data',c=>stderr+=c);const ended=once(child,'exit');
 await new Promise((resolve,reject)=>{child.stdout.on('data',c=>{if(c.toString().includes('READY'))resolve();});child.on('exit',()=>reject(Error(stderr||'lock helper exited')));child.on('error',reject);});
 try{f.del();const result=await f.retirement.apply(op,['gone']);assert.equal(result.pending_files,1);assert.equal(f.ledger.get('p1').state,'RETIRED');}
 finally{child.stdin.end('\n');await ended;}
 assert.equal((await f.retirement.apply(op,['gone'])).pending_files,0);await assert.rejects(fs.stat(file),{code:'ENOENT'});
});
