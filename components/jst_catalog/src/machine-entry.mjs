import readline from 'node:readline';
import fs from 'node:fs/promises';
import {CatalogService} from './service.mjs';
import {safeError} from './common.mjs';

const args=process.argv.slice(2),at=k=>{const i=args.indexOf(k);if(i<0||!args[i+1])throw Error('Missing '+k);return args[i+1];};
const send=frame=>process.stdout.write(JSON.stringify({protocol:'jst.catalog.v1',...frame})+'\n');
let service,closing=false;let inFlight=0;
try{
  const options=JSON.parse(await fs.readFile(at('--options'),'utf8'));
  service=await CatalogService.open(options);send({type:'ready',version:'0.31.0'});
  const input=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  input.on('line',async line=>{
    let request;
    try{
      if(Buffer.byteLength(line)>1024*1024)throw Error('Request too large');request=JSON.parse(line);
      if(request.protocol!=='jst.catalog.v1'||typeof request.request_id!=='string'||inFlight>=32)throw Error('Invalid or busy request');
      inFlight++;const result=await service.command(request);
      send({type:'response',request_id:request.request_id,ok:true,result});
      if(request.action==='shutdown'&&!closing){closing=true;input.close();await service.close();process.exitCode=0;}
    }catch(e){send({type:'response',request_id:request?.request_id||null,ok:false,error:safeError(e)});}
    finally{inFlight=Math.max(0,inFlight-1);}
  });
  input.on('close',()=>{if(!closing){closing=true;service.close().catch(e=>{send({type:'fatal',error:safeError(e)});process.exitCode=1;});}});
}catch(e){send({type:'fatal',error:safeError(e)});process.exitCode=1;}
