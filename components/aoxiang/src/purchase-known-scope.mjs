import {dateBounds} from './purchase-contract.mjs';
import {fail,sameIdentity,now} from './common.mjs';

// Query-mode fallback is allowed ONLY before any native export intent exists.
// It never retries an export or import with an unknown acceptance result.
export async function runKnownPurchase(engine,state,input,{createdDay=null,onFallback=()=>{}}={}){
  try{return await engine.run({...input,from:null,to:null,dateBasis:'created',queryMode:'order_id'});}
  catch(error){
    if(error.code!=='PURCHASE_EXACT_FILTERS_UNSUPPORTED')throw error;
    const prior=input.runId?state.getPurchase(input.runId):state.rows('purchase_runs').filter(r=>
      r.queryMode==='order_id'&&r.orderId===input.orderId&&r.target.buyerWarehouseCode===input.target.buyerWarehouseCode&&sameIdentity(r.identity,engine.config.tenantBinding)).at(-1);
    if(!prior||prior.queryMode!=='order_id'||Object.values(prior.exports).some(e=>e.stage!=='PLANNED'||e.taskId||e.receipt))throw error;
    state.putPurchase({...prior,status:'SUPERSEDED',supersededReason:'ORDER_ID_QUERY_UNSUPPORTED_BEFORE_EXPORT',updatedAt:now()});
    await onFallback(prior.id);
    if(!createdDay)fail('PURCHASE_EXACT_DATE_REQUIRED','当前采购页无法安全清除日期，且没有已核验的原生制单日；请核对原单资料，不按今天或文件名猜日期');
    dateBounds(createdDay,createdDay);
    return engine.run({...input,runId:undefined,from:createdDay,to:createdDay,dateBasis:'created',queryMode:undefined});
  }
}
