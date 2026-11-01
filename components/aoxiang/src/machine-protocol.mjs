import {once} from 'node:events';
import {TextDecoder} from 'node:util';
import {fail} from './common.mjs';
import {MAX_FRAME_BYTES} from './machine-commands.mjs';

const idPattern=/^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$/;
const fields={ping:[],configuration:[],list:['offset','limit'],reserve:['command'],status:['command_id'],
  events:['command_id','after','limit'],pause:['command_id'],resume:['command_id'],shutdown:[],
  discover:[],'setup.start':['candidate','output_root','targets'],'setup.status':['setup_id'],
  'setup.cancel':['setup_id'],'setup.commit':['setup_id'],
  'template.start':[],'template.status':['template_id'],'template.cancel':['template_id']};
export function validateRequest(value){
  if(!value||typeof value!=='object'||Array.isArray(value)||value.protocol_version!==1||
    typeof value.request_id!=='string'||!idPattern.test(value.request_id)||!Object.hasOwn(fields,value.action))fail('REQUEST_SCHEMA','机器请求版本、编号或动作无效');
  const allowed=['protocol_version','request_id','action',...fields[value.action]];
  if(Object.keys(value).some(k=>!allowed.includes(k)))fail('REQUEST_SCHEMA','机器请求包含未知字段');
  if(fields[value.action].includes('command_id')&&(typeof value.command_id!=='string'||!idPattern.test(value.command_id)))fail('REQUEST_SCHEMA','命令编号不能为空且必须符合契约');
  if(value.action==='reserve'&&!value.command)fail('REQUEST_SCHEMA','缺少待派发命令');
  if(fields[value.action].includes('setup_id')&&(typeof value.setup_id!=='string'||!idPattern.test(value.setup_id)))fail('REQUEST_SCHEMA','连接核验编号无效');
  if(fields[value.action].includes('template_id')&&(typeof value.template_id!=='string'||!idPattern.test(value.template_id)))fail('REQUEST_SCHEMA','模板获取编号无效');
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
