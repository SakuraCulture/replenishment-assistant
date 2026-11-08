"""Prepare one atomic payment review from the exact displayed generation records."""
import copy
from contextlib import closing
from decimal import Decimal
import jst_orders as orders
import jst_results as results
import work_repository as wr
from runtime_support import checkpoint
from export_progress import RowProgress


def selection(records,chosen=None):
    wanted={tuple(k) for k in chosen} if chosen is not None else None
    entries=[r for r in records if wanted is None or results.key(r['head']) in wanted]
    if wanted is not None and wanted!={results.key(r['head']) for r in entries}:
        raise ValueError('部分勾选订单已不在当前结果中，请刷新后重新勾选。')
    if not entries:raise ValueError('请先勾选要核对付款的门店或仓单，或使用当前筛选入口。')
    blocked=[r['head']['name']+' / '+r['head']['warehouse']+' / '+r['head']['number']+'：'+r['reason']
             for r in entries if r['state']!='ready' or not r['gid']]
    if blocked:raise ValueError('以下订单不能直接作为付款来源；本次未保存任何付款单。请处理后重试，或仅勾选有效生成单：\n'+'\n'.join(blocked))
    return copy.deepcopy(entries)


def prepare(path,entries,progress=lambda _:None,cancel=None):
    entries=selection(entries)
    identities=[results.key(r['head']) for r in entries]
    if len(set(identities))!=len(identities):raise ValueError('同一仓单不能重复加入付款核对。')
    with closing(results.connect(path)) as db:
        db.execute('BEGIN')
        try:results.validate_export(db,{'entries':entries,'allow_old':False},cancel)
        except wr.Conflict as exc:raise wr.Conflict(str(exc).replace('再导出','重新核对付款')) from exc
    plans=[]
    for i,r in enumerate(entries):
        checkpoint(cancel);h=r['head']
        try:
            p=orders.payment_plan(path,r['gid'],progress,cancel)['plans'][0]
            for row in p['payload']['rows']:
                if Decimal(str(row['base_quantity']))<=0:continue
                for field in ('price','amount'):
                    value=row.get(field)
                    if value is None or not Decimal(str(value)).is_finite() or Decimal(str(value))<0:
                        raise ValueError('商品 '+row['sku']+' 的单价或金额待核；请导入真实付款单，不以0代替。')
            plans.append(p)
        except (ValueError,wr.Conflict) as exc:
            raise type(exc)(h['name']+' / '+h['warehouse']+' / '+h['number']+'：'+str(exc)) from exc
        progress(RowProgress(f'准备付款核对 {i+1}/{len(entries)}',i+1,len(entries)))
    return {'plans':plans,'files':[]}
