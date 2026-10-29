import copy,json
import batch_service,engine

def metrics(result):
    distribution={'箱规商品':{},'无箱规约束':{}}
    for d in result['rows']:
        group='箱规商品' if semantics.numeric(d.get('BD')) and d['BD']>1 else '无箱规约束';days=semantics.coverage(d)
        bucket='无正预测或待核' if not semantics.numeric(days) else next((label for limit,label in [(7,'≤7天'),(14,'7～14天'),(21,'14～21天'),(30,'21～30天'),(60,'30～60天')] if days<=limit),'>60天')
        distribution[group][bucket]=distribution[group].get(bucket,0)+1
    s=result['summary'];return {'计算金额':s['calculated_amount'],'预算前需求金额':s['candidate_amount'],'执行金额':s['amount'],'必卖未满足金额':round(s['must_candidate_amount']-s['must_amount'],2),'最低持有缺口':s['holding_gaps'],'待核':s['issues'],'补货未计价':s['unpriced'],'按计算建议持有天数分布':distribution}

def compare(loaded,jobs,profile,config,progress=lambda s:None,cancel=None):
    report=[]
    for store,settings in jobs.items():
        engine.checkpoint(cancel);progress('参数试算：'+store);a=batch_service.calculate_store(loaded,store,settings,profile)
        changed={**copy.deepcopy(settings),'parameter_config':copy.deepcopy(config),'warehouses':copy.deepcopy(config['warehouses']),'exception':config['exception']}
        engine.checkpoint(cancel);b=batch_service.calculate_store(loaded,store,changed,profile)
        changed_count=sum(x['AS']!=y['AS'] or semantics.order_quantity(x)!=semantics.order_quantity(y) or semantics.order_amount(x)!=semantics.order_amount(y) for x,y in zip(a['rows'],b['rows']))
        report.append({'门店':store,'变化商品数':changed_count,'当前':metrics(a),'候选':metrics(b)})
    return '同一数据快照、同一预算、同一供货顺序。仅替换候选参数；未覆盖作业。\n持有天数使用预测日均及计算建议，未扣门店预算；箱规分组展示，无效预测单列。\n'+json.dumps(report,ensure_ascii=False,indent=2)
