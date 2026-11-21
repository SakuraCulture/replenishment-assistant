import {CatalogError, check} from './common.mjs';
import {activateControl} from './product-export.mjs';
import {buyerEvidence,canReadPayment,checkBuyer,fullOrder,nativeStatus} from './order-facts.mjs';

export const LIST_PATH = '/app/order/order/list.aspx';
const digits = /^[1-9]\d*$/;
const pagerCountPattern = /总共\s*\d+\s*页\s*(\d+)\s*条记录|共\s*(\d+)\s*条\s*\d+\s*\/\s*\d+\s*页/.source;
const statuses = {
  waitconfirm: ['awaiting', '已付款待审核'],
  waitfconfirm: ['audited', '已客审待财审'],
  delivering: ['review', '发货中(打单拣货)'],
  sent: ['review', '已发货'],
  question: ['exception', '异常'],
  waitoutersent: ['review', '等供销商|外仓发货'],
  waitpay: ['review', '待付款'],
  cancelled: ['review', '已取消'],
  merged: ['review', '被合并'],
  split: ['review', '被拆分'],
};
const fail = message => { throw new CatalogError('ORDER_PAGE_CHANGED', message); };

export function orderQueryMatches(request, frameUrl, online, status) {
  const expected = new URL(frameUrl);
  const actual = new URL(request.url());
  if (request.method() !== 'POST' || actual.origin !== expected.origin || actual.pathname !== LIST_PATH) return false;
  try {
    const form = new URLSearchParams(request.postData());
    const callback = JSON.parse(form.get('__CALLBACKPARAM'));
    if (form.get('__CALLBACKID') !== 'JTable1' || callback.Method !== 'LoadDataToJSON') return false;
    const filters = JSON.parse(callback.Args[1]);
    return Array.isArray(filters) && filters.length === (status ? 2 : 1)
      && filters.some(q => q.k === 'so_id' && q.v === online && q.c === '@=')
      && (!status || filters.some(q => q.k === 'status' && q.v === status && q.c === '@='));
  } catch {
    return false;
  }
}

async function readReply(response) {
  if (!response.ok()) fail('订单精确查询未获成功响应');
  try {
    const body = await response.text();
    const separator = body.indexOf('|');
    if (separator < 1 || !/^\d+$/.test(body.slice(0, separator))) throw Error();
    const reply = JSON.parse(body.slice(separator + 1));
    const result = JSON.parse(reply.ReturnValue);
    if (reply.IsSuccess !== true || reply.GotoLogin || !Array.isArray(result.datas) || !result.dp
        || result.dp.PageIndex !== 1 || result.dp.IsLast !== true) throw Error();
    return result.datas;
  } catch {
    fail('订单精确查询没有可核验的完整结果');
  }
}

async function orderControl(control, label, options) {
  try {
    await activateControl(control,options);
  } catch (error) {
    if (error.code==='CONTROL_NOT_READY') fail(label+'尚未就绪');
    if (error.code==='CONTROL_AMBIGUOUS') fail(label+'不唯一');
    throw error;
  }
}

async function checkBox(control, label, beforeDispatch, visibleLabel) {
  if (!await control.isChecked({timeout:8000})) {
    const target = visibleLabel && !await control.isVisible() ? visibleLabel : control;
    await orderControl(target,label,{timeout:8000,beforeDispatch});
    if (!await control.isChecked({timeout:8000})) fail(label+'未勾选成功');
  }
}

