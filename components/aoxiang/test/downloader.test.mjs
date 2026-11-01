import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {downloadKnownTask,validateResultAddress} from '../src/downloader.mjs';
import {sha256} from '../src/common.mjs';

test('已知任务传输的对象身份、字节和哈希',async()=>{
  const payload=Buffer.concat([Buffer.from('504b0304','hex'),Buffer.alloc(256,42)]);
  const server=http.createServer((req,res)=>{res.writeHead(200,{'Content-Length':payload.length});res.end(payload);});
  await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try{
    const base=`http://127.0.0.1:${server.address().port}`;const object=base+'/exports/task123/file.xlsx';
    const x=await downloadKnownTask({fileUrl:object+'?signature=not-a-real-secret',taskId:'task123',object,allowedHosts:[new URL(base).host],directory:await fs.mkdtemp(path.join(os.tmpdir(),'ax-dl-')),allowTestHttp:true});
    assert.equal(x.sha256,sha256(payload));assert.equal(x.bytes,payload.length);assert.equal(await fs.readFile(x.file).then(sha256),sha256(payload));assert.ok(!JSON.stringify(x).includes('signature'));
  }finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
});
test('下载器拒绝替换任务、任意主机和明文地址',()=>{
  const args={taskId:'task123',object:'https://allowed.example/exports/task123/file.xlsx',allowedHosts:['allowed.example']};
  assert.throws(()=>validateResultAddress('https://evil.example/exports/task123/file.xlsx',args),{code:'UNTRUSTED_DOWNLOAD_HOST'});
  assert.throws(()=>validateResultAddress('https://allowed.example/exports/task456/file.xlsx',args),{code:'RESULT_OBJECT_CHANGED'});
  assert.throws(()=>validateResultAddress('http://allowed.example/exports/task123/file.xlsx',args),{code:'UNTRUSTED_DOWNLOAD_HOST'});
});
