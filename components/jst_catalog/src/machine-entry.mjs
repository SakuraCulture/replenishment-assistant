import readline from 'node:readline';
import fs from 'node:fs/promises';
import {CatalogService} from './service.mjs';
import {safeError} from './common.mjs';
import {watchParent} from './service-lifetime.mjs';

const args=process.argv.slice(2),at=k=>{const i=args.indexOf(k);if(i<0||!args[i+1])throw Error('Missing '+k);return args[i+1];};
let service,input,closing=false,closePromise,outputClosed=false,stopWatching=()=>{};let inFlight=0;
const send=frame=>new Promise(resolve=>{
  if(outputClosed||process.stdout.destroyed){resolve();return;}
  const timer=setTimeout(resolve,1000);timer.unref();
  process.stdout.write(JSON.stringify({protocol:'jst.catalog.v1',...frame})+'\n',()=>{clearTimeout(timer);resolve();});
});
function shutdown(error){
  if(closePromise)return closePromise;
  closing=true;stopWatching();
  closePromise=Promise.resolve().then(async()=>{
    input?.close();process.stdin.destroy();let failed=error;
    try{await service?.close();}catch(e){failed||=e;}
    if(failed)await send({type:'fatal',error:safeError(failed)});
    // Running tasks and all state writes have settled before close returns. Exit
    // our transport even if an idle browser socket/cleanup handle is left open.
    // This never terminates the user's browser or a submitting task.
    process.exit(failed?1:0);
  });return closePromise;
}
process.stdout.on('error',()=>{outputClosed=true;void shutdown();});
try{
  const options=JSON.parse(await fs.readFile(at('--options'),'utf8'));
  service=await CatalogService.open(options);
  stopWatching=watchParent(options.parentPid,()=>{void shutdown();});
  if(closing)await closePromise;
  await send({type:'ready',version:'0.31.0'});
  input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  input.on('line',async line=>{
    let request,counted=false;
    try{
      if(Buffer.byteLength(line)>1024*1024)throw Error('Request too large');request=JSON.parse(line);
      if(request.protocol!=='jst.catalog.v1'||typeof request.request_id!=='string'||inFlight>=32)throw Error('Invalid or busy request');
      if(closing)throw Error('采集服务正在安全退出，未开始新动作');
      inFlight++;counted=true;const result=await service.command(request);
      await send({type:'response',request_id:request.request_id,ok:true,result});
      if(request.action==='shutdown')await shutdown();
    }catch(e){await send({type:'response',request_id:request?.request_id||null,ok:false,error:safeError(e)});}
    finally{if(counted)inFlight--;}
  });
  input.on('close',()=>{void shutdown();});
}catch(e){await shutdown(e);}