export async function queryOrder(page, frame, spec, status, signal) {
  check(signal);
  const filters = frame.frameLocator('#s_filter_frame');
  const online = filters.locator('input#so_id');
  const search = filters.locator('span.btn_search[onclick="FullSearch()"]').filter({hasText:/^组合查询$/});
  await orderControl(filters.locator('span[onclick="ClearSearch()"]').filter({hasText:/^清空$/}),'订单筛选的清空按钮');
  await online.fill(spec.online, {timeout:8000}).catch(()=>fail('线上订单号输入框尚未就绪'));
  if (status) {
    const id = 'status_'+status;
    await checkBox(filters.locator('input#'+id),'订单状态筛选（'+statuses[status][1]+'）',undefined,
      filters.locator(`label[for="${id}"]`).filter({visible:true}));
  }
  const pending = page.waitForResponse(r => orderQueryMatches(r.request(), frame.url(), spec.online, status), {timeout:25000});
  pending.catch(()=>{});
  await orderControl(search,'订单组合查询按钮',{beforeDispatch:async()=>{
    const checked = await filters.locator('input[id^="status_"]:checked').evaluateAll(nodes => nodes.map(n => n.id));
    if (await online.inputValue() !== spec.online || checked.join() !== (status ? 'status_'+status : ''))
      fail('原线上单号或状态查询条件已变化');
    check(signal);
  }});
  const received = await readReply(await pending.catch(()=>fail('未收到本次原线上单号的查询响应')));
  const total = received.length;
  if (total > 1) fail('线上单号对应多条订单，待核对');
  await frame.waitForFunction(({online,total,pagerCountPattern})=>{
    const text = e => (e?.innerText||'').split('\n')[0].trim();
    const rows = [...document.querySelectorAll('._jt_row')];
    const bar = document.querySelector('#_jt_pagebar')?.innerText || '';
    const count = bar.match(new RegExp(pagerCountPattern));
    return !!count && Number(count[1]??count[2])===total && rows.length===total
      && (!total || text(rows[0].querySelector('._jt_cell_so_id'))===online);
  }, {online:spec.online,total,pagerCountPattern}, {timeout:10000,polling:100}).catch(()=>fail('原订单列表未刷新到唯一精确结果'));
  check(signal);
  if (!total) return null;
  const row = frame.locator('._jt_row');
  const identity = await row.evaluate(r=>{
    const text = e => (e?.innerText||'').split('\n')[0].trim();
    return {
      internal:text(r.querySelector('._jt_cell_o_id .o_id')),
      online:text(r.querySelector('._jt_cell_so_id')),
      shop:(r.querySelector('._jt_cell_shop_buyer_id .shopname')?.textContent||'').trim(),
      buyer:text(r.querySelector('._jt_cell_shop_buyer_id > span')),
      amount:text(r.querySelector('._jt_cell_pay_amount')),
    };
  });
  if (!digits.test(identity.internal)) fail('查回的订单缺少可核验的内部单号');
  if (spec.internal && spec.internal !== identity.internal) fail('查回的内部单号与原单不同');
  if (identity.online !== spec.online) fail('查回的线上订单号与原单不同');
  const value=received[0];
  const native=value?.o_id&&value?.so_id?{
    internal:String(value.o_id),online:value.so_id,shop:value.shop_name,shop_id:String(value.shop_id),
    coid:String(value.co_id),buyer:value.shop_buyer_id,status:value.status,count:value.item_actual_count,quantity:value.item_actual_qty}:null;
  // The listing can abbreviate the display label. The matching native row
  // may supply the full name only with the same frozen company and shop IDs.
  const bound=native&&native.internal===identity.internal&&native.online===spec.online&&native.shop===spec.shop
    &&native.coid===spec.binding?.coid&&native.shop_id===spec.binding?.shop_id;
  if (identity.shop!==spec.shop&&!bound) fail('查回的店铺名称与原单不同');
  return {row,...identity,shop:bound?native.shop:identity.shop,native};
}

async function locateNativeOrder(page,frame,spec,signal){
  const found=await queryOrder(page,frame,spec,null,signal);
  if(!found)return null;
  const identity=found.native;
  if(!identity||identity.internal!==found.internal||identity.online!==spec.online||identity.shop!==spec.shop
      ||identity.coid!==spec.binding?.coid||identity.shop_id!==spec.binding?.shop_id)
    fail('查询的企业、店铺或原单与已保存的关联不同');
  const status=nativeStatus(identity.status);
  if(!Object.values(statuses).some(([,label])=>label===status)) fail('原单当前状态尚不能核实');
  return {...found,found:true,native_status:status};
}

