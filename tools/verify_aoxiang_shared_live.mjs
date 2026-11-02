// Opt-in live READ-ONLY probe. No submit/export/import API is called.
// Requires a known saved configuration and exact verified target. Never writes source settings.
import fs from 'node:fs/promises';
import path from 'node:path';
import {MachineSetup} from '../components/aoxiang/src/machine-setup.mjs';
import {BoundBrowserPool} from '../components/aoxiang/src/bound-browser.mjs';
import {StateStore} from '../components/aoxiang/src/state.mjs';
import {discoverEndpoint} from '../components/aoxiang/src/environment.mjs';
import {sha256,writeJson,readJson,sameIdentity,businessDate} from '../components/aoxiang/src/common.mjs';
const args=process.argv.slice(2),arg=n=>{const i=args.indexOf(n);if(i<0)throw Error('Required '+n);return args[i+1];};
const source=path.resolve(arg('--config-dir')),out=path.resolve(arg('--output')),code=arg('--store');
await fs.mkdir(out,{recursive:false});
const config=await readJson(path.join(source,'portable.config.json')),binding=await readJson(path.join(source,'machine.binding.json'));
const target=config.targets.find(t=>t.buyerWarehouseCode===code);if(!target)throw Error('Store must already be verified');
const before={};for(const name of ['portable.config.json','machine.binding.json'])before[name]=sha256(await fs.readFile(path.join(source,name)));
const report={passed:false,live_platform:true,store:code,steps:[],export_submissions:0,purchase_uploads:0,phases:[]};
let setup,state;const browsers=new BoundBrowserPool();
try{
  await discoverEndpoint(binding.userDataRoot); // fail fast on stale/disabled remote-debugging port
  const root=path.join(out,'component-state');
  state=await StateStore.open(root);
  await writeJson(path.join(root,'config/portable.config.json'),config);
  await writeJson(path.join(root,'config/machine.binding.json'),binding);
  setup=new MachineSetup({root,state,browsers});
  const job=setup.begin({candidate:{edge_executable:binding.edgeExecutable,actual_profile_path:binding.actualProfilePath},
    output_root:path.join(out,'native-files'),targets:[{code,name:target.displayName}]});
  const timer=setInterval(()=>{const stage=setup.job?.stage;if(stage!==report.phases.at(-1)){report.phases.push(stage);process.stdout.write(stage+'\n');}},500);
  try{await setup.promise;}finally{clearInterval(timer);}
  if(setup.job.state!=='READY')throw Object.assign(new Error(setup.job.error?.message||setup.job.state),{code:setup.job.error?.code});
  await setup.commit(job.id);
  const owned=browsers.browser,workerPid=owned.client.child.pid;
  report.worker_pid=workerPid;report.steps.push({module:'setup',worker_pid:workerPid,rows:setup.job.targets[0].rows});
  const safeContext={...owned,state,signal:new AbortController().signal,onProgress:e=>process.stdout.write(e.stage+'\n')};
  const active=await browsers.acquire(safeContext);
  const query={buyerWarehouseCode:code,name:target.displayName,from:businessDate(),to:businessDate(),dateBasis:'created',identity:config.tenantBinding};
  for(const [module,call] of [
    ['D13',()=>active.adapter.querySingle(code,target.displayName)],
    ['purchase',()=>active.adapter.queryPurchase(query)],
    ['purchase_create',()=>active.adapter.queryCreateOrders(query)],
    ['D13-return',()=>active.adapter.querySingle(code,target.displayName)]]){
    process.stdout.write(module+' read-only\n');const start=performance.now(),result=await call(),q=result.query||result;
    if(active.client.child.pid!==workerPid||!sameIdentity(q.identity,config.tenantBinding)||q.warehouseCode!==code)throw Error('Connection or scope changed');
    report.steps.push({module,worker_pid:workerPid,rows:q.total,seconds:(performance.now()-start)/1000});
  }
  report.passed=true;
}catch(error){report.error={code:error.code||'FAILED',message:error.message};process.exitCode=1;}
finally{
  await setup?.close();await browsers.close();state?.close();
  report.configuration_unchanged={};for(const [name,hash] of Object.entries(before))report.configuration_unchanged[name]=hash===sha256(await fs.readFile(path.join(source,name)));
  report.finished_at=new Date().toISOString();await writeJson(path.join(out,'report.json'),report);
  process.stdout.write(JSON.stringify({passed:report.passed,error:report.error,steps:report.steps.length})+'\n');
}
