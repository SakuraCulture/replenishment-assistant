"""Reserve warehouse generations and bind each executable share to its frozen target."""
from collections import defaultdict
from contextlib import ExitStack
from decimal import Decimal, ROUND_CEILING
from pathlib import Path
import json

import jst_allocation as allocation
import jst_batches
import jst_orders
import lifecycle_store
import material_orders
import material_stock
import normal_orders
import special_plans as plans
import warehouse_catalog
import warehouse_execution
import work_repository as wr
from catalog_write_lock import acquire
from jst_order_store import digest
from special_plans import now, instant, uid, text, number

ZERO=Decimal(0)


def commitments(db,plan):
    used, shared = defaultdict(Decimal), defaultdict(list)
    if not plans.exists(db,'special_claims'):
        return used,shared
    sql='''SELECT c.plan,c.line,c.store,c.sku,c.gid,c.qty,r.qty,n.version
           FROM special_claims c JOIN special_packages p ON p.oid=c.oid
           JOIN special_runs n ON n.run=p.run
           LEFT JOIN special_releases r ON r.gid=c.gid AND r.sku=c.sku ORDER BY c.rowid DESC'''
    balances={}
    settled={}
    if plans.exists(db,'special_groups'):
        for group, in db.execute('SELECT id FROM special_groups'):
            receipt=plans.latest(db,group,'正式实收')
            if receipt:
                for r in receipt['body']['rows']:
                    if not Decimal(r['pending']):
                        for c in r['shares']:
                            settled[c['oid'],c['line']]=Decimal(c['quantity'])
    generations={gid:oid for gid,oid in db.execute('SELECT gid,oid FROM special_packages')}
    for owner,line,store,sku,gid,qty,released,version in db.execute(sql):
        qty=Decimal(qty)
        key=gid,sku
        remaining=balances.setdefault(key,Decimal(released or '0'))
        take=min(qty,remaining)
        balances[key]-=take
        effective=qty-take
        posted=settled.get((generations[gid],line))
        if posted is not None:
            effective=posted
        if owner==plan:
            used[line]+=effective
        if effective and posted is None:
            shared[store,sku].append((gid,effective))
    return used,shared


def _observations(db,plan):
    history=defaultdict(list)
    for store,sku,raw in db.execute('SELECT store,sku,body FROM special_observations WHERE plan=? ORDER BY at,rowid',(plan,)):
        history[store,sku].append(json.loads(raw))
    return history


def _holding(line,target,history,supply,maintain):
    def observed(row):
        return (number(row['available'],'门店可用')+number(row['transit'],'门店在途')
                if row.get('available') is not None and row.get('transit') is not None else None)
    current=history[-1] if history else {}
    value=observed(current)
    attained=next((r['snapshot'] for r in history if observed(r) is not None and observed(r)>=target),None)
    if value is None:
        return ZERO,'门店快照缺可用或在途',None,attained
    if not maintain and attained or value>=target:
        return ZERO,'观测已达标',text(value),attained
    promise=number(current.get('promise',0),'外部承诺')
    included=current.get('included')
    if promise and (included is None or not current.get('evidence') or number(included,'重合量')>promise):
        return ZERO,'外部承诺与在途重合待核',text(value),attained
    excluded=promise-number(included or 0,'重合量')
    for gid,qty in supply:
        coverage=current.get('generations',{}).get(gid)
        if coverage is None:
            # Locally claimed after this observation cannot be inside its snapshot.
            if gid not in current.get('_after',()):
                return ZERO,'原代次 '+gid+' 是否在快照内待核',text(value),attained
            overlap=ZERO
        else:
            overlap=number(coverage,'代次重合量')
            if overlap>qty or not current.get('evidence'):
                return ZERO,'代次重合量或原件依据待核',text(value),attained
        excluded+=qty-overlap
    return max(ZERO,target-value-excluded),'',text(value),attained


