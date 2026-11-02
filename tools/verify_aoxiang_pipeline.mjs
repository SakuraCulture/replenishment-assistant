// Deterministic, offline platform simulator; real coordinator/state/XLSX validation.
// Timings demonstrate scheduling only, never claim live AX service performance.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {execFileSync} from 'node:child_process';
import assert from 'node:assert/strict';
import {performance} from 'node:perf_hooks';
import {StateStore} from '../components/aoxiang/src/state.mjs';
import {MachineCommands,mappingVersion} from '../components/aoxiang/src/machine-commands.mjs';
import {freshConfig,fieldContract} from '../components/aoxiang/src/config.mjs';
import {freezeRunAsset} from '../components/aoxiang/src/machine-assets.mjs';
import {createArchive} from '../components/aoxiang/src/zip.mjs';
import {sha256,wait} from '../components/aoxiang/src/common.mjs';
import {Coordinator} from '../components/aoxiang/src/coordinator.mjs';
import {MachineRuntime} from '../components/aoxiang/src/machine-runtime.mjs';

const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const args=process.argv.slice(2),outArg=args.indexOf('--output'),baseArg=args.indexOf('--baseline');
if(outArg<0||baseArg<0)throw Error('Required: --output NEW_DIRECTORY --baseline FIXED_COMMIT');
const output=path.resolve(args[outArg+1]),baseline=execFileSync('git',['rev-parse',args[baseArg+1]+'^{commit}'],{cwd:root,encoding:'utf8'}).trim();
await fs.mkdir(output,{recursive:false});
const oldRoot=path.join(root,'components/aoxiang/dev-evidence','pipeline-'+Date.now());
const names=execFileSync('git',['ls-tree','-r','--name-only',baseline,'components/aoxiang/src','components/aoxiang/assets'],{cwd:root,encoding:'utf8'}).trim().split('\n');
for(const name of names){const dest=path.join(oldRoot,name.replace('components/aoxiang/',''));await fs.mkdir(path.dirname(dest),{recursive:true});await fs.writeFile(dest,execFileSync('git',['show',baseline+':'+name],{cwd:root,maxBuffer:16*1024*1024}));}
const {Coordinator:OldCoordinator}=await import(pathToFileURL(path.join(oldRoot,'src/coordinator.mjs')));
const {MachineRuntime:OldRuntime}=await import(pathToFileURL(path.join(oldRoot,'src/machine-runtime.mjs')));
const identity={accountId:'qa-offline',merchantCode:'QA',displayName:'本地合成平台'};
const targets=['W3','W1','W5','W2','W4'].map(code=>({buyerWarehouseCode:code,displayName:'测试'+code,operatingStores:[],mappingSource:'offline fixture',mappingVerifiedAt:'2026-11-02T00:00:00.000Z'}));
const letters=n=>{let s='';for(n++;n;n=Math.floor((n-1)/26))s=String.fromCharCode(65+(n-1)%26)+s;return s;};
const escape=v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;');
const files=new Map();
for(const target of targets){
  const code=target.buyerWarehouseCode,headers=(await fieldContract()).columns.map(c=>c.rawHeader),values=headers.map(()=> '');
  values[1]=code;values[7]='001234567890123456789';
  const rows=[headers,values].map((row,i)=>`<row r="${i+1}">${row.map((v,k)=>`<c r="${letters(k)}${i+1}" t="inlineStr"><is><t>${escape(v)}</t></is></c>`).join('')}</row>`).join('');
  const file=path.join(output,code+'.xlsx');
  await createArchive(file,[{relativePath:'[Content_Types].xml',buffer:'<Types/>'},
    {relativePath:'xl/workbook.xml',buffer:'<workbook xmlns:r="urn:r"><sheets><sheet name="商品补货建议V2" r:id="r1"/></sheets></workbook>'},
    {relativePath:'xl/_rels/workbook.xml.rels',buffer:'<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>'},
    {relativePath:'xl/worksheets/sheet1.xml',buffer:'<worksheet><sheetData>'+rows+'</sheetData></worksheet>'}]);
  const bytes=await fs.readFile(file);files.set(code,{file,bytes:bytes.length,sha256:sha256(bytes)});
}

