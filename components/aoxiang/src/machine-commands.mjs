import Ajv from 'ajv/dist/2020.js';
import schema from '../assets/schemas/machine.command.schema.json' with {type:'json'};
import {canonical,sha256,now,fail} from './common.mjs';
import {validateConfig} from './config.mjs';
import {planReplenishmentRun,insertRunPlan} from './run-plan.mjs';

export const PROTOCOL_VERSION=1;
export const MAX_FRAME_BYTES=1024*1024;
const validate=new Ajv({strict:true,allErrors:true}).compile(schema);
export function validateCommand(value){
  if(!validate(value))fail('COMMAND_SCHEMA','自动化命令格式不符合接口契约',{
    issues:validate.errors.map(e=>({path:e.instancePath,message:e.message}))
  });
  if(Buffer.byteLength(JSON.stringify(value),'utf8')>MAX_FRAME_BYTES)fail('COMMAND_TOO_LARGE','命令超过接口大小限制');
  return value;
}
export function commandFingerprint(value){
  validateCommand(value);
  const {command_id,...content}=value;
  return sha256(canonical(content));
}
export const mappingVersion=config=>sha256(canonical({identity:config.tenantBinding,targets:config.targets}));

const transitions={
  QUEUED:['RUNNING','PAUSED'],
  RUNNING:['RUNNING','PAUSE_REQUESTED','PAUSED','INTERRUPTED','NEEDS_REVIEW','SUCCEEDED','COMPLETED_WITH_ERRORS'],
  PAUSE_REQUESTED:['PAUSE_REQUESTED','PAUSED','INTERRUPTED','NEEDS_REVIEW','SUCCEEDED','COMPLETED_WITH_ERRORS'],
  PAUSED:['QUEUED'],INTERRUPTED:['QUEUED'],
  NEEDS_REVIEW:[],SUCCEEDED:[],COMPLETED_WITH_ERRORS:['QUEUED']
};

