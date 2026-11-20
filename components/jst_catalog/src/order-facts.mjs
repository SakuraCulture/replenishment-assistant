// The native product popover calls ReloadOrdersV2(oid, true) to load all items.
// Read that same callback; never infer a complete order from its seven-item preview.
import {CatalogError,check} from './common.mjs';
const fail=message=>{throw new CatalogError('ORDER_PAGE_CHANGED',message);};
export const nativeStatus=value=>({取消:'已取消',发货中:'发货中(打单拣货)'})[value]||value;

export async function fullOrder(frame,spec,{coid,shop_id,count,payment=false}={},signal){
  check(signal);
  if(!/^[1-9]\d*$/.test(spec.internal||''))fail('整单明细缺少已核验内部订单号');
  const rows=await frame.evaluate(({internal})=>new Promise((resolve,reject)=>{
    if(typeof _ACP!=='function')return reject(Error('原生整单明细读取不可用'));
    const timer=setTimeout(()=>reject(Error('原生整单明细回执超时')),20000);
    _ACP('ReloadOrdersV2',rv=>{clearTimeout(timer);try{
      if(!Array.isArray(rv))throw Error();
      resolve(rv.map(v=>JSON.parse(v).data));
    }catch{reject(Error('原生整单明细结构变化'));}},internal,true);
  }),{internal:spec.internal});
  check(signal);
  const [row]=rows;
  if(rows.length!==1||String(row?.o_id)!==spec.internal||row.so_id!==spec.online||row.shop_name!==spec.shop
      ||coid&&String(row.co_id)!==coid||shop_id&&String(row.shop_id)!==shop_id)
    fail('原生整单明细与本次企业、店铺或原单身份不同');
  if(!Array.isArray(row.items)||!row.items.length||row.overcount===true
      ||Number.isInteger(count)&&count>0&&row.items.length!==count)
    fail('原生整单明细没有完整商品范围');
  const items=row.items.map(r=>({sku:String(r.sku_id||''),qty:String(r.qty),price:String(r.price),amount:String(r.amount),
    item_status:r.item_status||null,refund_status:r.refund_status||null,
    ...(payment?{sales_unit:r.sales_unit||null,per_unit:r.per_unit==null?null:String(r.per_unit)}:{})}));
  if(items.some(r=>!r.sku||!/^\d+(\.\d+)?$/.test(r.qty)||!/^\d+(\.\d+)?$/.test(r.price)||!/^\d+(\.\d+)?$/.test(r.amount))
      ||new Set(items.map(r=>r.sku)).size!==items.length)
    fail('原生整单商品编码、数量或成交价缺失、重复或不明');
  return {internal:spec.internal,online:spec.online,shop:spec.shop,shop_id:String(row.shop_id),coid:String(row.co_id),
    native_status:nativeStatus(row.status),amount:String(row.pay_amount),freight:String(row.freight??0),
    contact_ok:!!spec.contact&&row.shop_buyer_id===spec.contact.buyer,
    items,lines:items.map(r=>[r.sku,r.qty,r.price]),observed:new Date().toISOString(),
    ...(payment?{currency:row.currency||'CNY',detail_count:items.length}: {})};
}