async function sample(count,version,round){
  const folder=path.join(output,`${count}-${round}-${version}`),state=await StateStore.open(path.join(folder,'state'));
  const config=freshConfig(identity,targets.slice(0,count)),binding={outputRoot:path.join(folder,'output')};
  config.resourceLimits.pollIntervalMs=20;config.resourceLimits.taskWaitBudgetMs=5000;
  const commands=new MachineCommands(state),tasks=new Map(),events=[];
  const adapter={
    async querySingle(code,name){await wait(20);return {warehouseCode:code,name,total:1,observedAt:'2026-11-02T00:00:00.000Z',identity};},
    async prepareExport(){return {taskIds:['unrelated-new-task'],total:1};},
    async submitOnce({intentId,expected}){
      const code=expected.warehouseCode,id='task-'+code;
      assert.equal(tasks.has(id),false,'No duplicate submission');
      // The first file is intentionally slower; later stores can finish first.
      tasks.set(id,{code,readyAt:performance.now()+(code==='W3'?700:400)});
      events.push({action:'submit',code,task_id:id,ms:performance.now()});
      const receipt={intentId,taskId:id,code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',scope:{buyerWarehouseCodeList:[code],extraKeys:[]}};
      adapter.onReceipt?.(receipt);return receipt;
    },
    async findTask(id){
      assert.ok(tasks.has(id),'Must use exact accepted ID, not newest task');const task=tasks.get(id);
      return {taskId:id,taskStatus:performance.now()>=task.readyAt?'FINISHED':'RUNNING',successCount:'1',failCount:'0',code:'SAAS-CHAIN-PLAN-REPLENISH-ADVICE-EXPORT-V2',fileUrl:'https://tc-open-gateway-supply-deploy.oss-cn-zhangjiakou.aliyuncs.com/'+id+'.xlsx'};
    },
    async verifyIdentity(){return identity;}
  };
  const C=version==='baseline'?OldCoordinator:Coordinator,R=version==='baseline'?OldRuntime:MachineRuntime;
  const runtime=new R({commands,pipeline:version!=='baseline',executeItem:async({record,item,phase,signal,onProgress})=>{
    const result=await new C({state,config,binding,adapter,signal,onProgress,download:async({taskId})=>{
      const code=tasks.get(taskId).code;events.push({action:'download',code,task_id:taskId,ms:performance.now()});return {...files.get(code),taskId};
    }}).run({runId:item.run_id,...(version==='baseline'?{}:{phase})});
    if(result.pending)return {pending:true};return freezeRunAsset({state,binding,commandId:record.id,item});
  }});
  const started=performance.now();
  try{
    await commands.reserve({protocol_version:1,command_id:'fixed-order',operation:'replenishment.export',account_binding:{account_id:identity.accountId,merchant_code:identity.merchantCode},
      scope:{warehouse_codes:targets.slice(0,count).map(t=>t.buyerWarehouseCode)},basis_versions:{store_mapping:mappingVersion(config)},policy:{freshness:'refresh'}},config);
    runtime.kick();await runtime.idle();const elapsed=performance.now()-started,record=commands.get('fixed-order');
    assert.equal(record.state,'SUCCEEDED');assert.equal(tasks.size,count);
    const results=[];
    for(const item of record.items){
      const {run_id,file,...business}=item.result;
      assert.deepEqual(await fs.readFile(file),await fs.readFile(files.get(item.warehouse_code).file));
      results.push({index:item.index,state:item.state,result:business});
    }
    if(version!=='baseline')assert.equal(events.slice(0,count).every(e=>e.action==='submit'),true);
    return {count,version,round,milliseconds:elapsed,results,events:events.map(e=>({...e,ms:e.ms-started}))};
  }finally{await runtime.shutdown();state.close();}
}

const raw=[],summaries=[];
for(const count of [1,3,5]){
  let expected;
  for(let round=0;round<3;round++)for(const version of round%2?['candidate','baseline']:['baseline','candidate']){
    const result=await sample(count,version,round);raw.push(result);
    if(!expected)expected=result.results;else assert.deepEqual(result.results,expected,'Strict values/types/order equivalence');
  }
  const summary={stores:count};
  for(const version of ['baseline','candidate']){
    const values=raw.filter(r=>r.count===count&&r.version===version).map(r=>r.milliseconds).sort((a,b)=>a-b);
    summary[version]={min_ms:values[0],median_ms:values[1],max_ms:values[2]};
  }
  summary.median_reduction_percent=100*(1-summary.candidate.median_ms/summary.baseline.median_ms);summaries.push(summary);
}
const report={passed:true,live_platform:false,baseline_commit:baseline,excluded_result_fields:['run_id (random per execution)','file (isolated QA absolute path)'],
  method:'Same native fixture bytes; actual state/coordinator/runtime/validation/publication/immutable files. Sequential alternating runs, 3 each. Synthetic query 20ms, first task 700ms, others 400ms; unrelated newest task must never be used.',
  limitations:'Synthetic scheduling evidence only. No live AX latency, server concurrency limit, production speedup, or memory benchmark is claimed.',differences:0,summaries,raw};
await fs.writeFile(path.join(output,'report.json'),JSON.stringify(report,null,2));
console.log(JSON.stringify({passed:report.passed,differences:0,summaries},null,2));
