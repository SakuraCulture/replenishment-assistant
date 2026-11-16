// Actual Edge + persistent isolated profiles. Never opens the user's profile.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import assert from 'node:assert/strict';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'components/aoxiang/package.json'));
const {chromium}=require('playwright-core');
const [original,fixed,directory]=process.argv.slice(2),out=path.resolve(directory);
await fs.mkdir(out,{recursive:false});
const data={dates:['2026-11-11'],warehouses:['山东仓','河北仓'],sourceRange:'隔离合成',
  records:[['2026-11-11','山东仓','0001','合成商品',10,'启用','合成类目','合成供应商'],['2026-11-11','河北仓','0001','合成商品',0,'启用','合成类目','合成供应商']],observed:[]};
const source=await fs.readFile(original,'utf8'),repair=await fs.readFile(fixed,'utf8');
const synthetic=s=>s.replace(/^const DATA = .*$/m,'const DATA = '+JSON.stringify(data)+';');
const file=path.join(out,'dashboard.html'),url=pathToFileURL(file).href;
const checks=[],dialogs=[],errors=[];let context;
const executable=['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe','C:/Program Files/Microsoft/Edge/Application/msedge.exe'].find(p=>require('node:fs').existsSync(p));
assert(executable,'Edge executable required');
async function open(profile){
  context=await chromium.launchPersistentContext(path.join(out,profile),{executablePath:executable,headless:true,viewport:{width:1440,height:1000},args:['--no-first-run','--disable-background-networking','--disable-component-update']});
  await context.route('**/*',r=>['file:','about:','data:'].includes(new URL(r.request().url()).protocol)?r.continue():r.abort());
  const page=context.pages()[0]||await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  page.on('dialog',async d=>{dialogs.push({type:d.type(),text:d.message()});if(d.type()==='confirm')await d.accept();else await d.dismiss();});
  await page.goto(url);await page.waitForFunction(()=>document.getElementById('dateSelect').options.length>0);return page;
}
function check(ok,label){assert(ok,label);checks.push(label);}
async function stock(page,day){return page.evaluate(day=>DATA.records.find(r=>r[0]===day&&r[1]==='山东仓')?.[4],day);}
async function csv(page,day,qty){
  const text='观测日期,供货仓,商品编码,商品名称,可用库存,商品状态,商品分类,供应商名称\n'+`${day},山东仓,0001,合成商品,${qty},启用,合成类目,合成供应商\n`;
  await page.locator('#csvInput').setInputFiles({name:'合成更正.csv',mimeType:'text/csv',buffer:Buffer.from(text)});
  await page.waitForFunction(()=>document.getElementById('csvInput').value==='');
}
const report={passed:false,checks,dialogs,errors,synthetic:true,live_platform:false,actual_browser:'Edge',original_html_modified:false};
try{
  await fs.writeFile(file,synthetic(source));let page=await open('profile-legacy');
  await csv(page,'2026-11-11',99);check(await stock(page,'2026-11-11')===99,'旧版当页显示更正值');
  await page.reload();check(await stock(page,'2026-11-11')===10,'实际浏览器复现旧版已有日期重开回退');
  await csv(page,'2026-11-14',7);await page.reload();check(await stock(page,'2026-11-14')===7,'旧版新增日期第一次保存可重开');
  await csv(page,'2026-11-14',2);await page.reload();check(await stock(page,'2026-11-14')===7,'实际浏览器复现旧版新增日期再次更正回退');
  await page.evaluate(()=>localStorage.setItem('warehouse_stockout_actions_v1',JSON.stringify([{id:1001,level:'warehouse',target:'山东仓',warehouse:'山东仓',status:'待处理',baselineDate:'2026-11-11',baselineBase:1,baselineOut:0,baselineRate:0,statusFilter:'all'}])));
  await context.close();context=null;
  await fs.writeFile(file,synthetic(repair));page=await open('profile-legacy');
  check(await stock(page,'2026-11-14')===7,'修复版兼容原库存增量存储');
  check(await page.evaluate(()=>readActions()[0].id)===1001,'修复版保留原治理记录身份');
  await csv(page,'2026-11-11',99);await csv(page,'2026-11-14',2);
  await page.reload();check(await stock(page,'2026-11-11')===99&&await stock(page,'2026-11-14')===2,'已有和新增日期的更正均在重开后保留');
  const count=await page.evaluate(()=>DATA.records.length);await csv(page,'2026-11-14',2);
  check(await page.evaluate(()=>DATA.records.length)===count,'同键重复导入不新增商品记录');
  await csv(page,'2026-11-15',0);await csv(page,'2026-11-16',-3);await page.reload();
  check(await stock(page,'2026-11-15')===0&&await stock(page,'2026-11-16')===-3,'0和负库存真实保存，缺货口径保持');
  check(await page.evaluate(()=>aggregateFor('2026-11-16','山东仓','all').out)===1,'负库存仍计入原缺货统计');
  const before=await page.evaluate(()=>localStorage.getItem('warehouse_stockout_bundle_v2'));
  await page.evaluate(()=>{const set=Storage.prototype.setItem;Storage.prototype.setItem=function(k,v){if(k==='warehouse_stockout_bundle_v2')throw new DOMException('synthetic quota','QuotaExceededError');return set.call(this,k,v);};});
  await csv(page,'2026-11-11',123);
  check(await stock(page,'2026-11-11')===99,'存储空间不足时页面保留最后成功数据');
  check(await page.evaluate(()=>localStorage.getItem('warehouse_stockout_bundle_v2'))===before,'空间不足不覆盖已保存资料');
  check((await page.locator('#importStatus').innerText()).includes('未确认保存'),'空间不足提示不会被成功提示覆盖');
  await page.reload();check(await stock(page,'2026-11-11')===99,'空间不足后真实重开仍保留旧资料');
  const download=page.waitForEvent('download');await page.locator('#exportFullBackup').click();const saved=await download;
  const backup=path.join(out,'full-backup.json');await saved.saveAs(backup);
  const bundle=JSON.parse(await fs.readFile(backup,'utf8'));check(bundle.bundle.imported.length===4&&bundle.bundle.actions.length===1,'完整备份同时包含全部库存修订和治理记录');
  await page.screenshot({path:path.join(out,'已更正并重开.png'),fullPage:true});
  await context.close();context=null;page=await open('profile-fresh');
  check(await stock(page,'2026-11-11')===10,'新浏览器存储从原基线开始');
  await page.locator('#backupInput').setInputFiles(backup);await page.waitForFunction(()=>document.getElementById('backupInput').value==='');
  check(await stock(page,'2026-11-11')===99&&await stock(page,'2026-11-14')===2,'跨隔离浏览器用完整备份恢复库存');
  check(await page.evaluate(()=>readActions()[0].id)===1001,'完整备份恢复原治理记录身份');
  await context.close();context=null;page=await open('profile-fresh');
  check(await stock(page,'2026-11-11')===99,'浏览器进程关闭重开后修订仍保留');
  await page.screenshot({path:path.join(out,'完整恢复与重开.png'),fullPage:true});
  await page.evaluate(()=>localStorage.setItem('warehouse_stockout_bundle_v2','{synthetic broken json'));await page.reload();
  check((await page.locator('#importStatus').innerText()).includes('无法读取'),'损坏的旧存储明确提示，页面仍可打开');
  await csv(page,'2026-11-11',123);
  check(await page.evaluate(()=>localStorage.getItem('warehouse_stockout_bundle_v2'))==='{synthetic broken json','旧存储损坏时不覆盖成空记录或新导入');
  check(errors.length===0,'真实页面没有JavaScript运行错误');report.passed=true;
}catch(error){report.error=error.stack;throw error;}
finally{if(context)await context.close();await fs.writeFile(path.join(out,'acceptance.json'),JSON.stringify(report,null,2));console.log(JSON.stringify({passed:report.passed,checks:checks.length,out,errors}));}
