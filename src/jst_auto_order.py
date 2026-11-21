"""Prepare, submit and reconcile exact, warehouse-scoped 聚水潭 order tasks."""
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import base64
import re
import time

import jst_auto_order_store as store
import jst_auto_order_template as template
import jst_order_confirmation as confirmation
import jst_order_store as allocations
import lifecycle_store
import payment_shop_names
import work_repository as wr
from catalog_sync_client import ComponentError
from export_progress import RowProgress
from jst_allocation import number
from jst_delivery_profiles import save_profile
from jst_order_guards import require_generation, require_generation_basis, saved_execution_reason, upstream_stamp
from payment_references import HEAD_KEYS, reference_key
from runtime_support import checkpoint, Cancelled
from sqlite_write import begin, commit


class Check(ValueError):
    """Business message without recipient or browser-session data."""


class PlatformCheck(Check):
    pass


ORDINARY_AUDIT_DONE = confirmation.AUDITED
SHIPPING_PLATFORM_STATES = confirmation.SHIPPING
REVIEW_PLATFORM_STATES = confirmation.REVERSED


def show_name(head):
    return ' / '.join((head.get('name') or head['store'],head['warehouse'],head['number']))


def load_profiles(path, heads, progress=lambda _:None, cancel=None):
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        return {(h['store'],h['warehouse']):store.profile(db,h['store'],h['warehouse']) for h in heads}


def _shop(db, h):
    row=None
    if payment_shop_names.has(db,'lifecycle_mappings'):
        row=db.execute("SELECT payload FROM lifecycle_mappings WHERE stage='payment' AND name=?",
                       (payment_shop_names.key(h['store'],h['warehouse']),)).fetchone()
    value=wr.loads(row[0]) if row else {}
    if (value.get('store'),value.get('warehouse'))!=(h['store'],h['warehouse']) or not value.get('name') or not value.get('version'):
        raise Check('缺少该仓聚水潭店铺名称，请在“聚水潭店铺名称维护”中补齐')
    return {k:value[k] for k in ('name','version')}


def _online(h):
    # The default for a new task must be stable through previews and reloads.
    return 'BH'+sha256(('replenishment-jst-v1:'+h['number']).encode('utf-8')).hexdigest()[:24].upper()


def _generation_proof(db, generation, digest):
    require_generation(db,generation,digest)
    require_generation_basis(db,generation)
    products=generation.get('signed_products',generation['basis']['products'])
    return {'digest':digest,'bid':generation['bid'],'batch_id':generation.get('batch_id'),
            'legacy_products':generation.get('signed_products',{}),
            'stamp':upstream_stamp(db,generation['head'],products)}


def _require_plan(db, p):
    proof=p['generation']
    require_generation(db,{'head':p['head'],'gid':p['gid'],**proof},proof['digest'])
    if upstream_stamp(db,p['head'],proof['legacy_products'])!=proof['stamp']:
        raise Check('门店回传或运营签发已更新，请按有效生成单重新制表')
    if _shop(db,p['head'])!=p['shop'] or store.profile(db,p['head']['store'],p['head']['warehouse'])!=p['profile']:
        raise Check('店铺对应或收货资料已更新，请重新制表并预览')
    reason=saved_execution_reason(db,p['head'])
    if reason:
        raise Check(reason+'，请核对原订单')


def selection(records, chosen):
    result=[r for r in records if (r['head']['oid'],r['head']['warehouse']) in chosen]
    if not result:
        raise Check('请勾选要制表的仓单')
    for r in result:
        if not r['gid'] or r['state']!='ready':
            raise Check(show_name(r['head'])+'：需要当前有效的已保存生成单')
    return [{'gid':r['gid'],'head':r['head']} for r in result]


