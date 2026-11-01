import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {createArchive} from '../src/zip.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {writeJson,uid,now,exists,fail,APP_VERSION} from '../src/common.mjs';
import {verifyInstallation} from '../src/integrity.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..'),name=`翱象补货建议助手 ${APP_VERSION} 测试版`,dist=path.join(root,'dist'),stage=path.join(dist,'.build-'+uid()),destination=path.join(dist,name),zipFile=destination+'.zip';
if(await exists(destination)||await exists(zipFile))fail('BUILD_EXISTS','该版本发行目录已经存在，先核对现有生成物');
await fs.mkdir(stage,{recursive:true});
for(const item of ['src','assets','node_modules','package.json','package-lock.json','启动.cmd','README.md','实现与验证.md','NOTICE.txt','验收记录.json'])await fs.cp(path.join(root,item),path.join(stage,item),{recursive:true,force:false,errorOnExist:true});
await fs.mkdir(path.join(stage,'runtime'));for(const item of ['node.exe','LICENSE.txt'])await fs.copyFile(path.join(root,'runtime',item),path.join(stage,'runtime',item));
async function files(dir,prefix=''){const result=[];for(const e of await fs.readdir(dir,{withFileTypes:true})){const relative=prefix+e.name;if(e.isSymbolicLink())fail('BUILD_LINK','发行内容不允许链接');if(e.isDirectory())result.push(...await files(path.join(dir,e.name),relative+'/'));else result.push(relative);}return result;}
const members=await files(stage),hashes=[];
for(const relativePath of members){const file=path.join(stage,relativePath),st=await fs.stat(file);hashes.push({relativePath,bytes:st.size,sha256:await fileHash(file)});}
await writeJson(path.join(stage,'integrity.manifest.json'),{format:1,version:APP_VERSION,channel:'CONTROLLED_TEST_BUILD',builtAt:now(),files:hashes});
await verifyInstallation(stage);
// Whitelist-based packaging excludes dev-evidence, state roots, Local State,
// browser profiles and personal result files. Scan textual payload for secrets.
const suspect=[];
for(const relative of members){if(!/\.(?:mjs|js|json|md|txt|cmd|ps1|html|xml|yml|yaml)$/i.test(relative))continue;const body=await fs.readFile(path.join(stage,relative),'utf8');if(/(?:Signature|security-token|X-Amz-Signature)=[A-Za-z0-9%+/]{16,}|sk-proj-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA )?PRIVATE KEY-----/.test(body))suspect.push(relative);}
if(suspect.length)fail('PACKAGE_CREDENTIAL_SCAN','发行文本中发现疑似凭据',{files:suspect});
await fs.rename(stage,destination);
await createArchive(zipFile,(await files(destination)).map(relativePath=>({relativePath:name+'/'+relativePath,file:path.join(destination,relativePath)})));
const result={version:APP_VERSION,channel:'CONTROLLED_TEST_BUILD',directory:destination,zip:zipFile,bytes:(await fs.stat(zipFile)).size,sha256:await fileHash(zipFile),files:hashes.length+1,credentialScan:'PASS_WHITELIST_AND_TEXT_PATTERN',builtAt:now()};
await writeJson(path.join(dist,'发行记录.json'),result);console.log(JSON.stringify(result,null,2));
