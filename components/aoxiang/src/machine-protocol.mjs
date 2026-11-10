import {once} from 'node:events';
import {TextDecoder} from 'node:util';
import {fail} from './common.mjs';
import {MAX_FRAME_BYTES} from './machine-commands.mjs';
import {bindingRequest} from './purchase-create-contract.mjs';

const idPattern=/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
// A valid envelope can receive a correlated validation error without dispatching its body.
export const correlationId=value=>value?.protocol_version===1&&typeof value.request_id==='string'&&idPattern.test(value.request_id)?value.request_id:null;
const fields={ping:[],'session.release':[],'session.status':[],'session.health':[],configuration:[],list:['offset','limit'],reserve:['command'],status:['command_id'],
  'collection.preview':['command_id','operation'],'collection.apply':['command_id','operation','token'],'collection.continue':['command_id'],'orders.retire':['operation_id','oids'],
  events:['command_id','after','limit'],pause:['command_id'],resume:['command_id'],retry:['command_id','indices','sequence'],shutdown:[],
  discover:[],'setup.check_login':[],'setup.verify_saved':['codes'],'setup.start':['candidate','output_root','targets'],'setup.status':['setup_id'],
  'setup.cancel':['setup_id'],'setup.commit':['setup_id'],
  'template.start':[],'template.status':['template_id'],'template.cancel':['template_id'],
  'purchase.reserve':['package_file','package_sha256'],'purchase.start':['purchase_id'],'purchase.submit':['purchase_id'],
  'purchase.rebuild':['purchase_id','sequence','package_file','package_sha256','operation_id','reason','acknowledge_uncertainty'],
  'purchase.status':['purchase_id'],'purchase.pause':['purchase_id'],'purchase.cancel':['purchase_id'],'purchase.list':['offset','limit'],
  'binding.start':['purchase_id','binding_request'],'binding.status':['purchase_id'],'binding.pause':['purchase_id'],
  'binding.confirm':['purchase_id','proposal_id','sequence','manifest_sha256'],
  'receipt.start':['purchase_id'],'receipt.status':['purchase_id'],'receipt.pause':['purchase_id'],
  'receipt.synced':['purchase_id','manifest_sha256','receipt'],'receipt.host_void_conflict':['purchase_id','reason'],'receipt.list':['offset','limit']};
