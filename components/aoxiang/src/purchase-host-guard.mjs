// The host owns all business validation. Here only scoped version facts are compared.
import fs from 'node:fs';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {parse as losslessParse,isLosslessNumber} from 'lossless-json';
import {canonical,sha256,fail,businessDate,now} from './common.mjs';

const warehouses=['河北仓','山东仓'];
export const hostIdentity=p=>sha256(canonical({basis:p.basis,identity:p.identity,mappingVersion:p.mappingVersion,fileSha256:p.file.sha256}));

export function hostFacts(db,b){
  const tables=new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map(r=>r.name));
  const one=(table,sql,args)=>{const r=tables.has(table)?db.prepare(sql).get(...args):null;return r?Object.values(r):null;};
  const {oid,revision,warehouse,store}=b;
  let parent=one('normal_orders','SELECT day,store,number,current FROM normal_orders WHERE oid=?',[oid]);
  const managed=!!one('warehouse_execution_heads','SELECT 1 FROM warehouse_execution_heads WHERE oid=? LIMIT 1',[oid]);
  const child=managed?one('warehouse_execution_heads','SELECT source_revision,child_revision,state,epoch FROM warehouse_execution_heads WHERE oid=? AND warehouse=?',[oid,warehouse]):null;
  const signed=managed?one('warehouse_execution_revisions','SELECT identity_digest,snapshot_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',[oid,warehouse,revision]):one('normal_revisions','SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',[oid,revision]);
  const versions=Object.fromEntries(['return','operations','payment'].map(s=>[s,one('lifecycle_versions','SELECT version,digest FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage=? ORDER BY version DESC LIMIT 1',[oid,revision,warehouse,s])]));
  const route=one('lifecycle_routes','SELECT version FROM lifecycle_routes WHERE oid=? AND revision=?',[oid,revision]);
  const legacy=versions.return?null:one('merchant_feedback','SELECT version,payload FROM merchant_feedback WHERE oid=? AND revision=? ORDER BY version DESC LIMIT 1',[oid,revision]);
  if(legacy)legacy[1]=sha256(legacy[1]);
  const admission=one('store_admission','SELECT payload FROM store_admission WHERE store=?',[store]);
  const cancellation=one('jst_cancellations',`SELECT online,successor,
    (SELECT max(id) FROM jst_cancellation_reviews WHERE cancelled_online=jst_cancellations.online)
    FROM jst_cancellations WHERE oid=? AND warehouse=? ORDER BY observed DESC,rowid DESC LIMIT 1`,[oid,warehouse])
    ?? one('jst_cancellation_holds','SELECT online FROM jst_cancellation_holds WHERE oid=? AND warehouse=?',[oid,warehouse])
    ?? one('jst_auto_orders',`SELECT online FROM jst_auto_orders
      WHERE oid=? AND warehouse=? AND stage='REVIEW' AND internal IS NOT NULL
        AND (reason LIKE '聚水潭状态为已取消%' OR reason LIKE '聚水潭当前为“已取消”%')`,[oid,warehouse]);
  if(managed&&parent)parent=parent.slice(0,3);
  return {parent,managed,child,signed,
    visibility:one('archive_visibility','SELECT deleted,token FROM archive_visibility WHERE oid=? AND warehouse=?',[oid,warehouse]),
    deleted:one('deleted_order_tokens','SELECT operation_id FROM deleted_order_tokens WHERE oid=?',[oid]),
    retirement:one('warehouse_execution_retirements','SELECT operation_id FROM warehouse_execution_retirements WHERE oid=? AND warehouse=?',[oid,warehouse]),
    operations:!!(versions.operations||route&&route[0]>=2),versions,legacy,admission:admission?sha256(admission[0]):null,
    ...(cancellation?{jstCancellation:cancellation}:{})};
}

