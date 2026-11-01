import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {fileHash} from '../src/workbook-validator.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const dir=path.join(root,'runtime');await fs.mkdir(dir,{recursive:true});
const target=path.join(dir,'node.exe');await fs.copyFile(process.execPath,target);
let license;
try{license=await fs.readFile(path.join(dir,'LICENSE.txt'),'utf8');}catch{
  const licenseResponse=await fetch(`https://raw.githubusercontent.com/nodejs/node/${process.version}/LICENSE`,{signal:AbortSignal.timeout(30000)});
  if(!licenseResponse.ok)throw Error('无法取得固定Node版本的官方许可证，发行包暂不完整');
  license=await licenseResponse.text();
}
if(!license.includes('Node.js')||!license.includes('Permission is hereby granted'))throw Error('Node许可证内容不符合预期');
await fs.writeFile(path.join(dir,'LICENSE.txt'),license,'utf8');
if(await fileHash(target)!==await fileHash(process.execPath))throw Error('运行时复制哈希不符');
console.log(JSON.stringify({node:process.version,sha256:await fileHash(target),runtimeCopied:true}));
