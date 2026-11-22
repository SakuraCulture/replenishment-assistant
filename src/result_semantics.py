"""Single definition of calculation/candidate/execution result meanings. No UI or Excel I/O."""
from decimal import Decimal
import re
import model

SIGNED_GROUPS=('往期新品','本月新品','中心仓动销补货清单')

def numeric(v):return model.number(v)


def saved_model_version(result, outer_version=None):
    """The outer version identifies the same saved snapshot as result."""
    if isinstance(outer_version,str) and outer_version.strip():return outer_version.strip()
    metadata=result.get('version')
    version=metadata.get('model_version') if isinstance(metadata,dict) else None
    return version.strip() if isinstance(version,str) and version.strip() else None


def poisson_mean_field(model_version):
    """Select the calculation basis from the version saved with the result."""
    version = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)-rules', str(model_version))
    if version is None:
        return None
    return 'AO' if tuple(map(int, version.groups())) >= (0, 3, 7) else 'AP'


def poisson_mean_label(model_version):
    return {'AO':'趋势调整前需求 AO','AP':'趋势调整后需求 AP'}.get(poisson_mean_field(model_version),'未留存')


def safety_stock_formula(model_version):
    forecast_field = {'0.3.6-rules':'AP', '0.3.7-rules':'AO', '0.3.8-rules':'AP'}.get(model_version)
    return f'max(Q − {forecast_field}, 0)' if forecast_field else '未留存'


def pre_round_target(row):
    """Read the target from the saved forecast and buffer, without recalculating either."""
    forecast, safety = row.get('AP'), row.get('AL')
    return forecast + safety if numeric(forecast) and numeric(safety) else None


def effective_probability(result, row):
    config = result.get('effective_settings', {}).get('parameter_config') or result.get('config') or {}
    profile = '快销' if row.get('U') == '无分类' else row.get('U')
    default = config.get('parameters', {}).get('probability')
    category = config.get('profile_parameters', {}).get(profile, {}).get('probability', default)
    return row.get('_effective_probability', category)


def fmt(v,digits=2):
    if v is None:return '—'
    if numeric(v):return f'{v:,.{digits}f}'.rstrip('0').rstrip('.') if digits else f'{v:,.0f}'
    return str(v)


def issue_text(d):
    m=d['_meta'];issues=[s for s in m['blocking'] if not (d['_source']=='地采商品' and s=='待补商品资料')]
    floor=d.get('_mandatory_holding',{})
    if floor.get('state')=='待核' and floor.get('reason') and floor['reason'] not in issues:issues.append('必采最低持有待核：'+floor['reason'])
    return '；'.join(issues)


def exclusion_reason(d):
    reasons=list(d.get('_meta',{}).get('admission',{}).get('reasons',[]))
    reasons.extend(d.get('_meta',{}).get('execution_exclusions',[]))
    if str(d.get('S') or '').strip()=='禁用':reasons.append('商品资料禁用')
    if str(d.get('_meta',{}).get('export_status') or '').strip()=='停采':reasons.append('补货建议停采')
    return '；'.join(dict.fromkeys(reasons))


def work_eligible(d):
    """Working/export scope only. Keep raw/model/archive rows for traceability."""
    return not exclusion_reason(d)


def inbound_risk(d):
    """Warning only. 3.5 days is a review scenario, never a model override.

    Historical snapshots without either rule or cap audit stay uninterpreted. Existing
    inbound has no ETA; quantity coverage cannot establish shelf availability.
    """
    cap=d.get('_inbound_cap',{});wrapper=d.get('_audit',{}).get('inbound_cap_blocked',False)
    # New sales floors no longer create the old cap audit. Keep a warning for
    # a genuine inbound/cap gap, without treating the warning as an order.
    new_cap_gap=(d.get('_floor_rule')==model.FLOOR_RULE and d.get('AW')=='可计算'
        and all(numeric(d.get(k)) for k in ('K','L','H','I','J','BJ','BK','AR','AS'))
        and d['K']<=0 and d['L']>0 and max(d['H'],d['I'],d['J'])>0
        and d['BJ']>0 and d['BK']==d['AR']==d['AS']==0)
    if not (cap.get('previous_suggestion',0)>0 or wrapper or new_cap_gap) or not work_eligible(d) or d.get('_source')!='资料匹配':return None
    if not all(numeric(d.get(k)) for k in ('K','L','AN','AM','AH')):return None
    rate=d['AN']*d['AM']
    if rate<=0 or d['L']<=0:return None
    days=(max(d['K'],0)+max(d['L'],0))/rate
    gap=d['AH']+3.5-days
    if gap<=0:return None
    return {'current_days':days,'lead_days':d['AH'],'review_days':3.5,'gap_days':gap,
            'text':f'在途衔接风险：现有总量约{days:.2f}天；若3.5天后再下单，到货需{d["AH"]:g}天，覆盖缺口约{gap:.2f}天。请核对在途实际到店时间或提前复核；不自动追加。'}


