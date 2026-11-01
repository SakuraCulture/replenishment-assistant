import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {StateStore} from '../src/state.mjs';
import {freshConfig} from '../src/config.mjs';
import {MachineCommands,validateCommand,commandFingerprint,mappingVersion} from '../src/machine-commands.mjs';

const identity={accountId:'001',merchantCode:'M',displayName:'测试'};
const targets=['W2','W1'].map(code=>({buyerWarehouseCode:code,displayName:code,operatingStores:[],mappingSource:'fixture',mappingVerifiedAt:'2026-11-01T00:00:00.000Z'}));
const config=freshConfig(identity,targets);
function command(id='job-1'){
  return {protocol_version:1,command_id:id,operation:'replenishment.export',
    account_binding:{account_id:'001',merchant_code:'M'},scope:{warehouse_codes:['W2','W1']},
    basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'reuse'}};
}
async function context(t){
  const root=await fs.mkdtemp(path.join(os.tmpdir(),'ax-machine-'));
  const state=await StateStore.open(root);t.after(()=>state.close());
  return {root,state,commands:new MachineCommands(state)};
}

test('机器契约拒绝数字编码、未知字段、重复门店、超量和未实现采购操作',()=>{
  validateCommand(command());
  const cases=[{...command(),cookie:'secret'},{...command(),operation:'purchase.create'},
    {...command(),account_binding:{account_id:1,merchant_code:'M'}},
    {...command(),scope:{warehouse_codes:['W2','W2']}},
    {...command(),scope:{warehouse_codes:[]}},
    {...command(),scope:{warehouse_codes:Array.from({length:257},(_,i)=>String(i))}},
    {...command(),protocol_version:2}];
  for(const bad of cases)assert.throws(()=>validateCommand(bad),{code:'COMMAND_SCHEMA'});
  assert.equal(commandFingerprint(command()),commandFingerprint(command('another-id')));
  assert.notEqual(commandFingerprint(command()),commandFingerprint({...command(),scope:{warehouse_codes:['W1','W2']}}));
});

test('命令、逐店run及有序事件原子保存，重复派发只返回原绑定',async t=>{
  const {state,commands}=await context(t);
  const first=await commands.reserve(command(),config),again=await commands.reserve(command(),config);
  assert.equal(first.created,true);assert.equal(again.created,false);assert.deepEqual(again.record,first.record);
  assert.deepEqual(first.record.items.map(i=>i.warehouse_code),['W2','W1']);
  assert.equal(state.rows('runs').length,2);assert.equal(state.rows('items').length,2);
  for(const item of first.record.items){assert.equal(state.getRun(item.run_id).targets[0].buyerWarehouseCode,item.warehouse_code);assert.equal(state.getItems(item.run_id).length,1);}
  assert.deepEqual(commands.events(first.record.id).map(e=>e.sequence),[1]);
  const changed={...command(),policy:{freshness:'refresh'}};
  await assert.rejects(()=>commands.reserve(changed,config),{code:'COMMAND_ID_CONFLICT'});
  assert.equal(state.rows('runs').length,2);
});

test('事务中途失败整体回滚，未留下只有命令没有run的恢复断点',async t=>{
  const {state,commands}=await context(t);
  state.db.exec("CREATE TRIGGER fail_item BEFORE INSERT ON items BEGIN SELECT RAISE(ABORT,'injected failure'); END;");
  await assert.rejects(()=>commands.reserve(command(),config),/injected failure/);
  assert.equal(commands.get('job-1'),null);assert.equal(state.rows('runs').length,0);assert.equal(state.rows('items').length,0);
  assert.equal(commands.events('job-1').length,0);
  state.db.exec('DROP TRIGGER fail_item');assert.equal((await commands.reserve(command(),config)).created,true);
});

test('账号错误、未配置目标均不留下命令或部分门店',async t=>{
  const {state,commands}=await context(t);
  await assert.rejects(()=>commands.reserve({...command(),account_binding:{account_id:'other',merchant_code:'M'}},config),{code:'WRONG_IDENTITY'});
  await assert.rejects(()=>commands.reserve({...command(),scope:{warehouse_codes:['W2','missing']}},config),{code:'TARGET_NOT_CONFIGURED'});
  assert.equal(commands.list().length,0);assert.equal(state.rows('runs').length,0);
});

