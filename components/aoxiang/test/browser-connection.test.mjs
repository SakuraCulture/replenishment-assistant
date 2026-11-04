import test from 'node:test';
import assert from 'node:assert/strict';
import {EventEmitter} from 'node:events';
import {browserConnectionState,watchBrowserConnection,clientConnectionState} from '../src/browser-connection.mjs';
import {BrowserClient} from '../src/worker-client.mjs';
import {validateRequest} from '../src/machine-protocol.mjs';

test('connection observer follows actual page/socket, without browser commands',()=>{
  let connected=true,closed=false;const seen=[];
  const browser=Object.assign(new EventEmitter(),{isConnected:()=>connected});
  const page=Object.assign(new EventEmitter(),{isClosed:()=>closed});
  const bridge={browser,page};const stop=watchBrowserConnection(bridge,s=>seen.push(s));
  assert.deepEqual(seen,['CONNECTED']);closed=true;page.emit('close');assert.equal(seen.at(-1),'DISCONNECTED');
  closed=false;connected=false;browser.emit('disconnected');assert.equal(seen.at(-1),'DISCONNECTED');
  stop();assert.equal(browser.listenerCount('disconnected'),0);assert.equal(page.listenerCount('close'),0);
  assert.equal(browserConnectionState({}),'DISCONNECTED');
});
test('host does not mistake an old successful connection for live browser state',()=>{
  assert.equal(clientConnectionState(null),'NOT_CONNECTED');
  assert.equal(clientConnectionState({connected:{identity:'old'}}),'UNKNOWN');
  for(const state of ['CONNECTED','CONNECTING','NOT_CONNECTED','DISCONNECTED','UNKNOWN'])assert.equal(clientConnectionState({connectionState:state}),state);
  assert.equal(clientConnectionState({connectionState:'CONNECTED',closed:true}),'DISCONNECTED');
});
test('status protocol is strictly read-only with no extra command fields',()=>{
  assert.equal(validateRequest({protocol_version:1,request_id:'test',action:'session.health'}).action,'session.health');
  assert.throws(()=>validateRequest({protocol_version:1,request_id:'test',action:'session.health',connect:true}));
});
test('real idle worker remains unconnected; closing is observed without probe',async()=>{
  const client=new BrowserClient();assert.equal(clientConnectionState(client),'NOT_CONNECTED');
  client.child.emit('message',{event:'connection',state:'CONNECTED'});assert.equal(clientConnectionState(client),'CONNECTED');
  client.child.emit('message',{event:'connection',state:'DISCONNECTED'});assert.equal(clientConnectionState(client),'DISCONNECTED');
  client.child.emit('message',{event:'connection',state:'bad'});assert.equal(clientConnectionState(client),'UNKNOWN');
  await client.stop();assert.equal(clientConnectionState(client),'DISCONNECTED');assert.equal(client.pending.size,0);
});
