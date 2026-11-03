import test from 'node:test';
import assert from 'node:assert/strict';
import {activateControl} from '../src/product-export.mjs';

test('ambiguous controls are never activated',async()=>{
  let clicks=0;
  await assert.rejects(activateControl({count:async()=>2,dispatchEvent:async()=>clicks++}),{code:'CONTROL_AMBIGUOUS'});
  assert.equal(clicks,0);
});
test('hidden or disabled controls are not dispatched',async()=>{
  for(const visible of [false,true]){
    let clicks=0;
    await assert.rejects(activateControl({count:async()=>1,isVisible:async()=>visible,evaluate:async()=>true,dispatchEvent:async()=>clicks++},{timeout:0}),{code:'CONTROL_NOT_READY'});
    assert.equal(clicks,0);
  }
});
test('dispatch errors are not retried as another possible export',async()=>{
  let clicks=0;
  await assert.rejects(activateControl({count:async()=>1,isVisible:async()=>true,evaluate:async()=>false,
    dispatchEvent:async()=>{clicks++;throw Error('connection lost after dispatch');}}),/connection lost/);
  assert.equal(clicks,1);
});