def working_rows(rows):
    return [d for d in rows if work_eligible(d)]


def export_projection(result,excluded_keys=()):
    """Non-mutating projection; never recalculate or reallocate a signed result."""
    rows=working_rows(result['rows'])
    blocked={d['E'] for d in result['rows'] if not work_eligible(d)}
    keys=set(excluded_keys)|{(result['store'],code) for code in blocked}
    audit=result.get('import_audit')
    extra={'import_audit':{**audit,'conflicts':[c for c in audit.get('conflicts',[]) if (c['store'],c['sku']) not in keys]}} if audit else {}
    return {**result,'rows':rows,'summary':summarize(rows),
            'must_list_audit':[a for a in result.get('must_list_audit',[]) if a['sku'] not in blocked],**extra}


def order_quantity(d):
    if exclusion_reason(d):return 0
    candidate=d.get('_candidate_qty') if d.get('_mandatory_holding',{}).get('state')=='已核验' else quantity(d)
    return d.get('_allocated_qty') if d.get('_budget_active') else d.get('_execution_qty',candidate)


def order_amount(d):
    if exclusion_reason(d):return 0
    candidate=d.get('_candidate_amount') if d.get('_mandatory_holding',{}).get('state')=='已核验' else amount_value(d)
    return d.get('_allocated_amount') if d.get('_budget_active') else d.get('_execution_amount',candidate)


def orderable(d):
    return not exclusion_reason(d) and d.get('_source')=='资料匹配' and numeric(order_quantity(d)) and order_quantity(d)>0


def signed_rows(rows, profile):
    """Classify signed rows in source order, including an iterable of row blocks."""
    from new_products import is_new
    from new_product_focus import members
    from warehouse_newness import for_row
    focused=members(profile)
    for row in rows:
        if row.get('_source')!='资料匹配' or not work_eligible(row):continue
        if row.get('_preissue',{}).get('current_month'):yield '本月新品',row
        elif is_new(row):
            basis=for_row(row)
            recent=basis['new'] if basis else row['E'] in focused
            yield ('本月新品' if recent else '往期新品'),row
        elif orderable(row):yield '中心仓动销补货清单',row


def signed_groups(result, profile):
    """Keep the signed product scope and the original order within each group."""
    groups={name:[] for name in SIGNED_GROUPS}
    for group,row in signed_rows(result['rows'],profile):groups[group].append(row)
    return groups


def order_status(d):
    if exclusion_reason(d):return '已排除'
    if d.get('_preview'):return '待计算'
    if d.get('_mandatory_holding',{}).get('state')=='待核':return '待核'
    if d.get('_preissue',{}).get('delta') or d.get('_preissue',{}).get('kind')=='manual' and d['_preissue'].get('quantity')==0:
        return '人工不补' if order_quantity(d)==0 else '签发前调整'
    if d.get('_budget_gap',0)>0:return '预算部分分配' if numeric(order_quantity(d)) and order_quantity(d)>0 else '预算暂缓'
    if orderable(d):return '纳入补货'
    if numeric(d.get('AS')):return '无需补货'
    return '地采未计算' if d.get('_source')=='地采商品' else '待核'


def summarize(rows):
    total=lambda values:float(sum((Decimal(str(v)) for v in values if numeric(v)),Decimal(0)).quantize(Decimal('.01')))
    excluded=[d for d in rows if exclusion_reason(d)]
    return {'rows':len(rows),'local':sum(d['_source']=='地采商品' for d in rows),
        'issues':sum(bool(issue_text(d)) for d in rows),
        'calculable':sum(numeric(d.get('AS')) for d in rows),
        'positive':sum(orderable(d) for d in rows),
        'amount':total(order_amount(d) for d in rows),
        'calculated_positive':sum(numeric(d.get('AS')) and d['AS']>0 for d in rows),
        'calculated_amount':total(d.get('BV') for d in rows),
        'excluded':len(excluded),'excluded_positive':sum(numeric(d.get('AS')) and d['AS']>0 for d in excluded),
        'excluded_amount':total(d.get('BV') for d in excluded),
        'unpriced':sum(orderable(d) and not numeric(d.get('BV')) for d in rows),
        'candidate_amount':total(d.get('_candidate_amount',order_amount(d)) for d in rows),
        'must_amount':total(order_amount(d) for d in rows if d.get('_must_sell')),
        'must_candidate_amount':total(d.get('_candidate_amount',order_amount(d)) for d in rows if d.get('_must_sell')),
        'budget_deferred':sum(d.get('_budget_gap',0)>0 for d in rows),'holding_gaps':sum(d.get('_holding_gap',False) for d in rows),
        'exception':sum(bool(d.get('_audit',{}).get('applied')) for d in rows),
        'extra':round(sum(d.get('_audit',{}).get('incremental_amount',0) for d in rows),2)}


