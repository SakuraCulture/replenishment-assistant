// Isolated real-browser check. No login, user profile, network or platform export.
import {createRequire} from 'node:module';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import assert from 'node:assert/strict';
import {activateControl,openExportMenu} from '../components/jst_catalog/src/product-export.mjs';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const require=createRequire(path.join(root,'components/aoxiang/package.json'));
const {chromium}=require('playwright-core');
const executablePath=process.argv[2];
if(!executablePath)throw Error('Pass installed Edge executable; a temporary isolated headless profile is used.');
const browser=await chromium.launch({executablePath,headless:true});
try{
  const context=await browser.newContext();
  await context.route('**/*',r=>r.abort());
  const page=await context.newPage();
  await page.setContent('<button id="run">查询</button><input id="status" placeholder="商品状态"><button id="disabled" disabled>禁用</button><button id="hidden" style="display:none">隐藏</button>');
  await page.evaluate(()=>{
    window.clicks=0;document.querySelector('#run').onclick=()=>window.clicks++;
    window.focused=0;document.querySelector('#status').onfocus=()=>window.focused++;
    window.requestAnimationFrame=()=>1; // reproduce no animation frames
  });
  const foreground=await context.newPage(); // work page is not the selected tab
  const began=Date.now();await activateControl(page.locator('#run'),{timeout:2000});
  assert.equal(await page.evaluate(()=>window.clicks),1);
  await activateControl(page.locator('#status'),{timeout:2000,pointer:true});
  assert.equal(await page.evaluate(()=>window.focused),1);
  await assert.rejects(activateControl(page.locator('#disabled'),{timeout:0}),{code:'CONTROL_NOT_READY'});
  await assert.rejects(activateControl(page.locator('#hidden'),{timeout:0}),{code:'CONTROL_NOT_READY'});
  assert.equal(await page.evaluate(()=>window.clicks),1);
  await page.evaluate(()=>{
    const trigger=document.createElement('button');trigger.innerHTML='<span>导出</span>';document.body.append(trigger);
    const item=document.createElement('div');item.innerText='导出商品';item.style.display='none';document.body.append(item);
    trigger.onmouseover=()=>item.style.display='block';item.onclick=()=>window.clicks++;
  });
  await openExportMenu(page);
  assert.equal(await page.getByText('导出商品',{exact:true}).isVisible(),true);
  assert.equal(await page.evaluate(()=>window.clicks),1);
  await activateControl(page.getByText('导出商品',{exact:true}));
  assert.equal(await page.evaluate(()=>window.clicks),2);
  console.log(JSON.stringify({passed:true,checks:8,elapsed_ms:Date.now()-began,scope:'isolated headless Edge; requestAnimationFrame disabled; nonselected page; native focus and hover menu; no live platform or login'},null,2));
}finally{await browser.close();}