def prepare(path, selected, progress=lambda _:None, cancel=None, *, order_phone=None):
    """Freeze one portable native file per valid local warehouse order."""
    if order_phone is not None and (len(selected)!=1 or not re.fullmatch(r'[0-9]{11}',order_phone)):
        raise Check('单次测试号码只可用于一张仓单，需填11位数字')
    ready=[];failed=[]
    for i, row in enumerate(selected,1):
        checkpoint(cancel)
        h=row['head']; stats={}
        progress(RowProgress('制表 '+show_name(h),i,len(selected)))
        try:
            with closing(wr.read_connection(path)) as db:
                db.execute('BEGIN')
                generation,digest=allocations.read_generation(db,row['gid'])
                h=generation['head']
                if any(h[k]!=row['head'][k] for k in HEAD_KEYS):
                    raise Check('所选仓单已变化，请刷新结果页')
                old = store.read_warehouse_order(db,h)
                if old and old['stage'] in store.RELEASED:
                    ready.append(old['online'])
                    continue
                online = old['online'] if old else _online(h)
                proof=_generation_proof(db,generation,digest)
                reason=saved_execution_reason(db,h)
                if reason:
                    raise Check(reason+'，请核对原订单')
                shop=_shop(db,h)
                profile=store.profile(db,h['store'],h['warehouse'])
            contacts=(profile or {}).get('values',{})
            if order_phone is not None:
                contacts={**contacts,'phone':order_phone}
            missing=[title for k,(_,title) in template.CONTACT.items() if not contacts.get(k)]
            if missing:
                raise Check('缺少'+'、'.join(missing))
            lines,stats=template.order_lines(generation['rows'],cancel)
            import_format=template.SINGLE if stats['sku_cell_chars']>template.MAX_CELL else template.MULTI
            workbook,template_sha=template.make_file(contacts,online,lines,stats,import_format=import_format,cancel=cancel)
            snapshot={'head':h,'gid':row['gid'],'generation':proof,'online':online,'shop':shop,'profile':profile,
                      'lines':lines,'stats':{**stats,'file_bytes':len(workbook)},'template_sha':template_sha}
            if import_format==template.SINGLE:
                snapshot['import_format']=import_format
            if order_phone is not None:
                snapshot['order_phone']=order_phone
            with closing(store.connection(path)) as db,db:
                begin(db,cancel)
                _require_plan(db,snapshot)
                store.schema(db)
                store.put_prepared(db,snapshot,workbook,(old or {}).get('sequence'))
            ready.append(online)
        except ValueError as exc:
            failed.append({'order':show_name(h),'reason':str(exc),'stats':stats})
    return {'ready':ready,'failed':failed}


def export_file(path, online, target, progress=lambda _:None, cancel=None):
    with closing(wr.read_connection(path)) as db:
        row=store.read(db,online,workbook=True)
    if not row or row['stage'] in store.RELEASED:
        raise Check('此任务已有下单动作，请从原任务核对')
    checkpoint(cancel)
    dest=Path(target)
    with dest.open('xb') as stream:
        stream.write(row['workbook'])
    return str(dest)


def _client_error(exc):
    messages={
        'ACCOUNT_UNVERIFIED':'该仓专用浏览器和聚水潭企业尚未绑定核验',
        'LOGIN_REQUIRED':'请登录该仓专用浏览器后，继续原任务',
        'ACCOUNT_CHANGED':'该仓账号或企业已有变化，请核对原绑定',
        'SHOP_AMBIGUOUS':'平台店铺未唯一匹配，请核对该仓店铺名称和原店铺',
        'SHOP_CONTROL_UNAVAILABLE':'聚水潭导入页的店铺下拉框或店铺标识暂未识别，请查看网页',
        'SHOP_NOT_FOUND':'聚水潭导入页找不到全名一致的店铺，请核对该仓映射',
        'SHOP_DUPLICATE':'聚水潭导入页存在多条同名店铺，尚未选店，请核对店铺',
        'SHOP_SELECTION_FAILED':'聚水潭网页选店未生效或回读不一致，请核对后重试',
        'ORDER_PAGE_CHANGED':'平台页面结构待核，未取得可操作的订单控件',
        'ORDER_EXISTS':'平台已有相同店铺和线上单号，请核对原单',
        'PREVIEW_ISSUES':'聚水潭预览有异常，请在专用浏览器核对缺货、禁用或地址等提示',
        'PREVIEW_CONTACT':'聚水潭预览的买家或收货资料与本次制表不一致',
        'PREVIEW_CHANGED':'平台预览已变化，请重新核对该仓单',
        'RESPONSE_UNKNOWN':'组件应答超时，请继续核对原任务',
        'BUSY':'聚水潭正在同步资料或处理原任务，请待当前动作安全结束',
        'COMPONENT_MISSING':'聚水潭组件缺失，请使用包含组件的完整程序',
    }
    if exc.code in ('ORDER_PAGE_CHANGED','PREVIEW_CHANGED','PREVIEW_ISSUES') and str(exc):
        return PlatformCheck(str(exc))
    return PlatformCheck(messages.get(exc.code,'聚水潭连接或结果待核，请检查专用浏览器与原任务'))


