"""Store-wide budget layer. It never mutates AS/BV or buys beyond the candidate."""
import heapq
from quantity_contract import packs as pack_count
from decimal import Decimal,ROUND_FLOOR
from model import number

POLICY='必卖优先；同组先断货有销量，再按预测持有天数从短到长逐个最小出库单位分配；同天数低单次成本优先'
def dec(v):return Decimal(str(v))
def real_sales(d):return any(number(d.get(k)) and d[k]>0 for k in ['H','I','J'])
def stock(d):return max(d.get('K',0),0)+max(d.get('L',0),0)
def rate(d):
    return d['AN']*d['AM'] if number(d.get('AN')) and number(d.get('AM')) and d['AN']*d['AM']>0 else None

def apply(rows,budget,checkpoint=lambda:None,*,allow_overrun=True):
    """rows have validated _candidate_qty/_candidate_amount and _must_sell tags."""
    if isinstance(budget,bool):raise ValueError('预算必须为金额')
    limit=dec(budget)
    if not limit.is_finite() or limit<0 or limit!=limit.quantize(Decimal('.01')):raise ValueError('预算必须为非负金额，最多两位小数')
    remaining=limit;valid=[];unpriced=[]
    for i,d in enumerate(rows):
        d.pop('_quantity_normalization',None)
        d.pop('_budget_overrun',None)
        checkpoint();q=d.get('_candidate_qty')
        d['_budget_active']=True
        if not number(q):
            d.update(_allocated_qty=q,_allocated_amount=d.get('_candidate_amount'),_budget_reason='原数量待核，未参与资金分配',_budget_gap=0,_holding_gap=False);continue
        d.update(_allocated_qty=0,_allocated_amount=0,_budget_gap=0,_holding_gap=False)
        if q<=0:d['_budget_reason']='无可执行补货需求或已按状态排除';continue
        price,step=d.get('V'),d.get('BD')
        if not number(price) or price<=0 or not number(step) or step<=0:
            d.update(_budget_reason='价格或最小出库单位待核，未占用预算',_budget_gap=q);unpriced.append(i);continue
        try:packs=pack_count(q,step)
        except ValueError as exc:raise ValueError('预算前建议不符合最小出库单位，不能安全分配：'+str(d.get('E'))+'；'+str(exc)) from exc
        valid.append({'i':i,'step':dec(step),'price':dec(price),'cost':dec(step)*dec(price),'packs':int(packs),'bought':0})
    def key(x):
        d=rows[x['i']];held=dec(stock(d))+x['step']*x['bought'];r=rate(d)
        group=0 if held==0 and real_sales(d) else 1 if r and held/dec(r)<dec(d.get('AH') or 0) else 2 if r else 3
        days=held/dec(r) if r else Decimal('Infinity')
        return group,days,x['cost'],str(d.get('E')),x['i']
    for mandatory in [True,False]:
        tier=[x for x in valid if bool(rows[x['i']].get('_must_sell'))==mandatory]
        needed=sum((x['packs']*x['cost'] for x in tier),Decimal(0))
        if needed<=remaining:
            for x in tier:x['bought']=x['packs']
            remaining-=needed;continue
        heap=[(key(x),x) for x in tier if x['packs']>0];heapq.heapify(heap)
        while heap:
            checkpoint();_,x=heapq.heappop(heap)
            if x['cost']>remaining:continue
            x['bought']+=1;remaining-=x['cost']
            if x['bought']<x['packs']:heapq.heappush(heap,(key(x),x))
    # Keep the original target allocation intact. Only then consider ONE extra
    # outbound unit, with the original priority/tie key, under the 105% ceiling.
    extra=None;ceiling=limit*Decimal('1.05') if allow_overrun else limit
    if allow_overrun and limit>0:
        candidates=[x for x in valid if x['bought']<x['packs'] and x['cost']>remaining and limit-remaining+x['cost']<=ceiling]
        if candidates:
            checkpoint();extra=min(candidates,key=lambda x:(not bool(rows[x['i']].get('_must_sell')),key(x)))
            extra['bought']+=1;remaining-=extra['cost']
            rows[extra['i']]['_budget_overrun']={'quantity':float(extra['step']),'unit_cost':float(extra['cost']),'overspend':float(-remaining),'budget':float(limit),'ceiling':float(ceiling),'rule':'目标预算分配后最多追加一个最小出库单位，超支不超过5%'}
    for x in valid:
        d=rows[x['i']];q=x['step']*x['bought'];money=q*x['price']
        gap=dec(d['_candidate_qty'])-q
        if x['bought']==x['packs'] and gap!=0:
            d['_quantity_normalization']={'original':d['_candidate_qty'],'effective':float(q),'step':float(x['step']),'reason':'原有严格容差内的步长尾差'}
            gap=Decimal(0)
        d.update(_allocated_qty=float(q),_allocated_amount=float(money),_allocated_amount_exact=str(money),_budget_gap=float(gap))
        d['_budget_reason']='已足额分配' if d['_budget_gap']==0 else '预算不足，仅分配部分最小出库单位' if q>0 else '预算不足或剩余资金不足一个最小出库单位，暂缓'
        if d.get('_budget_overrun'):d['_budget_reason']+='；末尾追加一个最小出库单位，有限超支'
    for d in rows:
        d['_holding_gap']=bool(d.get('_budget_gap',0)>0 and number(d.get('_allocated_qty')) and d['_allocated_qty']==0 and number(d.get('K')) and number(d.get('L')) and stock(d)==0 and real_sales(d))
    spent=limit-remaining
    actual=sum((dec(d.get('_allocated_amount_exact','0')) for d in rows),Decimal(0))
    if spent!=actual or spent>ceiling or spent>limit and extra is None:raise RuntimeError('预算核对失败，未生成可执行清单')
    must_spent=sum((dec(rows[x['i']].get('_allocated_amount_exact','0')) for x in valid if rows[x['i']].get('_must_sell')),Decimal(0))
    report={'enabled':True,'limit':float(limit),'spent':float(spent),'remaining':float(remaining),'spent_exact':str(spent),
        'must_spent':float(must_spent),'other_spent':float(spent-must_spent),'deferred':sum(d.get('_budget_gap',0)>0 for d in rows),
        'holding_gaps':sum(d.get('_holding_gap',False) for d in rows),'unpriced':len(unpriced),'policy':POLICY}
    if allow_overrun:report['overrun']={'ceiling':float(ceiling),'amount':float(max(-remaining,Decimal(0))),'sku':str(rows[extra['i']].get('E')) if extra else None,'quantity':float(extra['step']) if extra else 0,'max_units':1,'ratio':.05}
    return report
