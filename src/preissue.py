"""Final issue edits after automatic allocation; never reruns the model or allocator.

Published result trees are immutable. Only edited rows are copied. Original
execution fields/order and store budget are retained, allowing exact restoration.
"""
from __future__ import annotations
import copy
import hashlib
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import result_semantics as sem
import work_repository as wr
from runtime_support import checkpoint

SCHEMA = 1
KINDS = {'manual':'人工调整', 'new':'新品补足', 'plan':'计划补足', 'promotion':'活动补足'}
EXEC_FIELDS = ('_candidate_qty','_candidate_amount','_execution_qty','_execution_amount',
               '_allocated_qty','_allocated_amount','_allocated_amount_exact',
               '_budget_gap','_budget_reason','_holding_gap','_budget_overrun','_quantity_normalization')


def digest(value):
    return hashlib.sha256(wr.dumps(value).encode('utf-8')).hexdigest()


def decimal(value, name='数量'):
    if isinstance(value, bool) or value is None or str(value).strip() == '':
        raise ValueError(name+'须为有效数字，空白不等于0')
    try:d = Decimal(str(value).strip())
    except InvalidOperation:raise ValueError(name+'须为有效数字') from None
    if not d.is_finite():raise ValueError(name+'不能为非有限数')
    return d


def scalar(value):
    """Use existing JSON numeric types without silent precision loss."""
    try:n = float(value)
    except (OverflowError, ValueError):raise ValueError('数量超过安全表示范围') from None
    if Decimal(str(n)) != value:raise ValueError('数量超过安全表示精度，请核对')
    return int(value) if value == value.to_integral_value() else n


def quantity(value, step):
    q, p = decimal(value), decimal(step, '箱规')
    if p <= 0:raise ValueError('箱规必须大于0')
    try:
        if q/p > Decimal(2**53-1):raise ValueError('出库单位个数超过安全表示范围')
        valid = q >= 0 and q % p == 0
    except DecimalException:raise ValueError('数量超过安全表示范围') from None
    if not valid:
        raise ValueError(f'只能填写{p:g}的非负整数倍：0、{p:g}、{2*p:g}……')
    return scalar(q)


def original(result):
    state = result.get('preissue')
    if not state:return result
    if state.get('schema') != SCHEMA:raise ValueError('不支持的签发调整格式')
    rows = []
    for d in result['rows']:
        a = d.get('_preissue')
        if not a:rows.append(d);continue
        x = {k:v for k,v in d.items() if k != '_preissue'}
        for k, saved in a['original_fields'].items():
            if saved['present']:x[k] = copy.deepcopy(saved['value'])
            else:x.pop(k, None)
        if set(x) != set(a['original_order']):raise ValueError('原自动字段与调整快照不一致')
        rows.append({k:x[k] for k in a['original_order']})
    out = {k:v for k,v in result.items() if k != 'preissue'}
    out.update(rows=rows, budget=copy.deepcopy(state['original_budget']), summary=copy.deepcopy(state['original_summary']))
    return out


def identity(result):
    rows = result['rows']; store = result['store']; by_code = {}
    for d in rows:
        code = d.get('E')
        if not isinstance(code, str) or not code or code in by_code or d.get('B') != store:
            raise ValueError('门店商品身份不唯一或混入其他门店')
        by_code[code] = d
    return by_code


def blocked(row):
    if sem.exclusion_reason(row):return sem.exclusion_reason(row)
    if row.get('_source') != '资料匹配':return '地采或商品资料未匹配，不能安排中心仓出货'
    if row.get('_preview'):return '尚未完成计算'
    if sem.issue_text(row):return sem.issue_text(row)
    for key, title in [('V','基本售价'), ('BD','箱规')]:
        try:v = decimal(row.get(key), title)
        except ValueError as exc:return str(exc)
        if v <= 0:return title+'须为有效正数'
    for key, title in [('K','可用库存'),('L','在途库存')]:
        try:decimal(row.get(key), title)
        except ValueError as exc:return str(exc)
    if not row.get('AU'):return '需求归属仓待核'
    if not sem.numeric(sem.order_quantity(row)):return '原执行数量待核，请先解决计算或输入问题'
    return ''


def holding(row):
    return max(decimal(row['K']), Decimal(0))+max(decimal(row['L']), Decimal(0))


