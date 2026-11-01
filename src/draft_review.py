"""Read-only projections for an unapplied final list. No model/budget execution."""
from decimal import Decimal
import preissue as rules
import result_semantics as sem
import review_amounts as money


def project(base, edit, lists):
    if not edit or rules.blocked(base):return base
    q=edit['quantity'];kind=edit.get('kind','manual');before=sem.order_quantity(base)
    if q==before and q!=0 and kind=='manual' and not edit.get('list_revision'):return base
    amount=rules.money_value(rules.decimal(q)*rules.decimal(base['V']))
    view=dict(base,_execution_qty=q,_execution_amount=amount)
    if q!=before:view['_budget_gap']=0
    if base.get('_budget_active'):view.update(_allocated_qty=q,_allocated_amount=amount)
    view['_preissue']={'kind':kind,'automatic_quantity':before,'automatic_amount':sem.order_amount(base),
                      'current_month':rules.current_month(base) and (kind!='manual' or edit.get('list_revision') in lists)}
    return view


def contribution(base, view):
    out=money.empty();out.update(total=Decimal(0),total_unknown=0,new_delta=Decimal(0),new_delta_unknown=0)
    amount=sem.order_amount(view);before=sem.order_amount(base)
    known=sem.numeric(amount);old_known=sem.numeric(before)
    if known:out['total']=Decimal(str(amount))
    elif view.get('_source')=='资料匹配' and sem.work_eligible(view):out['total_unknown']=1
    for key,active in [('manual',money.is_manual(view)),('new',money.is_new(view))]:
        if not active:continue
        out[key+'_count']=1
        if known:out[key+'_amount']=Decimal(str(amount))
        else:out[key+'_unknown']=1
        delta='manual_delta' if key=='manual' else 'new_delta';unknown='delta_unknown' if key=='manual' else 'new_delta_unknown'
        if known and old_known:out[delta]=Decimal(str(amount))-Decimal(str(before))
        else:out[unknown]=1
    return out


class DraftReview:
    """One initial scan; one changed SKU updates its store totals in O(1)."""
    def __init__(self,results,edits,lists):
        self.base={s:rules.original(r) for s,r in results.items()};self.edits=edits;self.lists=lists
        self.rows={(s,d['E']):d for s,r in self.base.items() for d in r['rows']}
        self.views={};self.parts={};self.totals={s:{**money.empty(), 'total':Decimal(0),'total_unknown':0,'new_delta':Decimal(0),'new_delta_unknown':0} for s in results}
        for key in self.rows:self.update(key)
    def update(self,key):
        s,sku=key;base=self.rows[key];view=project(base,self.edits[s].get(sku),self.lists[s]);part=contribution(base,view)
        previous=self.parts.get(key,{})
        for k,v in part.items():self.totals[s][k]+=v-previous.get(k,0)
        self.parts[key]=part;self.views[key]=view
    def summary(self,store=None):
        keys=self.totals.keys() if not store else [store];out={k:sum((self.totals[s][k] for s in keys),Decimal(0) if isinstance(v,Decimal) else 0) for k,v in next(iter(self.totals.values())).items()}
        return {k:v.quantize(Decimal('.01')) if isinstance(v,Decimal) else v for k,v in out.items()}
