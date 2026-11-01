import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import fs from 'node:fs/promises';
import path from 'node:path';
import {createArchive} from '../src/zip.mjs';
import {validateWorkbook} from '../src/workbook-validator.mjs';
import {parseResponse,businessDate,scopeHash,relativeSafe} from '../src/common.mjs';
import {fieldContract,validateConfig,freshConfig} from '../src/config.mjs';

const esc=s=>String(s).replaceAll('&','&amp;').replaceAll('<','&lt;');
const col=i=>{let s='';for(i++;i;i=Math.floor((i-1)/26))s=String.fromCharCode(65+(i-1)%26)+s;return s;};
async function fixture(dir,{warehouse='WH001',badHeader=false,duplicate=false}={}){
  const c=await fieldContract('v1');let header=c.columns.map(x=>x.rawHeader);if(badHeader)header[41]='错误毛利列';
  const data=Array(52).fill('');data[1]=warehouse;data[7]='009007199254740993';data[8]='001234';data[31]='12';
  const rows=[header,data,...(duplicate?[data]:[])].map((r,i)=>`<row r="${i+1}">${r.map((v,j)=>`<c r="${col(j)}${i+1}" t="inlineStr"><is><t>${esc(v)}</t></is></c>`).join('')}</row>`).join('');
  const file=path.join(dir,'fixture-'+Math.random()+'.xlsx');
  await createArchive(file,[
    {relativePath:'[Content_Types].xml',buffer:'<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'},
    {relativePath:'xl/workbook.xml',buffer:'<workbook xmlns:r="urn:relationships"><sheets><sheet name="商品补货建议" r:id="rId1"/></sheets></workbook>'},
    {relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>'},
    {relativePath:'xl/worksheets/sheet1.xml',buffer:`<worksheet><dimension ref="A1:A1"/><sheetData>${rows}</sheetData></worksheet>`},
  ]);return file;
}
test('真实XML行数、长编码和三个同名毛利列保留',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ax-test-'));
  const result=await validateWorkbook(await fixture(dir,{duplicate:true}),{warehouseCode:'WH001',expectedRows:2,contract:await fieldContract('v1')});
  assert.equal(result.rows,2);assert.equal(result.columns,52);assert.equal(result.uniqueWarehouseSkuKeys,1);assert.equal(result.duplicateWarehouseSkuRows,1);
});
test('仓店错位和字段漂移均不能通过',async()=>{
  const dir=await fs.mkdtemp(path.join(os.tmpdir(),'ax-test-'));
  const contract=await fieldContract('v1');
  const wrong=await fixture(dir,{warehouse:'OTHER'});
  await assert.rejects(()=>validateWorkbook(wrong,{warehouseCode:'WH001',expectedRows:1,contract}),{code:'ENTITY_MISMATCH'});
  const drift=await fixture(dir,{badHeader:true});
  await assert.rejects(()=>validateWorkbook(drift,{warehouseCode:'WH001',expectedRows:1,contract}),{code:'SCHEMA_DRIFT'});
  const short=await fixture(dir);
  await assert.rejects(()=>validateWorkbook(short,{warehouseCode:'WH001',expectedRows:2,contract}),{code:'ROW_COUNT_MISMATCH'});
});
test('无损响应解析保持大整数和前导零文本',()=>{
  const x=parseResponse('{"taskId":9007199254740993,"code":"000123","total":11782,"success":true}');
  assert.equal(x.taskId,'9007199254740993');assert.equal(x.code,'000123');assert.equal(x.total,'11782');assert.equal(x.success,true);
});
test('Asia/Shanghai跨日和范围指纹',()=>{
  assert.equal(businessDate(new Date('2026-10-31T16:00:00Z')),'2026-11-01');assert.notEqual(scopeHash('A'),scopeHash('B'));
});
test('迁移相对路径拒绝越界和绝对路径',()=>{
  for(const bad of ['../a','a/../b','C:/a','/a','a\\b','a//b','a/file.xlsx:stream','foo./bar','CON/x','a/aux.xlsx'])assert.throws(()=>relativeSafe(bad));
  assert.equal(relativeSafe('results/仓点A.xlsx'),'results/仓点A.xlsx');
});
test('完整JSON Schema拒绝未知凭据字段、未验证目标及数字ID',async()=>{
  const config=freshConfig({accountId:'123',merchantCode:'merchant',displayName:'商家'},[{buyerWarehouseCode:'WH001',displayName:'仓点',operatingStores:[],mappingSource:'native-selector',mappingVerifiedAt:new Date().toISOString()}]);
  await validateConfig('portable.config',config);
  await assert.rejects(()=>validateConfig('portable.config',{...config,cookie:'not-a-real-cookie'}),{code:'CONFIG_SCHEMA'});
  const bad=structuredClone(config);bad.tenantBinding.accountId=123;
  await assert.rejects(()=>validateConfig('portable.config',bad),{code:'CONFIG_SCHEMA'});
});