export async function readImportResult(page,frame,spec,signal){
  const found=await locateNativeOrder(page,frame,spec,signal);
  if(!found)return {found:false,shop_id:spec.binding.shop_id};
  const {internal,online,shop,native,native_status}=found;
  const [status]=Object.values(statuses).find(([,label])=>label===native_status);
  const result={found:true,internal,online,shop,shop_id:native.shop_id,status,native_status};
  const buyer=checkBuyer(native.buyer,spec.contact?.buyer);
  if (buyer!=='unavailable') return {...result,...buyerEvidence(buyer)};
  try {
    // Read the exact order's native detail with its verified internal ID. The
    // obscured list text cannot establish a buyer match or a mismatch.
    const facts=await fullOrder(frame,{...spec,internal},{coid:native.coid,shop_id:native.shop_id,count:Number(native.count)},signal);
    if (facts.native_status!==native_status) fail('原单状态已变化，请继续核对原单');
    return {...result,...buyerEvidence(facts.buyer_check)};
  } catch (error) {
    check(signal);
    return {...result,...buyerEvidence('unavailable'),
      detail_error:error.code==='ORDER_PAGE_CHANGED'?error.message:'暂未取得可核验的原单明细'};
  }
}

export async function readNativeOrder(page,frame,spec,{payment=false}={},signal){
  const observed=await locateNativeOrder(page,frame,spec,signal);
  if(!observed)return {found:false};
  const {native:identity,native_status:status}=observed;
  const listBuyer=checkBuyer(identity.buyer,spec.contact?.buyer);
  const result=payment?{...observed,...buyerEvidence(listBuyer)}:observed;
  if ((payment&&listBuyer==='mismatch')||!(payment?canReadPayment(status):status==='已发货')) return result;
  try {
    const count=Number(identity.count);
    const facts=await fullOrder(frame,spec,{coid:identity.coid,shop_id:identity.shop_id,count,payment},signal);
    if (!(payment?canReadPayment(facts.native_status):facts.native_status===status)) fail('原单状态已变化，请继续核对原单');
    if (!payment) {
      if (!facts.contact_ok) fail('原单状态或买家身份已变化');
      return {...observed,...facts};
    }
    // List and detail refer to the same verified native order. A complete
    // conflicting account overrides a match from the other response.
    const buyer=(facts.buyer_check==='mismatch'||listBuyer==='unavailable')?facts.buyer_check:listBuyer;
    return {...result,...facts,...buyerEvidence(buyer)};
  } catch(error) {
    check(signal);
    if(!payment) throw error;
    return {...result,detail_error:error.code==='ORDER_PAGE_CHANGED'?error.message:'原单状态已查回，整单明细暂未完整读回'};
  }
}

