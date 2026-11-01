import fs from 'node:fs/promises';
import path from 'node:path';
import crypto from 'node:crypto';
import {AppError,fail,idValue,uid,redact} from './common.mjs';

export function validateResultAddress(fileUrl,{taskId,object,allowedHosts,allowTestHttp=false}){
  idValue(taskId,'任务ID');let u;try{u=new URL(fileUrl);}catch{fail('INVALID_FILE_ADDRESS','任务结果地址无效');}
  if(u.username||u.password)fail('INVALID_FILE_ADDRESS','结果地址不能包含用户凭据');
  if(u.protocol!=='https:'&&!(allowTestHttp&&u.protocol==='http:'&&u.hostname==='127.0.0.1'))fail('UNTRUSTED_DOWNLOAD_HOST','只接受已验证的HTTPS结果文件');
  if(!allowedHosts.includes(u.host))fail('UNTRUSTED_DOWNLOAD_HOST','结果文件主机未通过本版本兼容性验证');
  if(u.origin+u.pathname!==object)fail('RESULT_OBJECT_CHANGED','结果对象与已知任务不一致');
  if(!u.pathname.split('/').some(s=>decodeURIComponent(s)===taskId))fail('TASK_OBJECT_MISMATCH','结果路径不属于已知任务');
  return u;
}
export async function downloadKnownTask({fileUrl,taskId,object,allowedHosts,directory,signal,timeoutMs=600000,maxBytes=1073741824,onProgress=()=>{},allowTestHttp=false}){
  let address=validateResultAddress(fileUrl,{taskId,object,allowedHosts,allowTestHttp});
  const combined=signal?AbortSignal.any([signal,AbortSignal.timeout(timeoutMs)]):AbortSignal.timeout(timeoutMs);
  let response;
  try{
    for(let redirect=0;redirect<4;redirect++){
      response=await fetch(address,{redirect:'manual',credentials:'omit',signal:combined,headers:{'Accept':'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,application/octet-stream'}});
      if([301,302,303,307,308].includes(response.status)){
        const location=response.headers.get('location');await response.body?.cancel();
        if(!location)fail('INVALID_REDIRECT','下载重定向缺少目标');
        address=validateResultAddress(new URL(location,address).href,{taskId,object,allowedHosts,allowTestHttp});continue;
      }break;
    }
    if(response.status===403)fail('RESULT_LINK_EXPIRED_OR_DENIED','结果链接已过期或访问被拒绝；需刷新同一任务地址');
    if(response.status!==200)fail('DOWNLOAD_HTTP_FAILED',`任务结果下载失败，HTTP ${response.status}`);
    const header=response.headers.get('content-length');
    const expectedBytes=header===null?null:Number(header);
    if(expectedBytes!==null&&(!Number.isSafeInteger(expectedBytes)||expectedBytes<1||expectedBytes>maxBytes))fail('DOWNLOAD_SIZE_LIMIT','任务结果大小超过保护值或无效');
    await fs.mkdir(directory,{recursive:true});
    const part=path.join(directory,uid()+'.part');const destination=part.replace(/\.part$/,'.xlsx');
    const file=await fs.open(part,'wx');const h=crypto.createHash('sha256');let bytes=0,signature=Buffer.alloc(0),lastUpdate=0;
    try{
      for await(const chunk of response.body){
        if(combined.aborted)throw combined.reason;
        bytes+=chunk.length;if(bytes>maxBytes)fail('DOWNLOAD_SIZE_LIMIT','任务结果超过工程保护值');
        if(signature.length<4)signature=Buffer.concat([signature,chunk]).subarray(0,4);
        h.update(chunk);
        for(let offset=0;offset<chunk.length;){const result=await file.write(chunk,offset,chunk.length-offset);if(!result.bytesWritten)fail('DOWNLOAD_WRITE_FAILED','文件写入没有取得进展');offset+=result.bytesWritten;}
        if(Date.now()-lastUpdate>1000){lastUpdate=Date.now();onProgress({bytes,expectedBytes});}
      }
      if(bytes<100||signature.toString('hex')!=='504b0304')fail('NOT_XLSX','下载内容不是XLSX，可能是错误页');
      if(expectedBytes!==null&&bytes!==expectedBytes)fail('DOWNLOAD_TRUNCATED','下载字节数与服务器声明不符');
      await file.sync();
    }finally{await file.close();}
    await fs.rename(part,destination);
    return {taskId,object,file:destination,bytes,sha256:h.digest('hex'),transport:'known_task_signed_result_stream'};
  }catch(e){
    await response?.body?.cancel().catch(()=>{});
    if(e instanceof AppError)throw e;
    if(signal?.aborted)throw new AppError('PAUSED','下载已停止，继续时重取同一任务结果');
    throw new AppError('DOWNLOAD_NETWORK_FAILED','脚本下载连接失败，浏览器代理与脚本网络可能不同',{cause:redact(String(e.message))});
  }
}
