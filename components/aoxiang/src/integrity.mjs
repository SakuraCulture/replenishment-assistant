import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {exists,fail,relativeSafe} from './common.mjs';
import {fileHash} from './workbook-validator.mjs';
export const installRoot=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
export async function verifyInstallation(root=installRoot){
  const manifestFile=path.join(root,'integrity.manifest.json');
  if(!await exists(manifestFile)){
    if(await exists(path.join(root,'.dev-checkout')))return {mode:'DEVELOPMENT_CHECKOUT',files:0};
    fail('INSTALLATION_INCOMPLETE','发行完整性清单缺失，请重新解压原始发行包');
  }
  const manifest=JSON.parse(await fs.readFile(manifestFile,'utf8'));
  if(manifest.format!==1||!Array.isArray(manifest.files)||manifest.files.length<10)fail('INSTALLATION_INCOMPLETE','发行完整性清单无效');
  for(const f of manifest.files){
    relativeSafe(f.relativePath);const file=path.join(root,...f.relativePath.split('/'));
    if(!await exists(file)||await fileHash(file)!==f.sha256)fail('INSTALLATION_HASH_MISMATCH','发行程序或依赖已变更，请重新解压发行包',{relativePath:f.relativePath});
  }return {mode:'RELEASE_VERIFIED',files:manifest.files.length};
}