export function validateRequest(value){
  if(!value||typeof value!=='object'||Array.isArray(value)||value.protocol_version!==1||
    typeof value.request_id!=='string'||!idPattern.test(value.request_id)||!Object.hasOwn(fields,value.action))fail('REQUEST_SCHEMA','机器请求版本、编号或动作无效');
  const allowed=['protocol_version','request_id','action',...fields[value.action]];
  if(['collection.preview','collection.apply'].includes(value.action)&&(!['end','delete'].includes(value.operation)||(value.action==='collection.apply'&&!/^[a-f0-9]{64}$/.test(value.token||''))))fail('REQUEST_SCHEMA','采集维护动作或确认凭据无效');
  if(value.action==='retry'&&(!Number.isInteger(value.sequence)||value.sequence<1||!Array.isArray(value.indices)||!value.indices.length||value.indices.length>256||value.indices.some(i=>!Number.isInteger(i)||i<0)||new Set(value.indices).size!==value.indices.length))fail('REQUEST_SCHEMA','重跑序号与范围无效');
  if(value.action==='orders.retire'&&(!/^[a-f0-9]{32}$/.test(value.operation_id)||!Array.isArray(value.oids)||!value.oids.length||value.oids.length>256||new Set(value.oids).size!==value.oids.length||value.oids.some(o=>typeof o!=='string'||!o||o.length>100)))fail('REQUEST_SCHEMA','删除操作号或原订单身份无效');
  if(value.action==='setup.verify_saved'&&(!Array.isArray(value.codes)||!value.codes.length||value.codes.length>256||value.codes.some(c=>typeof c!=='string'||!c||c.length>128)||new Set(value.codes).size!==value.codes.length))fail('REQUEST_SCHEMA','核验门店范围无效');
  if(Object.keys(value).some(k=>!allowed.includes(k)))fail('REQUEST_SCHEMA','机器请求包含未知字段');
  if(fields[value.action].includes('command_id')&&(typeof value.command_id!=='string'||!idPattern.test(value.command_id)))fail('REQUEST_SCHEMA','命令编号不能为空且必须符合契约');
  if(value.action==='reserve'&&!value.command)fail('REQUEST_SCHEMA','缺少待派发命令');
  if(fields[value.action].includes('setup_id')&&(typeof value.setup_id!=='string'||!idPattern.test(value.setup_id)))fail('REQUEST_SCHEMA','连接核验编号无效');
  if(fields[value.action].includes('template_id')&&(typeof value.template_id!=='string'||!idPattern.test(value.template_id)))fail('REQUEST_SCHEMA','模板获取编号无效');
  if(fields[value.action].includes('purchase_id')&&(typeof value.purchase_id!=='string'||!idPattern.test(value.purchase_id)))fail('REQUEST_SCHEMA','采购任务编号无效');
  if(value.action==='binding.start')bindingRequest(value.binding_request);
  if(value.action==='binding.confirm'&&(!idPattern.test(value.proposal_id||'')||!Number.isSafeInteger(value.sequence)||value.sequence<1||!/^[a-f0-9]{64}$/.test(value.manifest_sha256||'')))fail('REQUEST_SCHEMA','关联预览编号、版本或哈希无效');
  if(value.action==='receipt.synced'&&(!/^[a-f0-9]{64}$/.test(value.manifest_sha256||'')||!value.receipt||Object.keys(value.receipt).some(k=>!['number','revision','stage','version','changed'].includes(k))))fail('REQUEST_SCHEMA','正式实收回执或快照哈希无效');
  if(['purchase.reserve','purchase.rebuild'].includes(value.action)&&(typeof value.package_file!=='string'||!value.package_file||value.package_file.length>2048||!(/^[a-f0-9]{64}$/).test(value.package_sha256||'')))fail('REQUEST_SCHEMA','采购任务文件及哈希无效');
  if(value.action==='purchase.rebuild'&&(!Number.isSafeInteger(value.sequence)||value.sequence<1||!/^[a-f0-9]{32}$/.test(value.operation_id||'')||
    typeof value.reason!=='string'||!value.reason.trim()||value.reason.length>200||value.acknowledge_uncertainty!==true))fail('REQUEST_SCHEMA','请明确确认旧结果可能未核清并提供有效重建范围');
  if(value.action==='setup.start'){
    const text=v=>typeof v==='string'&&v.trim().length>0&&v.length<=1024;
    const candidate=value.candidate;
    if(!candidate||typeof candidate!=='object'||Object.keys(candidate).sort().join(',')!=='actual_profile_path,edge_executable'||!Object.values(candidate).every(text)||!text(value.output_root))fail('REQUEST_SCHEMA','浏览器与输出目录参数无效');
    if(!Array.isArray(value.targets)||!value.targets.length||value.targets.length>256||value.targets.some(t=>!t||typeof t!=='object'||Object.keys(t).sort().join(',')!=='code,name'||!text(t.code)||!text(t.name)||t.code.length>128||t.name.length>256))fail('REQUEST_SCHEMA','请提供1至256个准确编码和名称的门店');
    if(new Set(value.targets.map(t=>t.code)).size!==value.targets.length)fail('REQUEST_SCHEMA','待核验门店编码重复');
  }
  for(const key of ['offset','after'])if(value[key]!==undefined&&(!Number.isSafeInteger(value[key])||value[key]<0))fail('REQUEST_SCHEMA','分页或事件游标无效');
  if(value.limit!==undefined&&(!Number.isSafeInteger(value.limit)||value.limit<1||value.limit>100))fail('REQUEST_SCHEMA','每次查询最多100条');
  return value;
}
export async function* readFrames(stream){
  let pending=Buffer.alloc(0);const decoder=new TextDecoder('utf-8',{fatal:true});
  for await(const chunk of stream){
    const bytes=Buffer.isBuffer(chunk)?chunk:Buffer.from(chunk);
    let start=0;
    for(;;){
      const newline=bytes.indexOf(10,start),end=newline<0?bytes.length:newline;
      const piece=bytes.subarray(start,end);
      if(pending.length+piece.length>MAX_FRAME_BYTES)fail('FRAME_TOO_LARGE','机器请求超过1MiB，连接已停止');
      pending=Buffer.concat([pending,piece]);
      if(newline<0)break;
      if(pending.length){
        let text;try{text=decoder.decode(pending);}catch{fail('FRAME_ENCODING','机器请求必须是UTF-8');}
        let value;try{value=JSON.parse(text);}catch{fail('FRAME_JSON','机器请求必须是一行完整JSON');}
        yield value;
      }
      pending=Buffer.alloc(0);start=newline+1;
    }
  }
  if(pending.length)fail('FRAME_TRUNCATED','机器请求未完整传送；未执行残缺命令');
}
export async function writeFrame(stream,value){
  const frame=JSON.stringify(value)+'\n';
  if(Buffer.byteLength(frame)>MAX_FRAME_BYTES)fail('RESPONSE_TOO_LARGE','响应超过上限，请缩小分页');
  if(!stream.write(frame))await once(stream,'drain');
}
export function publicRecord(record){
  if(!record)return null;
  const {config_snapshot,...view}=record;return view;
}
