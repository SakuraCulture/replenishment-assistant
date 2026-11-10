import fs from 'node:fs/promises';
import crypto from 'node:crypto';
import path from 'node:path';
export const WAREHOUSES=['山东仓','河北仓'];
export const now=()=>new Date().toISOString();
export const sha=value=>crypto.createHash('sha256').update(value).digest('hex');
export const canonical=value=>JSON.stringify(value&&typeof value==='object'?Array.isArray(value)?value.map(v=>JSON.parse(canonical(v))):Object.fromEntries(Object.keys(value).sort().map(k=>[k,JSON.parse(canonical(value[k]))])):value??null);
export class CatalogError extends Error {constructor(code,message,details){super(message);this.code=code;if(details)this.details=details;}}
export function check(signal){if(signal?.aborted)throw new CatalogError('CANCELLED','已请求停止；已受理任务保留，未重复提交');}
export const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
export const safeError=e=>({code:e.code||'CATALOG_FAILED',message:String(e.message||e).replace(/https?:\/\/[^\s；]+/g,v=>{try{const u=new URL(v);return u.origin+u.pathname;}catch{return '[平台地址]';}}).slice(0,1600),...(e.code==='CATALOG_STATE_BUSY'&&e.details?{details:e.details}:{})});
export async function atomic(file,value){
  await fs.mkdir(path.dirname(file),{recursive:true});const tmp=file+'.'+crypto.randomUUID()+'.tmp';
  const handle=await fs.open(tmp,'wx');
  try{await handle.writeFile(JSON.stringify(value,null,2));await handle.sync();}finally{await handle.close();}
  try{await fs.rename(tmp,file);}finally{await fs.unlink(tmp).catch(()=>{});}
}
export function inside(root,file){const r=path.relative(path.resolve(root),path.resolve(file));return !r.startsWith('..'+path.sep)&&r!=='..'&&!path.isAbsolute(r);}
export function samePath(a,b){return path.resolve(a).toLowerCase()===path.resolve(b).toLowerCase();}