class Browser:
    """Only the shared component has access to the warehouse browser."""
    def __init__(self, lease):
        self.lease=lease

    def request(self, action, **values):
        try:
            return self.lease.request('order.'+action,timeout=180,**values)
        except ComponentError as exc:
            raise _client_error(exc) from None

    def action(self, action, order, **extra):
        p=order['payload']
        spec={'warehouse':p['head']['warehouse'],'shop':p['shop']['name'],'online':p['online'],
               'internal':order['internal'],'phase':order['stage'],
              'binding':None if action=='preview' else (order['proof'] or {}).get('binding')}
        if action=='payment_detail' or action=='lookup' and extra.get('identity_only'):
            spec['contact']={'buyer':p['profile']['values']['buyer']}
        elif action in ('preview','lookup','stage_import','stage_audit'):
            spec['contact']=p['profile']['values']
            if 'order_phone' in p:
                spec['contact']={**spec['contact'],'phone':p['order_phone']}
            spec['lines']=p['lines']
            if p.get('import_format')==template.SINGLE:
                spec['import_format']=template.SINGLE
        if action in ('preview','stage_import'):
            spec['file']=base64.b64encode(order['workbook']).decode('ascii')
            spec['sha']=order['file_sha']
            spec['preview_names']=order['preview_names']
        return self.request(action, spec=spec, **extra)


def _issues(result):
    labels={'STOCK_SHORT':'库存不足','PRODUCT_MISSING':'商品资料缺失','PRODUCT_DISABLED':'商品禁用或停采',
            'ADDRESS_INVALID':'地址待核','PRICE_DIFF':'价格或金额待核','OTHER':'平台异常待核'}
    issues=result.get('issues')
    return '、'.join(labels.get(x,labels['OTHER']) for x in issues) if isinstance(issues,list) else labels['OTHER'] if issues else ''


def _preview_evidence(order, result):
    p=order['payload'];binding=result.get('binding') or {}
    if result.get('exists'):
        raise Check('平台已有同店铺同线上单号，请核对原平台订单')
    if (result.get('online')!=order['online'] or binding.get('warehouse')!=p['head']['warehouse']
            or binding.get('shop')!=p['shop']['name'] or not all(binding.get(k) for k in ('company','coid','uid','shop_id'))):
        raise Check('预览中的企业、店铺或线上单号无法核对')
    issues=_issues(result)
    if issues:
        raise Check('聚水潭预览待核：'+issues)
    if result.get('contact_ok') is not True:
        raise Check('买家、手机号或收货地址与送审文件不一致')
    expected={sku:Decimal(qty) for sku,qty,_ in p['lines']}
    seen=set()
    for row in result.get('lines',[]):
        if not isinstance(row,(tuple,list)) or len(row)<2:
            raise Check('平台商品明细缺少编码或数量')
        sku,qty=row[:2]
        if sku not in expected or sku in seen or number(qty,'平台数量')!=expected[sku]:
            raise Check('平台 SKU 或逐项数量与已保存生成单不一致')
        seen.add(sku)
    if seen!=expected.keys():
        raise Check('平台商品范围与已保存生成单不一致')
    try:
        amount=template.decimal_text(number(result.get('amount'),'平台金额'))
    except ValueError:
        amount=None
    return {'binding':binding,'amount':amount,'file_sha':order['file_sha']}


def _preview_for_submit(order, result):
    evidence=_preview_evidence(order,result)
    if order['payload'].get('import_format')==template.SINGLE:
        if result.get('order_count')!=1 or result.get('import_format')!=template.SINGLE or evidence['amount'] is None:
            raise Check('单商品模式缺少一张整单及金额的完整平台预览，请核对原文件')
        evidence['import_format']=template.SINGLE
    return evidence


def _same_order(evidence, original):
    if evidence['binding']!=original['binding'] or evidence['file_sha']!=original['file_sha']:
        return False
    if evidence.get('import_format')==template.SINGLE:
        return original.get('import_format')==template.SINGLE and evidence['amount']==original['amount']
    return True


