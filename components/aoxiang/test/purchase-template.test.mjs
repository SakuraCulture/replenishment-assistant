import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {fileURLToPath} from 'node:url';
import {PurchaseTemplateService,inspectPurchaseTemplate,templateContract} from '../src/purchase-template.mjs';
import {purchaseTemplateSource} from '../src/purchase-template-download.mjs';
import {PurchaseUI} from '../src/purchase-ui.mjs';
import {fileHash} from '../src/workbook-validator.mjs';
import {uid,readJson} from '../src/common.mjs';
import {createArchive,openArchive} from '../src/zip.mjs';
import {isolatedEdge} from './edge-fixture.mjs';

const native=fileURLToPath(new URL('./fixtures/purchase-import-template.xlsx',import.meta.url));
const identity={accountId:'fixture-account',merchantCode:'fixture-merchant'};
const code='SAAS-CHAIN-PLAN-QZC-SUPPLY-PURCHASE-ORDER-MULTI-UNIT-IMPORT-V2';
const url='https://sc-gei-tc-supply-chain-portal.ele.me/gei/import/template/'+code;
async function root(){return fs.mkdtemp(path.join(os.tmpdir(),'ax-template-'));}
async function variant(file,change){
  const zip=await openArchive(native),members=[];
  try{for(const name of zip.entries.keys())members.push({relativePath:name,buffer:change(name,await zip.buffer(name))});}finally{zip.close();}
  await createArchive(file,members);return file;
}
function adapterFor(source=native){
  const stats={calls:0};let failure=false;
  return {stats,setFailure:v=>failure=v,adapter:{downloadPurchaseTemplate:async({directory,expectedIdentity})=>{
    stats.calls++;assert.deepEqual(expectedIdentity,identity);if(failure)throw Object.assign(new Error('offline'),{code:'PURCHASE_TEMPLATE_HTTP'});
    await fs.mkdir(directory,{recursive:true});const file=path.join(directory,uid()+'.xlsx');await fs.copyFile(source,file);
    return {file,sha256:await fileHash(file),identity,source:purchaseTemplateSource(url),checkedAt:new Date().toISOString(),uiProductLimit:12000,importSubmissions:0,transport:'fixture'};
  }}};
}

test('采购模板：原生12列、单位说明、示例行和隐藏是/否选项的语义契约',async()=>{
  const actual=await inspectPurchaseTemplate(native),contract=await templateContract();
  assert.equal(actual.sha256,'44c4f65dbbb82ae03244c9a2e1427b8a1c44cddbb8eef8857017c8009902eefa');assert.deepEqual(actual.schema,contract.schema);
  const [main,helper]=actual.schema.sheets;
  assert.equal(main.headers.length,12);assert.equal(main.headers[3].value,'*采购数量（基础单位）');assert.equal(main.headers[10].value,'是否创建外部订单');
  assert.deepEqual(main.sampleRows,[{row:8,isExample:true},{row:9,isExample:true}]);assert.equal(main.validations[0].formula1,'hidden_sheet1_col_10');
  assert.equal(helper.state,'hidden');assert.deepEqual(helper.rows.flatMap(r=>r.cells.map(c=>c.value)),['是','否']);
});

test('采购模板：每次准备都重新请求平台；相同内容只复用归档，不使用离线旧模板',async()=>{
  const outputRoot=await root(),fake=adapterFor(),service=new PurchaseTemplateService({adapter:fake.adapter,identity,outputRoot});
  const one=await service.fetchLatest(),two=await service.prepareImport();
  assert.equal(fake.stats.calls,2);assert.equal(one.file,two.file);assert.notEqual(one.attemptId,two.attemptId);assert.equal(two.status,'SUPPORTED');
  assert.equal((await readJson(two.latestFile)).attemptId,two.attemptId);assert.equal(await fileHash(one.file),one.sha256);
  fake.setFailure(true);await assert.rejects(()=>service.prepareImport(),{code:'PURCHASE_TEMPLATE_HTTP'});
  assert.equal(fake.stats.calls,3);assert.equal((await readJson(two.latestFile)).status,'FETCH_FAILED');assert.equal(await fileHash(one.file),one.sha256);
});

