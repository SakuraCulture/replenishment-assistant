// Independent local retirement. Never opens a browser or queries a platform.
import fs from 'node:fs/promises';
import {unlinkSync,readdirSync,lstatSync} from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {fail,now,sameIdentity} from './common.mjs';

export function retirementSchema(db){
 const tables=['retired_host_orders','retired_purchase_tokens','retired_purchase_runs','retirement_files','order_owned_files'];
 const present=tables.filter(t=>db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(t));
 const version=db.prepare("SELECT value FROM metadata WHERE key='orderRetirementProtocol'").get();
 if(version?.value==='1'&&present.length===tables.length){warehouseRetirementSchema(db);return;}
 if(version||present.length)fail('RETIREMENT_STATE_CORRUPT','删除退役标记缺失或不兼容，不能重建后重放旧命令');
 db.exec('BEGIN IMMEDIATE');try{db.exec(`
 CREATE TABLE IF NOT EXISTS retired_host_orders(oid TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retired_purchase_tokens(id TEXT PRIMARY KEY,oid TEXT,operation_id TEXT,request_hash TEXT,upload_attempted INTEGER,task_ids TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retired_purchase_runs(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retirement_files(path TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS order_owned_files(oid TEXT,path TEXT,PRIMARY KEY(oid,path));
 INSERT INTO metadata VALUES('orderRetirementProtocol','1');
 `);db.exec('COMMIT');}catch(e){db.exec('ROLLBACK');throw e;}warehouseRetirementSchema(db);}
