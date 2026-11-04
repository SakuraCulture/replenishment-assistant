// Actual Edge/CDP on disposable profiles and local pages. No platform or user login.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const executablePath=process.argv[2],out=path.resolve(process.argv[3]||'');
if(!executablePath||!process.argv[3])throw Error('Pass Edge executable and a NEW isolated QA directory. Optional fourth argument: components directory.');
const components=path.resolve(process.argv[4]||path.join(root,'components'));
const require=createRequire(path.join(components,'aoxiang/package.json'));
const {chromium}=require('playwright-core');
const {AccountSessions}=await import(pathToFileURL(path.join(components,'jst_catalog/src/accounts.mjs')));
const {watchBrowserConnection}=await import(pathToFileURL(path.join(components,'aoxiang/src/browser-connection.mjs')));
await fs.mkdir(out);const contexts=[],accounts=[],checks=[],binds=[];
const sessions=new AccountSessions({bridgeRoot:path.join(components,'aoxiang'),stateRoot:out,lockRoot:path.join(out,'locks')});
let report;
try{
  for(const [i,warehouse] of ['山东仓','河北仓'].entries()){
    const userDataRoot=path.join(out,'profile-'+i);
    const options={executablePath,headless:false,args:['--start-minimized','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0']};
    // Flush profile metadata before using production identity discovery.
    const initial=await chromium.launchPersistentContext(userDataRoot,options);await initial.close();
    const context=await chromium.launchPersistentContext(userDataRoot,options);contexts.push(context);
    await context.route('**/*',route=>new URL(route.request().url()).hostname==='127.0.0.1'?route.continue():route.abort());
    await context.pages()[0].setContent('<title>untouched-user-page-'+i+'</title>');
    const binding=await sessions.binding({profilePath:path.join(userDataRoot,'Default'),edgeExecutable:executablePath},accounts);
    accounts.push({warehouse,binding});
  }
  await sessions.initialize();const Native=sessions.components.EdgeBridge;
  sessions.components.EdgeBridge=class extends Native{async bind(...args){binds.push(args[0].actualProfilePath);return super.bind(...args);}};
  const first=await sessions.connect(accounts[0]),second=await sessions.connect(accounts[1]);
  const observed=[];const stopWatching=watchBrowserConnection(first,state=>observed.push(state));
  assert.deepEqual(observed,['CONNECTED']);
  for(let n=0;n<3;n++){
    assert.equal(await sessions.connect(accounts[0]),first);assert.equal(await sessions.connect(accounts[1]),second);
    assert(first.facts.profilePathVerified);assert(await first.page.title());
    assert.equal(sessions.status(accounts).filter(x=>x.state==='CONNECTED').length,2);
  }
  assert.equal(binds.length,2);checks.push('two real Edge profiles; three repeated operations per warehouse; only two initial CDP bindings');
  await sessions.disconnect(accounts[0].binding);assert.equal(sessions.status(accounts)[0].state,'DISCONNECTED');
  assert.equal(observed.at(-1),'DISCONNECTED');stopWatching();
  checks.push('AX connection observer receives actual Edge page/socket close events; no platform query');
  assert.equal(await sessions.connect(accounts[1]),second);assert.equal(binds.length,2);
  const replacement=await sessions.connect(accounts[0]);assert.notEqual(replacement,first);assert.equal(binds.length,3);
  checks.push('explicit SD reconnect opens exactly one new connection; HB connection retained');
  await replacement.page.close();assert.equal(sessions.status(accounts)[0].state,'DISCONNECTED');
  const recovered=await sessions.connect(accounts[0]);assert.notEqual(recovered,replacement);assert.equal(await sessions.connect(accounts[1]),second);assert.equal(binds.length,4);
  checks.push('closed SD job page detected; only SD recovers');
  await sessions.close();
  for(const [i,context] of contexts.entries()){
    assert.equal(await context.pages()[0].title(),'untouched-user-page-'+i);
    assert.equal(context.pages().length,1);
  }
  checks.push('application shutdown removes owned tabs and leaves both preexisting browser pages intact');
  report={passed:true,checks,connection_count:binds.length,initial_connections:2,reuse_rounds_per_warehouse:3,components,edge_version:recovered.facts?.edgeVersion,
    scope:'actual isolated Edge (start-minimized) + production AccountSessions and EdgeBridge; no production login/platform/export; does not test visible permission prompt or overnight stability'};
}catch(e){report={passed:false,checks,error:String(e.stack),code:e.code,details:e.details};}
finally{
  await sessions.close();for(const context of contexts)await context.close();
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2)+'\n');
}
console.log(JSON.stringify(report,null,2));if(!report.passed)process.exitCode=1;
