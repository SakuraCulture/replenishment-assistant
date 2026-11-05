"""Bounded probability-only trials over one immutable prepared batch.

Calls the existing whole-store model/allocator. This module does not implement
forecast, rounding, budget, admission or routing rules.
"""
import copy
from decimal import Decimal
import uuid
import automation_jobs
import parameter_schema
import result_semantics as sem
from workbench_actions import record_settings
from runtime_support import checkpoint
from order_analytics import num,digest


def candidates(prepared,categories,steps):
    if not categories or len(categories)!=len(set(categories)):raise ValueError('请明确选择不重复的分类参数组')
    if not 1<=len(steps)<=2:raise ValueError('每次最多两个候选')
    if any(num(s) is None or not Decimal(0)<abs(num(s))<=Decimal('.05') for s in steps):raise ValueError('每次改动须大于0且不超过5个百分点；2.5个百分点传0.025')
    result=[];seen=set()
    for step in steps:
        p={**prepared,'records':copy.deepcopy(prepared['records']),'jobs':copy.deepcopy(prepared['jobs']),
           'explicit_parameters':list(prepared['codes'])};changes=[]
        for s in p['codes']:
            record=p['records'][s];config=record['preset']['config']
            for name in categories:
                if name not in config['weight_profiles']:raise ValueError(s+'没有分类参数组：'+name)
                old=config.get('profile_parameters',{}).get(name,{}).get('probability',config['parameters']['probability'])
                value=float(min(Decimal('.99'),max(Decimal('.50'),num(old)+num(step))))
                if num(value)==num(old):continue
                origin='分类覆盖' if 'probability' in config.get('profile_parameters',{}).get(name,{}) else '沿用通用值，本次新增分类覆盖'
                config.setdefault('profile_parameters',{}).setdefault(name,{})['probability']=value
                changes.append({'store':s,'category':name,'old':old,'new':value,'source':origin})
            parameter_schema.validate(config)
            p['jobs'][s]=record_settings(record,p['profile'],p['loaded'].get('catalogs'),prepared['jobs'][s]['admission'])
        key=digest({s:r['preset']['config'] for s,r in p['records'].items()})
        if changes and key not in seen:result.append({'prepared':p,'changes':changes,'step':step});seen.add(key)
    if not result:raise ValueError('选定参数已在边界，没有不同候选')
    return result


def compute(p,progress,cancel):
    results={};states={}
    def notify(code,state,value,error):
        states[code]={'name':p['loaded']['stores'][code],'state':state,'error':error}
        progress(p['loaded']['stores'][code]+'：'+state)
        if value is not None:results[code]=value
    automation_jobs.calculate(p,p['codes'],notify,cancel)
    checkpoint(cancel)
    if set(results)!=set(p['codes']) or any(states.get(s,{}).get('state')!='已完成' for s in p['codes']):
        raise ValueError('试算未全部完成，不能比较或采用：'+str({s:v for s,v in states.items() if v['state']!='已完成'}))
    return {'results':{s:results[s] for s in p['codes']},'states':{s:states[s] for s in p['codes']},'codes':list(p['codes']),'prepared':p}


def strict_baseline(current,baseline):
    """Only explicitly dynamic/non-computational top-level fields are projected."""
    fields=('rows','baseline','summary','budget','config','warehouses','enabled','must_list','must_list_audit','sources',
            'checks','effective_settings','import_audit','catalogs','supply_route')
    for code,r in baseline.items():
        old=current.get(code)
        if old is None:continue
        for field in fields:
            if field in old or field in r:
                if type(old.get(field)) is not type(r.get(field)) or old.get(field)!=r.get(field):
                    raise ValueError(code+'基线重算与当前结果不一致：'+field+'；先核对当前输入与参数，不继续调参')


