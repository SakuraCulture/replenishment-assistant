import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import crypto from 'node:crypto';
import {receiveBoundFile} from '../src/product-export.mjs';

const address='https://jstzbstatic.erp321.com/export/abcdef.xlsx?token=synthetic-secret';
const bytes=Buffer.alloc(256,42);bytes.write('PK\x03\x04',0,'binary');
const sha=crypto.createHash('sha256').update(bytes).digest('hex');
async function fixture(t,options={}){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'catalog-response-release-'));
  t.after(()=>fs.rm(root,{recursive:true,force:true}));
  const state={requests:[],bodyCalls:0,disposeCalls:0,contextClosed:0,progress:[]};
  const response={ok:()=>options.status===undefined||options.status===200,status:()=>options.status??200,
    body:async()=>{state.bodyCalls++;if(options.bodyError)throw options.bodyError;options.onBody?.();return options.bytes??bytes;},
    dispose:async()=>{state.disposeCalls++;if(options.disposeError!==undefined)throw options.disposeError;},
  };
  const request={get:async(...args)=>{state.requests.push(args);if(options.getError)throw options.getError;return response;},dispose:async()=>{state.contextClosed++;throw Error('Shared context must stay open');}};
  const page={context:()=>({request,close:async()=>{state.contextClosed++;throw Error('Browser context must stay open');}})};
  const args={recordId:'123',expectedRows:9,expectedPath:'/export/abcdef.xlsx',output:root,
    onProgress:(...args)=>state.progress.push(args)};
  const invoke=(change={})=>receiveBoundFile(page,address,{...args,...change});
  return {root,state,args,invoke,response};
}

test('successful download keeps exact immutable file and receipt and releases one response',async t=>{
  const {root,state,invoke}=await fixture(t);const receipt=await invoke();
  const file=path.join(root,'聚水潭商品资料_123_'+sha.slice(0,12)+'.xlsx');
  assert.deepEqual(receipt,{file,recordId:'123',expectedRows:9,bytes:bytes.length,sha256:sha,
    nativePath:'https://jstzbstatic.erp321.com/export/abcdef.xlsx',sourceBytesPreserved:true});
  assert.deepEqual(await fs.readFile(file),bytes);
  assert.deepEqual(state.requests,[[address,{timeout:60000,maxRedirects:0}]]);
  assert.deepEqual(state.progress,[['DOWNLOADING','正在接收原任务文件']]);
  assert.equal(state.disposeCalls,1);assert.equal(state.contextClosed,0);
});

test('same existing file is verified, reused and each response released; context remains reusable',async t=>{
  const {state,invoke}=await fixture(t);const first=await invoke(),second=await invoke();
  assert.deepEqual(second,first);assert.equal(state.requests.length,2);assert.equal(state.disposeCalls,2);assert.equal(state.contextClosed,0);
});

test('conflicting existing file still rejects without replacing bytes and releases response',async t=>{
  const {root,state,invoke}=await fixture(t);const file=path.join(root,'聚水潭商品资料_123_'+sha.slice(0,12)+'.xlsx');
  await fs.writeFile(file,'original conflict');
  await assert.rejects(invoke(),/已有文件内容冲突/);assert.equal((await fs.readFile(file)).toString(),'original conflict');assert.equal(state.disposeCalls,1);
});

for(const status of [401,403,404,500])test('HTTP '+status+' keeps original rejection and releases response',async t=>{
  const {state,invoke}=await fixture(t,{status});
  await assert.rejects(invoke(),new RegExp('原生文件接收 HTTP '+status));
  assert.equal(state.bodyCalls,0);assert.equal(state.disposeCalls,1);assert.equal(state.contextClosed,0);
});

test('body failure identity survives cleanup and no final file is created',async t=>{
  const bodyError=new Error('synthetic body failure');const {root,state,invoke}=await fixture(t,{bodyError});
  await assert.rejects(invoke(),e=>e===bodyError);assert.equal(state.disposeCalls,1);assert.deepEqual(await fs.readdir(root),[]);
});

test('cancellation after receiving bytes releases response without writing the file',async t=>{
  const controller=new AbortController();const {root,state,invoke}=await fixture(t,{onBody:()=>controller.abort()});
  await assert.rejects(invoke({signal:controller.signal}),{code:'CANCELLED'});
  assert.equal(state.disposeCalls,1);assert.deepEqual(await fs.readdir(root),[]);
});

test('cancel before request and mismatched identity do not request or dispose another response',async t=>{
  const {state,invoke}=await fixture(t);const controller=new AbortController();controller.abort();
  await assert.rejects(invoke({signal:controller.signal}),{code:'CANCELLED'});
  await assert.rejects(invoke({expectedPath:'/export/other.xlsx'}),{code:'FILE_IDENTITY'});
  assert.equal(state.requests.length,0);assert.equal(state.disposeCalls,0);
});

for(const body of [Buffer.alloc(99),Buffer.alloc(101),Buffer.alloc(60*1024*1024+1)])
  test('unsupported XLSX length '+body.length+' keeps validation failure and releases response',async t=>{
    const {root,state,invoke}=await fixture(t,{bytes:body});
    await assert.rejects(invoke(),/下载不是受支持的XLSX/);assert.equal(state.disposeCalls,1);assert.deepEqual(await fs.readdir(root),[]);
  });

test('directory failure keeps original IO error and releases response',async t=>{
  const {root,state,invoke}=await fixture(t);const file=path.join(root,'not-directory');await fs.writeFile(file,'not-directory');
  await assert.rejects(invoke({output:path.join(file,'child')}),e=>['ENOTDIR','ENOENT'].includes(e.code));assert.equal(state.disposeCalls,1);
});

test('file write failure keeps original error identity and releases response',async t=>{
  const {state,invoke}=await fixture(t);const original=fs.writeFile,error=Object.assign(new Error('synthetic disk denied'),{code:'EACCES'});
  fs.writeFile=async(...args)=>{if(path.basename(String(args[0])).startsWith('聚水潭商品资料_'))throw error;return original(...args);};
  try{await assert.rejects(invoke(),e=>e===error);}finally{fs.writeFile=original;}
  assert.equal(state.disposeCalls,1);
});

test('request failure is unchanged and cannot dispose a response never obtained',async t=>{
  const error=new Error('synthetic connection closed');const {state,invoke}=await fixture(t,{getError:error});
  await assert.rejects(invoke(),e=>e===error);assert.equal(state.disposeCalls,0);
});

test('cleanup failure is sanitized diagnostic only after successful immutable save',async t=>{
  const {state,invoke}=await fixture(t,{disposeError:new Error('closed '+address)});const original=console.error,logs=[];
  console.error=(...args)=>logs.push(args.join(' '));let receipt;
  try{receipt=await invoke();}finally{console.error=original;}
  assert.equal(receipt.sha256,sha);assert.equal(state.disposeCalls,1);assert.equal(logs.length,1);
  assert.match(logs[0],/下载响应清理未完成/);assert.ok(!logs[0].includes('synthetic-secret'));
});

test('cleanup and diagnostic failures cannot mask original download error',async t=>{
  const error=new Error('original body failure');const {state,invoke}=await fixture(t,{bodyError:error,disposeError:new Error('cleanup closed')});
  const original=console.error;console.error=()=>{throw Error('diagnostic closed');};
  try{await assert.rejects(invoke(),e=>e===error);}finally{console.error=original;}
  assert.equal(state.disposeCalls,1);
});
