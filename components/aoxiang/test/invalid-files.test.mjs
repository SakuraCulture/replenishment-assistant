import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {nativeFixture} from './fixture.mjs';
import {validateWorkbook} from '../src/workbook-validator.mjs';
import {fieldContract} from '../src/config.mjs';
import {downloadKnownTask} from '../src/downloader.mjs';
test('HTML错误页和403分别识别，不把响应当原生Excel',async()=>{
  const server=http.createServer((req,res)=>{if(req.url.includes('denied'))res.writeHead(403).end('Expired');else res.writeHead(200,{'Content-Type':'text/html'}).end('<html>Login required</html>');});await new Promise(r=>server.listen(0,'127.0.0.1',r));
  try{const base=`http://127.0.0.1:${server.address().port}`,directory=await fs.mkdtemp(path.join(os.tmpdir(),'ax-badfile-'));for(const name of ['denied','html']){const object=base+'/task123/'+name+'.xlsx';await assert.rejects(()=>downloadKnownTask({fileUrl:object,object,taskId:'task123',allowedHosts:[new URL(base).host],directory,allowTestHttp:true}),{code:name==='denied'?'RESULT_LINK_EXPIRED_OR_DENIED':'NOT_XLSX'});}}
  finally{server.closeAllConnections();await new Promise(r=>server.close(r));}
});
test('压缩包CRC错误不能通过工作簿验收',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-crc-')),file=await nativeFixture(path.join(root,'native.xlsx')),bytes=await fs.readFile(file),central=bytes.indexOf(Buffer.from('504b0102','hex'));assert.ok(central>0);bytes.writeUInt32LE((bytes.readUInt32LE(central+16)^123)>>>0,central+16);await fs.writeFile(file,bytes);
  const contract=await fieldContract();await assert.rejects(()=>validateWorkbook(file,{contract}),{code:'ZIP_CRC_FAILED'});
});
