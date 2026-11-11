import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {Readable} from 'node:stream';
import {spawn} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {once} from 'node:events';
import readline from 'node:readline';
import {readFrames,validateRequest,publicRecord} from '../src/machine-protocol.mjs';
import {StateStore} from '../src/state.mjs';
import {MachineCommands,mappingVersion} from '../src/machine-commands.mjs';
import {freshConfig,saveConfig} from '../src/config.mjs';

const entry=fileURLToPath(new URL('../src/machine-entry.mjs',import.meta.url));
const request=(action,extra={})=>({protocol_version:1,request_id:'r1',action,...extra});
const collect=async chunks=>{const values=[];for await(const value of readFrames(Readable.from(chunks)))values.push(value);return values;};
test('NDJSON按字节分帧，支持分段中文、CRLF，多帧且拒绝超长/截断/非法编码',async()=>{
  const b=Buffer.from(JSON.stringify({name:'山东仓'})+'\r\n'+JSON.stringify(request('ping'))+'\n');
  assert.deepEqual(await collect([b.subarray(0,11),b.subarray(11,14),b.subarray(14)]),[{name:'山东仓'},request('ping')]);
  await assert.rejects(()=>collect([Buffer.alloc(1024*1024+1,65)]),{code:'FRAME_TOO_LARGE'});
  await assert.rejects(()=>collect(['{"a":1}']),{code:'FRAME_TRUNCATED'});
  await assert.rejects(()=>collect([Buffer.from([255,10])]),{code:'FRAME_ENCODING'});
  await assert.rejects(()=>collect(['not-json\n']),{code:'FRAME_JSON'});
});
test('机器动作字段严格校验，不接受未知开关和无效游标；内部配置不传入UI',()=>{
  validateRequest(request('collection.continue',{command_id:'job-1'}));
  assert.throws(()=>validateRequest(request('collection.continue',{command_id:'job-1',force:true})),{code:'REQUEST_SCHEMA'});
  validateRequest(request('status',{command_id:'job-1'}));
  for(const bad of [request('unknown'),request('status'),request('ping',{cookie:'x'}),request('events',{command_id:'x',after:-1}),request('list',{limit:101}),{...request('ping'),protocol_version:2}])assert.throws(()=>validateRequest(bad),{code:'REQUEST_SCHEMA'});
  assert.deepEqual(publicRecord({id:'x',config_snapshot:{large:'private'},state:'PAUSED'}),{id:'x',state:'PAUSED'});
});