test('采购模板：样式或ZIP元数据更新可直接接受，不误判为列映射变化',async()=>{
  const outputRoot=await root(),changed=path.join(outputRoot,'formatting.xlsx');
  await variant(changed,(name,body)=>name==='xl/worksheets/sheet1.xml'?Buffer.from(body.toString().replace('</worksheet>','<!-- platform metadata revision --></worksheet>')):body);
  const a=await inspectPurchaseTemplate(native),b=await inspectPurchaseTemplate(changed);assert.notEqual(a.sha256,b.sha256);assert.equal(a.semanticSha256,b.semanticSha256);
  const fake=adapterFor(changed),result=await new PurchaseTemplateService({adapter:fake.adapter,outputRoot,identity}).prepareImport();assert.equal(result.status,'SUPPORTED');
});

test('采购模板：字段、填写规则、隐藏表和新增样例行漂移均保存新版并阻止导入准备',async()=>{
  const changes=[
    (n,s)=>n==='xl/worksheets/sheet1.xml'?s.replace(/(<c r="A7"[^>]*><is><t>)[\s\S]*?(<\/t>)/,'$1*门店编码新字段$2'):s,
    (n,s)=>n==='xl/worksheets/sheet1.xml'?s.replace(/(<c r="A6"[^>]*><is><t>)[\s\S]*?(<\/t>)/,'$1必填：改用其他口径$2'):s,
    (n,s)=>n==='xl/workbook.xml'?s.replace('state="hidden"','state="visible"'):s,
    (n,s)=>n==='xl/workbook.xml'?s.replace('hidden_sheet1_col_10!A1:A3002','hidden_sheet1_col_10!K1:K3002'):s,
    (n,s)=>n==='xl/worksheets/sheet1.xml'?s.replace('</sheetData>','<row r="10"><c r="A10" t="inlineStr"><is><t>示例：new</t></is></c></row></sheetData>'):s
  ];
  for(const change of changes){
    const outputRoot=await root(),source=path.join(outputRoot,'changed.xlsx');await variant(source,(name,body)=>/\.xml$/.test(name)?Buffer.from(change(name,body.toString())):body);
    const fake=adapterFor(source);let error;
    try{await new PurchaseTemplateService({adapter:fake.adapter,outputRoot,identity}).prepareImport();}catch(e){error=e;}
    assert.equal(error?.code,'PURCHASE_TEMPLATE_CHANGED');const record=await readJson(error.details.recordFile);
    assert.equal(record.status,'NEEDS_REVIEW');assert.equal(await fileHash(record.file),await fileHash(source));assert.equal((await readJson(record.latestFile)).status,'NEEDS_REVIEW');
  }
});

test('采购模板：用户修改过缓存时不覆盖；商家不符或暂停时不交付可用模板',async()=>{
  const outputRoot=await root(),fake=adapterFor(),service=new PurchaseTemplateService({adapter:fake.adapter,outputRoot,identity}),one=await service.fetchLatest();
  await fs.appendFile(one.file,'user edit');const edited=await fileHash(one.file);
  await assert.rejects(()=>service.prepareImport(),{code:'TEMPLATE_CACHE_CHANGED'});assert.equal(await fileHash(one.file),edited);
  const controller=new AbortController();controller.abort();await assert.rejects(()=>new PurchaseTemplateService({adapter:fake.adapter,outputRoot,identity,signal:controller.signal}).fetchLatest(),{code:'PAUSED'});
  const bad={downloadPurchaseTemplate:async p=>({...await fake.adapter.downloadPurchaseTemplate(p),identity:{...identity,merchantCode:'wrong'}})};
  await assert.rejects(()=>new PurchaseTemplateService({adapter:bad,outputRoot,identity}).prepareImport(),{code:'WRONG_IDENTITY'});
});

