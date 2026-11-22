import {CatalogError,check} from './common.mjs';
import {saveExportBytes} from './product-export.mjs';

const maximum=60*1024*1024;

export async function receiveArchiveFile(page,url,{recordId,expectedRows,expectedPath,output,signal,onProgress}){
  if(expectedPath&&url.pathname!==expectedPath)
    throw new CatalogError('FILE_IDENTITY','原报表任务的文件身份已变化');
  check(signal);
  onProgress('DOWNLOADING','通过本仓浏览器接收原报表文件');
  const session=await page.context().newCDPSession(page);
  const result=Promise.withResolvers();
  result.promise.catch(()=>{});
  const timeout=setTimeout(()=>result.reject(new CatalogError('DOWNLOAD_TIMEOUT','原报表文件接收超时，请继续原任务')),50000);
  const abort=()=>{try{check(signal);}catch(error){result.reject(error);}};
  const onResponse=async info=>{
    const id=info.requestId;
    if(info.request.url!==url.href){
      await session.send('Fetch.continueRequest',{requestId:id}).catch(()=>{});
      return;
    }
    try{
      check(signal);
      if(info.responseStatusCode!==200||info.responseErrorReason){
        const status=Number.isInteger(info.responseStatusCode)?'HTTP '+info.responseStatusCode:
          (info.responseErrorReason||'无响应');
        throw new CatalogError('DOWNLOAD_FAILED','本仓浏览器未取得原报表原件（'+status+'）');
      }
      const length=Number(info.responseHeaders?.find(h=>h.name.toLowerCase()==='content-length')?.value);
      if(length>maximum)throw new CatalogError('FILE_SIZE','原报表文件超过单片接收上限');
      const data=await session.send('Fetch.getResponseBody',{requestId:id});
      await session.send('Fetch.failRequest',{requestId:id,errorReason:'Aborted'});
      result.resolve(Buffer.from(data.body,data.base64Encoded?'base64':'utf8'));
    }catch(error){
      result.reject(error instanceof CatalogError?error:new CatalogError('DOWNLOAD_FAILED','读取原报表的浏览器响应时连接中断'));
      await session.send('Fetch.failRequest',{requestId:id,errorReason:'Aborted'}).catch(()=>{});
    }
  };
  signal?.addEventListener('abort',abort,{once:true});
  session.on('Fetch.requestPaused',onResponse);
  try{
    await session.send('Fetch.enable',{patterns:[{urlPattern:url.origin+url.pathname+'?*',requestStage:'Response'}]});
    check(signal);
    // Keep the established Edge proxy and networking path. The interception is
    // limited to this task's existing tab and URL and never changes downloads
    // globally or visits another account.
    await page.evaluate(href=>{
      const link=document.createElement('a');link.href=href;document.body.append(link);link.click();link.remove();
    },url.href);
    const bytes=await result.promise;
    check(signal);
    return await saveExportBytes(bytes,url,{recordId,expectedRows,output});
  }finally{
    clearTimeout(timeout);signal?.removeEventListener('abort',abort);
    session.off('Fetch.requestPaused',onResponse);
    await session.send('Fetch.disable').catch(()=>{});
    await session.detach().catch(()=>{});
  }
}
