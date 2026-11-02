// Offline replay of the frozen, actually created test PO evidence. No browser,
// upload, platform calls or business database writes. Keep QA sources local.
import fs from 'node:fs/promises';
import path from 'node:path';
import {pathToFileURL,fileURLToPath} from 'node:url';
import crypto from 'node:crypto';
const args=process.argv.slice(2),arg=name=>args[args.indexOf(name)+1];
if(!args.includes('--qa')||!args.includes('--output'))throw Error('Use --qa <QA root> --output <new JSON file> [--component <component root>]');
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const qa=path.resolve(arg('--qa')),component=args.includes('--component')?path.resolve(arg('--component')):path.join(root,'components/aoxiang');
const {nativeCreationEvidence}=await import(pathToFileURL(path.join(component,'src/purchase-create-service.mjs')));
const {checkCreatedPurchase}=await import(pathToFileURL(path.join(component,'src/purchase-create-contract.mjs')));
const {fileHash}=await import(pathToFileURL(path.join(component,'src/workbook-validator.mjs')));
const read=async p=>JSON.parse(await fs.readFile(p,'utf8'));
const packaged=await read(path.join(qa,'ax08-live-purchase-20261102-v1/package-native.json'));
const p=await read(packaged.package_file);
if(await fileHash(packaged.package_file)!==packaged.package_sha256||await fileHash(p.file.path)!==p.file.sha256)throw Error('Frozen intent or workbook changed');
const prior=await read(path.join(qa,'ax10-live-purchase-20261102_094243/frozen-ledger-record.json'));
if(prior.id!==p.id||prior.state!=='BOUND'||prior.binding.orderId!=='PO202611020938424570')throw Error('Wrong accepted test purchase');
const old=prior.evidence,files=Object.fromEntries(old.files.map(f=>[f.source,f]));
const run={status:'SUCCEEDED',orderId:prior.binding.orderId,query:{orderIds:[prior.binding.orderId]},exports:{}};
for(const source of ['D16','D17']){
 if(await fileHash(files[source].file)!==files[source].sha256)throw Error('Native bytes changed: '+source);
 run.exports[source]={asset:{rows:source==='D16'?1:p.rows.length,sha256:files[source].sha256}};
}
const proof=await nativeCreationEvidence(p,old.task,{supplyOrderId:prior.binding.orderId,buyerMessage:p.note},{sourceFile:async(_,s)=>files[s].file},run);
const bound=checkCreatedPurchase(p,prior.receipt,proof);
if(JSON.stringify(proof.main)!==JSON.stringify(old.main)||JSON.stringify(proof.rows)!==JSON.stringify(old.rows))throw Error('Native field/order/type changed');
const report={passed:true,scope:'offline actual D16/D17 exact-file replay; no platform call',order:bound.orderId,
 status:bound.status,rows:proof.rows,main:proof.main,file_sha256:files,
 runtime_source_sha256:Object.fromEntries(await Promise.all(['purchase-ui.mjs','purchase-contract.mjs','purchase-coordinator.mjs','purchase-create-contract.mjs'].map(async n=>[n,crypto.createHash('sha256').update(await fs.readFile(path.join(component,'src',n))).digest('hex')])))};
await fs.writeFile(path.resolve(arg('--output')),JSON.stringify(report,null,2)+'\n',{flag:'wx'});
console.log(JSON.stringify({passed:true,order:bound.orderId,status:bound.status,rows:proof.rows.length,sku_count_raw:proof.main.skuCount}));