def target_quantity(row, strategy, target=None):
    reason = blocked(row)
    if reason:raise ValueError(reason)
    p = decimal(row['BD']); h = holding(row)
    if strategy == 'minimum':return scalar(p if h == 0 else Decimal(0))
    if strategy != 'target':raise ValueError('未知清单补足策略')
    t = decimal(target, '目标持有量')
    if t <= 0:raise ValueError('目标持有量必须大于0；不参与请取消勾选')
    return scalar((max(Decimal(0),t-h)/p).to_integral_value(rounding=ROUND_CEILING)*p)


def current_month(row):
    return all(sem.numeric(row.get(k)) and not isinstance(row.get(k), bool) and row[k] == 0 for k in ('J','K'))


def stock_scope(row):
    if blocked(row):return 'unknown'
    if decimal(row['K']) > 0:return 'available'
    return 'inbound' if decimal(row['L']) > 0 else 'zero'


def _risk(kind, result, row, q, source, text):
    evidence = {'kind':kind, 'store':result['store'], 'sku':row['E'], 'warehouse':row['AU'], 'quantity':q, 'source':source}
    return {**evidence, 'id':digest(evidence), 'text':text}


def row_risks(base, row, q, source):
    risks = []; before = decimal(sem.order_quantity(row)); amount = decimal(q)
    if amount == 0 and holding(row) == 0 and any(sem.numeric(row.get(k)) and row[k]>0 for k in ('H','I','J')):
        risks.append(_risk('zero', base, row, q, source, '取消最低持有：有销量且可用/在途双零，存在缺货风险'))
    if amount > before:
        params = base.get('effective_settings',{}).get('parameter_config',{}).get('parameters') or base.get('config',{}).get('parameters',{})
        days = params.get('cap_days',21)
        known = all(sem.numeric(row.get(k)) and not isinstance(row.get(k), bool) for k in ('BH','BN'))
        if days != 0 and known:
            h = decimal(row['K'])+decimal(row['L']); p=decimal(row['BD']); days=decimal(days)
            limit=max(days*decimal(row['BH']),min(days*decimal(row['BN']),h+amount))
            allowed=max(Decimal(0),((limit-h)/p).to_integral_value(rounding=ROUND_FLOOR))*p
            if amount > allowed:
                risks.append(_risk('cap',base,row,q,source,f'超出当时普通库存额度（{days:g}天口径）；仅本商品本次数量豁免'))
        if not known or not all(sem.numeric(row.get(k)) and row[k]>0 for k in ('AN','AM')):
            risks.append(_risk('unknown',base,row,q,source,'销量参考不足，预测持有天数或库存额度无法评估；请确认经营需求'))
    return risks


def requests(result):
    return copy.deepcopy(result.get('preissue',{}).get('edits',{}))


