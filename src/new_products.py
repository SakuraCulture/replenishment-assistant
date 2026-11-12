"""Explicit store first-fill candidates; original model and allocation policy stay intact."""
import copy
from decimal import Decimal
import allocation
import result_semantics as sem

AUTO_RULE='monthly-first-fill-1'
FIRST_FILL_KEYS=('_new_requested','_candidate_qty','_candidate_amount','_execution_qty','_execution_amount','_new_stockout_basis')


def calculation_settings(settings, new_focus, day):
    out=copy.deepcopy(settings)
    if new_focus is not None:out['monthly_first_fill']={'day':day,'focus':copy.deepcopy(new_focus)}
    return out


def seed_monthly(result, context, cancel=None,*,budget=None):
    """Seed an owned fresh calculation before its single budget allocation.

    The compact per-row undo evidence avoids retaining a second complete store
    result just to allow a later manual change. It never updates saved results.
    """
    if not context or context.get('focus') is None:return
    from datetime import date
    import new_product_focus as focus
    import first_fill
    from engine import checkpoint
    from restock_policy import result_values
    profile=focus.validate(context['focus']);day=date.fromisoformat(context['day']).isoformat()
    audit={'rule':AUTO_RULE,'day':day,'month':profile['month'],'focus_revision':profile['revision'],'selected':0,'budget_input':budget}
    result['monthly_first_fill']=audit
    if profile['month']!=day[:7]:audit['state']='关注月份与业务日期不同，未自动首铺';return
    if not result_values(result)['new_stocking']:audit['state']='本店新品铺设已关闭';return
    focused=focus.members(profile);selected={};skipped={}
    for d in result['rows']:
        checkpoint(cancel)
        if d['E'] not in focused:continue
        reason=blocked_reason(d)
        if reason:skipped[d['E']]=reason;continue
        q,note=first_fill.suggestion(d)
        reason=first_fill.quantity_issue(d,q)
        if reason:skipped[d['E']]=reason;continue
        original={k:{'present':k in d,'value':copy.deepcopy(d.get(k))} for k in FIRST_FILL_KEYS}
        floor=d.get('_mandatory_holding',{})
        actual=max(q,floor['required_quantity']) if floor.get('state')=='已核验' else q
        d.update(_new_requested=q,_candidate_qty=actual,_candidate_amount=float(Decimal(str(actual))*Decimal(str(d['V']))),
                 _execution_qty=actual,_execution_amount=float(Decimal(str(actual))*Decimal(str(d['V']))),
                 _new_stockout_basis='本月关注且满足现有新品资格，自动使用归属仓首铺量',
                 _monthly_first_fill={'rule':AUTO_RULE,'source':note,'original_fields':original})
        selected[d['E']]=q
    audit.update(state='已自动安排' if selected else '没有符合自动首铺资格的本月新品',selected=len(selected),skipped=skipped)
    if selected:result['new_products']={'selection':selected,'rule':AUTO_RULE,'priority':allocation.POLICY,'automatic':True}


def original(result):
    """Rebuild the model/budget base lazily when automatic first-fill is edited."""
    if not result.get('new_products',{}).get('automatic'):return result
    out=copy.deepcopy(result)
    for d in out['rows']:
        evidence=d.pop('_monthly_first_fill',None)
        if not evidence:continue
        if evidence.get('rule')!=AUTO_RULE or set(evidence.get('original_fields',{}))!=set(FIRST_FILL_KEYS):raise ValueError('自动首铺原始依据不完整，请重新计算')
        for k,saved in evidence['original_fields'].items():
            if saved['present']:d[k]=saved['value']
            else:d.pop(k,None)
    budget_input=out.get('monthly_first_fill',{}).get('budget_input')
    out.pop('new_products',None);out.pop('monthly_first_fill',None)
    if out.get('budget',{}).get('enabled'):
        for d in out['rows']:d.pop('_allocated_amount_exact',None)
        out['budget']=allocation.apply(out['rows'],out['budget']['limit'] if budget_input is None else budget_input)
    out['summary']=sem.summarize(out['rows'])
    return out