export class MachineCommands{
  constructor(state){
    this.state=state;this.db=state.db;
    const tables=['machine_protocol','machine_commands','machine_events'];
    const present=tables.filter(name=>this.db.prepare("SELECT name FROM sqlite_master WHERE type='table' AND name=?").get(name));
    const marker=this.db.prepare("SELECT value FROM metadata WHERE key='machineProtocol'").get();
    if(!marker&&!present.length){
      state.write(()=>{
        this.db.exec(`CREATE TABLE machine_protocol(version INTEGER NOT NULL);
          INSERT INTO machine_protocol VALUES(1);
          CREATE TABLE machine_commands(
          id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,state TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL);
          CREATE TABLE machine_events(
          command_id TEXT NOT NULL,sequence INTEGER NOT NULL,payload TEXT NOT NULL,
          PRIMARY KEY(command_id,sequence),FOREIGN KEY(command_id) REFERENCES machine_commands(id));
          INSERT INTO metadata VALUES('machineProtocol','1');`);
      });
    }else if(marker?.value!=='1'||present.length!==tables.length){
      fail('MACHINE_STATE_CORRUPT','自动化命令表或版本标记缺失，不能重建空表后重复派发');
    }
    const versions=this.db.prepare('SELECT version FROM machine_protocol').all();
    if(versions.length!==1||versions[0].version!==PROTOCOL_VERSION)fail('MACHINE_PROTOCOL_VERSION','自动化命令记录版本不兼容，不能重建');
  }
  get(id){
    const row=this.db.prepare('SELECT payload FROM machine_commands WHERE id=?').get(id);
    return row?JSON.parse(row.payload):null;
  }
  list(){
    return this.db.prepare('SELECT payload FROM machine_commands ORDER BY rowid').all().map(r=>JSON.parse(r.payload));
  }
  events(id,after=0,limit=100){
    if(!Number.isSafeInteger(after)||after<0||!Number.isSafeInteger(limit)||limit<1||limit>1000)fail('EVENT_CURSOR','事件游标或数量无效');
    return this.db.prepare('SELECT payload FROM machine_events WHERE command_id=? AND sequence>? ORDER BY sequence LIMIT ?').all(id,after,limit).map(r=>JSON.parse(r.payload));
  }
  insertEvent(record,kind,detail){
    const event={protocol_version:PROTOCOL_VERSION,command_id:record.id,sequence:record.sequence,
      state:record.state,kind,observed_at:record.updated_at,detail};
    this.db.prepare('INSERT INTO machine_events VALUES(?,?,?)').run(record.id,record.sequence,JSON.stringify(event));
    return event;
  }
  async reserve(command,config){
    validateCommand(command);await validateConfig('portable.config',config);
    if(command.account_binding.account_id!==config.tenantBinding.accountId||command.account_binding.merchant_code!==config.tenantBinding.merchantCode)fail('WRONG_IDENTITY','自动化命令账号与组件绑定不同');
    const fingerprint=commandFingerprint(command);
    return this.state.write(()=>{
      const existing=this.get(command.command_id);
      if(existing){
        if(existing.fingerprint!==fingerprint)fail('COMMAND_ID_CONFLICT','同一命令编号不能更改门店、来源或执行策略');
        return {created:false,record:existing};
      }
      if(command.basis_versions.store_mapping!==mappingVersion(config))fail('STALE_STORE_MAPPING','门店绑定已变化，请刷新目录后重新核对');
      const queued=this.db.prepare("SELECT COUNT(*) AS n FROM machine_commands WHERE state IN ('QUEUED','RUNNING','PAUSE_REQUESTED')").get().n;
      if(queued>=32)fail('QUEUE_FULL','自动化等待队列已满，请等待现有作业完成');
      const plans=command.scope.warehouse_codes.map(warehouseCode=>planReplenishmentRun(config,{
        warehouseCode,mode:command.policy.freshness==='refresh'?'rerun':'normal'
      }));
      const at=now();
      const record={id:command.command_id,fingerprint,request:structuredClone(command),state:'QUEUED',sequence:1,
        created_at:at,updated_at:at,config_snapshot:structuredClone(config),
        items:plans.map((p,index)=>({index,warehouse_code:p.run.targets[0].buyerWarehouseCode,run_id:p.run.id,state:'PENDING',result:null,error:null})),
        result:null,error:null};
      this.db.prepare('INSERT INTO machine_commands VALUES(?,?,?,?,?)').run(record.id,fingerprint,record.state,1,JSON.stringify(record));
      for(const plan of plans)insertRunPlan(this.db,plan);
      this.insertEvent(record,'RESERVED',{items:record.items});
      return {created:true,record};
    });
  }
  advance(id,expectedSequence,next,{kind='STATE',detail={},result,error,items}={}){
    return this.state.write(()=>{
      const current=this.get(id);
      if(!current)fail('COMMAND_NOT_FOUND','未找到自动化作业');
      if(current.sequence!==expectedSequence)fail('STALE_COMMAND','作业状态已更新，请重新读取');
      if(!transitions[current.state]?.includes(next))fail('COMMAND_TRANSITION','该作业状态不能执行此操作');
      const record={...current,state:next,sequence:current.sequence+1,updated_at:now()};
      if(next==='QUEUED'&&this.db.prepare("SELECT COUNT(*) AS n FROM machine_commands WHERE state IN ('QUEUED','RUNNING','PAUSE_REQUESTED')").get().n>=32)fail('QUEUE_FULL','自动化等待队列已满');
      if(items!==undefined){
        if(items.length!==current.items.length||items.some((item,index)=>['index','warehouse_code','run_id'].some(key=>item[key]!==current.items[index][key])))fail('COMMAND_ITEMS_CHANGED','命令的门店顺序及原作业关联不能改变');
        record.items=structuredClone(items);
      }
      if(result!==undefined)record.result=structuredClone(result);
      if(error!==undefined)record.error=structuredClone(error);
      this.db.prepare('UPDATE machine_commands SET state=?,sequence=?,payload=? WHERE id=?').run(next,record.sequence,JSON.stringify(record),id);
      const event=this.insertEvent(record,kind,detail);
      return {record,event};
    });
  }
  recoverInterrupted(){
    // Caller must hold coordinator.lock.sqlite, proving there is no live owner.
    const changed=[];
    for(const record of this.list())if(['QUEUED','RUNNING','PAUSE_REQUESTED'].includes(record.state)){
      changed.push(this.advance(record.id,record.sequence,record.state==='QUEUED'?'PAUSED':'INTERRUPTED',{
        kind:'OWNER_EXITED',detail:{reason:'执行进程已退出；继续时核对原 run 和平台任务，不创建新任务'}
      }).record);
    }
    return changed;
  }
}