def preview(result, edits, day, *, lists=None, expected=None, cancel=None, progress=lambda _:None):
    """edits is the complete desired overlay, not incremental quantity deltas."""
    checkpoint(cancel)
    if expected is not None and digest(result) != expected:raise ValueError('结果已变化，请重新核对')
    base = original(result); source = digest(base); by_code = identity(base)
    if result.get('preissue') and source != result['preissue']['source']:
        raise ValueError('原自动结果已变化，不能沿用旧调整')
    unknown = set(edits)-set(by_code)
    if unknown:raise ValueError('清单商品不在本店输入中：'+'、'.join(sorted(unknown)))
    lists=copy.deepcopy(lists if lists is not None else result.get('preissue',{}).get('lists',{}))
    out = {**base,'rows':list(base['rows'])}; normalized={}; risks=[]; changed=[]
    for i,d in enumerate(base['rows']):
        if i % 200 == 0:checkpoint(cancel);progress(f'校验最终清单 {i} / {len(base["rows"])}')
        if d['E'] not in edits:continue
        e = copy.deepcopy(edits[d['E']]); q = quantity(e.get('quantity'),d.get('BD'))
        error=blocked(d)
        if error:raise ValueError(d['E']+'：'+error)
        kind=e.get('kind','manual')
        if kind not in KINDS:raise ValueError('未知调整类型')
        reason=str(e.get('reason','')).strip()
        if not reason or len(reason)>200:raise ValueError(d['E']+'：请填写1～200字调整原因')
        before=sem.order_quantity(d); changed_qty=decimal(q)!=decimal(before)
        # List membership is retained even when the target is already satisfied.
        if not changed_qty and kind=='manual' and q!=0:continue
        e.update(quantity=q,kind=kind,reason=reason)
        if e.get('warehouse',d['AU']) != d['AU']:raise ValueError(d['E']+'：归属仓已变化，请重新核对')
        e['warehouse']=d['AU']; normalized[d['E']]=e
        money=decimal(q)*decimal(d['V']); x=copy.deepcopy(d)
        audit={'schema':SCHEMA,'quantity':q,'amount':scalar(money),'automatic_quantity':before,
               'automatic_amount':sem.order_amount(d),'delta':scalar(decimal(q)-decimal(before)),
               'kind':kind,'reason':reason,'day':day,'current_month':current_month(d) and
                   (kind!='manual' or e.get('list_revision') in lists),
               'source':copy.deepcopy(e.get('source')),'target':e.get('target'),'strategy':e.get('strategy'),
               'original_fields':{k:{'present':k in d,'value':copy.deepcopy(d.get(k))} for k in EXEC_FIELDS},
               'original_order':list(d)}
        if changed_qty:
            x.update(_candidate_qty=q,_candidate_amount=scalar(money),_execution_qty=q,_execution_amount=scalar(money),
                     _budget_gap=0,_holding_gap=bool(q==0 and holding(d)==0 and any(sem.numeric(d.get(k)) and d[k]>0 for k in ('H','I','J'))),
                     _budget_reason='签发前明确人工调整；未重新分配其他商品')
            if d.get('_budget_active'):x.update(_allocated_qty=q,_allocated_amount=scalar(money),_allocated_amount_exact=str(money))
            x.pop('_budget_overrun',None);x.pop('_quantity_normalization',None)
        x['_preissue']=audit;out['rows'][i]=x
        risks.extend(row_risks(base,d,q,source))
        changed.append({'store':base['store'],'sku':d['E'],'name':d.get('D',''),'warehouse':d['AU'],
                        'before':before,'after':q,'delta':audit['delta'],'amount':audit['amount'],'reason':reason})
    if not normalized:
        return {'result':base,'source':source,'expected':digest(result),'risks':[],'errors':[],'changes':[], 'can_apply':True}
    out['summary']=sem.summarize(out['rows'])
    budget, errors, budget_risk = _budget(base,out,normalized,source)
    if budget_risk:risks.append(budget_risk)
    out['budget']=budget
    out['preissue']={'schema':SCHEMA,'day':day,'source':source,'edits':normalized,'lists':lists,'original_budget':copy.deepcopy(base['budget']),
                     'original_summary':copy.deepcopy(base['summary']),'risks':risks,'confirmed':[],'errors':errors}
    checkpoint(cancel)
    return {'result':out,'source':source,'expected':digest(result),'risks':risks,'errors':errors,'changes':changed,'can_apply':not errors}


def _budget(base,out,edits,source):
    b=copy.deepcopy(base['budget']);errors=[];risk=None
    if not b.get('enabled'):return b,errors,risk
    if not any(d.get('_preissue',{}).get('delta') for d in out['rows']):return b,errors,risk
    limit=decimal(b['limit']);total=sum((decimal(sem.order_amount(d)) for d in out['rows'] if sem.orderable(d)),Decimal(0))
    if limit==0 and any(sem.numeric(sem.order_quantity(d)) and sem.order_quantity(d)>0 for d in out['rows']):errors.append('预算为0，不能签发正数量；请改量或明确修改预算')
    extra=None;rows={d['E']:d for d in out['rows']};old={d['E']:d for d in base['rows']}
    if total>limit:
        if limit<=0 or total>limit*Decimal('1.05'):errors.append('整店最终金额超过允许预算上限')
        candidates=[]
        prior=base.get('budget',{}).get('overrun',{}).get('sku')
        if prior in rows and decimal(sem.order_quantity(rows[prior]))>=decimal(sem.order_quantity(old[prior])):candidates.append(prior)
        # Ordered edit sequence is recorded explicitly, independent of JSON key order.
        positions={s:i for i,s in enumerate(rows)}
        changed=sorted((s for s in edits if decimal(sem.order_quantity(rows[s]))>decimal(sem.order_quantity(old[s]))),key=lambda s:(edits[s].get('sequence',0),positions[s]))
        if changed:candidates.append(changed[-1])
        for sku in dict.fromkeys(candidates):
            d=rows[sku];cost=decimal(d['BD'])*decimal(d['V'])
            if decimal(sem.order_quantity(d))>=decimal(d['BD']) and total-cost<=limit:
                extra={'sku':sku,'warehouse':d['AU'],'quantity':d['BD'],'unit_cost':scalar(cost)};break
        if not extra:errors.append('超预算不符合唯一末尾出库单位条件，不能按每次编辑或每仓重复追加')
        if not errors:
            evidence={'source':source,'store':out['store'],'total':str(total),'limit':str(limit),'extra':extra}
            risk={'id':digest(evidence),'kind':'budget','store':out['store'],'sku':extra['sku'],'warehouse':extra['warehouse'],
                  'quantity':sem.order_quantity(rows[extra['sku']]),'text':f'整店有限超支{total-limit:g}元；仅一个末尾出库单位','evidence':evidence}
    must=sum((decimal(sem.order_amount(d)) for d in out['rows'] if d.get('_must_sell') and sem.orderable(d)),Decimal(0))
    b.update(spent=scalar(total),spent_exact=str(total),remaining=scalar(limit-total),must_spent=scalar(must),other_spent=scalar(total-must),
             deferred=out['summary']['budget_deferred'],holding_gaps=out['summary']['holding_gaps'])
    if 'overrun' in b:
        b['overrun']={'ceiling':scalar(limit*Decimal('1.05')),'amount':scalar(max(Decimal(0),total-limit)),
                      'sku':extra['sku'] if extra else None,'quantity':extra['quantity'] if extra else 0,'max_units':1,'ratio':.05}
    return b,errors,risk


