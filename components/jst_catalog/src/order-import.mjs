import crypto from 'node:crypto';
import {CatalogError, canonical, check, sha} from './common.mjs';
import {activateControl} from './product-export.mjs';
import {LIST_PATH, ordinaryAudit, queryOrder, readNativeOrder, readOrder} from './order-list.mjs';

const IMPORT_PATH = '/app/order/import/import.aspx';
const PREVIEW_PATH = '/app/order/import/import_show.aspx';
const MULTI = 'MultiCommodity';
const SINGLE = 'SingleCommodity';
const same = (a,b) => canonical(a) === canonical(b);
const displayName = value => String(value || '').trim().replace(/\s+/g,' ');
const workbookName = value => displayName(String(value || '').replace(/_x([0-9a-f]{4})_/gi,
  (_,hex)=>String.fromCharCode(Number.parseInt(hex,16))));

function fail(code, message) { throw new CatalogError(code,message); }
function originAllowed(url) {
  try {
    const loc=new URL(url);
    return loc.protocol==='https:' && /(^|\.)erp321\.com$/.test(loc.hostname);
  } catch {
    // Frames can have an empty URL until their native page has loaded.
    return false;
  }
}

async function one(locator, label, code='ORDER_PAGE_CHANGED') {
  const matches=locator.filter({visible:true});
  await matches.first().waitFor({state:'visible',timeout:15000}).catch(()=>{});
  if (await matches.count()!==1) fail(code,'未找到唯一'+label);
  return matches;
}

async function chooseShop(frame, name) {
  const menus=frame.locator('select').filter({has:frame.getByRole('option',{name,exact:true})});
  const select=await one(menus,'店铺选择框','SHOP_AMBIGUOUS');
  const choices=await select.locator('option').evaluateAll((nodes, target)=>
    nodes.filter(n=>n.textContent.trim()===target&&!n.disabled).map(n=>n.value),name);
  if (choices.length!==1||!choices[0]) fail('SHOP_AMBIGUOUS','对应店铺不存在或不唯一');
  await select.selectOption({value:choices[0]});
  if (await select.inputValue()!==choices[0]) fail('SHOP_AMBIGUOUS','原生店铺选择未生效');
  return choices[0];
}

async function chooseImportShop(frame, name) {
  const form=frame.locator('form#form1');
  const widget=form.locator('#shopOrDrpFroms');
  if (!await widget.count()) {
    if (!await form.locator('select').filter({visible:true}).count())
      fail('SHOP_CONTROL_UNAVAILABLE','未识别导入页的店铺下拉框');
    return chooseShop(frame,name);
  }
  const search=await one(widget.locator('input#shopOrDrpFrom'),'店铺搜索框','SHOP_CONTROL_UNAVAILABLE');
  const selection=widget.locator('input#shopOrDrpFrom_v');
  if (await selection.count()!==1)
    fail('SHOP_CONTROL_UNAVAILABLE','导入页缺少唯一的店铺标识字段');
  if (await search.inputValue()) {
    await search.press('Control+a',{timeout:8000});
    await search.press('Backspace',{timeout:8000});
  }
  const list=widget.locator('div._cbb_items');
  if (await list.count()!==1)
    fail('SHOP_CONTROL_UNAVAILABLE','导入页店铺选项容器无法识别');
  if (!await list.isVisible()) await search.click({timeout:8000});
  const labels=(await one(list,'店铺下拉列表','SHOP_CONTROL_UNAVAILABLE'))
    .locator('div._cbb_item > label._cbb_label').filter({visible:true});
  const matches=await labels.evaluateAll((nodes,target)=>nodes
    .filter(node=>node.textContent.trim()===target).map(node=>node.getAttribute('value')),name);
  if (!matches.length) fail('SHOP_NOT_FOUND','导入页未找到全名一致的店铺');
  if (matches.length!==1) fail('SHOP_DUPLICATE','导入页存在多条同名店铺');
  const [id]=matches;
  if (!/^[1-9]\d*$/.test(id||'')) fail('SHOP_CONTROL_UNAVAILABLE','店铺选项缺少可核验的标识');
  const option=labels.and(list.locator(`label._cbb_label[value="${id}"]`));
  if (await option.count()!==1||((await option.textContent({timeout:8000}))||'').trim()!==name)
    fail('SHOP_SELECTION_FAILED','点击前店铺选项已变化');
  try {
    await option.click({timeout:8000});
  } catch {
    fail('SHOP_SELECTION_FAILED','导入页未能选中该店铺');
  }
  // The visible text is searchable. The paired value is what the form submits.
  if (await search.inputValue()!==name||await selection.inputValue()!==id)
    fail('SHOP_SELECTION_FAILED','已选店铺名称或提交标识与目标不一致');
  return id;
}