def _preview_names(db, order):
    payload=order['payload']
    bid=payload['generation']['bid']
    baseline=allocations.decode(db.execute('SELECT payload,digest FROM jst_baselines WHERE bid=?',(bid,)).fetchone())
    items=(baseline or {}).get('catalog',{}).get('items',{})
    names={sku:items.get(sku,{}).get('商品名称') for sku,_,_ in payload['lines']}
    if any(not name for name in names.values()):
        generation,_=allocations.read_generation(db,order['gid'])
        fallback={row['sku']:row.get('name') for row in generation['rows']}
        names={sku:name or fallback.get(sku) for sku,name in names.items()}
    return names


def _event(db, order, title, detail):
    allocations.event(db,order['payload']['head'],title,
                      {'online':order['online'],'gid':order['gid'],**detail})


def _save(path, order, stage, cancel, *, check_plan=False, proof=None, reason=''):
    with closing(store.connection(path)) as db,db:
        begin(db,cancel)
        if check_plan:
            _require_plan(db,order['payload'])
        result=store.update(db,order,stage,proof=proof,reason=reason)
        if stage in store.UNRESOLVED and stage!=order['stage']:
            _event(db,result,'聚水潭自动下单执行意图',{'stage':stage})
        commit(db,cancel)
    return result


def _legacy_audit_not_dispatched(order):
    # The previous adapter emitted this exact failure before selecting a row
    # or dispatching ordinary audit. Other unknown outcomes remain protected.
    return (order['stage']=='AUDIT_UNKNOWN' and order['platform_status']=='awaiting'
            and order['reason']=='原订单商品详情尚未关闭')