def finalize(prepared, confirmed=(), *, current=None):
    if current is not None and digest(current)!=prepared['expected']:raise ValueError('预览后结果已变化，请重新核对')
    if prepared['errors']:raise ValueError('；'.join(prepared['errors']))
    required={r['id'] for r in prepared['risks']}
    if not required<=set(confirmed):raise ValueError('请明确确认全部已列出的风险；确认只对当前数量与依据有效')
    out=prepared['result']
    if out.get('preissue'):
        out={**out,'preissue':{**out['preissue'],'confirmed':sorted(required)}}
    return out


def validate_result(result, day, *, cancel=None):
    state=result.get('preissue')
    if not state:
        if any('_preissue' in d for d in result['rows']):raise ValueError('调整行缺少整店校验依据')
        return
    if state.get('day')!=day:raise ValueError('人工调整属于其他业务日期，请重新复核')
    prepared=preview(result,state['edits'],day,lists=state.get('lists',{}),cancel=cancel)
    rebuilt=finalize(prepared,state['confirmed'])
    if digest(rebuilt)!=digest(result):raise ValueError('最终数量、金额或调整依据校验不一致，请重新核对')


def validate_payload(payload, codes, *, cancel=None):
    for s in codes:
        checkpoint(cancel)
        if s in payload.get('preissue_drafts',{}):raise ValueError('门店尚有待核对的签发前调整：'+s)
        validate_result(payload['results'][s],payload['day'],cancel=cancel)


def list_edits(result, listing, strategy, target=None, scopes=('zero',), *, replace_manual=False, sequence=0):
    """Pure list preview. Missing/blocked records are explicit, never fabricated."""
    by_code=identity(result);out=requests(result);pending=[];skipped=[]
    for item in listing['items']:
        sku=item['sku'];d=by_code.get(sku)
        if d is None:pending.append({'sku':sku,'reason':'缺本店销量库存记录'});continue
        reason=blocked(d)
        if reason:pending.append({'sku':sku,'reason':reason});continue
        if stock_scope(d) not in scopes:skipped.append({'sku':sku,'reason':'不在所选库存状态'});continue
        if out.get(sku,{}).get('kind')=='manual' and not replace_manual:skipped.append({'sku':sku,'reason':'保留已人工修改数量'});continue
        t=item.get('target') if item.get('target') is not None else target
        q=max(decimal(sem.order_quantity(d)),decimal(target_quantity(d,strategy,t)))
        out[sku]={'quantity':scalar(q),'reason':KINDS[listing['kind']],'kind':listing['kind'],'target':t,'strategy':strategy,
                  'warehouse':d['AU'],'sequence':sequence,'list_revision':listing['revision'],
                  'source':{k:copy.deepcopy(listing.get(k)) for k in ('name','kind','revision','sources')}}
    lists={**result.get('preissue',{}).get('lists',{}),listing['revision']:copy.deepcopy(listing)}
    return {'edits':out,'lists':lists,'pending':pending,'skipped':skipped}