async function tab(page, name) {
  const tabs=page.getByRole('tab',{name,exact:true}).filter({visible:true});
  if (await tabs.count()!==1) {
    await activateControl(await one(page.locator('a[class^="menuItem"]').filter({hasText:/^订单$/}),'订单菜单'));
    const entries=page.locator('div[class^="title"]').filter({visible:true})
      .filter({has:page.getByText(name,{exact:true})}).or(page.locator('div[class^="title"]').filter({visible:true}).filter({
        hasText:new RegExp('^'+name+'$')}));
    // The captured ERP menu renders two matching labels. The second is the
    // clickable sub-menu in that known structure.
    const count=await entries.count();
    if (count!==1&&count!==2) fail('ORDER_PAGE_CHANGED','未定位原生'+name+'入口');
    await activateControl(entries.nth(count-1));
  }
  const target=await one(tabs,name+'标签');
  if (await target.getAttribute('aria-selected')!=='true') await activateControl(target);
  if (await target.getAttribute('aria-selected')!=='true')
    fail('ORDER_PAGE_CHANGED',name+'标签尚未激活');
}

async function orderFrame(page, signature, allowed, title, timeout=12000) {
  const deadline=Date.now()+timeout;
  while (Date.now()<deadline) {
    const found=[];
    for (const frame of page.frames()){
      if (frame.isDetached()||!allowed(frame.url())) continue;
      try {
        if (await signature(frame)) found.push(frame);
      } catch (error) {
        // The ERP can replace a loading iframe after changing tabs.
        if (!frame.isDetached()) throw error;
      }
    }
    if (found.length===1) return found[0];
    if (found.length>1) fail('ORDER_PAGE_CHANGED','订单页面不唯一');
    await new Promise(resolve=>setTimeout(resolve,200));
  }
  if (process.env.JST_AUTO_ORDER_DIAGNOSTIC==='1')
    console.error('订单页面诊断 '+JSON.stringify({phase:title,paths:page.frames().map(f=>f.url().split(/[?#]/)[0]),
      tabs:await page.getByRole('tab').evaluateAll(nodes=>nodes.map(n=>[n.textContent.trim(),n.getAttribute('aria-selected')]))}));
  fail('ORDER_PAGE_CHANGED','未取得聚水潭'+title+'的可核验控件');
}

async function visible(frame) {
  const element=await frame.frameElement();
  return element.isVisible();
}

async function importForm(frame) {
  const {pathname,searchParams}=new URL(frame.url());
  return pathname===IMPORT_PATH && !searchParams.has('source')
    && await frame.locator('form#form1 input#order[type=file]').count()===1 && await visible(frame);
}

async function importPreview(frame) {
  return new URL(frame.url()).pathname===PREVIEW_PATH
    && await frame.locator('form#form1 input#txt_exception_datas').count()===1 && await visible(frame);
}

function compact(value) {
  return String(value || '').replace(/\s+/g,'');
}

function contactMatches(spec, facts) {
  const {buyer,recipient,phone,province,city,district,address} = spec.contact;
  const location = compact(province+city+district+address);
  const name = compact(recipient);
  const tel = compact(phone);
  const displayed = compact(facts.address).replace(/[\[\]]/g,'');
  return facts.buyer===buyer && [
    name+tel+location,tel+name+location,location+name+tel,
    location+tel+name,name+location+tel,tel+location+name,
  ].includes(displayed);
}

function issueCode(message) {
  if (/缺货|库存不足/.test(message)) return 'STOCK_SHORT';
  if (/手机号.*(?:格式|错误|无效)/.test(message)) return 'PHONE_INVALID';
  if (/禁用|停采/.test(message)) return 'PRODUCT_DISABLED';
  if (/商品.*不存在|商品资料|未匹配/.test(message)) return 'PRODUCT_MISSING';
  if (/地址|地区/.test(message)) return 'ADDRESS_INVALID';
  if (/金额|价格|单价/.test(message)) return 'PRICE_DIFF';
  return 'OTHER';
}

function quantityText(value) {
  if (!/^\d+(?:\.\d+)?$/.test(value)) fail('PREVIEW_CHANGED','平台预览数量无法识别');
  return value.replace(/^0+(?=\d)/,'').replace(/(\.\d*?)0+$/,'$1').replace(/\.$/,'');
}

async function readPreview(form, spec) {
  const facts = await form.evaluate(f=>{
    const text = element => (element?.textContent || '').trim();
    const rows = Array.from(f.querySelectorAll('tr'));
    const orders = rows.filter(r=>r.cells.length===20 && r.cells[2].tagName==='TD'
      && text(r.cells[2]) && text(r.cells[2])!=='线上订单号');
    const exceptions = rows.filter(r=>r.cells.length===4 && /^BH[A-F0-9]{24}$/.test(text(r.cells[2])))
      .map(r=>text(r.cells[3]));
    const cells = orders.length===1 ? orders[0].cells : [];
    return {
      action:new URL(f.action).pathname,count:orders.length,online:text(cells[2]),
      buyer:text(cells[5]),status:text(cells[7]),address:text(cells[11]),courier:text(cells[12]),
      items:cells[13] ? Array.from(cells[13].querySelectorAll('span'),text) : [],
      invalidItems:!!cells[13]?.querySelector('.no_in_itemsku'),
      exceptionData:f.querySelector('#txt_exception_datas')?.value,exceptions,
      amounts:text(cells[17]).match(/[￥¥]\s*[\d,]+(?:\.\d+)?/g)||[],
    };
  });
  const issues = new Set(facts.exceptions.map(issueCode));
  if (facts.invalidItems) issues.add('PRODUCT_MISSING');
  if (typeof facts.exceptionData!=='string') fail('ORDER_PAGE_CHANGED','预览缺少异常校验字段');
  if (facts.exceptionData && !issues.size) issues.add('OTHER');
  const evidence = {online:spec.online,shop:spec.shop,lines:[],amount:null,issues:[...issues],contact_ok:false};
  if (issues.has('PHONE_INVALID'))
    fail('PREVIEW_ISSUES','聚水潭提示手机号不符合格式，请核对本仓买家和收货资料');
  if (issues.size) {
    if (process.env.JST_AUTO_ORDER_DIAGNOSTIC==='1') {
      const summarize = value => {
        let text=String(value||'');
        for (const secret of Object.values(spec.contact).filter(Boolean).sort((a,b)=>b.length-a.length))
          text=text.replaceAll(secret,'[已隐藏]');
        return text.replace(/\d{7,}/g,'[已隐藏]').slice(0,240);
      };
      let format='text';
      try {
        const value=JSON.parse(facts.exceptionData);
        format=Array.isArray(value) ? 'array:'+value.length : value===null ? 'null' : typeof value;
      } catch {}
      console.error('订单页面诊断 '+JSON.stringify({phase:'preview-issues',issues:[...issues],
        detail:facts.exceptions.slice(0,5).map(summarize),rows:facts.count,
        renderedItems:facts.items.length,invalidItems:facts.invalidItems,hiddenLength:facts.exceptionData.length,format}));
    }
    return evidence;
  }
  const single=spec.import_format===SINGLE;
  if (single && facts.count!==1)
    fail('PREVIEW_CHANGED','单商品格式预览识别到 '+facts.count+' 张订单，整单必须只生成一张，未提交');
  if (facts.action!==PREVIEW_PATH || facts.count!==1 || facts.online!==spec.online
      || facts.status!=='已付款待审核' || facts.courier!=='现场取货')
    fail('PREVIEW_CHANGED','预览订单号、订单数、付款状态或取货方式不符');
  if (facts.items.length!==spec.lines.length)
    fail('PREVIEW_CHANGED','平台预览商品款数与原生成单不同');
  const observed = new Map();
  for (const raw of facts.items) {
    const item=displayName(raw).match(/^(.*) \* ([\d,]+(?:\.\d+)?)$/);
    if (!item) fail('PREVIEW_CHANGED','平台预览商品数量格式无法识别');
    const key=JSON.stringify([item[1],quantityText(item[2].replace(/,/g,''))]);
    observed.set(key,(observed.get(key)||0)+1);
  }
  // The native preview may sort rows. Match the displayed names and quantities
  // as a multiset, with exact SKU verification on the resulting order.
  const missing=[];
  const names=new Set();
  for (const [sku,qty] of spec.lines) {
    const name=workbookName(spec.preview_names?.[sku]);
    if (single && name && names.has(name))
      fail('PREVIEW_CHANGED','整单含同名商品，平台预览无法按名称区分编码，未提交');
    if (single) names.add(name);
    const amount=quantityText(qty);
    const key=JSON.stringify([name,amount]);
    const count=observed.get(key)||0;
    if (!name || !count) missing.push([sku,name,amount]);
    else if (count===1) observed.delete(key);
    else observed.set(key,count-1);
    evidence.lines.push([sku,amount]);
  }
  if (missing.length) {
    if (process.env.JST_AUTO_ORDER_DIAGNOSTIC==='1')
      console.error('订单页面诊断 '+JSON.stringify({phase:'preview-quantity',missing:missing.length,
        sample:missing.slice(0,3),unexpected:[...observed.keys()].slice(0,3).map(key=>JSON.parse(key))}));
    fail('PREVIEW_CHANGED','平台预览有'+missing.length+'款商品或数量与原生成单不同');
  }
  evidence.amount = facts.amounts[0]?.replace(/[￥¥,\s]/g,'') ?? null;
  if (single) {
    if (evidence.amount===null) fail('PREVIEW_CHANGED','整单预览未返回可核验的金额，未提交');
    evidence.import_format=SINGLE;
    evidence.order_count=facts.count;
  }
  evidence.contact_ok = contactMatches(spec,facts);
  return evidence;
}

export class OrderPages {
  constructor({allowed=originAllowed}={}) {
    this.allowed=allowed;
  }

  async openImport(page, spec) {
    await tab(page,'导入订单');
    let frame=await orderFrame(page,async f=>await importForm(f)||await importPreview(f),this.allowed,'导入表单');
    if (new URL(frame.url()).pathname===PREVIEW_PATH) {
      // The portal can leave an old, unsubmitted preview in this tab.
      // Return to a fresh form with GET. Never invoke the preview confirmation.
      const target=new URL(IMPORT_PATH,frame.url());
      if (!this.allowed(target.href)) fail('ORDER_PAGE_CHANGED','导入表单地址无法核验');
      await frame.goto(target.href,{waitUntil:'domcontentloaded',timeout:12000});
      frame=await orderFrame(page,importForm,this.allowed,'导入表单');
    }
    const mode=await one(frame.locator('#importbyshop'),'按店铺导入选项');
    if (!await mode.isChecked()) await mode.check({timeout:8000});
    if (!await mode.isChecked()) fail('ORDER_PAGE_CHANGED','未选中按店铺导入');
    const shop_id=await chooseImportShop(frame,spec.shop);
    return {frame,shop_id};
  }

  async orderList(page) {
    await tab(page,'订单');
    return orderFrame(page,async frame=>{
      if (new URL(frame.url()).pathname!==LIST_PATH || !await visible(frame)) return false;
      const filterFrame=frame.locator('#s_filter_frame');
      if (await filterFrame.count()!==1) return false;
      const filters=filterFrame.contentFrame();
      return await filters.locator('input#so_id').count()===1
        && await filters.locator('span.btn_search[onclick="FullSearch()"]').count()===1;
    },this.allowed,'订单列表',30000);
  }

  async lookup(page, spec, signal, pendingOnly, nativeOnly=false) {
    const shop_id = spec.binding?.shop_id;
    if (!/^[1-9]\d*$/.test(shop_id||'')) fail('ACCOUNT_CHANGED','原任务未保存已核对的店铺标识');
    const frame = await this.orderList(page);
    return {...await readOrder(page,frame,spec,{pendingOnly,nativeOnly},signal),shop_id};
  }

  async paymentDetail(page,spec,signal) {
    if(!/^[1-9]\d*$/.test(spec.internal||'')||!/^[1-9]\d*$/.test(spec.binding?.shop_id||''))
      fail('ACCOUNT_CHANGED','缺少已绑定的店铺和内部订单号');
    return readNativeOrder(page,await this.orderList(page),spec,{payment:true},signal);
  }

  async preview(page,spec,signal) {
    const list=await this.orderList(page);
    if (await queryOrder(page,list,spec,null,signal)) fail('ORDER_EXISTS','该店铺与线上单号已存在，需核对原单');
    check(signal);
    const {frame,shop_id}=await this.openImport(page,spec);
    if (spec.binding && shop_id!==spec.binding.shop_id) fail('ACCOUNT_CHANGED','原任务店铺标识已变化');
    const file=Buffer.from(spec.file||'','base64');
    if (!file.length||file.length>3000000||sha(file)!==spec.sha) fail('ORDER_FILE','制表文件摘要或大小不符');
    const format=spec.import_format??MULTI;
    if (format!==MULTI && format!==SINGLE) fail('ORDER_FILE','聚水潭导入格式无法核验');
    const options=['#importbyshop','#'+format,'#sku_price','#default_pay_date'];
    for (const selector of options.slice(1)) await frame.locator(selector).check({timeout:8000});
    const filename=spec.online+'.xls';
    await frame.locator('input#order[type=file]').setInputFiles({
      name:filename,mimeType:'application/vnd.ms-excel',buffer:file});
    const pane=frame.locator('form#form1');
    const button=pane.locator('span#btnImport').filter({hasText:/^确认导入订单$/});
    const navigation=frame.waitForURL(url=>this.allowed(url.href)&&url.pathname===PREVIEW_PATH,
      {waitUntil:'domcontentloaded',timeout:35000});
    navigation.catch(()=>{});
    await activateControl(button,{beforeDispatch:async()=>{
      const selected=await pane.evaluate((f,opts)=>{
        const files=f.querySelector('input#order')?.files;
        return {shop:f.querySelector('#shopOrDrpFrom')?.value,id:f.querySelector('#shopOrDrpFrom_v')?.value,
          checked:opts.every(v=>f.querySelector(v)?.checked),count:files?.length,
          name:files?.[0]?.name,size:files?.[0]?.size,path:new URL(f.action).pathname,method:f.method,
          format:f.querySelector('input[name="import_formart"]:checked')?.value};
      },options);
      if (selected.shop!==spec.shop || selected.id!==shop_id || !selected.checked || selected.count!==1
          || selected.name!==filename || selected.size!==file.length || selected.path!==PREVIEW_PATH || selected.method!=='post'
          || (format===SINGLE&&selected.format!==SINGLE))
        fail('PREVIEW_CHANGED','聚水潭预览前文件、店铺或导入选项已变化');
      check(signal);
    }});
    await navigation.catch(()=>fail('ORDER_PAGE_CHANGED','聚水潭没有返回当前文件的预览'));
    const preview=await one(pane,'聚水潭订单预览');
    const result=await readPreview(preview,spec);
    return {...result,shop_id,preview,page};
  }

  async finishImport(staged, spec, signal) {
    const {preview,page}=staged;
    const controls=preview.locator('span.btnImport').filter({hasText:/^确定导入$/,visible:true});
    const actions=await controls.evaluateAll(nodes=>nodes.map(n=>n.getAttribute('onclick')));
    // The native template duplicates the same primary control above and below the single preview.
    if (actions.length!==2 || actions.some(action=>action!=='Confirm()'))
      fail('ORDER_PAGE_CHANGED','未找到可核验的单张订单最终导入控件');
    await activateControl(controls.first(),{
      beforeDispatch:async()=>{
        const {amount,...scope}=await readPreview(preview,spec);
        if (!same(scope,staged.evidence)||(spec.import_format===SINGLE&&amount!==staged.amount))
          fail('PREVIEW_CHANGED','最终导入前预览内容已变化');
        check(signal);
      }});
    const receipt=page.frameLocator('#epaasDialogFrame');
    await one(receipt.getByText('成功条数：1，失败条数：0',{exact:true}),'单张导入成功回执');
    await activateControl(await one(receipt.getByRole('link',{name:'关闭',exact:true}),'导入回执的关闭入口'));
  }

  async finishAudit(staged, spec, signal) {
    await ordinaryAudit(staged,spec,signal);
  }
}

export class OrderFlow {
  constructor(driver,{pages=new OrderPages()}={}) {
    this.driver=driver;this.pages=pages;
    this.permits=new Map();
  }
  async run(action, input, account, signal) {
    check(signal);
    if (action==='import'||action==='audit'){
      const hold=this.permits.get(input.permit);
      this.permits.delete(input.permit);
      if (!hold||hold.action!==action||hold.spec.online!==input.online||hold.expires<Date.now()
          ||(action==='audit'&&hold.spec.internal!==input.internal))
        fail('PREVIEW_CHANGED','原预览或普通审核许可已失效');
      if (hold.spec.warehouse!==account?.warehouse||hold.binding.coid!==account.expectedCoid||
          hold.binding.uid!==account.expectedUid||hold.binding.company!==account.expectedCompany
          ||hold.binding.version!==account.bindingVersion)
        fail('ACCOUNT_CHANGED','聚水潭仓账号绑定已变化');
      if (action==='import') await this.pages.finishImport(hold.staged,hold.spec,signal);
      else await this.pages.finishAudit(hold.staged,hold.spec,signal);
      return {dispatched:true};
    }
    this.permits.clear();
    const spec=input.spec;
    if (!spec||!/^BH[A-F0-9]{24}$/.test(spec.online||'')||spec.warehouse!==account?.warehouse||
        typeof spec.shop!=='string'||!spec.shop.trim()) fail('ACCOUNT_UNVERIFIED','缺少原订单和已核验仓账号');
    const page=await this.driver.page(account,signal,{retainedOnly:action==='payment_detail'&&input.retained_only===true});
    const {scope}=await this.driver.query(page,account);
    if (scope.coid!==account.expectedCoid||scope.uid!==account.expectedUid||scope.company!==account.expectedCompany)
      fail('ACCOUNT_CHANGED','浏览器企业或用户身份与原仓账号不同');
    check(signal);
    const scoped={warehouse:spec.warehouse,shop:spec.shop,company:scope.company,
      coid:scope.coid,uid:scope.uid,version:account.bindingVersion};
    if (action==='probe') {
      const {shop_id}=await this.pages.openImport(page,spec);
      return {binding:{...scoped,shop_id}};
    }
    if(action==='payment_detail') {
      const binding={...scoped,shop_id:spec.binding?.shop_id};
      if(!same(binding,spec.binding)) fail('ACCOUNT_CHANGED','核验店铺与原付款资料的绑定不同');
      const {frame,row,native,buyer,lines,...facts}=await this.pages.paymentDetail(page,spec,signal);
      return {...facts,binding,online:spec.online,internal:spec.internal};
    }
    const observed=['lookup','stage_audit'].includes(action)
      ? await this.pages.lookup(page,spec,signal,action==='stage_audit')
      : await this.pages.preview(page,spec,signal);
    check(signal);
    const {frame,row,detail,preview,page:previewPage,shop_id,buyer,...facts}=observed;
    const binding={...scoped,shop_id};
    if (!/^[1-9]\d*$/.test(shop_id||'')||spec.binding&&!same(binding,spec.binding))
      fail('ACCOUNT_CHANGED','核验店铺与本次目标或原任务不同');
    if (action==='lookup'||action==='preview') return {...facts,binding};
    if (action==='stage_audit'&&(!facts.found||facts.status!=='awaiting'||facts.internal!==spec.internal))
      fail('PREVIEW_CHANGED','原订单当前不处于可普通审核状态');
    const permit=crypto.randomUUID();
    const staged={frame,row,detail,preview,page:previewPage,internal:facts.internal,evidence:{
      online:facts.online,shop:facts.shop,lines:facts.lines,
      issues:facts.issues,contact_ok:facts.contact_ok}};
    if (action==='stage_import'&&spec.import_format===SINGLE) {
      if (facts.order_count!==1||facts.import_format!==SINGLE||facts.amount==null)
        fail('PREVIEW_CHANGED','单商品格式缺少一张整单的预览凭据，未提交');
      staged.amount=facts.amount;
      staged.evidence.order_count=facts.order_count;
      staged.evidence.import_format=facts.import_format;
    }
    this.permits.set(permit,{action:action==='stage_import'?'import':'audit',spec,binding,staged,expires:Date.now()+45000});
    return {...facts,binding,permit};
  }
}