// json.dumps(sort_keys=True, ensure_ascii=False) with original numeric lexemes.
// Only used on a changed catalog file: metadata refreshes must not invalidate the same items.
function pythonJson(v){
  if(isLosslessNumber(v))return v.value;
  if(Array.isArray(v))return '['+v.map(pythonJson).join(', ')+']';
  if(v!==null&&typeof v==='object'){
    const sorted=Object.keys(v).sort((a,b)=>{const x=Array.from(a,c=>c.codePointAt(0)),y=Array.from(b,c=>c.codePointAt(0));for(let n=0;n<Math.min(x.length,y.length);n++)if(x[n]!==y[n])return x[n]-y[n];return x.length-y.length;});
    return '{'+sorted.map(k=>JSON.stringify(k)+': '+pythonJson(v[k])).join(', ')+'}';
  }
  return JSON.stringify(v);
}
const stamp=file=>{try{const s=fs.statSync(file,{bigint:true});return [s.dev,s.ino,s.size,s.mtimeNs,s.ctimeNs].map(String).join(':');}catch(e){if(e.code==='ENOENT')return null;throw e;}};
function sources(file,p,proof){
  const folder=path.dirname(file),checks=[];
  for(const warehouse of warehouses){
    const source=path.join(folder,'warehouse_catalogs',warehouse+'.json'),before=stamp(source),expected=proof.catalogs?.[warehouse];
    if(!expected)fail('HOST_GUARD_REQUIRED','原采购缺少完整仓资料核验凭据，请回主程序重新核验；尚未上传');
    if(expected.legacy){
      const hash=before===null?null:sha256(fs.readFileSync(source));
      if(hash!==expected.sha256)fail('PURCHASE_BASIS_CHANGED','当前仓资料已更新，请重新核对；尚未上传');
    }else if(before===null){if(expected.revision!==null)fail('PURCHASE_BASIS_CHANGED','原仓资料已移除，请重新核对；尚未上传');}
    else{
      if(expected.revision===null)fail('PURCHASE_BASIS_CHANGED','仓资料已变化，请重新核对；尚未上传');
      const bytes=fs.readFileSync(source);
      if(sha256(bytes)!==expected.sha256){
        let data;try{data=losslessParse(bytes.toString('utf8'));}catch{fail('PURCHASE_BASIS_CHANGED','仓资料无法核验，保留原预览；尚未上传');}
        if(data.warehouse!==warehouse||data.revision!==expected.revision||sha256(pythonJson(data.items)).slice(0,16)!==expected.revision)fail('PURCHASE_BASIS_CHANGED','仓资料内容已变化，请重新核对；尚未上传');
      }
    }
    if(stamp(source)!==before)fail('PURCHASE_BASIS_CHANGED','仓资料在核验时变化，请重试；尚未上传');
    checks.push([source,before]);
  }
  const supplier=path.join(folder,'aoxiang','suppliers.json'),before=stamp(supplier);
  if(before){let value;try{value=JSON.parse(fs.readFileSync(supplier,'utf8'));}catch{fail('PURCHASE_CONFIGURATION_CHANGED','供应商配置无法读取；尚未上传');}
    if(value[p.basis.warehouse]!==p.supplierCode)fail('PURCHASE_CONFIGURATION_CHANGED','本仓供应商已变化，请重新核对；尚未上传');
  }else if(proof.supplierFile)fail('PURCHASE_CONFIGURATION_CHANGED','原供应商配置已移除；尚未上传');
  checks.push([supplier,before]);return checks;
}

function checkLegacyCatalogs(db,proof){
  for(const warehouse of warehouses){
    const expected=proof.catalogs[warehouse].legacy;
    if(!expected)continue;
    const tables=db.prepare("SELECT count(*) AS n FROM sqlite_master WHERE name IN ('jst_warehouses','jst_baselines')").get().n;
    const current=tables===2?db.prepare('SELECT b.bid,b.digest FROM jst_warehouses w JOIN jst_baselines b ON b.bid=w.bid WHERE w.warehouse=?').get(warehouse):null;
    if(!current||canonical(Object.values(current))!==canonical(expected))
      fail('PURCHASE_BASIS_CHANGED','旧版仓资料依据已更新，请重新核对；尚未上传');
  }
}