def quantity(d):
    if d['_source']=='地采商品':return '未计算'
    return d.get('AS') if numeric(d.get('AS')) else '待核' if not d.get('_preview') else '待计算'


def amount_value(d):
    if d['_source']=='地采商品':return '未计价'
    return d.get('BV') if numeric(d.get('BV')) else '待核' if not d.get('_preview') else '待计算'


def coverage(d):
    if not all(numeric(d.get(k)) for k in ['AN','AM','K','L','AS']):return None
    rate=d['AN']*d['AM']
    return (max(d['K'],0)+max(d['L'],0)+d['AS'])/rate if rate>0 else None


def _execution_explanation(d):
    if exclusion_reason(d):return '默认不补：'+exclusion_reason(d)+'；计算结果仅供参考'
    if issue_text(d):return issue_text(d)
    adjustment=d.get('_preissue',{})
    if adjustment.get('delta') or adjustment.get('kind')=='manual' and adjustment.get('quantity')==0:
        return f"签发前调整：自动{fmt(adjustment['automatic_quantity'])} → 最终{fmt(adjustment['quantity'])}；{adjustment['reason']}"
    if d.get('_budget_overrun'):return d['_budget_reason']+f"；本店超预算{d['_budget_overrun']['overspend']:.2f}元（上限5%）"
    if d.get('_new_requested',0)>0:
        source=d.get('_monthly_first_fill')
        automatic='仓45天新品自动首铺 ' if source and source.get('warehouse_newness') else '本月新品自动首铺 '
        return (automatic+fmt(d['_new_requested'])+'；'+source['source'] if source else '新品首铺 · 人工选择 '+fmt(d['_new_requested']))+'；'+(d.get('_budget_reason','不限预算，按首铺候选执行'))+('；保留必采最低持有量' if d.get('_mandatory_holding',{}).get('required_quantity',0)>d['_new_requested'] else '')
    if d.get('_budget_gap',0)>0:
        return ('必卖：' if d.get('_must_sell') else '普通：')+d.get('_budget_reason','')+('；必采最低持有仍有缺口' if d.get('_holding_gap') and d.get('_mandatory_holding') else '')
    if d.get('_mandatory_holding') and (d['_mandatory_holding'].get('state')!='已核验' or d['_mandatory_holding'].get('added_quantity',0)>0):
        from mandatory_holding import explanation as holding_explanation
        return holding_explanation(d)
    if d['_source']=='地采商品':return '商品资料未匹配，按地采单列；保留销量库存，本版不补造价格或数量'
    if d.get('_preview'):return '已匹配，等待计算'
    return d.get('BM','')


def extreme_recovery_reason(d):
    """Explain the recorded recovery and execution without replaying the rule."""
    if d.get('AW')!='极端缺货自动拉回':return None
    audit=d.get('_extreme_restore') or {};saved=d.get('BM') or ''
    recovery=d.get('BO');unit=str(d.get('G') or '库存单位')
    if not numeric(recovery) and numeric(d.get('AQ')):recovery=d['AQ']
    if audit.get('enabled') is False or '极端恢复已关闭' in saved:
        reason='极端缺货，已关闭自动恢复，恢复量 0 '+unit
    elif numeric(recovery) and recovery>0:
        step=d.get('BD')
        prefix='按一个出库单位恢复' if recovery==step or '恢复一个出库单位' in saved else '记录的恢复量为'
        reason=f'极端缺货，{prefix} {fmt(recovery,10)} {unit}'
    elif recovery==0 and '已有最低覆盖' in saved:
        reason='极端缺货，已有最低覆盖，无需追加，恢复量 0 '+unit
    else:return '未留存'
    executed=order_quantity(d)
    if numeric(recovery) and numeric(executed) and executed!=recovery:
        detail=_execution_explanation(d)
        adjustment=detail if detail and detail!=saved else '调整原因未留存'
        reason+='。后续调整原因 '+adjustment
    if numeric(executed):return reason+f'。最终执行量 {fmt(executed,10)} {unit}'
    return reason+'。最终执行量 '+('未留存' if executed is None else str(executed))


def net_demand_reason(d):
    recovery=extreme_recovery_reason(d)
    if recovery is not None:return recovery
    if d.get('BG')=='分位计算待复核':return '泊松分位校验待复核'
    if not all(numeric(d.get(k)) for k in ('AP','AL','K','L','AQ')):return '未留存'
    ap,al,stock,inbound=(fmt(d[k],10) for k in ('AP','AL','K','L'))
    return f'趋势后需求 {ap} ＋ 安全库存 {al} − 可用库存 {stock} − 在途库存 {inbound}，最低取 0'


def explanation(d):
    recovery=extreme_recovery_reason(d)
    return _execution_explanation(d) if recovery is None else recovery


def routing(d):return d.get('_meta',{}).get('routing',{})