function launch(root){
  const child=spawn(process.execPath,[entry,'--state-root',root],{windowsHide:true,stdio:['pipe','pipe','pipe']});
  const lines=[],waiters=[];let stderr='';const exit=once(child,'exit');
  child.stderr.on('data',data=>{stderr+=data.toString();});
  readline.createInterface({input:child.stdout}).on('line',line=>{
    let value;try{value=JSON.parse(line);}catch{value={invalid:line};}
    const waiter=waiters.shift();if(waiter)waiter.resolve(value);else lines.push(value);
  });
  const next=async()=>{
    if(lines.length)return lines.shift();
    return new Promise((resolve,reject)=>{
      const waiter={resolve:value=>{clearTimeout(timer);resolve(value);}};
      const timer=setTimeout(()=>{const index=waiters.indexOf(waiter);if(index>=0)waiters.splice(index,1);reject(Error('machine frame timeout: '+stderr));},10000);
      waiters.push(waiter);
    });
  };
  return {child,next,exit,stderr:()=>stderr,send:frame=>child.stdin.write(JSON.stringify(frame)+'\n')};
}
async function seeded(){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-wire-')),state=await StateStore.open(root);
  const identity={accountId:'100',merchantCode:'M',displayName:'测试'},target={buyerWarehouseCode:'W1',displayName:'测试店',operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00Z'};
  const config=freshConfig(identity,[target]);await saveConfig(root,config);
  const commands=new MachineCommands(state);
  await commands.reserve({protocol_version:1,command_id:'known',operation:'replenishment.export',account_binding:{account_id:'100',merchant_code:'M'},scope:{warehouse_codes:['W1']},basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'reuse'}},config);
  commands.advance('known',1,'RUNNING');state.close();return root;
}
test('实际Node子进程使用独立根目录，查询恢复原命令；第二实例互斥，退出后锁释放',async t=>{
  const root=await seeded(),first=launch(root);t.after(()=>{if(first.child.exitCode===null)first.child.kill();});
  const hello=await first.next();assert.equal(hello.type,'ready');assert.equal(hello.recovered[0].state,'INTERRUPTED');
  first.send(request('status',{command_id:'known'}));const status=await first.next();assert.equal(status.ok,true);assert.equal(status.result.state,'INTERRUPTED');
  assert.equal('config_snapshot' in status.result,false);const runId=status.result.items[0].run_id;
  const second=launch(root);const rejected=await second.next();assert.equal(rejected.type,'fatal');assert.equal(rejected.error.code,'APP_BUSY');assert.equal((await second.exit)[0],1);
  first.send(request('shutdown'));assert.equal((await first.next()).result.closing,true);assert.equal((await first.exit)[0],0);
  const third=launch(root);t.after(()=>{if(third.child.exitCode===null)third.child.kill();});
  assert.equal((await third.next()).type,'ready');third.send(request('status',{command_id:'known'}));assert.equal((await third.next()).result.items[0].run_id,runId);
  third.send(request('events',{command_id:'known',after:0}));assert.deepEqual((await third.next()).result.events.map(e=>e.sequence),[1,2,3]);
  third.child.stdin.end();assert.equal((await third.exit)[0],0);
});
test('非法机器帧不当作命令执行，错误不回显原始敏感文本',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-wire-invalid-')),child=launch(root);
  assert.equal((await child.next()).type,'ready');child.child.stdin.write('secret-cookie-not-json\n');
  const error=await child.next();assert.equal(error.type,'fatal');assert.equal(error.error.code,'FRAME_JSON');
  assert.equal(JSON.stringify(error).includes('secret-cookie'),false);assert.equal((await child.exit)[0],1);
});

test('invalid business form returns its exact request error and leaves the same process usable',async t=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-form-invalid-')),child=launch(root);
  t.after(async()=>{if(child.child.exitCode===null){child.send(request('shutdown'));await child.exit;}await fs.rm(root,{recursive:true,force:true});});
  assert.equal((await child.next()).type,'ready');
  const bad=request('binding.start',{purchase_id:'p',binding_request:{order_id:'PO1',created_day:'2026-11-01',reason:''}});
  child.send(bad);const error=await child.next();assert.equal(error.request_id,bad.request_id);assert.equal(error.ok,false);assert.equal(error.error.code,'BINDING_INPUT');
  child.send(request('ping'));const answer=await child.next();assert.equal(answer.ok,true);assert.equal(answer.result.active_binding_id,null);
  child.send(request('purchase.list'));assert.equal((await child.next()).result.total,0);
});

test('configuration exposes only saved browser form fields without connecting or rewriting binding',async t=>{
  const root=await seeded();
  const file=path.join(root,'config/machine.binding.json');
  const binding={edgeExecutable:'C:/saved/edge.exe',edgeVersion:'test',actualProfilePath:'C:/saved/Profile 1',
    profileDirectory:'Profile 1',outputRoot:'C:/saved/downloads',privateSentinel:'must-not-expose'};
  await fs.writeFile(file,JSON.stringify(binding));const before=await fs.readFile(file);
  const child=launch(root);
  t.after(async()=>{if(child.child.exitCode===null){child.send(request('shutdown'));await child.exit;}});
  assert.equal((await child.next()).type,'ready');
  child.send(request('configuration'));const frame=await child.next();assert.equal(frame.ok,true);
  assert.deepEqual(frame.result.browser_binding,{edge_executable:binding.edgeExecutable,edge_version:'test',
    actual_profile_path:binding.actualProfilePath,profile_directory:'Profile 1',output_root:binding.outputRoot});
  assert.equal(JSON.stringify(frame).includes('must-not-expose'),false);
  child.send(request('session.status'));const status=(await child.next()).result;
  assert.equal(status.worker_pid,null);assert.equal(status.connected,false);
  assert.deepEqual(await fs.readFile(file),before);
});