export async function readOrder(page, frame, spec, {pendingOnly=false,nativeOnly=false}={}, signal) {
  if(nativeOnly) return readNativeOrder(page,frame,spec,{},signal);
  if (!pendingOnly && !await queryOrder(page,frame,spec,null,signal)) return {found:false};
  const keys = Object.keys(statuses);
  const auditFollowUp = ['AUDIT_UNKNOWN','AUDITED','REVIEW'].includes(spec.phase);
  const candidates = auditFollowUp ? ['waitfconfirm', ...keys.filter(key => key !== 'waitfconfirm')] : keys;
  for (const key of pendingOnly ? ['waitconfirm'] : candidates) {
    let order = await queryOrder(page,frame,spec,key,signal);
    if (!order) continue;
    const details = frame.locator('.full_item_table').filter({visible:true});
    let lines;
    if (order.native || spec.import_format==='SingleCommodity') {
      // Both import formats can read the complete order without opening the popover.
      const identity=order.native;
      if (!identity||identity.internal!==order.internal||identity.online!==spec.online||identity.shop!==spec.shop
          ||identity.coid!==spec.binding?.coid||identity.shop_id!==spec.binding?.shop_id)
        fail('查回的整单企业、店铺或订单标识与原任务不同');
      const full=await fullOrder(frame,{...spec,internal:order.internal},{
        coid:identity.coid,shop_id:identity.shop_id,count:spec.lines?.length??Number(identity.count)},signal);
      if (!full.contact_ok||full.native_status!==statuses[key][1])
        fail('查回的整单买家或状态与原任务不同');
      lines=full.lines;
    } else {
      const cells = order.row.locator('._jt_cell_items');
      await orderControl(cells,'原单的商品明细入口');
      await details.waitFor({timeout:10000}).catch(()=>fail('无法读取原单完整商品明细'));
      lines = await details.locator('.jt_item_row').evaluateAll(rows=>rows.map(row=>{
        const txt = s => row.querySelector(s)?.innerText.trim() || '';
        return [txt('.sku_id'),txt('[title="数量"]').replace(/^[x×]\s*/i,''),txt('[title="单价"]').replace(/^[￥¥]\s*/,'')];
      }));
    }
    if (!lines.length) fail('查回的订单缺少商品明细');
    if (pendingOnly) {
      // Some ERP layouts leave the product popover open when the pager is
      // clicked. Reload only this verified list, then select the exact order
      // again before granting the ordinary-audit permit.
      const original = new URL(frame.url());
      check(signal);
      await frame.goto(original.href,{waitUntil:'domcontentloaded',timeout:30000})
        .catch(()=>fail('普通审核前未能重新打开订单列表'));
      const current = new URL(frame.url());
      if (current.origin!==original.origin || current.pathname!==LIST_PATH || await details.isVisible())
        fail('普通审核前订单列表尚未回到可操作状态');
      const refreshed = await queryOrder(page,frame,spec,key,signal);
      if (!refreshed || refreshed.internal!==order.internal || refreshed.buyer!==order.buyer)
        fail('普通审核前原单身份或待审核状态已变化');
      order = refreshed;
    }
    const [status,native_status] = statuses[key];
    return {...order,found:true,frame,detail:details,lines,status,native_status,issues:[],
      contact_ok:order.buyer===spec.contact.buyer};
  }
  if (pendingOnly) fail('原单已离开已付款待审核状态');
  fail('已查到原单，平台状态仍待核');
}

export async function ordinaryAudit(staged, spec, signal) {
  const {frame,row,detail,internal} = staged;
  if (await detail.isVisible()) fail('普通审核前原单商品详情已重新打开，请继续核对原单');
  const target = row.locator('._jt_cell_checked input[type=checkbox]');
  const assertScope = async (wantChecked)=>{
    const snapshot = await frame.locator('body').evaluate((b,pagerCountPattern)=>{
      const text = e => (e?.innerText||'').split('\n')[0].trim();
      const rs = [...b.querySelectorAll('._jt_row')];
      const count = b.querySelector('#_jt_pagebar')?.innerText.match(new RegExp(pagerCountPattern));
      return {rows:rs.map(r=>[text(r.querySelector('._jt_cell_o_id .o_id')),text(r.querySelector('._jt_cell_so_id')),
        text(r.querySelector('.shopname')),!!r.querySelector('._jt_cell_checked input:checked')]),
        header:!!b.querySelector('#_jt_h_checked:checked'),
        total:count?.[1]??count?.[2]};
    },pagerCountPattern);
    if (snapshot.rows.length!==1 || snapshot.total!=='1' || snapshot.header && !wantChecked
        || JSON.stringify(snapshot.rows[0])!==JSON.stringify([internal,spec.online,spec.shop,wantChecked]))
      fail('普通审核前，选中范围与原订单不一致');
    check(signal);
  };
  await assertScope(false);
  await checkBox(target,'原订单',()=>assertScope(false));
  const button = frame.locator('#ToolConfirm_Btn > span.ding_db_txt').filter({hasText:/^审核$/});
  await orderControl(button,'普通审核按钮',{beforeDispatch:()=>assertScope(true)});
}
