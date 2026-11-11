// Real browser navigation, every request fulfilled/aborted locally. No user profile or platform calls.
import test from 'node:test';
import assert from 'node:assert/strict';
import {chromium} from 'playwright-core';
import {AoxiangUI} from '../src/aoxiang-ui.mjs';
import {ModuleSession} from '../src/module-session.mjs';
const body={status:true,success:true,data:{account:{accountId:'100'},user:{accountId:'100',userId:'1',merchantCode:'SYNTHETIC'}}};
const html='<input id="buyerWarehouseCodeList"><script>fetch("/account/session/getLoginInfo").then(r=>r.json()).then(j=>window.loaded=true).catch(()=>{})</script>';
async function fixture(t,{noRequest=false,abortFirst=false,missingControl=false,reply=body}={}){
  const browser=await chromium.launch({headless:true,channel:'msedge'});t.after(()=>browser.close());
  const page=await browser.newPage();let requests=0;const unexpected=[],phases=[];
  await page.route('**/*',route=>{
    const u=new URL(route.request().url());
    if(u.pathname==='/account/session/getLoginInfo'){
      requests++;return abortFirst&&requests===1?route.abort():route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(reply)});
    }
    if(u.origin==='https://saas-retail.ele.me'&&u.pathname==='/')return route.fulfill({status:200,contentType:'text/html',body:noRequest?'<input id="buyerWarehouseCodeList">':missingControl?html.replace('<input id="buyerWarehouseCodeList">',''):html});
    unexpected.push(u.pathname);return route.abort();
  });
  const session=new ModuleSession({page,adapters:{D13:AoxiangUI},timeoutMs:1200,onPhase:p=>phases.push(p)});
  t.after(()=>session.dispose());return {page,session,phases,requests:()=>requests,unexpected};
}
test('已登录SPA只切hash时主动刷新并取得新身份，连续核验均不导出',async t=>{
  const f=await fixture(t);await f.page.goto('https://saas-retail.ele.me/#/home');await f.page.waitForFunction(()=>window.loaded);
  assert.equal(f.requests(),1);await f.session.open('D13',null,{force:true,observeCurrent:true});
  assert.equal(f.requests(),2);assert.equal(f.session.identity.merchantCode,'SYNTHETIC');assert.ok(f.phases.some(p=>p.includes('刷新页面')));
  await f.session.open('D13',null,{force:true,observeCurrent:true});assert.equal(f.requests(),3);assert.deepEqual(f.unexpected,[]);
});
test('新页面正常核验一次；临时身份响应失败仅恢复一次只读查询',async t=>{
  for(const abortFirst of [false,true]){
    const f=await fixture(t,{abortFirst});await f.session.open('D13',null,{force:true,observeCurrent:true});
    assert.equal(f.requests(),abortFirst?2:1);assert.equal(f.session.identity.accountId,'100');
    assert.equal(f.phases.filter(p=>p.includes('核验一次')).length,abortFirst?1:0);assert.deepEqual(f.unexpected,[]);
  }
});
test('持续未发身份请求有界失败，不沿用旧成功身份、不提交导出',async t=>{
  const f=await fixture(t,{noRequest:true});f.session.identity={accountId:'cached',merchantCode:'SYNTHETIC'};
  await assert.rejects(()=>f.session.open('D13',null,{force:true,observeCurrent:true}),e=>e.code==='IDENTITY_RESPONSE_TIMEOUT'&&e.details.phase==='等待身份请求');
  assert.equal(f.session.ready,false);assert.equal(f.session.ui.identity,null);assert.equal(f.requests(),0);
  assert.equal(f.phases.filter(p=>p.includes('核验一次')).length,1);assert.deepEqual(f.unexpected,[]);
});
test('确认未登录或身份不符不重试，也不拿缓存冒充成功',async t=>{
  const loggedOut=await fixture(t,{reply:{status:false,success:false}});
  await assert.rejects(()=>loggedOut.session.open('D13',null,{observeCurrent:true}),{code:'WAIT_LOGIN'});assert.equal(loggedOut.requests(),1);
  const wrong=await fixture(t);await assert.rejects(()=>wrong.session.open('D13',{accountId:'other',merchantCode:'OTHER'}),{code:'WRONG_IDENTITY'});
  assert.equal(wrong.requests(),1);assert.deepEqual(wrong.unexpected,[]);
});
test('身份读取成功而业务页未就绪时准确报告加载环节，仍不导出',async t=>{
  const f=await fixture(t,{missingControl:true});
  await assert.rejects(()=>f.session.open('D13',null,{observeCurrent:true}),e=>e.code==='IDENTITY_CHECK_FAILED'&&e.details.phase==='补货建议页就绪'&&e.message.includes('已读取登录身份'));
  assert.equal(f.requests(),2);assert.equal(f.session.ready,false);assert.deepEqual(f.unexpected,[]);
});
