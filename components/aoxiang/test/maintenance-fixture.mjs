import fs from 'node:fs/promises';
import path from 'node:path';
import {StateStore} from '../src/state.mjs';
import {MachineCommands,mappingVersion} from '../src/machine-commands.mjs';
import {freshConfig,saveConfig} from '../src/config.mjs';
import {scopeHash,sha256} from '../src/common.mjs';
export async function fixture(root){
  const identity={accountId:'001',merchantCode:'SYNTHETIC',displayName:'离线验收商家'};
  const targets=['W2','W1','W3'].map((code,n)=>({buyerWarehouseCode:code,displayName:['合成琅琊店','合成乳山店','合成已采用店'][n],operatingStores:[],mappingSource:'synthetic',mappingVerifiedAt:'2026-11-04T00:00:00Z'}));
  const config=freshConfig(identity,targets),state=await StateStore.open(root),commands=new MachineCommands(state);
  await saveConfig(root,config);
  const command=(id,codes=['W2','W1'])=>({protocol_version:1,command_id:id,operation:'replenishment.export',account_binding:{account_id:'001',merchant_code:'SYNTHETIC'},scope:{warehouse_codes:codes},basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'refresh'}});
  const reserve=async(id,codes)=>{await commands.reserve(command(id,codes),config);commands.advance(id,1,'RUNNING');return commands.get(id);};
  const old=await reserve('old-failed');
  for(const item of old.items){
    const row=state.getItems(item.run_id)[0],taskId='task-'+item.warehouse_code;
    const file=path.join(root,item.warehouse_code+'.xlsx');await fs.writeFile(file,'SYNTHETIC downloaded count mismatch '+item.warehouse_code);
    state.putAttempt({id:'attempt-'+item.warehouse_code,itemId:row.id,runId:item.run_id,...identity,buyerWarehouseCode:item.warehouse_code,scopeHash:scopeHash(item.warehouse_code,'D13-all-info-2'),semanticVersion:'D13-all-info-2',stage:'DOWNLOADED',taskId,createdAt:'2026-11-02T00:00:00Z',query:{total:10},taskSummary:{taskId,successCount:9,failCount:0},downloaded:{file}});
    state.putItem({...row,stage:'DOWNLOADED',attemptId:'attempt-'+item.warehouse_code});
    state.putRun({...state.getRun(item.run_id),status:'NEEDS_ATTENTION'});
  }
  commands.advance(old.id,2,'COMPLETED_WITH_ERRORS',{items:old.items.map(i=>({...i,state:'FAILED',error:{code:'QUERY_FILE_COUNT_DIFF',message:'合成行数不一致'}}))});
  const used=await reserve('adopted',['W3']),item=used.items[0],bytes=Buffer.from('SYNTHETIC adopted immutable input'),digest=sha256(bytes);
  const file=path.join(root,'command-files',used.id,'0-'+digest+'.xlsx');await fs.mkdir(path.dirname(file),{recursive:true});await fs.writeFile(file,bytes);
  const result={warehouse_code:'W3',run_id:item.run_id,file,sha256:digest,bytes:bytes.length,rows:1,account_binding:used.request.account_binding,reused:false};
  commands.advance(used.id,2,'SUCCEEDED',{items:[{...item,state:'SUCCEEDED',result}]});
  const blocked=await reserve('blocked');
  commands.advance(blocked.id,2,'NEEDS_REVIEW',{items:blocked.items.map((i,n)=>n?i:{...i,state:'NEEDS_REVIEW',error:{code:'UNRESOLVED_TASK'}}),error:{code:'UNRESOLVED_TASK'}});
  return {state,commands,config,command,identity,root};
}