test('状态序号拒绝过期更新，终态和待人工核对不能自动重启',async t=>{
  const {commands}=await context(t);await commands.reserve(command(),config);
  commands.advance('job-1',1,'RUNNING');
  assert.throws(()=>commands.advance('job-1',1,'PAUSED'),{code:'STALE_COMMAND'});
  const done=commands.advance('job-1',2,'SUCCEEDED',{result:{files:[]}});
  assert.equal(done.record.sequence,3);
  assert.throws(()=>commands.advance('job-1',3,'QUEUED'),{code:'COMMAND_TRANSITION'});
  await commands.reserve(command('unknown'),config);commands.advance('unknown',1,'RUNNING');commands.advance('unknown',2,'NEEDS_REVIEW');
  assert.throws(()=>commands.advance('unknown',3,'QUEUED'),{code:'COMMAND_TRANSITION'});
  assert.deepEqual(commands.events('job-1',1).map(e=>e.sequence),[2,3]);
});

test('重开数据库保留原命令及run，重启只标中断不自动执行',async t=>{
  const {root,state,commands}=await context(t);const before=(await commands.reserve(command(),config)).record;
  commands.advance('job-1',1,'RUNNING');commands.advance('job-1',2,'PAUSE_REQUESTED');
  const second=await StateStore.open(root);
  try{
    const reopened=new MachineCommands(second);assert.equal(reopened.recoverInterrupted().length,1);
    const interrupted=reopened.get('job-1');assert.equal(interrupted.state,'INTERRUPTED');assert.deepEqual(interrupted.items,before.items);
    reopened.advance('job-1',interrupted.sequence,'QUEUED');
    const again=await reopened.reserve(command(),config);assert.equal(again.created,false);assert.deepEqual(again.record.items,before.items);
    assert.equal(state.rows('runs').length,2);assert.equal(reopened.recoverInterrupted().length,1);
    assert.equal(reopened.get('job-1').state,'PAUSED');assert.equal(reopened.recoverInterrupted().length,0);
  }finally{second.close();}
});

test('命令表或版本标记丢失均拒绝重建，防止丢失派发记录后重提',async t=>{
  const {state,commands}=await context(t);await commands.reserve(command(),config);
  state.db.exec('DROP TABLE machine_events');
  assert.throws(()=>new MachineCommands(state),{code:'MACHINE_STATE_CORRUPT'});
  assert.equal(state.db.prepare("SELECT name FROM sqlite_master WHERE name='machine_events'").get(),undefined);
  assert.equal(commands.get('job-1').items.length,2);
});

test('冻结原参数和目录，调用方后续改动不改变命令依据',async t=>{
  const {commands}=await context(t),input=structuredClone(config);
  const before=(await commands.reserve(command(),input)).record;
  input.targets[0].displayName='改名';input.resourceLimits.pollIntervalMs+=1000;
  assert.deepEqual(commands.get('job-1').config_snapshot,before.config_snapshot);
  assert.equal((await commands.reserve(command(),input)).created,false);
  await assert.rejects(()=>commands.reserve(command('new-command'),input),{code:'STALE_STORE_MAPPING'});
  commands.advance('job-1',1,'RUNNING');
  const swapped=[...before.items].reverse();
  assert.throws(()=>commands.advance('job-1',2,'RUNNING',{items:swapped}),{code:'COMMAND_ITEMS_CHANGED'});
});

test('等待任务有上限，重复命令在队列满时仍能查询原任务',async t=>{
  const {commands}=await context(t);
  for(let i=0;i<32;i++)await commands.reserve(command('job-'+i),config);
  await assert.rejects(()=>commands.reserve(command('job-overflow'),config),{code:'QUEUE_FULL'});
  assert.equal((await commands.reserve(command('job-0'),config)).created,false);
  assert.equal(commands.list().length,32);
});