def is_new(row):
    # User-confirmed 2026-10-30: blank original stockout days mean zero for
    # first-fill recognition. Preserve the raw N and all model intermediates.
    return all(sem.numeric(row.get(k)) and row[k] == 0 for k in ('J', 'K', 'L')) and (row.get('N') is None or row.get('N')=='' or sem.numeric(row.get('N')) and row['N']==0)


def blocked_reason(row):
    if not is_new(row):return '四项有效零值条件不成立'
    if sem.exclusion_reason(row):return sem.exclusion_reason(row)
    if row.get('_source') != '资料匹配':return '商品资料未匹配，地采待核'
    if sem.issue_text(row):return sem.issue_text(row)
    if any(not sem.numeric(row.get(k)) or row[k] != 0 for k in ('H','I')):return '日销/周销与月销零值不一致，销量待核'
    if not sem.numeric(row.get('AS')) or row['AS'] != 0:return '原模型建议非零或待核，请核对'
    for key, name in [('V','基本售价'),('BD','最小出库单位')]:
        if isinstance(row.get(key),bool) or not sem.numeric(row.get(key)) or row[key] <= 0:return name+'待核'
    return ''


def category(row):
    if row.get('_new_requested',0)>0:return '新品推荐'
    if any(sem.numeric(row.get(k)) and row[k]>0 for k in ('H','I','J')):return '动销补货'
    if is_new(row):return '新品候选'
    return '缺货恢复及其他'


def apply(base, selections, cancel=None, progress=lambda s:None):
    """Always start from the unmodified calculation. Empty selection is exact identity."""
    from engine import checkpoint
    if base.get('new_products'):raise ValueError('必须从原计算结果重新分配，不能叠加新品')
    if selections:
        from restock_policy import guard_new
        guard_new(base)
    result=copy.deepcopy(base)
    if not selections:return result
    by_code={d['E']:d for d in result['rows']}
    if len(by_code)!=len(result['rows']):raise ValueError('门店商品编码重复，不能选新品')
    progress('正在校验新品，并重新分配本店总预算…')
    for sku,q in selections.items():
        checkpoint(cancel)
        if sku not in by_code:raise ValueError('新品不在本店输入中：'+sku)
        d=by_code[sku];reason=blocked_reason(d)
        if reason:raise ValueError(sku+'：'+reason)
        if isinstance(q,bool) or not sem.numeric(q) or q<=0:raise ValueError('首铺数量必须为正数：'+sku)
        if Decimal(str(q)) % Decimal(str(d['BD'])) != 0:raise ValueError('首铺数量须为最小出库单位倍数：'+sku)
        requested=q
        floor=d.get('_mandatory_holding',{})
        if floor.get('state')=='已核验':q=max(q,floor['required_quantity'])
        d['_new_requested']=requested;d['_candidate_qty']=q
        d['_new_stockout_basis']='原缺货天数为空，按用户确认口径视为0识别新品' if d.get('N') in (None,'') else '原缺货天数为0'
        d['_candidate_amount']=float(Decimal(str(q))*Decimal(str(d['V'])))
        # Unlimited execution is explicit; AS/BV retain the model's zero suggestion.
        d['_execution_qty']=q;d['_execution_amount']=d['_candidate_amount']
    if result.get('budget',{}).get('enabled'):
        result['budget']=allocation.apply(result['rows'],base['budget']['limit'],lambda:checkpoint(cancel))
    result['new_products']={'selection':copy.deepcopy(selections),'rule':'J/K/L numeric zero; N numeric zero or blank treated as zero; raw N retained; explicit selection','priority':allocation.POLICY}
    result['summary']=sem.summarize(result['rows'])
    return result


def changes(before, after):
    old={d['E']:d for d in before['rows']};out=[]
    for d in after['rows']:
        prev=old[d['E']]
        if (sem.order_quantity(prev),sem.order_amount(prev),prev.get('_candidate_qty'))!=(sem.order_quantity(d),sem.order_amount(d),d.get('_candidate_qty')):
            out.append({'sku':d['E'],'name':d.get('D',''),'type':category(d),'before_candidate':prev.get('_candidate_qty'),'after_candidate':d.get('_candidate_qty'),'before_qty':sem.order_quantity(prev),'after_qty':sem.order_quantity(d),'before_amount':sem.order_amount(prev),'after_amount':sem.order_amount(d),'holding_gap':bool(d.get('_holding_gap'))})
    return out
