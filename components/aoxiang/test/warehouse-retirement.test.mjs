import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {StateStore} from '../src/state.mjs';
import {PurchaseCreates} from '../src/purchase-create-ledger.mjs';
import {OrderRetirement,requireHostOrder,captureOwnedFiles} from '../src/order-retirement.mjs';
import {sha256} from '../src/common.mjs';
const h='a'.repeat(64),operation='e'.repeat(32);
function intent(id,warehouse){return {schema:1,operation:'purchase.create',id,identity:{accountId:'A',merchantCode:'M'},mappingVersion:h,
 basis:{oid:'shared',number:'BH-T-'+(warehouse==='河北仓'?'HB':'SD'),warehouse,store:'001',planFingerprint:h,paymentVersion:1,paymentDigest:h},
 target:{buyerWarehouseCode:'001',displayName:'隔离合成店'},supplierCode:warehouse==='河北仓'?'2078246':'ZXC2025',marker:'AX_CREATE:'+id,note:'AX_CREATE:'+id+' 本地单:BH-T-'+(warehouse==='河北仓'?'HB':'SD'),
 createExternalOrder:false,postCreationState:'待提交',file:{path:path.resolve('synthetic.xlsx'),sha256:h,bytes:300},template:{recordFile:path.resolve('template.json'),sha256:h,attemptId:'get-one'},
 rows:[{sku:'0001',name:'合成商品',quantity:'2',amount:'1.00'}],totalQuantity:'2',totalAmount:'1.00'};}
async function fixture(t){
 const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-warehouse-retirement-'));
 const main=new DatabaseSync(path.join(root,'main.sqlite'));
 main.exec(`CREATE TABLE normal_orders(oid TEXT PRIMARY KEY); INSERT INTO normal_orders VALUES('shared'); CREATE TABLE deleted_order_tokens(oid TEXT PRIMARY KEY,operation_id TEXT); CREATE TABLE warehouse_execution_heads(oid TEXT,warehouse TEXT,state TEXT); INSERT INTO warehouse_execution_heads VALUES('shared','河北仓','active'),('shared','山东仓','active'); CREATE TABLE warehouse_execution_retirements(oid TEXT,warehouse TEXT,operation_id TEXT,PRIMARY KEY(oid,warehouse));`);
 const old=process.env.REPLENISHMENT_BUSINESS_DB;process.env.REPLENISHMENT_BUSINESS_DB=path.join(root,'main.sqlite');
 const state=await StateStore.open(path.join(root,'component')),ledger=await PurchaseCreates.open(state),retirement=new OrderRetirement(state);await state.enablePurchases();
 t.after(async()=>{state.close();main.close();if(old===undefined)delete process.env.REPLENISHMENT_BUSINESS_DB;else process.env.REPLENISHMENT_BUSINESS_DB=old;
   assert.ok(path.resolve(root).startsWith(path.resolve(os.tmpdir())+path.sep));await fs.rm(root,{recursive:true,force:true});});
 return {root,state,ledger,main,retirement,retire(){main.prepare("UPDATE warehouse_execution_heads SET state='deleted' WHERE warehouse='河北仓'").run();main.prepare('INSERT INTO warehouse_execution_retirements VALUES(?,?,?)').run('shared','河北仓',operation);}};
}
test('warehouse deletion proof is required, sibling command remains active and old command cannot replay',async t=>{
 const f=await fixture(t),hb=intent('hb','河北仓'),sd=intent('sd','山东仓');
 f.ledger.reserve(hb,sha256(JSON.stringify(hb)));f.ledger.reserve(sd,sha256(JSON.stringify(sd)));
 const scopes=[{oid:'shared',warehouse:'河北仓'}];
 await assert.rejects(f.retirement.apply(operation,['shared'],scopes),{code:'RETIREMENT_NOT_CONFIRMED'});
 f.retire();assert.throws(()=>requireHostOrder('shared','河北仓'),{code:'SOURCE_ORDER_DELETED'});requireHostOrder('shared','山东仓');
 await f.retirement.apply(operation,['shared'],scopes);
 assert.equal(f.ledger.get('hb').state,'RETIRED');assert.equal(f.ledger.get('sd').state,'RESERVED');
 assert.deepEqual(f.ledger.get('sd').intent,sd);
 assert.equal(f.state.db.prepare('SELECT count(*) AS n FROM retired_host_orders').get().n,0);
 assert.throws(()=>f.ledger.reserve({...hb,id:'hb-replay'},h),{code:'SOURCE_ORDER_DELETED'});
 f.state.putPurchase({id:'sd-native',hostOrderId:'shared',hostWarehouse:'山东仓',hostPurchaseId:'sd'});
 assert.equal(f.state.rows('purchase_runs').length,1);
});
test('only selected warehouse owned files are removed and sibling original bytes remain',async t=>{
 const f=await fixture(t);for(const wh of ['河北仓','山东仓']){const p=intent(wh==='河北仓'?'hb':'sd',wh);f.ledger.reserve(p,sha256(JSON.stringify(p)));}
 const hb=path.join(f.state.root,'hb.xlsx'),sd=path.join(f.state.root,'sd.xlsx');await fs.writeFile(hb,'hb');await fs.writeFile(sd,'sd');
 captureOwnedFiles(f.state,'shared',{file:hb},'河北仓');captureOwnedFiles(f.state,'shared',{file:sd},'山东仓');
 f.retire();await f.retirement.apply(operation,['shared'],[{oid:'shared',warehouse:'河北仓'}]);
 await assert.rejects(fs.stat(hb),{code:'ENOENT'});assert.equal(await fs.readFile(sd,'utf8'),'sd');
 const late=path.join(f.state.root,'hb-late.xlsx');await fs.writeFile(late,'late');captureOwnedFiles(f.state,'shared',{file:late},'河北仓');
 await f.retirement.apply(operation,['shared'],[{oid:'shared',warehouse:'河北仓'}]);await assert.rejects(fs.stat(late),{code:'ENOENT'});
});