def _record_found(path, order, result, cancel, *, identity_only=False):
    not_dispatched=_legacy_audit_not_dispatched(order)
    if result.get('found') is not True:
        if not_dispatched:
            return {**order,'reason':'尚未查到唯一原单，请继续核对'}
        return _save(path,order,order['stage'],cancel,reason='按原线上单号尚未查到唯一订单，保留原状态，请继续核对')
    proof=order['proof']
    if result.get('binding')!=proof['binding'] or result.get('online')!=order['online']:
        raise Check('回查的企业、店铺或线上单号与原任务不同')
    if identity_only:
        buyer_problem=confirmation.buyer_issue(result)
        if buyer_problem:
            detail=result.get('detail_error')
            raise Check(buyer_problem+(' · '+detail if detail and result.get('buyer_check')=='unavailable' else ''))
    else:
        _preview_evidence(order, {**result,'exists':False,'issues':[]})
    internal=result.get('internal')
    if not isinstance(internal,str) or not re.fullmatch(r'[0-9]{1,40}',internal) or order['internal'] not in (None,internal):
        raise Check('原线上单号尚未对应唯一的聚水潭内部订单号')
    reported_state=result.get('status')
    native_status=result.get('native_status')
    if identity_only and native_status not in confirmation.PAYMENT_STATES | confirmation.REVERSED:
        raise Check('已查到原单，平台状态仍待核')
    state=reported_state
    confirmed=identity_only and native_status in confirmation.PAYMENT_STATES
    if confirmed:
        state='audited' if native_status==confirmation.AUDITED else 'awaiting' if native_status==confirmation.AWAITING else 'ordered'
    elif native_status in SHIPPING_PLATFORM_STATES:
        state='review'
    elif reported_state in ('audited','review'):
        state='audited' if native_status==ORDINARY_AUDIT_DONE else 'review'
    stage={'awaiting':'IMPORTED','ordered':'IMPORTED','audited':'AUDITED','exception':'REVIEW','review':'REVIEW'}.get(state)
    if stage is None:
        raise Check('已查到原单，平台状态仍待核')
    if (stage=='IMPORTED' and order['stage']=='AUDIT_UNKNOWN' and state=='awaiting'
            and not (not_dispatched or result.get('audit_rejected'))):
        stage='AUDIT_UNKNOWN'
    if order['stage']=='AUDITED' and stage=='IMPORTED' and not confirmed:
        stage='REVIEW'
    issues=_issues(result)
    if issues:
        stage='REVIEW'
    reason={'IMPORTED':'已按线上单号核对原单，导入完成','AUDITED':'普通审核已完成，聚水潭状态为'+ORDINARY_AUDIT_DONE,
            'AUDIT_UNKNOWN':'普通审核结果待核，请继续核对原单',
            'REVIEW':issues or '聚水潭显示缺货、异常或待人工审核，请查看原订单'}[stage]
    if stage=='IMPORTED' and result.get('audit_rejected'):
        reason='上次普通审核未通过，请查看平台原单提示后继续处理'
    elif stage=='IMPORTED' and not_dispatched:
        reason='上次审核在发送前中止，已核对原单仍待审核'
    elif stage=='AUDIT_UNKNOWN' and native_status==confirmation.AWAITING:
        reason=f'原单仍显示“{native_status}”，普通审核结果有待确认'
    if stage=='REVIEW':
        if native_status in SHIPPING_PLATFORM_STATES:
            reason=f'聚水潭已进入后续发货环节（{native_status}），请核对原单'
        elif native_status in REVIEW_PLATFORM_STATES:
            reason='聚水潭状态为'+native_status
        elif reported_state=='audited' and not issues:
            reason='未核实聚水潭普通审核的完成状态，请核对原单'
        if issues and reason!=issues:
            reason+=' · '+issues
    if confirmed and stage in ('IMPORTED','AUDITED'):
        reason=f'已核对原单，聚水潭当前为“{native_status}”'
    h=order['payload']['head']
    with closing(store.connection(path)) as db,db:
        begin(db,cancel)
        if allocations.active(db,h)!=order['gid']:
            raise wr.Conflict('外部原单已查到，本机原生成版本已变化，请核对关联')
        current=store.update(db,order,stage,internal=internal,status=state,reason=reason)
        lock=allocations.locked(db,h)
        if lock and lock!=order['gid']:
            raise wr.Conflict('已存在其他生成版本的下单标记，请核对')
        if not lock:
            db.execute('INSERT INTO jst_locks VALUES(?,?,?,?,?,?) ON CONFLICT(oid,warehouse) DO UPDATE SET gid=excluded.gid,locked=1,at=excluded.at,reason=excluded.reason',
                       (h['oid'],h['warehouse'],order['gid'],1,wr.now(),'聚水潭自动下单已按原单回查'))
        lifecycle_store.schema(db)
        ref=reference_key(h)
        registered=db.execute("SELECT payload FROM lifecycle_mappings WHERE stage='payment' AND name=?",(ref,)).fetchone()
        if registered and wr.loads(registered[0]).get('number')!=internal:
            raise wr.Conflict('本机登记的聚水潭内部订单号不同，请核对原关联')
        if not registered:
            lifecycle_store.save_mapping(db,'payment',ref,{'head':{k:h[k] for k in HEAD_KEYS},'number':internal,
                                                          'saved':wr.now(),'source':'聚水潭自动下单'})
        detail={'internal':internal,'stage':stage}
        if native_status in (ORDINARY_AUDIT_DONE,'已付款待审核',*SHIPPING_PLATFORM_STATES,*REVIEW_PLATFORM_STATES):
            detail['native_status']=native_status
        if (stage=='AUDITED' and not identity_only) or native_status in confirmation.REVERSED:
            confirmation.record(db,current,native_status)
        _event(db,current,'聚水潭原单核对',detail)
        commit(db,cancel)
    return current


def _review(path, browser, order, cancel, *, identity_only=False):
    options={'identity_only':True} if identity_only else {}
    result=browser.action('lookup',order,**options)
    return _record_found(path,order,result,cancel,identity_only=identity_only)


def _audit(path, browser, order, cancel):
    # Verify current platform facts again, and use this exact pending order only.
    order=_review(path,browser,order,cancel)
    if order['stage']!='IMPORTED':
        return order
    result=browser.action('stage_audit',order)
    if result.get('internal')!=order['internal'] or not result.get('permit'):
        raise Check('未取得原订单的普通审核入口')
    if not _same_order(_preview_evidence(order,result),order['proof']):
        raise Check('普通审核前原订单内容已变化，请继续核对')
    intent=_save(path,order,'AUDIT_UNKNOWN',cancel,reason='已记下原单，正在核对普通审核结果')
    browser.request('audit',permit=result['permit'],online=order['online'],internal=order['internal'],
                    warehouse=order['payload']['head']['warehouse'])
    return _review(path,browser,intent,cancel)


