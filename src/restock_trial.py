"""Read-only switch previews, preserving explicit choices until final confirmation."""
import copy
import batch_service, new_products, preissue, parameter_schema
import restock_policy as policy
import result_semantics as sem
from runtime_support import checkpoint
from export_progress import RowProgress

def prepare(loaded, jobs, current, bases, drafts, configs, profile, day, progress=lambda _:None, cancel=None):
    out={'stores':{},'rows':[],'risks':[],'errors':[],'amounts':[]}
    for pos,(store,cfg) in enumerate(configs.items()):
        checkpoint(cancel);old=current[store];before=policy.result_values(old);after=policy.values(cfg)
        progress(RowProgress('准备策略对照：'+old['store_name'],pos,len(configs)))
        settings=copy.deepcopy(jobs[store]);settings['parameter_config']=copy.deepcopy(cfg);settings['parameter_signature']=parameter_schema.signature(cfg)
        auto=preissue.original(old);recomputed=before['extreme_restore']!=after['extreme_restore']
        if recomputed:
            base=batch_service.calculate_store(loaded,store,settings,profile,progress,cancel)
            base=new_products.original(base)
        else:
            if auto.get('new_products') and not auto['new_products'].get('automatic') and store not in bases:raise ValueError('缺少新品原计算快照，请先重新计算：'+old['store_name'])
            base=copy.deepcopy(bases[store]) if store in bases and auto.get('new_products') else new_products.original(auto)
            if base is auto:base=copy.deepcopy(base)
            if policy.KEY in cfg:base['config'][policy.KEY]=copy.deepcopy(cfg[policy.KEY])
            else:base['config'].pop(policy.KEY,None)
            base['effective_settings']=settings
        selections=auto.get('new_products',{}).get('selection',{}) if after['new_stocking'] else {}
        if after['new_stocking'] and not (auto.get('new_products') and not auto['new_products'].get('automatic')):
            automatic=copy.deepcopy(base);new_products.seed_monthly(automatic,settings.get('monthly_first_fill'),cancel,budget=settings.get('budget'))
            if automatic.get('budget',{}).get('enabled'):
                import allocation
                automatic['budget']=allocation.apply(automatic['rows'],automatic['budget']['limit'],lambda:checkpoint(cancel))
            automatic['summary']=sem.summarize(automatic['rows'])
        else:automatic=new_products.apply(base,selections,cancel,progress)
        overlay=drafts.get(store) or old.get('preissue',{})
        edits=copy.deepcopy(overlay.get('edits',{}));rows=preissue.identity(automatic)
        removed=[]
        if not after['new_stocking']:
            for sku,e in list(edits.items()):
                list_kind=overlay.get('lists',{}).get(e.get('list_revision'),{}).get('kind')
                if sku in rows and policy.new_request(rows[sku],list_kind or e.get('kind','manual')) and e.get('quantity',0)>0:
                    removed.append(sku);del edits[sku]
        trial=preissue.preview(automatic,edits,day,lists=overlay.get('lists',{}),cancel=cancel,progress=progress)
        out['stores'][store]={'prepared':trial,'base':base,'expected':preissue.digest(old),'removed_edits':removed,'recomputed':recomputed}
        out['risks'].extend(trial['risks']);out['errors'].extend(trial['errors'])
        proposed=trial['result'];out['amounts'].append([old['store_name'],old['summary']['amount'],proposed['summary']['amount'],'重算模型与预算' if recomputed else '仅重算新品与预算'])
        previous=preissue.identity(old)
        if list(previous)!=list(rows):raise ValueError('对照前后商品顺序变化，请重新检查输入')
        for d in proposed['rows']:
            a=previous[d['E']]
            if any(x!=y for x,y in [(a.get('AS'),d.get('AS')),(a.get('_candidate_qty'),d.get('_candidate_qty')),(sem.order_quantity(a),sem.order_quantity(d)),(sem.order_amount(a),sem.order_amount(d))]):
                out['rows'].append([old['store_name'],d['E'],d.get('D'),a.get('AS'),d.get('AS'),sem.order_quantity(a),sem.order_quantity(d),sem.order_amount(a),sem.order_amount(d)])
        checkpoint(cancel)
    progress(RowProgress('策略对照完成',len(configs),len(configs)))
    return out

def finalize(preview, current, confirmed=()):
    if preview['errors']:raise ValueError('；'.join(preview['errors']))
    results={}
    for store,item in preview['stores'].items():
        if store not in current or preissue.digest(current[store])!=item['expected']:raise ValueError('预览后结果已变化，请重新核对')
        results[store]=preissue.finalize(item['prepared'],confirmed)
    return results
