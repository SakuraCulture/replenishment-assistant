import fs from 'node:fs/promises';
import path from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {CatalogError,WAREHOUSES,canonical,sha,now} from './common.mjs';

export const version=(data,wh)=>data.accounts?.[wh]?.bindingVersion??data.bindingEpochs?.[wh]??0;
export const retired=i=>!!i?.retirement;
export function requireLive(i){if(retired(i))throw new CatalogError('ITEM_RETIRED','原采集已在本地结束并归档，不能恢复、重试或采用；平台任务和文件仍保留');return i;}
export function resetPlan(data,warehouses){
  if(!Array.isArray(warehouses)||!warehouses.length||new Set(warehouses).size!==warehouses.length||warehouses.some(w=>!WAREHOUSES.includes(w)))throw Error('请选择明确的一仓或两仓');
  const selected=[...warehouses].sort(),accounts=Object.fromEntries(selected.map(w=>[w,data.accounts?.[w]||null]));
  const items=[];
  for(const [bid,b] of Object.entries(data.batches))for(const i of b.items)
    if(selected.includes(i.account.warehouse)&&!i.adoption&&!retired(i))items.push({batch_id:bid,item:i});
  const operations=Object.values(data.operations||{}).filter(o=>selected.includes(o.warehouse)&&!o.retirement);
  const basis={warehouses:selected,accounts,epochs:Object.fromEntries(selected.map(w=>[w,version(data,w)])),items,operations};
  return {warehouses:selected,fingerprint:sha(canonical(basis)),accounts,unfinished:items.length,
    files:items.filter(x=>x.item.receipt||x.item.native?.file).length,
    rows:selected.map(warehouse=>({warehouse,company:accounts[warehouse]?.expectedCompany||'尚未绑定',
      unfinished:items.filter(x=>x.item.account.warehouse===warehouse).length})),basis};
}
export async function resetBindings(service,c){
  if(!/^[0-9a-f]{32}$/.test(c.operation_id||'')||c.confirmed!==true)throw Error('请先核对纠错范围并确认');
  let data=service.store.read(),previous=data.bindingResets?.[c.operation_id];
  if(previous){
    if(previous.fingerprint!==c.expected_fingerprint||canonical(previous.warehouses)!==canonical([...c.warehouses].sort()))throw Error('同一纠错编号不能替换范围');
    return previous;
  }
  if(service.active)throw new CatalogError('BUSY','请先安全停止当前采集或核验，等待完成后再纠正绑定');
  const gate=new DatabaseSync(path.join(service.stateRoot,'binding-change.lock.sqlite'));
  try{
    try{gate.exec('PRAGMA busy_timeout=0; BEGIN IMMEDIATE;');}
    catch(e){if([5,6].includes(e.errcode))throw new CatalogError('ADOPTION_BUSY','仓资料正在采用或保存，请等待本次保存完成后再纠正绑定');throw e;}
    data=service.store.read();const plan=resetPlan(data,c.warehouses);
    if(plan.fingerprint!==c.expected_fingerprint)throw new CatalogError('CONFIG_CONFLICT','绑定或原采集记录已变化，请重新核对纠错范围');
    const dir=path.join(service.stateRoot,'binding_backups');await fs.mkdir(dir,{recursive:true});
    const backup=path.join(dir,c.operation_id+'.json'),saved=now();
    const content={schema:1,operation_id:c.operation_id,fingerprint:plan.fingerprint,warehouses:plan.warehouses,saved,before:data};
    try{
      const handle=await fs.open(backup,'wx');
      try{await handle.writeFile(JSON.stringify(content,null,2));await handle.sync();}finally{await handle.close();}
    }catch(e){
      if(e.code!=='EEXIST')throw e;
    }
    const verified=JSON.parse(await fs.readFile(backup,'utf8'));
    if(verified.operation_id!==c.operation_id||verified.fingerprint!==plan.fingerprint||canonical(verified.before)!==canonical(data))throw Error('纠错备份不完整或与当前状态不符，未解除绑定，也未覆盖原备份');
    const result={operation_id:c.operation_id,fingerprint:plan.fingerprint,warehouses:plan.warehouses,
      retired_items:plan.unfinished,backup_path:backup,at:saved};
    await service.store.write(d=>{
      if(resetPlan(d,c.warehouses).fingerprint!==plan.fingerprint)throw new CatalogError('CONFIG_CONFLICT','采集记录已变化，未解除绑定');
      const retirement={operation_id:c.operation_id,at:saved,reason:'用户纠正仓绑定；本地结束，平台任务未撤销'};
      for(const b of Object.values(d.batches)){
        for(const i of b.items)if(plan.warehouses.includes(i.account.warehouse)&&!i.adoption&&!retired(i))i.retirement=retirement;
        if(b.items.length&&b.items.every(i=>i.adoption||retired(i))&&b.items.some(retired))b.state='LOCALLY_ENDED';
      }
      for(const op of Object.values(d.operations||{}))if(plan.warehouses.includes(op.warehouse)&&!op.retirement)op.retirement=retirement;
      d.bindingEpochs||={};d.rebindRequired||={};d.accounts||={};
      for(const wh of plan.warehouses){d.bindingEpochs[wh]=version(d,wh)+1;d.rebindRequired[wh]=true;delete d.accounts[wh];}
      d.bindingResets||={};d.bindingResets[c.operation_id]=result;
    });
    return result;
  }finally{try{gate.exec('ROLLBACK');}catch{}gate.close();}
}
