import {CatalogError} from './common.mjs';

export function parentAlive(pid){
  try{process.kill(pid,0);return true;}catch(error){if(error.code==='ESRCH')return false;return true;}
}

// stdin EOF remains the primary shutdown signal. This independent check also
// covers an inherited/open pipe after the owning desktop process disappeared.
export function watchParent(pid,onGone,{interval=1000,alive=parentAlive}={}){
  if(pid===undefined)return ()=>{};
  if(!Number.isSafeInteger(pid)||pid<=0||pid===process.pid)throw new CatalogError('PARENT_ID_INVALID','采集服务的主程序身份无效');
  let timer,notified=false;
  const check=()=>{if(!notified&&!alive(pid)){notified=true;clearInterval(timer);onGone();}};
  timer=setInterval(check,interval);timer.unref();check();return ()=>clearInterval(timer);
}

export async function disconnectBounded(close,timeout=3000){
  let timer;
  try{return await Promise.race([Promise.resolve().then(close),new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new CatalogError('DISCONNECT_TIMEOUT','当前任务已安全结束，浏览器连接收尾超时；本地任务锁将正常释放')),timeout);
  })]);}finally{clearTimeout(timer);}
}