def _allocate(group,stock,strategy):
    if not group:
        return
    # Each group shares one warehouse SKU and one actual issue unit.
    step=number(group[0]['step'],'最小出库单位')
    if any(Decimal(r['step'])!=step for r in group):
        for row in group:
            row['reason']='同仓商品的最小出库单位冲突'
        return
    capacity=int(stock//step)
    wants=[]
    for row in group:
        demand=Decimal(row['demand'])
        units=int(demand//step)
        if row.get('round_up') and demand%step:
            units+=1
        wants.append(units)
    assigned=[0]*len(wants)
    if strategy=='优先配齐':
        for i,n in enumerate(wants):
            assigned[i]=min(capacity,n)
            capacity-=assigned[i]
    else:
        active=[i for i,n in enumerate(wants) if n]
        while active and capacity:
            whole=min(capacity//len(active),min(wants[i]-assigned[i] for i in active))
            if not whole:
                for i in active[:capacity]:
                    assigned[i]+=1
                break
            for i in active:
                assigned[i]+=whole
            capacity-=len(active)*whole
            active=[i for i in active if assigned[i]<wants[i]]
    for row,units in zip(group,assigned):
        qty=step*units
        demand=Decimal(row['demand'])
        row.update(before=text(stock),after=text(stock-qty),quantity=text(qty),
                   amount=text(qty*Decimal(row['price'])),extra=text(max(ZERO,qty-demand)))
        if demand>qty:
            row['reason']='严格整箱或最小单位不足' if demand<step or stock>=demand else '仓库存不足'
        stock-=qty


def prepare(db,path,body,*,at=None,limit=800):
    if type(limit)!=int or not 1<=limit<=30000:
        raise ValueError('子包款数无效')
    at=instant(at or now())
    plan=body['plan']
    used,shared=commitments(db,plan)
    history=_observations(db,plan)
    confirms={}
    for s,seq,raw in db.execute('SELECT store,seq,body FROM special_confirmations WHERE plan=? AND version=? ORDER BY seq',(plan,body['version'])):
        confirms[s]=(seq,json.loads(raw))
    gen_times={g:instant(t) for g,t in db.execute('SELECT gid,saved FROM jst_generations')} if shared else {}
    whs=list(dict.fromkeys(r['warehouse'] for r in body['lines']))
    catalogs,versions=warehouse_catalog.Repository(Path(path).parent).snapshots(whs)
    stocks,balances,unknown,units={},{},{},{}
    for wh,cat in catalogs.items():
        wanted={r['sku'] for r in body['lines'] if r['warehouse']==wh}
        base={'catalog':cat,'included':[]}
        base['included']=sorted(material_stock.included(db,wh,base)) if jst_orders.exists(db) else []
        stocks[wh]=base
        check=material_stock.cross_reservations(db,wh,base,wanted,material=True)
        unknown[wh]=check['uncertain']
        units[wh]={sku:item.get('商品单位') for sku,item in cat['items'].items() if sku in wanted and item.get('商品单位')}
        missing=(wanted & cat['items'].keys())-units[wh].keys()
        if missing:
            units[wh].update(material_orders._units(db,cat,missing,None))
        for sku in wanted & cat['items'].keys():
            try:
                balances[wh,sku]=number(cat['items'][sku].get('可用数'),'仓可用数')-Decimal(check['reserved'].get(sku,'0'))
            except ValueError:
                continue
    candidates=defaultdict(list)
    from admission import execution_evidence
    scopes=defaultdict(set)
    for line in body['lines']:
        scopes[line['store'],line['warehouse']].add(line['sku'])
    admissions={key:execution_evidence(path,*key,skus,catalogs=catalogs,db=db)['items'] for key,skus in scopes.items()}
    rows=[]
    store_order={store:i for i,store in enumerate(body['stores'])}
    for line in body['lines']:
        wh,sku,store=line['warehouse'],line['sku'],line['store']
        key=store,sku
        target=Decimal(line['target'])
        reply=confirms.get(store)
        chosen=Decimal(reply[1].get(line['id'],'0')) if reply else target
        reason=''
        current=None
        attained=None
        snap=None
        demand=max(ZERO,chosen-used[line['id']])
        if body['require_confirmation'] and not reply:
            reason='等待门店确认'
        elif body['mode'] in ('持有下限','任选款') and chosen:
            obs=history.get(key,[])
            if obs:
                snap=obs[-1]['snapshot']
                when=instant(obs[-1]['at'])
                obs[-1]['_after']=[gid for gid,_ in shared[key] if gen_times[gid]>when]
            demand,reason,current,attained=_holding(line,chosen,obs,shared[key],body['maintain'])
        item=catalogs.get(wh,{}).get('items',{}).get(sku)
        try:
            if not item:
                raise ValueError('仓资料缺档')
            issue=allocation.status_issue(item)
            if issue:
                raise ValueError(issue)
            denied=admissions[store,wh][sku]['reasons']
            if denied:
                raise ValueError('；'.join(denied))
            if units.get(wh,{}).get(sku)!=line['unit']:
                raise ValueError('仓库存单位不明或不一致')
            price=number(item.get('基本售价'),'仓商品单价')
            step=number(item.get('出库箱规') if line.get('strict') else line.get('minimum','1'),'出库单位')
            if step<=0 or (wh,sku) not in balances or balances[wh,sku]<0:
                raise ValueError('出库单位或仓有效余额待核')
            if sku in unknown.get(wh,{}):
                raise ValueError('原代次是否已计入本仓资料待核 '+','.join(unknown[wh][sku]))
        except ValueError as exc:
            reason=reason or str(exc)
            price=ZERO
            step=Decimal(1)
        row=dict(line,chosen=text(chosen),claimed=text(used[line['id']]),demand=text(demand),
                 confirmation=reply[0] if reply else None,snapshot=snap,observed=current,attained=attained,
                 basis=catalogs.get(wh,{}).get('revision'),catalog_row=(item or {}).get('_row'),
                 name=(item or {}).get('商品名称') or sku,status=(item or {}).get('商品状态'),price=text(price),
                 step=text(step),quantity='0',amount='0',extra='0',before=None,after=None,reason=reason,
                 window_open=plans.in_window(body,line,at))
        rows.append(row)
        if not reason and demand:
            candidates[wh,sku].append(row)
    if body['mode']=='任选款':
        for store in body['stores']:
            selection=[r for r in rows if r['store']==store and Decimal(r['chosen'])]
            reached=list(dict.fromkeys(r['sku'] for r in selection if r['reason']=='观测已达标'))
            choices=(reached+[s for s in dict.fromkeys(r['sku'] for r in selection) if s not in reached])[:body['k']]
            for row in selection:
                if row['sku'] not in choices:
                    row['reason']='本轮未选此款'
    excluded=set()
    while True:
        for k,group in candidates.items():
            selected=sorted((r for r in group if r['id'] not in excluded and r['reason']!='本轮未选此款'),
                            key=lambda r:store_order[r['store']])
            for r in selected:
                r['reason']=''
            _allocate(selected,balances[k],body['strategy'])
        broken={(r['store'],r['kit']) for r in rows if r.get('kit') and Decimal(r['quantity'])<Decimal(r['demand'])}
        blocked=[r for r in rows if r.get('kit') and (r['store'],r['kit']) in broken and r['id'] not in excluded]
        if not blocked:
            break
        for r in blocked:
            excluded.add(r['id'])
            r.update(reason='齐套组未齐',quantity='0',amount='0',before=None,after=None)
    packets=[]
    for scope,items in group_items(rows).items():
        size=limit if body['kind']=='opening' else max(1,len(items))
        for i in range(0,len(items),size):
            packets.append(dict(scope=json.loads(scope),items=items[i:i+size]))
    wave=db.execute('SELECT coalesce(max(wave),0)+1 FROM special_runs WHERE plan=?',(plan,)).fetchone()[0]
    view=dict(plan=plan,version=body['version'],wave=wave,limit=limit,rows=rows,packets=packets,
              sources={w:{'revision':s['catalog']['revision'],'sha':s['catalog']['source_sha256'],'adoption':versions.get(w),'included':s['included']} for w,s in stocks.items()})
    return view,stocks


def group_items(rows):
    groups={}
    for r in rows:
        qty=Decimal(r['quantity'])
        if not qty:
            continue
        items=groups.setdefault(wr.dumps(plans.scope(r)),{})
        item=items.get(r['sku'])
        if item and (item['unit'],item['price'])!=(r['unit'],r['price']):
            raise ValueError('同 SKU 的单位或计价依据不兼容 '+r['sku'])
        if not item:
            item={k:r[k] for k in ('sku','name','unit','price','step','status','catalog_row','before')}
            item.update(quantity='0',amount='0',demand='0',missing='0',reason='专项分配',issue=False,
                        unit_basis='冻结原行及仓库存单位',return_sources=[],claims=[])
            items[r['sku']]=item
        for k in ('quantity','amount','demand'):
            item[k]=text(Decimal(item[k])+Decimal(r[k]))
        item['after']=r['after']
        item['claims'].append({'line':r['id'],'source_row':r['source_row'],'quantity':r['quantity']})
        item['return_sources'].append({'row':r['source_row'],'plan_line':r['id']})
    return {k:list(v.values()) for k,v in groups.items()}


def preview(path,plan,*,limit=800):
    with plans.connection(path) as db:
        view,_=prepare(db,path,plans.read(db,plan),limit=limit)
        return dict(view=view,sha=preview_digest(view))


def preview_digest(view):
    return digest({**view,'rows':[{k:v for k,v in r.items() if k!='window_open'} for r in view['rows']]})


def _save_package(db,batch,body,packet,pos,stamp):
    from jst_auto_order import _online
    wh,store=packet['scope']['warehouse'],packet['scope']['store']
    oid=uid(batch['id'],pos)
    prefix='KY' if body['kind']=='opening' else 'ZC'
    number_local=f'{prefix}-{body["plan"][:12].upper()}-{batch["wave"]:03}-{pos:03}'
    store_row=db.execute('SELECT data FROM stores WHERE code=?',(store,)).fetchone()
    name=(json.loads(store_row[0]).get('name') if store_row else None) or store
    rows=[dict(E=i['sku'],D=i['name'],AU=wh,G=i['unit'],V=material_orders._number(i['price']),
               _source='资料匹配',_meta={'blocking':[]},_execution_qty=material_orders._number(i['quantity']),
               _execution_amount=material_orders._number(i['amount']),BV=material_orders._number(i['amount']),
               S=i['status'],_special={'plan':body['plan'],'run':batch['id'],'lines':i['claims']}) for i in packet['items']]
    result=dict(order_kind=body['kind'],store=store,store_name=name,rows=rows,summary={'rows':len(rows)})
    payload={'result':result,'work_revision':'special-'+batch['id']}
    sha=normal_orders.fingerprint_owned(result)
    db.execute('INSERT INTO normal_orders VALUES(?,?,?,?,?,1)',(batch['day'],store,body['kind'],oid,number_local))
    db.execute('INSERT INTO normal_revisions VALUES(?,1,?,?,?)',(oid,sha,stamp,normal_orders.encode(payload,db)))
    warehouse_execution.publish(db,oid,1,sha,stamp,payload,[wh])
    h=warehouse_execution._head(dict(oid=oid,day=batch['day'],store=store,name=name,number=number_local,kind=body['kind']),wh,1,sha,stamp)
    h['visibility_token']=0
    saved=material_orders._save_generation(db,batch,h,packet['items'],pos,stamp,kind=body['kind'])
    online=_online(h)
    db.execute('INSERT INTO special_packages VALUES(?,?,?,?,?,?,?,?,?)',(oid,body['plan'],batch['id'],pos,saved['gid'],wr.dumps(packet['scope']),h['number'],online,wr.dumps(packet)))
    db.executemany('INSERT INTO special_claims VALUES(?,?,?,?,?,?,?,?)',
                   [(body['plan'],c['line'],store,wh,i['sku'],oid,saved['gid'],c['quantity']) for i in packet['items'] for c in i['claims']])
    return {**saved,'online':online,'position':pos,'count':len(rows),'amount':text(sum(Decimal(i['amount']) for i in packet['items']))}


def claim(path,previewed,*,before_commit=None):
    original=previewed['view']
    if preview_digest(original)!=previewed['sha']:
        raise ValueError('本轮预览摘要不符')
    with ExitStack() as locks:
        for wh in (w for w in warehouse_catalog.WAREHOUSES if w in original['sources']):
            locks.enter_context(acquire(Path(path).parent,wh))
        with plans.connection(path,True) as db:
            old=db.execute('SELECT sha,body FROM special_runs WHERE plan=? AND wave=?',(original['plan'],original['wave'])).fetchone()
            if old:
                if old[0]!=previewed['sha']:
                    raise wr.Conflict('该轮已按另一预览认领，请查原轮次')
                return json.loads(old[1])['orders']
            body=plans.read(db,original['plan'])
            if not plans.enabled(db,body['kind']):
                raise ValueError('当前仅开放预览，须先启用该专项执行')
            stamp=now()
            view,stocks=prepare(db,path,body,at=stamp,limit=original['limit'])
            if not plans.in_window(body,{},instant(stamp)) or any(not r['window_open'] for r in view['rows'] if Decimal(r['quantity'])):
                raise ValueError('不在有效执行时段，该轮没有占用')
            if preview_digest(view)!=previewed['sha']:
                raise wr.Conflict('仓资料、目标版本、门店回复或剩余量已变化，请重新预览差额')
            if not view['packets']:
                raise ValueError('本轮无可执行行，查看逐行原因')
            run=uid(body['plan'],view['wave'])
            day=stamp[:10]
            normal_orders.order_header_schema(db)
            jst_batches.schema(db)
            lifecycle_store.schema(db)
            fixed={'batch_id':run,'created':stamp,'label':body['title'],'plan':body['plan'],'version':body['version']}
            db.execute('INSERT INTO jst_batches VALUES(?,?,?,?,?,?,?)',(run,stamp,body['title'],'complete',0,wr.dumps(fixed),digest(fixed)))
            for wh,stock in stocks.items():
                stock.update(bid=uid(run,wh),warehouse=wh,created=stamp,batch_id=run)
                db.execute('INSERT INTO jst_baselines VALUES(?,?,?,?,?)',(stock['bid'],wh,stamp,wr.dumps(stock),digest(stock)))
                db.execute('INSERT INTO jst_batch_stock VALUES(?,?,?,0)',(run,wh,stock['bid']))
            db.execute('INSERT INTO normal_work_revisions VALUES(?,?,?)',('special-'+run,day,normal_orders.encode(fixed,db)))
            batch=dict(id=run,day=day,stocks=stocks,wave=view['wave'])
            orders=[_save_package(db,batch,body,p,i,stamp) for i,p in enumerate(view['packets'],1)]
            db.execute('INSERT INTO special_runs VALUES(?,?,?,?,?,?,?)',(run,body['plan'],body['version'],view['wave'],stamp,previewed['sha'],
                        wr.dumps({'rows':view['rows'],'sources':view['sources'],'orders':orders})))
            if before_commit:
                before_commit()
            return orders
