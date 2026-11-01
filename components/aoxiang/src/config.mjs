import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import Ajv from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import {readJson,writeJson,fail,idValue} from './common.mjs';
const appRoot=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
let validators;
export async function schemaValidators(){
  if(validators)return validators;
  const ajv=new Ajv({allErrors:true,strict:false});addFormats(ajv);validators={};
  for(const name of ['portable.config','machine.binding','migration.manifest'])validators[name]=ajv.compile(await readJson(path.join(appRoot,'assets/schemas',name+'.schema.json')));
  return validators;
}
export async function validateConfig(type,value,{allowExample=false}={}){
  const v=(await schemaValidators())[type];if(!v||!v(value))fail('CONFIG_SCHEMA','配置格式不符合契约',{issues:(v?.errors||[]).map(e=>({path:e.instancePath,message:e.message}))});
  if(value.exampleOnly&&!allowExample)fail('EXAMPLE_CONFIG','示例配置不能运行，请使用配置向导');
  if(type==='portable.config'){
    const t=value.targets;const keys=t.map(x=>x.buyerWarehouseCode);
    if(new Set(keys).size!==keys.length)fail('DUPLICATE_TARGET','同一接收仓店只能配置一次');
    if(new Set(value.batches.map(b=>b.id)).size!==value.batches.length)fail('DUPLICATE_BATCH','批次ID重复');
    for(const b of value.batches)if(b.targetWarehouseCodes.some(c=>!keys.includes(c)))fail('BATCH_TARGET_MISSING','批次引用的仓店不存在');
    if(value.enabled){
      idValue(value.tenantBinding.accountId);idValue(value.tenantBinding.merchantCode);
      for(const target of t){idValue(target.buyerWarehouseCode);if(!target.mappingVerifiedAt)fail('TARGET_UNVERIFIED','仓店目录尚未现场核实');}
    }
  }
  return value;
}
export async function loadConfig(root){return validateConfig('portable.config',await readJson(path.join(root,'config/portable.config.json')));}
export async function saveConfig(root,value){await validateConfig('portable.config',value);await writeJson(path.join(root,'config/portable.config.json'),value);}
export const fieldContract = (variant='current')=>readJson(path.join(appRoot,'assets',variant==='v1'?'field-contract.json':'field-contract-v2.json'));
export function freshConfig(identity,targets){return {
  schemaVersion:'2.0',exampleOnly:false,enabled:true,timezone:'Asia/Shanghai',
  browserInteraction:{mode:'background_tab'},
  tenantBinding:{accountId:identity.accountId,merchantCode:identity.merchantCode,displayName:identity.displayName},
  exportPolicy:{sourceId:'D13',variant:'all_information',semanticVersion:'D13-all-info-2',allGoods:true,suggestedQuantityPositiveOnly:false,additionalFilters:{},normalRun:'reuse_valid_daily_asset_then_export_missing',rerun:'explicit_export_then_validate_then_replace'},
  targets,batches:[{id:'default',name:'默认补货批次',targetWarehouseCodes:targets.map(t=>t.buyerWarehouseCode)}],
  resultLayout:{rootPreset:'Documents',folderTemplate:'{tenantAlias}/{YYYY-MM-DD}',filenameTemplate:'{storeName}_{buyerWarehouseCode}_{YYYYMMDD}_补货建议.xlsx'},
  resourceLimits:{connectTimeoutMs:60000,pageTimeoutMs:30000,pollIntervalMs:10000,taskWaitBudgetMs:900000,downloadTimeoutMs:600000,maxCompressedFileBytes:1073741824}
};}