export function checkHostDispatch(p,{consume=false,requestHash=null}={}){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;
  if(!file||!fs.existsSync(file))fail('HOST_GUARD_REQUIRED','请在补货助手重新核验本单采购预览；本次尚未上传');
  const db=new DatabaseSync(file,{readOnly:!consume});
  try{
    db.exec('PRAGMA busy_timeout=1000');if(consume)db.exec('PRAGMA synchronous=FULL');
    if(!db.prepare("SELECT 1 FROM sqlite_master WHERE name='purchase_dispatch_guards'").get())fail('HOST_GUARD_REQUIRED','旧采购预览需要在主程序重新核验；尚未上传');
    const first=db.prepare('SELECT * FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id);
    if(!first||first.identity_hash!==hostIdentity(p))fail('HOST_GUARD_REQUIRED','采购提交凭据与原付款或文件不符，请重新核验；尚未上传');
    let proof;try{proof=JSON.parse(first.proof);}catch{fail('HOST_GUARD_REQUIRED','采购提交凭据无法读取；尚未上传');}
    if(!proof||proof.schema!==1||!proof.facts?.versions||proof.day!==businessDate())fail('PURCHASE_BASIS_CHANGED','采购核验日期或依据已变化，请重新核验；尚未上传');
    const files=sources(file,p,proof);
    db.exec(consume?'BEGIN IMMEDIATE':'BEGIN');
    const current=db.prepare('SELECT * FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id);
    if(!current||current.identity_hash!==first.identity_hash||current.proof!==first.proof||current.state!=='AUTHORIZED')fail('HOST_SUBMISSION_REVIEW','原采购已有提交登记或正在更新，请核对原任务；未重新上传');
    checkLegacyCatalogs(db,proof);
    const actual=hostFacts(db,p.basis);
    if(canonical(actual)!==canonical(proof.facts)){
      if(canonical(actual.jstCancellation??null)!==canonical(proof.facts.jstCancellation??null))
        fail('PURCHASE_BASIS_CHANGED','聚水潭原单取消或付款重核依据已变化；尚未上传，请重新核对');
      const message=canonical(actual.versions.payment)!==canonical(proof.facts.versions.payment)?'付款单已更新，不能上传旧付款数量':'本仓签发、上游或准入依据已变化';
      fail('PURCHASE_BASIS_CHANGED',message+'；尚未上传，请在主程序重新核对并生成预览');
    }
    if(files.some(([f,s])=>stamp(f)!==s))fail('PURCHASE_BASIS_CHANGED','采购依据文件在核验时变化；尚未上传');
    if(consume){
      if(!/^[a-f0-9]{64}$/.test(requestHash||''))fail('HOST_REQUEST_PROOF','缺少原生请求摘要；尚未上传');
      db.prepare("UPDATE purchase_dispatch_guards SET state='CONSUMED',request_hash=?,updated_at=? WHERE purchase_id=? AND state='AUTHORIZED'").run(requestHash,now(),p.id);
    }
    db.exec('COMMIT');return {purchaseId:p.id,requestHash,consumed:consume};
  }finally{if(db.isTransaction)db.exec('ROLLBACK');db.close();}
}

// Called only while the native guard still owns an uncontinued request.
export function abortHostDispatch(p,requestHash){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;
  if(!file||!fs.existsSync(file))fail('HOST_GUARD_REQUIRED','未能核对本次上传状态，请保留原任务');
  const db=new DatabaseSync(file);
  try{
    db.exec('PRAGMA busy_timeout=1000');db.exec('BEGIN IMMEDIATE');
    const r=db.prepare('SELECT * FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id);
    if(!r||r.identity_hash!==hostIdentity(p)||r.state!=='CONSUMED'||r.request_hash!==requestHash)fail('HOST_SUBMISSION_REVIEW','原提交登记不符，保留原任务待核');
    db.prepare("UPDATE purchase_dispatch_guards SET state='ABORTED',updated_at=? WHERE purchase_id=?").run(now(),p.id);db.exec('COMMIT');
  }finally{if(db.isTransaction)db.exec('ROLLBACK');db.close();}
}

export function requireAbortedDispatch(p,requestHash){
  const file=process.env.REPLENISHMENT_BUSINESS_DB;if(!file)fail('HOST_SUBMISSION_REVIEW','未找到未放行证明');
  const db=new DatabaseSync(file,{readOnly:true});
  try{
    const r=db.prepare('SELECT * FROM purchase_dispatch_guards WHERE purchase_id=?').get(p.id);
    if(!r||r.state!=='ABORTED'||r.identity_hash!==hostIdentity(p)||r.request_hash!==requestHash)fail('HOST_SUBMISSION_REVIEW','未放行证明不匹配，保留提交事实');
  }finally{db.close();}
}
