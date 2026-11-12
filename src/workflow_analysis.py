"""Frozen seven-node projections and weighted ratio trends. No business writes."""
from collections import OrderedDict
from datetime import date,timedelta
from decimal import Decimal
import json
import order_lifecycle as lc
from runtime_support import checkpoint

STAGES=('model','signed','return','operations','allocation','payment','receipt')
NAMES=dict(model='模型建议',signed='补货签发',**{'return':'门店回传'},operations='运营签发',allocation='聚水潭分配',payment='付款下单',receipt='实际收货')
PAIRS=(('model','signed'),('signed','return'),('return','payment'),('payment','receipt'),('model','receipt'),
       ('return','operations'),('operations','allocation'),('allocation','payment'))
RATIOS=('签发／建议','回传／签发','付款／回传','收货／付款','收货／建议','运营／回传','分配／运营','付款／分配')
METRICS={'quantity':'商品数量','skus':'商品款数','value':'签发价货值'}

class AllocationReader:
    """Scan small event identities once, not the full journal once per order."""
    def __init__(self,db,spec,cancel=None):
        self.db=db;self.latest={}
        if db.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name IN ('jst_events','normal_orders')").fetchone()[0]!=2:return
        clauses=['o.day>=?','o.day<=?',"e.action IN ('生成聚水潭订单','库存不足未生成','撤销未下单生成单')"];args=[spec['from'],spec['to']]
        if spec.get('as_of'):clauses.append('julianday(e.at)<=julianday(?)');args.append(spec['as_of'])
        if spec.get('stores'):clauses.append('o.store IN ('+','.join('?'*len(spec['stores']))+')');args.extend(spec['stores'])
        if spec.get('warehouse'):clauses.append('e.warehouse=?');args.append(spec['warehouse'])
        for oid,warehouse,ident in db.execute('SELECT e.oid,e.warehouse,e.id FROM jst_events e JOIN normal_orders o ON o.oid=e.oid WHERE '+' AND '.join(clauses)+' ORDER BY e.id DESC',args):
            checkpoint(cancel);self.latest.setdefault((oid,warehouse),ident)
    def event(self,head):
        ident=self.latest.get((head['oid'],head['warehouse']))
        return self.db.execute('SELECT action,detail,at FROM jst_events WHERE id=?',(ident,)).fetchone() if ident is not None else False

def allocation_fact(db,head,view,as_of=None,*,event=None):
    """Only a saved execution decision matching this exact signed/dependency set."""
    import jst_orders as j
    if not j.exists(db):return None
    if event is not None:pass
    elif as_of:
        event=db.execute('''SELECT action,detail,at FROM jst_events WHERE oid=? AND warehouse=?
          AND action IN ('生成聚水潭订单','库存不足未生成','撤销未下单生成单')
          AND julianday(at)<=julianday(?) ORDER BY id DESC LIMIT 1''',(head['oid'],head['warehouse'],as_of)).fetchone()
    else:
        event=db.execute('''SELECT action,detail,at FROM jst_events WHERE oid=? AND warehouse=?
          AND action IN ('生成聚水潭订单','库存不足未生成','撤销未下单生成单') ORDER BY id DESC LIMIT 1''',(head['oid'],head['warehouse'])).fetchone()
    if not event or event[0]=='撤销未下单生成单':return None
    detail=json.loads(event[1]);p=None
    if event[0]=='生成聚水潭订单':
        p=j.decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(detail['gid'],)).fetchone())
        if not as_of and j.active(db,head)!=detail['gid']:return None
    elif detail.get('runid'):
        run=j.decode(db.execute('SELECT payload,digest FROM jst_runs WHERE runid=?',(detail['runid'],)).fetchone())
        p=next((p for p in (run or {}).get('plans',[]) if p['head']['oid']==head['oid'] and p['head']['warehouse']==head['warehouse']),None)
    if not p or p['head']['revision']!=head['revision'] or p['basis'].get('signed_digest')!=head['digest']:return None
    for stage,token in p['basis'].get('dependencies',{}).items():
        doc=view['documents'].get(stage)
        actual=lc.rules.basis_token(doc)
        if stage=='return' and doc is None and view.get('legacy_return'):
            old=view['legacy_return'];actual={'version':old['version'],'digest':old['digest']}
        if actual!=token or not view['health'].get(stage):return None
    return {'rows':{r['sku']:r for r in p['rows']},'saved':event[2],'gid':p['gid'],
            'digest':j.digest(p),'catalog':p['catalog'],'bid':p['bid']}

