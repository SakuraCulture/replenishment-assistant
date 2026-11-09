// Independent local retirement. Never opens a browser or queries a platform.
import fs from 'node:fs/promises';
import {unlinkSync} from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {fail,now,sameIdentity} from './common.mjs';

export function retirementSchema(db){
 const tables=['retired_host_orders','retired_purchase_tokens','retired_purchase_runs','retirement_files','order_owned_files'];
 const present=tables.filter(t=>db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(t));
 const version=db.prepare("SELECT value FROM metadata WHERE key='orderRetirementProtocol'").get();
 if(version?.value==='1'&&present.length===tables.length)return;
 if(version||present.length)fail('RETIREMENT_STATE_CORRUPT','删除退役标记缺失或不兼容，不能重建后重放旧命令');
 db.exec('BEGIN IMMEDIATE');try{db.exec(`
 CREATE TABLE IF NOT EXISTS retired_host_orders(oid TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retired_purchase_tokens(id TEXT PRIMARY KEY,oid TEXT,operation_id TEXT,request_hash TEXT,upload_attempted INTEGER,task_ids TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retired_purchase_runs(id TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS retirement_files(path TEXT PRIMARY KEY,operation_id TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS order_owned_files(oid TEXT,path TEXT,PRIMARY KEY(oid,path));
 INSERT INTO metadata VALUES('orderRetirementProtocol','1');
 `);db.exec('COMMIT');}catch(e){db.exec('ROLLBACK');throw e;}}
export const retired=(db,id)=>!!db.prepare('SELECT 1 FROM retired_purchase_tokens WHERE id=?').get(id);
export function requireUnretired(db,id,oid){
  if(id&&retired(db,id)||oid&&db.prepare('SELECT 1 FROM retired_host_orders WHERE oid=?').get(oid))fail('SOURCE_ORDER_DELETED','本机来源订单已删除；旧任务不能恢复或重新提交');
}
export function requireHostOrder(oid){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;if(!file)return;
  if(typeof oid!=='string'||!oid)fail('SOURCE_ORDER_ID_REQUIRED','原任务缺少主库订单身份，不能执行');
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    db.exec('PRAGMA busy_timeout=1000');
    const has=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='normal_orders'").get();
    const tokens=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_order_tokens'").get();
    if(tokens&&db.prepare('SELECT 1 FROM deleted_order_tokens WHERE oid=?').get(oid)||!has||!db.prepare('SELECT 1 FROM normal_orders WHERE oid=?').get(oid))fail('SOURCE_ORDER_DELETED','来源订单已删除，未发起新平台动作');
  }finally{db.close();}
}
export function requireDeletedHostOrders(operation,oids){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;
  if(!file)fail('RETIREMENT_HOST_REQUIRED','必须由已提交删除事务的主库确认退役范围');
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    const has=db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name='deleted_order_tokens'").get();
    if(!has)fail('RETIREMENT_NOT_CONFIRMED','主库没有已提交的删除依据');
    for(const oid of oids)if(db.prepare('SELECT operation_id FROM deleted_order_tokens WHERE oid=?').get(oid)?.operation_id!==operation)
      fail('RETIREMENT_NOT_CONFIRMED','该订单不属于已提交的删除范围，未退役');
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
export function captureOwnedFiles(state,oid,value){
  if(!oid)return;
  const retired=state.db.prepare('SELECT operation_id FROM retired_host_orders WHERE oid=?').get(oid);
  for(const file of strings(value))if(path.isAbsolute(file)&&privatePath(state.root,file)){
    if(retired)state.db.prepare('INSERT OR IGNORE INTO retirement_files VALUES(?,?)').run(file,retired.operation_id);
    else state.db.prepare('INSERT OR IGNORE INTO order_owned_files VALUES(?,?)').run(oid,file);
  }
}
function referenced(db,file){
  if(db.prepare('SELECT 1 FROM order_owned_files WHERE path=?').get(file))return true;
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
  async apply(operation,oids){
    if(!/^[a-f0-9]{32}$/.test(operation)||!Array.isArray(oids)||!oids.length||oids.length>256||oids.some(o=>typeof o!=='string'||!o||o.length>100))fail('RETIREMENT_SCOPE','退役操作号或订单身份无效');
    requireDeletedHostOrders(operation,oids);
    const db=this.db,owned=new Set(oids),candidates=new Set();
    const exists=t=>!!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(t);
    db.exec('PRAGMA secure_delete=ON');
    this.state.write(()=>{
      for(const oid of oids)db.prepare('INSERT OR IGNORE INTO retired_host_orders VALUES(?,?)').run(oid,operation);
      for(const oid of oids){for(const r of db.prepare('SELECT path FROM order_owned_files WHERE oid=?').all(oid))candidates.add(r.path);db.prepare('DELETE FROM order_owned_files WHERE oid=?').run(oid);}
      const removed=[];
      const proofHint=r=>({intent:{identity:r.intent?.identity,basis:{oid:r.intent?.basis?.oid}},binding:r.binding?{orderId:r.binding.orderId,files:r.binding.files}:null});
      if(exists('purchase_creates'))for(const {id} of db.prepare('SELECT id FROM purchase_creates').all()){
        const row=db.prepare('SELECT id,payload FROM purchase_creates WHERE id=?').get(id);
        const r=JSON.parse(row.payload);if(!owned.has(r.intent?.basis?.oid))continue;
        removed.push(proofHint(r));strings(r,candidates);
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
        if((owned.has(r.hostOrderId)||candidates.has(r.id)||removed.some(proofMatches))&&!survivors.has(r.id)&&!shared.length){
          strings(r,candidates);db.prepare('INSERT OR IGNORE INTO retired_purchase_runs VALUES(?,?)').run(r.id,operation);db.prepare('DELETE FROM purchase_runs WHERE id=?').run(r.id);
        }else{
          if(owned.has(r.hostOrderId)){
            delete r.hostOrderId;delete r.hostPurchaseId;
            r.hostOrderIds=shared.map(c=>c.intent.basis.oid);
            db.prepare('UPDATE purchase_runs SET payload=? WHERE id=?').run(JSON.stringify(r),r.id);
          }
          strings(r,survivors);
        }
      }
      const roots=[path.resolve(this.state.root)+path.sep,path.resolve(this.state.root,'../purchase-packages')+path.sep];
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
