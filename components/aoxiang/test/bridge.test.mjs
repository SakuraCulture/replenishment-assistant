import test from 'node:test';
import assert from 'node:assert/strict';
import {once} from 'node:events';
import {WebSocket,WebSocketServer} from 'ws';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import net from 'node:net';
import {createGuard} from '../src/edge-bridge.mjs';
import {discoverEndpoint} from '../src/environment.mjs';

test('控制桥保留文本协议，拦截关闭用户浏览器和改下载行为',async()=>{
  const upstream=new WebSocketServer({host:'127.0.0.1',port:0});await once(upstream,'listening');
  const observed=[];
  upstream.on('connection',s=>s.on('message',(data,binary)=>{observed.push({request:JSON.parse(String(data)),binary});s.send(JSON.stringify({id:JSON.parse(String(data)).id,result:{ok:true}}));}));
  const guard=await createGuard(`ws://127.0.0.1:${upstream.address().port}`);
  const client=new WebSocket(guard.url);await once(client,'open');
  try{
    const request=async(id,method,params)=>{const got=once(client,'message');client.send(JSON.stringify({id,method,params}));const [raw,binary]=await got;assert.equal(binary,false);return JSON.parse(String(raw));};
    assert.equal((await request(1,'Browser.getVersion')).result.ok,true);
    assert.equal(observed[0].binary,false);
    assert.ok((await request(2,'Browser.close')).error);
    assert.ok((await request(3,'Browser.setDownloadBehavior',{behavior:'allowAndName'})).error);
    assert.ok((await request(4,'Target.closeTarget',{targetId:'user-tab'})).error);
    guard.ownedTargets.add('own-tab');assert.equal((await request(5,'Target.closeTarget',{targetId:'own-tab'})).result.ok,true);
    assert.deepEqual(observed.map(x=>x.request.id),[1,5]);
  }finally{client.terminate();await guard.close();for(const s of upstream.clients)s.terminate();await new Promise(r=>upstream.close(r));}
});
test('发现文件存在但端口无服务不能声称连接可用',async()=>{
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-port-'));
  await fs.writeFile(path.join(root,'DevToolsActivePort'),'0\n/devtools/browser/not-a-real-id\n');
  await assert.rejects(()=>discoverEndpoint(root),{code:'INVALID_ENDPOINT'});
  const server=net.createServer();await new Promise(r=>server.listen(0,'127.0.0.1',r));const port=server.address().port;await new Promise(r=>server.close(r));
  await fs.writeFile(path.join(root,'DevToolsActivePort'),port+'\n/devtools/browser/stale-id\n');
  await assert.rejects(()=>discoverEndpoint(root),{code:'WAIT_BROWSER_SETUP'});
});