def enrich(rows,head,view,allocation,signed_at):
    doc=view['documents'].get('operations');ops={r['sku']:r for r in doc['payload']['rows']} if doc else {}
    saved={'model':None,'signed':signed_at,**{s:d['saved'] if d else None for s,d in view['documents'].items()},'allocation':allocation['saved'] if allocation else None}
    versions={s:{'version':d['version'],'digest':d['digest']} for s,d in view['documents'].items() if d}
    allocation_basis={k:allocation[k] for k in ('gid','digest','catalog','bid')} if allocation else None
    for r in rows:
        sku=r['sku'];r['flow_version']=view['flow_version']
        q=ops.get(sku,{}).get('base_quantity')
        r['quantities']['operations']=Decimal(str(q)) if q is not None else Decimal(0) if doc and doc['payload'].get('schema')==lc.SCHEMA else None
        r['valid']['operations']=bool(view['health'].get('operations')) and r['quantities']['operations'] is not None
        r['states']['operations']='已确认' if r['valid']['operations'] else '待重新核对' if doc else '未启用／未留存' if view['flow_version']==1 else '未导入'
        a=(allocation or {}).get('rows',{}).get(sku)
        r['quantities']['allocation']=Decimal(str(a['quantity'])) if a else Decimal(0) if allocation else None
        r['valid']['allocation']=bool(allocation) and not (a or {}).get('issue',False)
        r['states']['allocation']='已分配' if r['valid']['allocation'] else '执行字段待核' if allocation else '未分配／依据已变化'
        r['stage_saved']=saved;r['stage_versions']=versions;r['allocation_basis']=allocation_basis
    return rows

def selected(rows,filters=None):
    filters=filters or {}
    return [r for r in rows if all(not v or r.get(k) in v for k,v in filters.items())]

def metric(rows,stage,kind):
    valid=[r for r in rows if r['scope']=='纳入' and r['valid'].get(stage)]
    if kind=='skus':return Decimal(len({r['sku'] for r in valid if r['quantities'][stage]>0})) if valid else None
    values=[]
    for r in valid:
        q=r['quantities'][stage]
        if kind=='quantity':values.append(q)
        elif q==0:values.append(Decimal(0))
        elif r['price'] is not None:values.append(q*r['price'])
    return sum(values,Decimal(0)) if values else None

def pair(rows,a,b,kind):
    common=[r for r in rows if r['scope']=='纳入' and r['valid'].get(a) and r['valid'].get(b)
            and (kind!='value' or r['price'] is not None or r['quantities'][a]==r['quantities'][b]==0)]
    x,y=metric(common,a,kind),metric(common,b,kind)
    return {'before':x,'after':y,'rate':y/x if x is not None and x>0 and y is not None else None,
            'orders':len({r['order'] for r in common}),'rows':len(common),'skus':len({r['sku'] for r in common})}

def summary(rows,kind='value'):
    if kind not in METRICS:raise ValueError('未知过程指标')
    return {'stages':{s:metric(rows,s,kind) for s in STAGES},'pairs':[pair(rows,a,b,kind) for a,b in PAIRS],
            'orders':len({r['order'] for r in rows}),'rows':len(rows),'pending':sum(r['scope']=='待核' for r in rows),
            'units':sorted({str(r.get('unit') or '单位未留存') for r in rows})}

def period(day,grain):
    d=date.fromisoformat(day)
    return d.isoformat() if grain=='day' else (d-timedelta(days=d.weekday())).isoformat() if grain=='week' else d.replace(day=1).isoformat()

def end_period(day,grain):
    d=date.fromisoformat(day)
    return day if grain=='day' else (d+timedelta(days=6)).isoformat() if grain=='week' else ((d.replace(day=28)+timedelta(days=4)).replace(day=1)-timedelta(days=1)).isoformat()

def trends(rows,start,end,grain='day',kind='value',*,cancel=None):
    if grain not in ('day','week','month'):raise ValueError('趋势周期无效')
    buckets=OrderedDict();d=date.fromisoformat(start);last=date.fromisoformat(end)
    if (last-d).days>3660:raise ValueError('趋势一次最多10年，请缩小日期范围')
    while d<=last:
        key=period(d.isoformat(),grain);buckets.setdefault(key,[]);d+=timedelta(days=1)
    for r in rows:
        if start<=r['day']<=end:buckets[period(r['day'],grain)].append(r)
    out=[];prior=None
    for day,rs in buckets.items():
        checkpoint(cancel);m=summary(rs,kind);values=[p['rate'] for p in m['pairs']]
        delta=[(v-p)*100 if v is not None and p is not None else None for v,p in zip(values,prior or [None]*len(PAIRS))]
        out.append({'period':day,'end':end_period(day,grain),'partial':day<start or end_period(day,grain)>end,
                    'grain':grain,'metric':kind,**m,'delta_pp':delta})
        prior=values  # Do not silently skip a missing intervening period.
    return out

def action_trends(rows):
    """Action-day throughput is separate from the business-day ratio cohort."""
    groups=OrderedDict();seen=set()
    for r in rows:
        for stage,saved in r.get('stage_saved',{}).items():
            identity=(r['order'],stage,saved)
            if not saved or stage not in STAGES or identity in seen or not r['valid'].get(stage):continue
            seen.add(identity);key=(saved[:10],stage);groups.setdefault(key,set()).add(r['order'])
    return [{'day':day,'stage':stage,'orders':len(orders)} for (day,stage),orders in sorted(groups.items())]
