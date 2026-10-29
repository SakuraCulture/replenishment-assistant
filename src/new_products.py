"""Explicit store first-fill candidates; original model and allocation policy stay intact."""
import copy
from decimal import Decimal
import allocation
import result_semantics as sem


def is_new(row):
    return all(not isinstance(row.get(k), bool) and sem.numeric(row.get(k)) and row[k] == 0 for k in ('J', 'K', 'L', 'N'))


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
        d['_new_requested']=q;d['_candidate_qty']=q
        d['_candidate_amount']=float(Decimal(str(q))*Decimal(str(d['V'])))
        # Unlimited execution is explicit; AS/BV retain the model's zero suggestion.
        d['_execution_qty']=q;d['_execution_amount']=d['_candidate_amount']
    if result.get('budget',{}).get('enabled'):
        result['budget']=allocation.apply(result['rows'],base['budget']['limit'],lambda:checkpoint(cancel))
    result['new_products']={'selection':copy.deepcopy(selections),'rule':'J/K/L/N valid numeric zero; explicit selection','priority':allocation.POLICY}
    result['summary']=sem.summarize(result['rows'])
    return result


def changes(before, after):
    old={d['E']:d for d in before['rows']};out=[]
    for d in after['rows']:
        prev=old[d['E']]
        if (sem.order_quantity(prev),sem.order_amount(prev),prev.get('_candidate_qty'))!=(sem.order_quantity(d),sem.order_amount(d),d.get('_candidate_qty')):
            out.append({'sku':d['E'],'name':d.get('D',''),'type':category(d),'before_candidate':prev.get('_candidate_qty'),'after_candidate':d.get('_candidate_qty'),'before_qty':sem.order_quantity(prev),'after_qty':sem.order_quantity(d),'before_amount':sem.order_amount(prev),'after_amount':sem.order_amount(d),'holding_gap':bool(d.get('_holding_gap'))})
    return out
