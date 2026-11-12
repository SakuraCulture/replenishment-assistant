"""Optional, read-only linkage to existing published stockout days. No initialization."""
from pathlib import Path
import order_analytics as analytics
from runtime_support import checkpoint

def read(path,spec,filters=None,progress=lambda _:None,cancel=None):
    from stockout_repository import Repository as Stores
    import stockout_metrics as sm
    import warehouse_stockout as wh
    root=Path(path).parent;store=Stores(root);warehouse=wh.Repository(root)
    end=min(spec['to'],spec['as_of'][:10]) if spec.get('as_of') else spec['to']
    q={'start':spec['from'],'end':end,'view':'all','exclude_stopped':True,'exclude_disabled':True,'filters':{}}
    if spec.get('stores'):q['filters']['store']=spec['stores']
    if spec.get('warehouse'):q['filters']['warehouse']=[spec['warehouse']]
    filters=filters or {}
    for key,target in [('store','store'),('warehouse','warehouse'),('upstream_supplier','supplier'),('product_category','category')]:
        if filters.get(key):q['filters'][target]=filters[key]
    if filters.get('group'):q['filters']['mandatory']=[{'必采':1,'非必采':0,'属性待核':None}[v] for v in filters['group']]
    out={'query':q,'filter_scope':filters,'store':[],'warehouse':[],'notes':[],
         'basis':'当前已发布观测日报；考核品固定剔除，门店禁采/停采与仓禁用剔除。不由订单数量推算实际库存。历史截至不回滚独立分析库的日报修订。'}
    if end<spec['from']:
        out['notes'].append('历史截至早于观测范围');out['token']=analytics.digest(out);return out
    for title,repo in [('门店',store),('大仓',warehouse)]:
        checkpoint(cancel)
        if not repo.path.exists():out['notes'].append(title+'缺货率尚无留存分析库');continue
        try:
            progress('读取'+title+'已发布缺货率日报，不采集资料…')
            if title=='门店':out['store']=sm.trend(repo,q,cancel=cancel)
            else:
                wq={k:v for k,v in q.items() if k in ('start','end','filters')};wq['filters']={k:v for k,v in q['filters'].items() if k in ('warehouse','supplier','category')}
                out['warehouse']=list(wh.iter_cuts(repo,wq,['day','warehouse'],cancel=cancel))
                if spec.get('stores'):out['notes'].append('大仓缺货率按所选供货仓统计，不以门店编码过滤大仓商品基数')
        except Exception as exc:
            from runtime_support import Cancelled
            if isinstance(exc,Cancelled):raise
            out['notes'].append(title+'留存趋势未读取：'+str(exc))
    out['token']=analytics.digest(out);return out

def validate(path,spec,snapshot,cancel=None):
    if read(path,spec,snapshot.get('filter_scope'),cancel=cancel)['token']!=snapshot['token']:
        raise ValueError('留存缺货率日报已变化，请重新加载趋势；未混入新旧口径')