def _process(path, browser, order, action, cancel):
    if action=='probe':
        browser.action('probe',order)
        return _save(path,order,order['stage'],cancel,reason='已核对本仓企业、店铺和导入入口')
    if action in ('recover','recover_delete'):
        if order['stage'] not in store.RELEASED:
            raise Check('此任务尚未导入，请先完成预览')
        return _review(path,browser,order,cancel,identity_only=action=='recover')
    if action=='audit':
        if order['stage']!='IMPORTED':
            raise Check('请先核对原单，确认已导入且待普通审核')
        return _audit(path,browser,order,cancel)
    if order['stage'] in store.RELEASED:
        raise Check('此任务已有导入动作，请继续核对原单')
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        _require_plan(db,order['payload'])
        order['preview_names']=_preview_names(db,order)
    if action=='preview':
        evidence=_preview_for_submit(order,browser.action('preview',order))
        return _save(path,order,'PREVIEWED',cancel,check_plan=True,proof=evidence,reason='平台预览通过，等待确认导入')
    if action!='submit' or order['stage']!='PREVIEWED':
        raise Check('请先预览并确认本次原文件')
    fresh=browser.action('stage_import',order)
    if not _same_order(_preview_for_submit(order,fresh),order['proof']) or not fresh.get('permit'):
        raise Check('平台预览或目标店铺已变化，请重新预览')
    intent=_save(path,order,'IMPORT_UNKNOWN',cancel,check_plan=True,reason='已保存导入意图，等待原单回查')
    browser.request('import',permit=fresh['permit'],online=order['online'],warehouse=order['payload']['head']['warehouse'])
    return _review(path,browser,intent,cancel,identity_only=True)


def process(path, keys, browser, action, progress=lambda _:None, cancel=None, *, approved=None, prepare_payment=None):
    """Process at most the explicitly confirmed selection, in displayed order."""
    responses=[]
    for i,online in enumerate(dict.fromkeys(keys),1):
        checkpoint(cancel)
        with closing(wr.read_connection(path)) as db:
            order=store.read(db,online,workbook=action in ('preview','submit'))
        if not order:
            raise Check('原下单任务已找不到，请刷新')
        if approved is not None and order['sequence']!=approved.get(online):
            responses.append({'online':online,'stage':order['stage'],'reason':'确认范围内的任务已更新，请刷新后重新核对'})
            continue
        progress(RowProgress(show_name(order['payload']['head']),i,len(keys)))
        try:
            result=_process(path,browser,order,action,cancel)
        except Cancelled:
            raise
        except ValueError as exc:
            with closing(wr.read_connection(path)) as db:
                current=store.read(db,online)
            if current and not _legacy_audit_not_dispatched(current):
                current=_save(path,current,current['stage'],cancel if action in ('recover','recover_delete') else None,reason=str(exc))
            result={**(current or order),'reason':str(exc)}
        response={'online':online,'stage':result['stage'],'reason':result['reason']}
        if (prepare_payment and action in ('submit','recover') and result.get('internal')
                and result['stage'] in ('IMPORTED','AUDITED','AUDIT_UNKNOWN','REVIEW')):
            payment=prepare_payment(path,[online],browser,progress,cancel)[0]
            response['payment']=payment
            response['stage']=payment.get('order_stage',response['stage'])
        responses.append(response)
    return responses


def run_browser(path, keys, owner, action, approved=None, progress=lambda _:None, cancel=None, *, prepare_payment=None):
    checkpoint(cancel)
    try:
        lease=owner.acquire()
    except ComponentError as exc:
        raise _client_error(exc) from None
    try:
        try:
            lease.start()
        except ComponentError as exc:
            raise _client_error(exc) from None
        return process(path,keys,Browser(lease),action,progress,cancel,approved=approved,prepare_payment=prepare_payment)
    finally:
        while not lease.close(timeout=.5):
            progress('等待聚水潭当前动作安全结束，保留原任务')
            time.sleep(.2)


def recover_for_deletion(path, pending, owner, progress=lambda _:None, cancel=None):
    """Reconcile the captured tasks using the ordinary read-only platform lookup."""
    progress('查询所选仓单的原单和普通审核状态…')
    run_browser(path,list(pending),owner,'recover_delete',pending,progress,cancel)
    checkpoint(cancel)
