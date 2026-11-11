// Evidence for upgrading a prior explicit local end. No date/store guessing.
import {sameIdentity} from './common.mjs';
export function legacyEndedCollections(db){
  const has=name=>!!db.prepare("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?").get(name);
  if(!has('runs')||!has('machine_commands'))return [];
  const rows=db.prepare("SELECT r.payload FROM runs r WHERE json_extract(r.payload,'$.status')='ENDED' AND json_extract(r.payload,'$.endedAt') IS NOT NULL"+
    (has('retired_collections')?' AND NOT EXISTS(SELECT 1 FROM retired_collections t WHERE t.run_id=r.id)':'')).all();
  if(!rows.length)return [];
  const commands=db.prepare('SELECT payload FROM machine_commands').all().map(row=>JSON.parse(row.payload)),result=[];
  for(const row of rows){
    const run=JSON.parse(row.payload),owners=commands.filter(c=>Array.isArray(c.items)&&c.items.some(i=>i.run_id===run.id));
    if(!run.identity||!Array.isArray(run.targets)||!run.targets.length||!owners.length||!owners.some(c=>c.items.some(i=>i.run_id===run.id&&i.state==='ENDED')))continue;
    if(owners.some(c=>!sameIdentity(c.config_snapshot?.tenantBinding,run.identity)||c.items.some(i=>i.run_id===run.id&&
      (!['ENDED','SUCCEEDED'].includes(i.state)||!run.targets.some(t=>t.buyerWarehouseCode===i.warehouse_code)))))continue;
    result.push({run,owners});
  }
  return result;
}
