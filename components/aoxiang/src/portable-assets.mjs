// Read-only lookup for imported immutable paths. No payload/hash is rewritten.
import fs from 'node:fs/promises';
import path from 'node:path';
let cache;
function invalid(){const e=new Error('迁入文件映射无效或越出资料目录，未读取');e.code='MIGRATION_PATH_INVALID';throw e;}
const key=value=>process.platform==='win32'?path.resolve(value).toLowerCase():path.resolve(value);
export async function portablePath(file,{sha256}={}){
  const marker=process.env.REPLENISHMENT_PORTABLE_PATHS;
  if(!marker||typeof file!=='string')return file;
  try{if((await fs.stat(file)).isFile())return file;}catch(e){if(e.code!=='ENOENT'&&e.code!=='ENOTDIR')throw e;}
  let st;try{st=await fs.stat(marker);}catch(e){if(e.code==='ENOENT')return file;throw e;}
  if(st.size>16777216)invalid();
  const stamp=marker+'|'+st.size+'|'+st.mtimeMs;
  if(cache?.stamp!==stamp){
    const value=JSON.parse(await fs.readFile(marker,'utf8'));
    if(value.version!==1||!value.paths||!value.contents)invalid();
    cache={stamp,root:path.dirname(path.resolve(marker)),value,paths:new Map(Object.entries(value.paths).map(([old,relative])=>[key(old),relative]))};
  }
  const relative=cache.paths.get(key(file))||(sha256&&/^[a-f0-9]{64}$/.test(sha256)?cache.value.contents[sha256]?.[0]:null);
  if(!relative)return file;
  if(typeof relative!=='string'||path.isAbsolute(relative)||relative.includes('\\')||relative.split('/').some(p=>!p||p==='.'||p==='..'||p.includes(':')))invalid();
  const target=path.resolve(cache.root,...relative.split('/'));let real;
  try{real=await fs.realpath(target);}catch(e){if(e.code==='ENOENT')return target;throw e;}
  const root=await fs.realpath(cache.root),tail=path.relative(root,real);
  if(tail==='..'||tail.startsWith('..'+path.sep)||path.isAbsolute(tail))invalid();
  return real;
}
