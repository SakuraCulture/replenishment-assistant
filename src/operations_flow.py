"""Order-scoped lifecycle routing. Existing orders keep their original route."""
from lifecycle_routes import CURRENT, LEGACY, demand_stage, identity, required, route, schema, stages


def adopt_plan(path, heads, progress=lambda _:None, cancel=None):
    """Use the exact same reconciliation/save contract as file-based returns."""
    import copy
    from contextlib import closing
    import purchases,order_lifecycle as lc
    from runtime_support import checkpoint
    plans=[];seen=set()
    with closing(purchases.connect(path)) as db:
        db.execute('BEGIN')
        for head in heads:
            checkpoint(cancel);h=lc.full_head(path,head);k=identity(h)
            if k in seen:raise ValueError('同一仓单重复选择')
            seen.add(k);purchases.validate_head(db,h)
            basis=lc.upstream(db,h,'operations')
            if not basis['ready']:raise ValueError(h['number']+'：回传尚未确认，请先完成回传；可留存运营文件待核')
            doc=lc.current(db,h,'return')
            if doc:rows=copy.deepcopy(doc['payload']['rows'])
            else:
                rows=[dict(sku=s,name=p.get('name'),quantity=str(p['quantity']),base_quantity=str(p['quantity']),unit='库存单位',factor=None,conversion_basis=None,amount=None,currency='',document_number='',document_date=None,note=None,sources=[]) for s,p in basis['products'].items()]
            p=lc.make_plan(db,h,'operations',rows,[])
            p['payload']['source_kind']='采用已确认回传'
            p['digest']=lc.fingerprint(p['payload'])
            old=lc.current(db,h,'operations');p['changed']=not old or old['digest']!=p['digest']
            plans.append(p)
            progress({'done':len(plans),'total':len(heads)})
    if not plans:raise ValueError('请勾选要采用的仓单')
    return {'plans':plans,'files':[]}


def adopt(path,heads,progress=lambda _:None,cancel=None):
    import order_lifecycle as lc
    return lc.commit(path,adopt_plan(path,heads,progress,cancel),progress,cancel)
