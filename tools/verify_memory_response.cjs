"use strict";
// Run the real frozen/candidate receiveBoundFile against a local HTTP origin.
// The production URL validator stays intact; only this probe's request adapter
// translates the validated synthetic address to 127.0.0.1. No browser/platform.
const fs=require('fs'),path=require('path'),http=require('http'),crypto=require('crypto'),assert=require('assert');
const {pathToFileURL}=require('url');
const args={};for(let n=2;n<process.argv.length;n+=2)args[process.argv[n].replace(/^--/,'')]=process.argv[n+1];
assert(args.source&&args.out,'--source component root and --out new isolated directory are required');
assert(args.bridge,'--bridge shared packaged component root required');
const {request}=require(path.join(path.resolve(args.bridge),'node_modules/playwright-core'));
const source=path.resolve(args.source),out=path.resolve(args.out),count=Number(args.count||5),size=Number(args.bytes||2*1024*1024);
assert(count>=1&&count<=100&&Number.isSafeInteger(count));assert(size>=100&&size<=60*1024*1024&&Number.isSafeInteger(size));
assert(!fs.existsSync(out),'Never overwrite earlier evidence');fs.mkdirSync(out,{recursive:true});
const payload=Buffer.alloc(size,42);payload.write('PK\x03\x04',0,'binary');
const expected=crypto.createHash('sha256').update(payload).digest('hex');
const native='https://jstzbstatic.erp321.com/export/abcdef.xlsx?probe=synthetic-only';
const checkpoint=async()=>{for(let n=0;n<3;n++){global.gc?.();await new Promise(setImmediate);}return process.memoryUsage();};
(async()=>{
  let context,server;const result={passed:false,synthetic:true,browser_started:false,platform_called:false,
    source,source_sha256:crypto.createHash('sha256').update(fs.readFileSync(path.join(source,'src/product-export.mjs'))).digest('hex'),
    node:process.version,body_bytes:size,requests:count,receipts:[],uids:[],gc_probe:typeof global.gc==='function'};
  try{
    const {receiveBoundFile}=await import(pathToFileURL(path.join(source,'src/product-export.mjs')).href);
    server=http.createServer((req,res)=>{res.writeHead(200,{'Content-Type':'application/octet-stream','Content-Length':payload.length});res.end(payload);});
    await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const url='http://127.0.0.1:'+server.address().port+'/synthetic.xlsx';
    context=await request.newContext();
    const page={context:()=>({request:{get:async(address,options)=>{
      assert.equal(address,native);assert.deepEqual(options,{timeout:60000,maxRedirects:0});
      const response=await context.get(url,options);result.uids.push(response._fetchUid());return response;
    }}})};
    result.identity={pid:process.pid};fs.writeFileSync(path.join(out,'identity.json'),JSON.stringify(result.identity));
    result.before=await checkpoint();const start=performance.now();
    for(let n=0;n<count;n++){
      const receipt=await receiveBoundFile(page,native,{recordId:String(100+n),expectedRows:9,expectedPath:'/export/abcdef.xlsx',output:path.join(out,'files')});
      assert.equal(receipt.sha256,expected);assert.deepEqual(fs.readFileSync(receipt.file),payload);
      result.receipts.push({...receipt,file:path.basename(receipt.file)});
    }
    result.operation_seconds=(performance.now()-start)/1000;
    result.after_local_refs_dropped=await checkpoint();result.retained_body_sizes=[];
    for(const uid of result.uids){const response=await context._channel.fetchResponseBody({fetchUid:uid},{timeout:0});result.retained_body_sizes.push(response.binary?.length||0);}
    result.retained_body_bytes=result.retained_body_sizes.reduce((a,b)=>a+b,0);
    result.after_body_availability_check=await checkpoint();
    assert(result.retained_body_bytes===0||result.retained_body_bytes===count*size,'Unexpected partially retained request bodies');
    await context.dispose();context=null;result.after_context_dispose=await checkpoint();
    result.passed=true;
  }catch(error){result.error=error.stack;process.exitCode=1;}
  finally{if(context)await context.dispose();if(server)await new Promise(resolve=>server.close(resolve));}
  fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(result,null,2));
  console.log(JSON.stringify({passed:result.passed,source:result.source,retained_body_bytes:result.retained_body_bytes,operation_seconds:result.operation_seconds,report:path.join(out,'report.json')}));
})();