test('采购模板：空XML成员明确拒绝；下载来源只允许当前原生采购模板GET',async()=>{
  const folder=await root(),file=path.join(folder,'empty.xlsx'),zip=await openArchive(native),members=[];
  try{for(const name of zip.entries.keys())members.push({relativePath:name,buffer:await zip.buffer(name)});}finally{zip.close();}
  members.push({relativePath:'xl/drawings/empty.xml',buffer:''});await createArchive(file,members);
  await assert.rejects(()=>inspectPurchaseTemplate(file),{code:'PURCHASE_TEMPLATE_XML'});
  assert.equal(purchaseTemplateSource(url.replace('V2','V3')).taskCode.endsWith('V3'),true);
  for(const value of [url+'?token=secret',url.replace('sc-gei-tc-supply-chain-portal.ele.me','wrong.example'),url.replace('PURCHASE-ORDER','OTHER')])assert.throws(()=>purchaseTemplateSource(value),{code:'PURCHASE_TEMPLATE_SOURCE'});
});

test('真实隔离Edge：仅点模板按钮；空窗取消；不确认、不上传，阻断PUT且拒绝HTML登录页',{skip:process.platform!=='win32',timeout:45000},async()=>{
  const fixture=await isolatedEdge();let ui;
  try{
    const context=fixture.browser.contexts()[0],page=context.pages()[0],body=await fs.readFile(native),directory=await root();
    await page.route('**/gei/import/template/**',r=>r.fulfill({status:200,headers:{'Access-Control-Allow-Origin':'*'},body:'ok'}));
    let networkWrites=0;
    await page.route('**/gei/import/task/**',r=>{networkWrites++;return r.fulfill({status:200,body:'wrong'});});
    ui=new PurchaseUI(page,{timeoutMs:3000});ui.open=async expected=>{assert.deepEqual(expected,identity);ui.identity=identity;return identity;};
    const originalGet=context.request.get.bind(context.request);let isHtml=false,getCount=0;
    context.request.get=async(value,options)=>{getCount++;assert.equal(value,url);assert.equal(options.maxRedirects,0);return {status:()=>200,headers:()=>({}),body:async()=>isHtml?Buffer.from('<html>登录</html>'):body,dispose:async()=>{}};};
    async function reset(withWrite=false){
      await page.setContent('<button onclick="document.querySelector(\'.next-dialog\').hidden=false">Excel批量导入创建</button><div class="next-dialog" hidden><p>请按照规定格式上传文件，下载导入模板</p><p>每次添加的商品数量不能超过 12000个</p><input type="file"><button id="template">下载导入模板</button><button onclick="window.confirmed=true">确定</button><button onclick="window.cancelled=true;this.parentElement.hidden=true">取消</button></div>');
      await page.evaluate(({url,withWrite})=>{document.querySelector('#template').onclick=()=>{if(withWrite)fetch(url.replace('/template/','/task/'),{method:'PUT',body:'bad'}).catch(()=>{});fetch(url).catch(()=>{});};},{url,withWrite});
    }
    await reset();const result=await ui.downloadPurchaseTemplate({directory,expectedIdentity:identity});
    assert.equal(result.sha256,await fileHash(native));assert.equal(result.uiProductLimit,12000);assert.equal(result.importSubmissions,0);assert.equal(await page.evaluate(()=>window.confirmed),undefined);assert.equal(await page.evaluate(()=>window.cancelled),true);
    await reset();isHtml=true;await assert.rejects(()=>ui.downloadPurchaseTemplate({directory,expectedIdentity:identity}),{code:'PURCHASE_TEMPLATE_NOT_XLSX'});
    await reset(true);isHtml=false;await assert.rejects(()=>ui.downloadPurchaseTemplate({directory,expectedIdentity:identity}),{code:'PURCHASE_TEMPLATE_SIDE_EFFECT'});assert.equal(networkWrites,0);assert.equal(getCount,3);
    context.request.get=originalGet;
  }finally{await ui?.dispose();await fixture.close();}
});
