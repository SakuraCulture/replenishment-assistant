import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createArchive} from '../src/zip.mjs';
import {purchaseContract} from '../src/purchase-contract.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {nativeCreationEvidence} from '../src/purchase-create-service.mjs';
import {checkCreatedPurchase,IMPORT_CODE} from '../src/purchase-create-contract.mjs';

const escape=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
function col(i){let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;}
async function workbook(file,source,rows){
  const c=purchaseContract(source),data=[c.columns.map(c=>c.rawHeader),...rows];
  const xml=data.map((r,i)=>`<row r="${i+1}">${r.map((v,j)=>`<c r="${col(j)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},{relativePath:'xl/workbook.xml',buffer:`<workbook xmlns:r="urn:r"><sheets><sheet name="${c.nativeSheetName}" r:id="r1"/></sheets></workbook>`},{relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},{relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+xml+'</sheetData></worksheet>'}]);
}
test('native main/detail XLSX values are read by contract and strictly compared per SKU',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-create-proof-'));t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const p={id:'p1',identity:{accountId:'A',merchantCode:'M'},basis:{store:'T'},supplierCode:'2078246',note:'AX_CREATE:p1 本地单:BH-1',file:{sha256:'a'.repeat(64)},rows:[{sku:'0001234567890123456789',quantity:'7',amount:'0.99'}],totalQuantity:'7',totalAmount:'0.99'};
  const main=Array(25).fill('');Object.assign(main,{0:'PO-1',1:'待提交',3:'T',5:'2078246',6:'1',7:'7',8:'0.99'});
  const detail=Array(43).fill('');Object.assign(detail,{0:'PO-1',1:'待提交',7:'T',9:'2078246',12:p.rows[0].sku,18:'7',26:'0.99',36:p.note});
  const files={D16:path.join(root,'main.xlsx'),D17:path.join(root,'detail.xlsx')};await workbook(files.D16,'D16',[main]);await workbook(files.D17,'D17',[detail]);
  const r={intentId:p.id,taskId:'GEI@'+IMPORT_CODE+'@261101173932@7114',code:IMPORT_CODE,fileSha256:p.file.sha256,exactSourceBytesPresent:true,importRequests:1,requestSha256:'b'.repeat(64)};
  const task={taskId:r.taskId,code:IMPORT_CODE,status:'FINISHED',successCount:'1',failCount:'0'};
  const run={status:'SUCCEEDED',orderId:'PO-1',query:{orderIds:['PO-1']},exports:{}};
  for(const source of ['D16','D17'])run.exports[source]={asset:{rows:1,sha256:await fileHash(files[source])}};
  const evidence=await nativeCreationEvidence(p,task,{supplyOrderId:'PO-1',buyerMessage:p.note},{sourceFile:async(_,s)=>files[s]},run);
  assert.equal(evidence.rows[0].sku,p.rows[0].sku);assert.equal(evidence.rows[0].amount,'0.99');assert.equal(checkCreatedPurchase(p,r,evidence).orderId,'PO-1');
  detail[26]='';await workbook(path.join(root,'blank.xlsx'),'D17',[detail]);
  const old=files.D17;files.D17=path.join(root,'blank.xlsx');run.exports.D17.asset.sha256=await fileHash(files.D17);
  const blank=await nativeCreationEvidence(p,task,{supplyOrderId:'PO-1',buyerMessage:p.note},{sourceFile:async(_,s)=>files[s]},run);
  assert.equal(blank.rows[0].amount,'');assert.throws(()=>checkCreatedPurchase(p,r,blank),{code:'PURCHASE_DECIMAL'});assert.ok(await fileHash(old));
});