function warehouseRetirementSchema(db){
 const version=db.prepare("SELECT value FROM metadata WHERE key='warehouseRetirementProtocol'").get();
 const tables=['retired_host_warehouses','warehouse_owned_files'];
 const present=tables.filter(t=>db.prepare("SELECT 1 FROM sqlite_master WHERE name=?").get(t));
 if(version?.value==='1'&&present.length===tables.length)return;
 if(version||present.length)fail('RETIREMENT_STATE_CORRUPT','分仓退役标记缺失，不能重放旧任务');
 db.exec('BEGIN IMMEDIATE');try{db.exec(`CREATE TABLE retired_host_warehouses(oid TEXT,warehouse TEXT,operation_id TEXT NOT NULL,PRIMARY KEY(oid,warehouse)); CREATE TABLE warehouse_owned_files(oid TEXT,warehouse TEXT,path TEXT,PRIMARY KEY(oid,warehouse,path)); INSERT INTO metadata VALUES('warehouseRetirementProtocol','1');`);db.exec('COMMIT');}catch(e){db.exec('ROLLBACK');throw e;}
}
export const retired=(db,id)=>!!db.prepare('SELECT 1 FROM retired_purchase_tokens WHERE id=?').get(id);
export function requireUnretired(db,id,oid,warehouse){
  if(id&&retired(db,id)||oid&&db.prepare('SELECT 1 FROM retired_host_orders WHERE oid=?').get(oid)||oid&&warehouse&&db.prepare('SELECT 1 FROM retired_host_warehouses WHERE oid=? AND warehouse=?').get(oid,warehouse))fail('SOURCE_ORDER_DELETED','本机来源仓单已删除；旧任务不能恢复或重新提交');
}
export function requireHostOrder(oid,warehouse){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;if(!file)return;
  if(typeof oid!=='string'||!oid)fail('SOURCE_ORDER_ID_REQUIRED','原任务缺少主库订单身份，不能执行');
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    db.exec('PRAGMA busy_timeout=1000');
    const has=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='normal_orders'").get();
    const tokens=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_order_tokens'").get();
    if(tokens&&db.prepare('SELECT 1 FROM deleted_order_tokens WHERE oid=?').get(oid)||!has||!db.prepare('SELECT 1 FROM normal_orders WHERE oid=?').get(oid))fail('SOURCE_ORDER_DELETED','来源订单已删除，未发起新平台动作');
    if(warehouse&&db.prepare("SELECT 1 FROM sqlite_master WHERE name='warehouse_execution_heads'").get()&&db.prepare('SELECT 1 FROM warehouse_execution_heads WHERE oid=?').get(oid)){
      const head=db.prepare('SELECT state FROM warehouse_execution_heads WHERE oid=? AND warehouse=?').get(oid,warehouse);
      if(head?.state!=='active')fail('SOURCE_ORDER_DELETED','来源仓单已删除或本轮未参与，未发起平台动作');
    }
  }finally{db.close();}
}
export function requireDeletedHostOrders(operation,oids,scopes){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;
  if(!file)fail('RETIREMENT_HOST_REQUIRED','必须由已提交删除事务的主库确认退役范围');
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    if(scopes){
      if(!Array.isArray(scopes)||!scopes.length||scopes.some(s=>!oids.includes(s.oid)||!['河北仓','山东仓'].includes(s.warehouse)))fail('RETIREMENT_SCOPE','分仓退役范围无效');
      if(!db.prepare("SELECT 1 FROM sqlite_master WHERE name='warehouse_execution_retirements'").get())fail('RETIREMENT_NOT_CONFIRMED','没有已提交的分仓删除依据');
      const packages=[];
      for(const s of scopes){
        if(db.prepare('SELECT operation_id FROM warehouse_execution_retirements WHERE oid=? AND warehouse=?').get(s.oid,s.warehouse)?.operation_id!==operation)fail('RETIREMENT_NOT_CONFIRMED','该仓单不属于本次已提交删除范围');
        if(db.prepare("SELECT 1 FROM sqlite_master WHERE name='warehouse_component_packages'").get())for(const r of db.prepare('SELECT package_id FROM warehouse_component_packages WHERE oid=? AND warehouse=?').all(s.oid,s.warehouse))packages.push(r.package_id);
      }
      return packages;
    }
    const has=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_order_tokens'").get();
    if(!has)fail('RETIREMENT_NOT_CONFIRMED','主库没有已提交的删除依据');
    for(const oid of oids)if(db.prepare('SELECT operation_id FROM deleted_order_tokens WHERE oid=?').get(oid)?.operation_id!==operation)
      fail('RETIREMENT_NOT_CONFIRMED','该订单不属于已提交的删除范围，未退役');
    const packages=[];
    if(db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='order_component_packages'").get())for(const oid of oids)
      for(const r of db.prepare('SELECT package_id FROM order_component_packages WHERE oid=?').all(oid))packages.push(r.package_id);
    return packages;
  }finally{db.close();}
}
export function retiredRecord(db,id){
  const r=db.prepare('SELECT * FROM retired_purchase_tokens WHERE id=?').get(id);
  return r?{id,state:'RETIRED',sourceDeleted:true,oid:r.oid,uploadAttempted:!!r.upload_attempted,taskIds:JSON.parse(r.task_ids),requestHash:r.request_hash,operationId:r.operation_id}:null;
}
export function lateRetiredReceipt(db,receipt){
  const r=retiredRecord(db,receipt.intentId);if(!r)return false;
  const ids=[...new Set([...r.taskIds,String(receipt.taskId)])];
  db.prepare('UPDATE retired_purchase_tokens SET task_ids=? WHERE id=?').run(JSON.stringify(ids),receipt.intentId);return true;
}
function strings(value,out=new Set(),pathsOnly=false){
  if(typeof value==='string'&&(path.isAbsolute(value)||!pathsOnly&&/^[a-zA-Z0-9-]{16,}$/.test(value)))out.add(path.isAbsolute(value)?canonicalPath(value):value);
  else if(Array.isArray(value))for(const v of value)strings(v,out,pathsOnly);
  else if(value&&typeof value==='object')for(const [key,v] of Object.entries(value))strings(v,out,pathsOnly||key==='rows');
  return out;
}
const canonicalPath=p=>process.platform==='win32'?path.resolve(p).toLowerCase():path.resolve(p);
const privatePath=(root,file)=>[root,path.resolve(root,'../purchase-packages')].some(r=>canonicalPath(file).startsWith(canonicalPath(r)+path.sep));
export function captureOwnedFiles(state,oid,value,warehouse){
  if(!oid)return;
  const retired=state.db.prepare('SELECT operation_id FROM retired_host_orders WHERE oid=?').get(oid)||(warehouse&&state.db.prepare('SELECT operation_id FROM retired_host_warehouses WHERE oid=? AND warehouse=?').get(oid,warehouse));
  for(const file of strings(value))if(path.isAbsolute(file)&&privatePath(state.root,file)){
    if(retired)state.db.prepare('INSERT OR IGNORE INTO retirement_files VALUES(?,?)').run(file,retired.operation_id);
    else if(warehouse)state.db.prepare('INSERT OR IGNORE INTO warehouse_owned_files VALUES(?,?,?)').run(oid,warehouse,file);
    else state.db.prepare('INSERT OR IGNORE INTO order_owned_files VALUES(?,?)').run(oid,file);
  }
}
function referenced(db,file){
  if(db.prepare('SELECT 1 FROM order_owned_files WHERE path=?').get(file))return true;
  if(db.prepare('SELECT 1 FROM warehouse_owned_files WHERE path=?').get(file))return true;
  for(const {name} of db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all()){
    if(!/^[a-z_]+$/.test(name)||/^(retired_|retirement_)/.test(name))continue;
    if(!db.prepare('PRAGMA table_info('+name+')').all().some(c=>c.name==='payload'))continue;
    for(const r of db.prepare('SELECT payload FROM '+name).iterate()){
      let p;try{p=JSON.parse(r.payload);}catch{continue;}
      if(strings(p).has(file))return true;
    }
  }
  return false;
}
export class OrderRetirement{
  constructor(state){this.state=state;this.db=state.db;retirementSchema(this.db);}
  async apply(operation,oids,scopes){
    if(!/^[a-f0-9]{32}$/.test(operation)||!Array.isArray(oids)||!oids.length||oids.length>256||oids.some(o=>typeof o!=='string'||!o||o.length>100))fail('RETIREMENT_SCOPE','退役操作号或订单身份无效');
    const packages=new Set(requireDeletedHostOrders(operation,oids,scopes));
    const db=this.db,owned=new Set(oids),candidates=new Set();
    const matches=b=>owned.has(b?.oid)&&(!scopes||scopes.some(s=>s.oid===b.oid&&s.warehouse===b.warehouse));
    // Pre-upgrade packages may never have reached the component reservation.
    // Inspect only native intent ownership in this managed folder, one file at
    // a time; a store/date/name match is never sufficient evidence.
    const scan=scopes?oids:oids.filter(oid=>!db.prepare('SELECT 1 FROM metadata WHERE key=?').get('retirementScanned:'+oid));
    if(scan.length){
      const root=path.resolve(this.state.root,'../purchase-packages');let directory;
      try{directory=await fs.opendir(root);}catch(e){if(e.code!=='ENOENT')throw e;}
      if(directory){
        const realRoot=canonicalPath(await fs.realpath(root))+path.sep;
        for await(const entry of directory){
          if(!entry.isDirectory()||!/^purchase-[a-f0-9]{32}$/.test(entry.name))continue;
          const file=path.join(root,entry.name,'采购任务.json');
          try{
            const stat=await fs.lstat(file);if(!stat.isFile()||stat.isSymbolicLink()||stat.size>10485760||!canonicalPath(await fs.realpath(file)).startsWith(realRoot))continue;
            const p=JSON.parse(await fs.readFile(file,'utf8'));
            if(p.id===entry.name&&scan.includes(p.basis?.oid)&&matches(p.basis))packages.add(entry.name);
          }catch(e){if(!['ENOENT'].includes(e.code)&&!(e instanceof SyntaxError))throw e;}
        }
      }
    }
    const exists=t=>!!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(t);
    db.exec('PRAGMA secure_delete=ON');
    this.state.write(()=>{
      if(!scopes){
        for(const oid of scan)db.prepare('INSERT OR IGNORE INTO metadata VALUES(?,?)').run('retirementScanned:'+oid,'1');
        for(const oid of oids)db.prepare('INSERT OR IGNORE INTO retired_host_orders VALUES(?,?)').run(oid,operation);
        for(const oid of oids){for(const r of db.prepare('SELECT path FROM order_owned_files WHERE oid=?').all(oid))candidates.add(r.path);db.prepare('DELETE FROM order_owned_files WHERE oid=?').run(oid);}
      }
      for(const s of scopes||oids.map(oid=>({oid,warehouse:null}))){
        if(s.warehouse)db.prepare('INSERT OR IGNORE INTO retired_host_warehouses VALUES(?,?,?)').run(s.oid,s.warehouse,operation);
        const sql='SELECT path FROM warehouse_owned_files WHERE oid=?'+(s.warehouse?' AND warehouse=?':'');
        for(const r of db.prepare(sql).all(...(s.warehouse?[s.oid,s.warehouse]:[s.oid])))candidates.add(r.path);
        db.prepare('DELETE FROM warehouse_owned_files WHERE oid=?'+(s.warehouse?' AND warehouse=?':'')).run(...(s.warehouse?[s.oid,s.warehouse]:[s.oid]));
      }
      const removed=[];
      const proofHint=r=>({intent:{identity:r.intent?.identity,basis:{oid:r.intent?.basis?.oid}},binding:r.binding?{orderId:r.binding.orderId,files:r.binding.files}:null});
      if(exists('purchase_creates'))for(const {id} of db.prepare('SELECT id FROM purchase_creates').all()){
        const row=db.prepare('SELECT id,payload FROM purchase_creates WHERE id=?').get(id);
        const r=JSON.parse(row.payload);if(!matches(r.intent?.basis))continue;
        removed.push(proofHint(r));strings(r,candidates);packages.add(r.id);
        db.prepare('INSERT OR IGNORE INTO retired_purchase_tokens VALUES(?,?,?,?,?,?)').run(r.id,r.intent.basis.oid,operation,r.requestHash,Number(!!r.uploadAttempted),JSON.stringify([r.receipt?.taskId,...r.additionalTaskIds||[]].filter(Boolean)));
        if(exists('purchase_receipts')){const receipt=db.prepare('SELECT payload FROM purchase_receipts WHERE id=?').get(r.id);if(receipt)strings(JSON.parse(receipt.payload),candidates);}
        for(const table of ['purchase_receipt_events','purchase_receipts','purchase_create_events'])if(exists(table))db.prepare('DELETE FROM '+table+' WHERE id=?').run(r.id);
        db.prepare('DELETE FROM purchase_creates WHERE id=?').run(r.id);
      }
      const survivors=new Set(),liveCreates=[];
      if(exists('purchase_creates'))for(const r of db.prepare('SELECT payload FROM purchase_creates').iterate()){const c=JSON.parse(r.payload);liveCreates.push(proofHint(c));strings(c,survivors);}
      if(exists('purchase_receipts'))for(const r of db.prepare('SELECT payload FROM purchase_receipts').iterate())strings(JSON.parse(r.payload),survivors);
      if(exists('purchase_runs'))for(const {id} of db.prepare('SELECT id FROM purchase_runs').all()){
        const row=db.prepare('SELECT id,payload FROM purchase_runs WHERE id=?').get(id);
        const r=JSON.parse(row.payload);
        const proofMatches=c=>c.binding?.orderId===r.orderId&&sameIdentity(c.intent?.identity,r.identity)&&c.binding.files?.length===2&&c.binding.files.every(f=>r.exports?.[f.source]?.asset?.sha256===f.sha256);
        const shared=liveCreates.filter(proofMatches);
        const ownedRun=scopes?(packages.has(r.hostPurchaseId)||removed.some(proofMatches)):owned.has(r.hostOrderId);
        if((ownedRun||candidates.has(r.id)||removed.some(proofMatches))&&!survivors.has(r.id)&&!shared.length){
          strings(r,candidates);db.prepare('INSERT OR IGNORE INTO retired_purchase_runs VALUES(?,?)').run(r.id,operation);db.prepare('DELETE FROM purchase_runs WHERE id=?').run(r.id);
        }else{
          if(ownedRun){
            delete r.hostOrderId;delete r.hostPurchaseId;
            r.hostOrderIds=shared.map(c=>c.intent.basis.oid);
            db.prepare('UPDATE purchase_runs SET payload=? WHERE id=?').run(JSON.stringify(r),r.id);
          }
          strings(r,survivors);
        }
      }
      const roots=[path.resolve(this.state.root)+path.sep,path.resolve(this.state.root,'../purchase-packages')+path.sep];
      // UUID-owned, flat host package folders also own interrupted temporary
      // files. Enumerate files only, never recurse or infer ownership by store.
      for(const id of packages)if(/^purchase-[a-f0-9]{32}$/.test(id)){
        const directory=path.resolve(this.state.root,'../purchase-packages',id);
        try{
          const stat=lstatSync(directory);if(!stat.isDirectory()||stat.isSymbolicLink())continue;
          for(const entry of readdirSync(directory,{withFileTypes:true}))if(entry.isFile())candidates.add(path.join(directory,entry.name));
        }catch(e){if(e.code!=='ENOENT')throw e;}
      }
      for(const file of candidates){
        if(survivors.has(file)||!path.isAbsolute(file))continue;
        const resolved=path.resolve(file);
        if(roots.some(root=>resolved.toLowerCase().startsWith(root.toLowerCase())))db.prepare('INSERT OR IGNORE INTO retirement_files VALUES(?,?)').run(resolved,operation);
      }
    });
    // A locked file never undoes business deletion. Retry its local cleanup later.
    const roots=await Promise.all([this.state.root,path.resolve(this.state.root,'../purchase-packages')].map(async root=>{try{return canonicalPath(await fs.realpath(root))+path.sep;}catch{return canonicalPath(root)+path.sep;}}));
    for(const {path:file} of db.prepare('SELECT path FROM retirement_files WHERE operation_id=? LIMIT 16').all(operation)){
      try{
        const stat=await fs.lstat(file),real=canonicalPath(await fs.realpath(file));
        if(stat.isSymbolicLink()||!roots.some(root=>real.startsWith(root)))continue;
        if(stat.isFile()&&!referenced(db,canonicalPath(file)))unlinkSync(file);
        // Shared files and directories remain; neither is owned only by this deletion.
      }
      catch(e){if(e.code!=='ENOENT')continue;}
      db.prepare('DELETE FROM retirement_files WHERE path=?').run(file);
    }
    return {operation_id:operation,businessDeleted:true,retired:oids.length,pending_files:db.prepare('SELECT COUNT(*) AS n FROM retirement_files WHERE operation_id=?').get(operation).n};
  }
}
