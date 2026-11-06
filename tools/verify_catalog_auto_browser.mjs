// Real installed Edge, isolated fresh profiles and local pages only. No platform/login.
import fs from 'node:fs/promises';
import path from 'node:path';
import assert from 'node:assert/strict';
import {fileURLToPath,pathToFileURL} from 'node:url';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const components=process.argv[3]?path.resolve(process.argv[3]):path.join(root,'components');
const load=relative=>import(pathToFileURL(path.join(components,relative)).href);
const {AccountSessions}=await load('jst_catalog/src/accounts.mjs');
const {discoverEndpoint}=await load('aoxiang/src/environment.mjs');
const {WebSocket}=await load('aoxiang/node_modules/ws/wrapper.mjs');
const out=path.resolve(process.argv[2]||'');
if(!out.startsWith(path.join(root,'qa')+path.sep))throw Error('Output must be a new directory below project qa');
await fs.mkdir(out,{recursive:false});
const sessions=new AccountSessions({stateRoot:out,bridgeRoot:path.join(components,'aoxiang'),lockRoot:path.join(out,'locks')});
const report={passed:false,scope:'installed Edge; fresh QA profiles; local pages only; no platform requests',components,warehouses:[]};
const bindings=[];
try{
  for(const warehouse of ['山东仓','河北仓']){
    bindings.push({userDataRoot:path.join(out,'browsers',warehouse==='山东仓'?'sd':'hb','User Data')});
    const start=performance.now(),binding=await sessions.automaticBinding(warehouse,bindings.slice(0,-1).map(binding=>({binding})));
    bindings[bindings.length-1]=binding;const bridge=await sessions.connect({binding});
    assert(bridge.browser.isConnected());assert(bridge.page.url().startsWith('http://127.0.0.1:')||bridge.page.url().startsWith('edge://'));
    assert.equal((await sessions.connect({binding})),bridge);
    report.warehouses.push({warehouse,seconds:(performance.now()-start)/1000,facts:bridge.facts,connection_reused:true});
  }
  assert.notEqual(await discoverEndpoint(bindings[0].userDataRoot),await discoverEndpoint(bindings[1].userDataRoot));
  report.passed=true;
}catch(e){report.error=e.stack;}
finally{
  await sessions.close();report.closed=[];
  // These profiles were created by this test, contain no user tabs and have never visited a platform.
  for(const binding of bindings){
    const endpoint=await discoverEndpoint(binding.userDataRoot).catch(()=>null);
    if(endpoint)await new Promise((resolve,reject)=>{
      const socket=new WebSocket(endpoint),timer=setTimeout(()=>{socket.terminate();reject(Error('QA browser did not close'));},10000);
      socket.once('open',()=>socket.send(JSON.stringify({id:1,method:'Browser.close'})));
      socket.once('close',()=>{clearTimeout(timer);resolve();});socket.once('error',reject);
    }).catch(e=>{report.passed=false;report.cleanup_error=e.message;});
    let closed=false;for(let n=0;n<30;n++){if(!await discoverEndpoint(binding.userDataRoot).catch(()=>null)){closed=true;break;}await new Promise(r=>setTimeout(r,100));}
    report.closed.push(closed);if(!closed)report.passed=false;
  }
  await fs.writeFile(path.join(out,'report.json'),JSON.stringify(report,null,2));
}
console.log(JSON.stringify(report));process.exitCode=report.passed?0:1;
