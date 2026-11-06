import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {isolatedEdge} from './edge-fixture.mjs';
import {AoxiangUI} from '../src/aoxiang-ui.mjs';
import {EdgeBridge} from '../src/edge-bridge.mjs';
import path from 'node:path';
const prefix='https://tc-supply-chain-portal.ele.me';
const html=String.raw`<!doctype html><meta charset="utf-8"><style>.overlay{position:fixed;inset:0;z-index:99;background:#8888;display:grid;place-content:center}.box{background:white;padding:40px}</style><main id="suggestTable"><button onclick="historyOpen()">历史记录</button></main><script>
window.historyCalls=0;window.ackCalls=0;
function notice(){if(document.querySelector('.overlay'))return;const wrap=document.createElement('div');wrap.className='overlay';wrap.innerHTML='<div role="dialog" class="box"><h2>正在导出数据...</h2><p>请点击 历史记录 查看结果</p><button onclick="window.ackCalls++;this.closest(\'.overlay\').remove()">我知道了</button><button>历史记录</button></div>';document.body.append(wrap);}
function historyOpen(){historyCalls++;fetch('${prefix}/ebai/gei/task/list',{method:'POST',body:'{}'});const m=document.createElement('div');m.setAttribute('role','dialog');m.setAttribute('aria-label','历史记录');m.innerHTML='<button onclick="historyCalls++;fetch(\'${prefix}/ebai/gei/task/list\',{method:\'POST\',body:\'{}\'})">刷新列表</button>';document.body.append(m);}
</script>`;
test('真实Edge复现：受理后迟到的导出弹窗自动关闭，历史查询只执行一次',{skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge();let ui;
  try{
    const page=fixture.browser.contexts()[0].pages()[0];await page.route(prefix+'/**',r=>r.fulfill({status:200,headers:{'content-type':'application/json','access-control-allow-origin':'*'},body:'{"success":true,"total":1,"data":[{"taskId":"known1","taskStatus":"FINISHED"}]}'}));
    await page.setContent(html);ui=new AoxiangUI(page,{timeoutMs:5000});ui.identity={accountId:'test',merchantCode:'M'};await ui.activate();
    assert.equal(await ui.exportNotice.isVisible(),false);
    await page.evaluate(()=>notice());
    const history=await ui.history();assert.equal(history.data[0].taskId,'known1');assert.equal(ui.noticeDismissals,1);assert.equal(await page.evaluate(()=>historyCalls),1);assert.equal(await page.evaluate(()=>ackCalls),1);assert.equal(await ui.exportNotice.isVisible(),false);
  }finally{await ui?.dispose();await fixture.close();}
});
test('只关闭已知导出提示；其他确认弹窗保留',{skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge();let ui;
  try{const page=fixture.browser.contexts()[0].pages()[0];await page.setContent(html);ui=new AoxiangUI(page,{timeoutMs:800});ui.identity={accountId:'test',merchantCode:'M'};await ui.activate();await page.evaluate(()=>{const n=document.createElement('div');n.className='overlay';n.innerHTML='<div role="dialog">确认修改补货参数？<button>确认</button></div>';document.body.append(n);});await assert.rejects(()=>ui.click(page.locator('#suggestTable button')));assert.equal(ui.noticeDismissals,0);assert.equal(await page.evaluate(()=>historyCalls),0);assert.ok(await page.getByRole('dialog').isVisible());}finally{await ui?.dispose();await fixture.close();}
});
test('真实Edge离线分页：末页刷新先回第一页，查无任务后重试仍可找到原任务',{skip:process.platform!=='win32',timeout:30000},async()=>{
  const fixture=await isolatedEdge();let ui;const requests=[];
  try{
    const page=fixture.browser.contexts()[0].pages()[0];
    await page.route(prefix+'/**',r=>{
      const p=JSON.parse(r.request().postData()).page;requests.push(p);
      return r.fulfill({status:200,headers:{'content-type':'application/json','access-control-allow-origin':'*'},body:JSON.stringify({success:true,total:3,data:[{taskId:'task'+p,taskStatus:'FINISHED'}]})});
    });
    await page.setContent(`<div role="dialog" aria-label="历史记录"><button onclick="load(current)">刷新列表</button><button class="ant-pagination-item-1" onclick="load(1)">1</button><button class="ant-pagination-next ant-pagination-disabled" onclick="load(current+1)">下一页</button></div><script>
      let current=3;function load(p){current=p;document.querySelector('.ant-pagination-item-1').classList.toggle('ant-pagination-item-active',p===1);document.querySelector('.ant-pagination-next').classList.toggle('ant-pagination-disabled',p===3);fetch('${prefix}/ebai/gei/task/list',{method:'POST',body:JSON.stringify({page:p})});}
      </script>`);
    ui=new AoxiangUI(page,{timeoutMs:5000});ui.identity={accountId:'test',merchantCode:'M'};
    assert.equal((await ui.findTask('task1')).taskId,'task1');assert.deepEqual(requests,[1]);
    await assert.rejects(ui.findTask('task-missing'),{code:'TASK_NOT_FOUND'});
    assert.deepEqual(requests,[1,1,2,3]);
    assert.equal((await ui.findTask('task1')).taskId,'task1');assert.deepEqual(requests,[1,1,2,3,1]);
  }finally{await ui?.dispose();await fixture.close();}
});
test('后台标签使用同一已核对Profile；关闭绑定页和作业页后保留原页面',{skip:process.platform!=='win32',timeout:60000},async()=>{
  const fixture=await isolatedEdge(),bridge=new EdgeBridge();
  const server=http.createServer((req,res)=>res.writeHead(200,{'Content-Type':'text/html'}).end('<title>Isolated job fixture</title>'));await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try{
    const original=fixture.browser.contexts()[0].pages()[0];await original.goto('about:blank#original');
    const marker=await original.context().newPage();await marker.goto('edge://version/');const actual=await marker.locator('#profile_path').textContent();assert.equal(path.resolve(actual.trim()).toLowerCase(),path.join(fixture.root,'Default').toLowerCase());
    bridge.browser=fixture.browser;bridge.page=marker;bridge.ownedPages.add(marker);bridge.guard={ownedTargets:new Set(),close:async()=>{}};
    const c=await marker.context().newCDPSession(marker),info=(await c.send('Target.getTargetInfo')).targetInfo;await c.detach();bridge.guard.ownedTargets.add(info.targetId);
    const targetId=await bridge.backgroundJob(`http://127.0.0.1:${server.address().port}/job/fixture`,info,path.join(fixture.root,'Default'));
    assert.ok(bridge.guard.ownedTargets.has(targetId));assert.equal((await bridge.page.context().pages()).length,2);assert.ok(!original.isClosed());assert.ok(marker.isClosed());
    await bridge.close();assert.ok(!original.isClosed());assert.equal(fixture.browser.contexts()[0].pages().length,1);
  }finally{await bridge.close();await fixture.close();server.closeAllConnections();await new Promise(r=>server.close(r));}
});