def metrics(before,after):
    if list(before)!=list(after):raise ValueError('候选门店顺序发生变化')
    stores=[];details=[];hard=[];risks=[]
    for code,base in before.items():
        rows=base['rows'];other=after[code]['rows']
        if [r['E'] for r in rows]!=[r['E'] for r in other] or len({r['E'] for r in rows})!=len(rows):raise ValueError('商品集合或顺序发生变化')
        values={k:Decimal(0) for k in ('before','after','model_before','model_after','candidate_before','candidate_after','must_before','must_after')}
        bins={'≤保护期':0,'保护期至+2天':0,'+2至+4天':0,'超过+4天':0,'无预测参考':0};thresholds={21:0,30:0,60:0}
        distributions={kind:{stage:{'bins':dict.fromkeys(bins,0),'over_days':dict.fromkeys(thresholds,0)} for stage in ('before','after')} for kind in ('单件','箱规','箱规待核')}
        changed=0
        for a,b in zip(rows,other):
            if a.get('_source')!='资料匹配' or sem.exclusion_reason(a):continue
            for fixed in ('H','I','J','K','L','N','O','V','AU','U','AN','AM','AG','AH','BD','_must_sell'):
                if type(a.get(fixed)) is not type(b.get(fixed)) or a.get(fixed)!=b.get(fixed):raise ValueError('候选改变固定业务字段：'+code+'/'+str(a['E'])+'/'+fixed)
            qty0,qty1=num(sem.order_quantity(a)),num(sem.order_quantity(b));price=num(a.get('V'))
            if qty0 is None or qty1 is None or price is None or price<0:
                hard.append({'store':code,'sku':a['E'],'reason':'执行数量或价格待核'});continue
            for k,value in [('before',qty0*price),('after',qty1*price),('model_before',num(a.get('AS'))),('model_after',num(b.get('AS'))),('candidate_before',num(a.get('_candidate_qty'))),('candidate_after',num(b.get('_candidate_qty')))]:
                if value is not None:values[k]+=value if k in ('before','after') else value*price
            if a.get('_must_sell'):values['must_before']+=qty0*price;values['must_after']+=qty1*price
            if qty0!=qty1:changed+=1
            rate0,trend=(num(a.get(k)) for k in ('AN','AM'));stock,inbound,depth,lead=(num(a.get(k)) for k in ('K','L','AG','AH'))
            rate=rate0*trend if rate0 is not None and trend is not None else None
            coverage0=coverage1=deficit0=deficit1=None
            reason=[];available_cover=position_cover=arrival_gap=None;box=num(a.get('BD'));kind='箱规待核' if box is None else '箱规' if box>1 else '单件'
            if stock is not None and inbound is not None:
                holding=max(stock,0)+max(inbound,0)
                if any((num(a.get(k)) or 0)>0 for k in ('H','I','J')) and stock==0 and inbound==0 and qty0>0 and qty1==0:
                    reason.append('新增双零最低持有缺口')
                if rate is not None and rate>0 and depth is not None and lead is not None:
                    period=depth+lead;coverage0=(holding+qty0)/rate;coverage1=(holding+qty1)/rate
                    available_cover=max(stock,0)/rate;position_cover=holding/rate;arrival_gap=max(rate*lead-max(stock,0),0)
                    deficit0=max(period*rate-holding-qty0,0);deficit1=max(period*rate-holding-qty1,0)
                    if deficit1>deficit0:reason.append('保护期预测缺口增加')
                    gap=coverage1-period;bins['≤保护期' if gap<=0 else '保护期至+2天' if gap<=2 else '+2至+4天' if gap<=4 else '超过+4天']+=1
                    for threshold in thresholds:thresholds[threshold]+=coverage1>threshold
                    for stage,cover in (('before',coverage0),('after',coverage1)):
                        gap=cover-period;part=distributions[kind][stage]
                        part['bins']['≤保护期' if gap<=0 else '保护期至+2天' if gap<=2 else '+2至+4天' if gap<=4 else '超过+4天']+=1
                        for threshold in thresholds:part['over_days'][threshold]+=cover>threshold
                else:
                    bins['无预测参考']+=1
                    for stage in ('before','after'):distributions[kind][stage]['bins']['无预测参考']+=1
            else:hard.append({'store':code,'sku':a['E'],'reason':'销量库存依据待核'})
            record={'store':code,'sku':a['E'],'name':a.get('D'),'warehouse':a.get('AU'),'must_sell':a.get('_must_sell'),
                'box':box,'before':qty0,'after':qty1,'amount_delta':(qty1-qty0)*price,
                'coverage_before':coverage0,'coverage_after':coverage1,'deficit_before':deficit0,'deficit_after':deficit1,'risks':reason,
                'available_cover':available_cover,'position_cover':position_cover,'arrival_gap_without_timed_inbound':arrival_gap}
            details.append(record)
            if reason:risks.append(record)
        stores.append({'store':code,'name':base['store_name'],**values,'delta':values['after']-values['before'],
            'changed':changed,'coverage_bins':bins,'over_days':thresholds,'distributions':distributions})
    return {'stores':stores,'details':details,'hard_issues':hard,'risks':risks,
        'recommended':not hard and not risks,
        'note':'覆盖按基线预测日均×趋势固定参照，使用预算后执行量。可用+在途是库存位置，不代表到货前货架不断货；理论安全库存不等于实际资金节省。'}


def run(snapshot,arguments,current,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if not arguments.get('stores'):raise ValueError('请明确选择试算门店，不自动扩大到全部门店')
    p=automation_jobs.prepare(snapshot,{'mode':'trial','stores':arguments['stores']},progress,cancel)
    if not p['report']['ready']:raise ValueError('当前资料或参数尚未就绪：'+str(p['report']['issues']))
    from workbench_actions import recalculation_impact
    impact=recalculation_impact(current,p['codes'])
    if impact['manual'] or impact['new_products']:raise ValueError('已有人工改量或新品安排，请先在原工作台核对；诊断不清除这些安排')
    options=candidates(p,arguments['categories'],arguments.get('steps',[-.025,.025]))
    progress('重跑同一冻结依据的基线…');baseline=compute(p,progress,cancel);strict_baseline(current,baseline['results'])
    for n,option in enumerate(options,1):
        checkpoint(cancel);progress(f'计算候选 {n}/{len(options)}，完整重做每店预算分配…')
        calculated=compute(option['prepared'],progress,cancel);option.update(calculation=calculated,comparison=metrics(baseline['results'],calculated['results']),result_hash=digest(calculated['results']))
    return {'id':uuid.uuid4().hex,'context_token':snapshot['context_token'],'baseline':baseline,'options':options,'stores':p['codes'],
            'adopted':False,'baseline_hash':digest(baseline['results']),'rule':'probability-only-v1'}
