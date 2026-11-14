"""Read-only store inventory valuation. No model, allocation, or current catalog reads.

One store/day round; active warehouses keep their own frozen signing evidence.
Shared local/unknown-source facts occur once. Bounded to two rounds while aggregating.
Detail is loaded on demand from the same signed snapshot and visibility basis.
"""
from collections import Counter
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
import sqlite3

import archive_orders as ao
import order_analytics as analytics
import storage_chunks as chunks
import warehouse_execution as execution
from runtime_support import checkpoint
from export_progress import RowProgress

VERSION = 'inventory-structure-1'
VIEWS = {'stock':'现货', 'inbound':'在途', 'position':'现货＋在途'}
SOURCES = ('中心仓','地采','来源待核')
STATUSES = ('启用','备用','禁用','状态待核')
GROUPS = ('必采','非必采','属性待核')
DEFINITIONS = {
    'scope':'门店本次导入范围，非大仓自身库存；不以补货建议或准入过滤存量。未证明覆盖全店。',
    'price':'冻结归属仓基本售价估值，非采购成本。地采仅在快照明确保存分析单价及库存单位时计价；否则未计价。',
    'value':'所选视角库存数量乘当时单价；known_value只表示已计价部分，value在全部有效时才有值。',
    'coverage':'有效正数量SKU中有有效单价且单位明确的比例；不是未知货值占比。零库存货值为0，缺价另列。',
    'dimensions':'中心仓状态、必采属性分别分组，不能互相相加。禁用、停采、拒收、淘汰、资质受限存量均保留。',
    'change':'同店前一有效业务日；数量影响=(本轮量-上轮量)*上轮价，价格影响=本轮量*(本轮价-上轮价)。非销售或采购归因。',
    'migration':'共同有效商品先数量再价格，最后按本轮货值移动来源/仓/状态/必采分类；迁移与整体货值增减不能重复相加。',
    'overview':'默认选定结束日，同店库存不跨轮累加。各店最新模式明确列每店日期，不代表同时点库存。',
    'history':'只读当前有效签发修订及冻结参数资料；缺字段不以当前资料回填。删除或重新签发后须重新查询。',
}


def query_spec(spec):
    allowed={'from','to','stores','view','overview','detail_store','detail_day'}
    if set(spec)-allowed:raise ValueError('存在未开放的货盘筛选字段')
    q={**spec}
    for k in ('from','to'):date.fromisoformat(q[k])
    if q['from']>q['to']:raise ValueError('开始日期不能晚于结束日期')
    stores=q.get('stores',[])
    if not isinstance(stores,list) or any(not isinstance(s,str) or not s for s in stores):raise ValueError('门店须为精确编码列表')
    q['stores']=list(dict.fromkeys(stores));q.setdefault('view','stock');q.setdefault('overview','end_day')
    if q['view'] not in VIEWS or q['overview'] not in ('end_day','latest'):raise ValueError('未知库存视角或大盘日期口径')
    if bool(q.get('detail_store'))!=bool(q.get('detail_day')):raise ValueError('明细须同时指定门店编码和业务日')
    if q.get('detail_day'):date.fromisoformat(q['detail_day'])
    return q


def number(value):
    if isinstance(value,bool):return None
    try:
        x=Decimal(str(value))
        return x if x.is_finite() else None
    except (ValueError,ArithmeticError):return None


def quantity(value):
    x=number(value)
    return x if x is not None and x>=0 else None


def facts(result, head, visibility=None, cancel=None):
    rows=result.get('rows',[]);counts=Counter(str(r.get('E','')) for r in rows)
    listing=result.get('must_list') or {};audit=result.get('must_list_audit');must_codes={a.get('sku') for a in audit} if isinstance(audit,list) else None
    def member(r,sku):
        if not listing.get('revision'):return '属性待核'
        if type(r.get('_must_sell')) is bool:return '必采' if r['_must_sell'] else '非必采'
        if must_codes is not None and len(must_codes)==listing.get('total'):return '必采' if sku in must_codes else '非必采'
        return '属性待核'
    visibility=visibility or {};out=[]
    for i,r in enumerate(rows):
        if i%250==0:checkpoint(cancel)
        wh=ao.warehouse(r)
        if visibility.get((head.get('oid'),wh),{}).get('deleted'):continue
        sku=str(r.get('E',''));unit=r.get('G');source='地采' if r.get('_source')=='地采商品' else '中心仓' if wh in ('山东仓','河北仓') and r.get('_source')!='匹配待核' else '来源待核'
        raw_price=r.get('V') if source!='地采' else r.get('_inventory_analysis_price')
        price=number(raw_price);price=price if price is not None and price>0 else None
        price_source='冻结归属仓基本售价' if source=='中心仓' else '未明确价格依据'
        if source=='地采' and r.get('_inventory_analysis_unit')==unit and r.get('_inventory_analysis_source'):
            price_source=str(r['_inventory_analysis_source'])
        elif source=='地采':price=None
        if source=='来源待核':price=None
        k,l=quantity(r.get('K')),quantity(r.get('L'));issues=[]
        if not sku or counts[sku]>1:issues.append('商品编码缺失或重复，未合并估值');k=l=None
        if not unit:issues.append('库存单位缺失');k=l=None
        if quantity(r.get('K')) is None:issues.append('现货数量无效或未提供')
        if quantity(r.get('L')) is None:issues.append('在途数量无效或未提供')
        if price is None:issues.append('单价未提供或无效')
        if source=='来源待核':issues.append('供货来源待核')
        status=r.get('S') if r.get('S') in STATUSES[:3] else '状态待核'
        meta=r.get('_meta') or {}
        out.append({'store':head['store'],'store_name':str(r.get('A') or head['store']),'day':head['day'],
            'order':head.get('number','本轮预览'),'sku':sku,'name':str(r.get('D') or ''),'unit':unit,
            'stock':k,'inbound':l,'position':k+l if k is not None and l is not None else None,
            'price':price,'price_source':price_source,'source':source,'warehouse':wh,'status':status,'raw_status':r.get('S'),
            'group':member(r,sku),'must_version':listing.get('revision'),
            'catalog_version':meta.get('product_revision'),'routing_version':(meta.get('routing') or {}).get('version'),
            'source_file':meta.get('export_file'),'source_sheet':meta.get('export_sheet'),'source_row':meta.get('export_row'),
            'observed_at':meta.get('export_updated'),'issues':issues})
    return out


def value(r, view):
    q=r[view]
    return Decimal(0) if q==0 else q*r['price'] if q is not None and r['price'] is not None else None


def metric(rows,view):
    known=Decimal(0);invalid=unpriced=held=priced=missing_price=0
    for r in rows:
        q=r[view];v=value(r,view)
        if r['price'] is None:missing_price+=1
        if q is None:invalid+=1
        elif q>0:
            held+=1
            if v is None:unpriced+=1
            else:priced+=1
        if v is not None:known+=v
    return {'known_value':known,'value':None if invalid or unpriced else known,'records':len(rows),
            'held_skus':held,'unpriced':unpriced,'invalid_quantity':invalid,'missing_price':missing_price,
            'coverage':Decimal(priced)/held if held else None}


def group_metric(rows,field,keys,view):
    groups={k:[] for k in keys}
    for r in rows:groups.setdefault(r[field],[]).append(r)
    return {k:metric(v,view) for k,v in groups.items()}


def structure(rows,view):
    center=[r for r in rows if r['source']=='中心仓']
    return {'total':metric(rows,view),'sources':group_metric(rows,'source',SOURCES,view),
            'statuses':group_metric(center,'status',STATUSES,view),'groups':group_metric(center,'group',GROUPS,view),
            'warehouses':group_metric(center,'warehouse',('山东仓','河北仓'),view),
            'cross':{s:group_metric([r for r in center if r['status']==s],'group',GROUPS,view) for s in STATUSES}}


def changes(previous,current,view,detail=False):
    prev={};curr={};duplicates={k for k,v in Counter(r['sku'] for r in previous).items() if v>1}|{k for k,v in Counter(r['sku'] for r in current).items() if v>1}
    for r in previous:prev.setdefault(r['sku'],r)
    for r in current:curr.setdefault(r['sku'],r)
    quantity_effect=Decimal(0);price_effect=Decimal(0);range_effect=Decimal(0);comparable=0;counts=Counter();details=[];migrations={}
    # Existing source order, then missing records in their former order. No sorted SKU reordering.
    pairs=[(r['sku'],prev.get(r['sku']),r) for r in current]+[(r['sku'],r,None) for r in previous if r['sku'] not in curr]
    for sku,p,c in pairs:
        qe=pe=None
        if sku in duplicates or not sku:kind='重复或缺失编码待核'
        elif p is None:kind='本轮新出现记录'
        elif c is None:kind='本轮未提供记录'
        elif p['unit']!=c['unit']:kind='库存单位变化'
        elif p[view] is None or c[view] is None or p['price'] is None or c['price'] is None:kind='估值范围待核或变化'
        else:
            kind='共同可比商品';qe=(c[view]-p[view])*p['price'];pe=c[view]*(c['price']-p['price'])
            quantity_effect+=qe;price_effect+=pe;comparable+=1
            for field in ('source','warehouse','status','group'):
                if p[field]!=c[field]:
                    key=(field,p[field],c[field]);m=migrations.setdefault(key,{'dimension':field,'from':p[field],'to':c[field],'value':Decimal(0),'skus':0});m['value']+=value(c,view);m['skus']+=1
        if qe is None:range_effect+=(value(c,view) or Decimal(0)) if c else Decimal(0);range_effect-=(value(p,view) or Decimal(0)) if p else Decimal(0)
        counts[kind]+=1
        if detail:details.append({**(c or p),'change_kind':kind,'previous_quantity':p[view] if p else None,
            'previous_price':p['price'] if p else None,'previous_value':value(p,view) if p else None,
            'current_quantity':c[view] if c else None,'current_value':value(c,view) if c else None,
            'quantity_effect':qe,'price_effect':pe,'previous_source':p['source'] if p else None,
            'previous_warehouse':p['warehouse'] if p else None,'previous_status':p['status'] if p else None,'previous_group':p['group'] if p else None})
    return {'quantity_effect':quantity_effect,'price_effect':price_effect,'comparable_delta':quantity_effect+price_effect,
            'comparable_skus':comparable,'counts':dict(counts),'migrations':list(migrations.values()),'noncomparable_known_delta':range_effect,'details':details}


def _basis(db):
    tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'normal_orders' not in tables:return analytics.digest([])
    heads=db.execute('SELECT o.oid,o.day,o.store,o.number,o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision ORDER BY o.oid').fetchall()
    visible=db.execute('SELECT oid,warehouse,deleted,token FROM archive_visibility ORDER BY oid,warehouse').fetchall() if 'archive_visibility' in tables else []
    basis=[heads,visible]
    if 'warehouse_execution_heads' in tables:
        basis.append(db.execute('SELECT oid,warehouse,source_revision,child_revision,state,epoch FROM warehouse_execution_heads ORDER BY oid,warehouse').fetchall())
        basis.append(db.execute('SELECT oid,warehouse,source_revision,snapshot_digest FROM warehouse_execution_revisions ORDER BY oid,warehouse,source_revision').fetchall())
    return analytics.digest(basis)


def validate_basis(path,report,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if report.get('preview'):raise ValueError('本轮预览请从当前工作台重新生成后导出，不作为正式历史')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if _basis(db)!=report['data_stamp']:raise ValueError('签发或删除状态已变化，请重新查询货盘')


def _load(db,head,visibility,cancel):
    row=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',(head['oid'],head['revision'])).fetchone()
    if not row:raise ValueError('该轮签发已不存在，请重新查询')
    payload=chunks.load(row[0],db,binding=head['digest'])
    if 'execution_sources' in head:
        return _load_executions(db,head,payload,visibility,cancel)
    scopes=ao.scopes(payload['result']['rows'])
    deleted=[wh for wh in scopes if visibility.get((head['oid'],wh),{}).get('deleted')]
    if scopes and len(deleted)==len(scopes):return None
    head['partial']=bool(deleted);head['scope']='部分仓已删除，本次导入的剩余范围' if deleted else '本次导入范围（未证明全店）'
    return facts(payload['result'],head,visibility,cancel)


def _load_executions(db,head,payload,visibility,cancel):
    """Combine frozen child facts without broadening the signed warehouse scope."""
    from history_read import repository
    sources=head['execution_sources']
    if not sources:return None
    repo=repository(db.execute('PRAGMA database_list').fetchone()[2]);center=[]
    for child in sources:
        checkpoint(cancel)
        if visibility.get((head['oid'],child['warehouse']),{}).get('deleted'):continue
        order=execution.get_order(repo,child,db=db)
        center.extend(r for r in facts(order['payload']['result'],child,visibility,cancel) if r['source']=='中心仓')
    common=[r for r in facts(payload['result'],head,visibility,cancel) if r['source']!='中心仓']
    # Retain the shared calculation's source order where it contains the pinned
    # facts. A sibling can retain an older source revision; any older-only rows
    # remain in that warehouse's original order after the current source rows.
    pools={}
    for i,r in enumerate(center):pools.setdefault((r['warehouse'],r['sku']),[]).append((i,r))
    common_pool={}
    for i,r in enumerate(common):common_pool.setdefault((r['warehouse'],r['sku']),[]).append((i,r))
    result=[];used_center=set();used_common=set()
    for raw in payload['result'].get('rows',[]):
        key=(ao.warehouse(raw),str(raw.get('E','')))
        pool=common_pool if raw.get('_source') in ('地采商品','匹配待核') or key[0] not in ('山东仓','河北仓') else pools
        if pool.get(key):
            i,r=pool[key].pop(0);result.append(r)
            (used_common if pool is common_pool else used_center).add(i)
    result.extend(r for i,r in enumerate(center) if i not in used_center)
    result.extend(r for i,r in enumerate(common) if i not in used_common)
    counts=Counter(r['sku'] for r in result)
    for r in result:
        if not r['sku'] or counts[r['sku']]>1:
            r.update(stock=None,inbound=None,position=None)
            issue='商品编码缺失或重复，未合并估值'
            if issue not in r['issues']:r['issues'].insert(0,issue)
    parent_scopes=set(ao.scopes(payload['result']['rows']));active={s['warehouse'] for s in sources}
    head['partial']=bool(parent_scopes-active) or any(visibility.get((head['oid'],w),{}).get('deleted') for w in active)
    head['scope']='已参与仓各自冻结签发范围＋共同地采/待核；不同仓可来自不同签发时点，未证明覆盖全店'
    return result


def _round(head,rows,prev_head,prev_rows,view):
    s=structure(rows,view);s.update(head);s['store_name']=rows[0]['store_name'] if rows else head['store']
    s['previous']=prev_head;s['previous_day']=prev_head['day'] if prev_head else None
    s['delta']=s['total']['known_value']-metric(prev_rows,view)['known_value'] if prev_head else None
    s['changes']=changes(prev_rows,rows,view) if prev_head else None
    s['source_deltas']={k:s['sources'][k]['known_value']-metric([r for r in prev_rows if r['source']==k],view)['known_value'] if prev_head else None for k in SOURCES}
    s['status_deltas']={k:s['statuses'][k]['known_value']-metric([r for r in prev_rows if r['source']=='中心仓' and r['status']==k],view)['known_value'] if prev_head else None for k in STATUSES}
    s['group_deltas']={k:s['groups'][k]['known_value']-metric([r for r in prev_rows if r['source']=='中心仓' and r['group']==k],view)['known_value'] if prev_head else None for k in GROUPS}
    s['warehouse_structures']={wh:structure([r for r in rows if r['warehouse']==wh],view) for wh in ('山东仓','河北仓')}
    return s


def finish_report(spec,rounds,stamp,preview=False):
    chosen={}
    for r in rounds:
        if spec['overview']=='latest' or r['day']==spec['to']:chosen[r['store']]=r
    selected=list(chosen.values())
    def combine(metrics):
        out={k:sum((m[k] for m in metrics),Decimal(0) if k=='known_value' else 0) for k in ('known_value','records','held_skus','unpriced','invalid_quantity','missing_price')}
        out['value']=None if out['unpriced'] or out['invalid_quantity'] else out['known_value']
        out['coverage']=Decimal(out['held_skus']-out['unpriced'])/out['held_skus'] if out['held_skus'] else None
        return out
    overview={'total':combine([r['total'] for r in selected]),'stores':len(selected),'dates':sorted({r['day'] for r in selected}),
              'sources':{k:combine([r['sources'][k] for r in selected]) for k in SOURCES},
              'statuses':{k:combine([r['statuses'][k] for r in selected]) for k in STATUSES},
              'groups':{k:combine([r['groups'][k] for r in selected]) for k in GROUPS}}
    overview['delta']=sum((r['delta'] for r in selected if r['previous']),Decimal(0))
    overview['comparison_stores']=sum(bool(r['previous']) for r in selected)
    overview['source_deltas']={k:sum((r['source_deltas'][k] for r in selected if r['previous']),Decimal(0)) for k in SOURCES}
    overview['missing_stores']=[s for s in spec['stores'] if s not in chosen]
    return {'metric_version':VERSION,'query':spec,'data_stamp':stamp,'preview':preview,'rounds':rounds,'overview':overview,
            'snapshot_id':analytics.digest([VERSION,spec,stamp,rounds]),'as_of':datetime.now().isoformat(timespec='seconds'),'definitions':DEFINITIONS}


def collect(path,spec,progress=lambda _:None,cancel=None):
    spec=query_spec(spec);rounds=[]
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN');stamp=_basis(db);visibility=ao.states(db)
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='normal_orders'").fetchone():return finish_report(spec,[],stamp)
        # Metadata only; keep the nearest pre-range round plus requested rounds, not all payloads.
        heads=[dict(zip(('day','store','oid','number','revision','digest','saved'),r)) for r in db.execute('SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid AND o.current=r.revision WHERE o.kind=? AND o.day<=? ORDER BY o.store,o.day DESC',('normal_model',spec['to'])) if not spec['stores'] or r[1] in spec['stores']]
        managed={h['oid'] for h in heads if execution.has_parent(db,h['oid'])}
        by_parent={}
        for child in execution.heads(db,[h for h in heads if h['oid'] in managed]) if managed else []:
            by_parent.setdefault(child['oid'],[]).append(child)
        for h in heads:
            if h['oid'] in managed:h['execution_sources']=by_parent.get(h['oid'],[])
        # Immutable revision IDs/digests are now pinned. Release the read transaction
        # before decoding/aggregating; do not hold a SQLite read lock across all rounds.
        db.commit()
        selected=[];found_prev=set()
        for h in heads:
            if 'execution_sources' in h and not h['execution_sources']:continue
            if h['day']<spec['from'] and h['store'] in found_prev:continue
            checkpoint(cancel)
            # Deleted nearest parent is skipped; seek a still-visible older round.
            states=[v for (oid,_),v in visibility.items() if oid==h['oid']]
            if states and all(v.get('deleted') for v in states):
                probe=_load(db,h,visibility,cancel)
                if probe is None:continue
            selected.append(h)
            if h['day']<spec['from']:found_prev.add(h['store'])
        selected.sort(key=lambda h:(h['store'],h['day']))
        last_store=None;prev_head=None;prev_rows=[]
        for i,h in enumerate(selected):
            checkpoint(cancel);progress(RowProgress('读取货盘轮次',i+1,len(selected)))
            rows=_load(db,h,visibility,cancel)
            if rows is None:continue
            if h['store']!=last_store:prev_head=None;prev_rows=[];last_store=h['store']
            if h['day']>=spec['from']:rounds.append(_round(h,rows,prev_head,prev_rows,spec['view']))
            prev_head=h;prev_rows=rows
        db.execute('BEGIN')
        if _basis(db)!=stamp:raise ValueError('查询期间签发或删除状态已变化，请重新查询货盘')
    checkpoint(cancel);report=finish_report(spec,rounds,stamp)
    if spec.get('detail_store'):report['details']=detail(path,report,spec['detail_store'],spec['detail_day'],progress,cancel)
    return report


def detail(path,report,store,day,progress=lambda _:None,cancel=None):
    validate_basis(path,report,cancel=cancel)
    head=next((r for r in report['rounds'] if r['store']==store and r['day']==day),None)
    if head is None:raise ValueError('所选门店轮次不在当前查询范围')
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        if _basis(db)!=report['data_stamp']:raise ValueError('签发或删除状态已变化，请重新查询货盘')
        vis=ao.states(db);db.commit()
        rows=_load(db,dict(head),vis,cancel);previous=_load(db,dict(head['previous']),vis,cancel) if head['previous'] else []
        if rows is None or previous is None:raise ValueError('轮次已删除，请重新查询')
        result=changes(previous,rows,report['query']['view'],detail=True)['details']
        db.execute('BEGIN')
        if _basis(db)!=report['data_stamp']:raise ValueError('读取明细期间签发或删除状态已变化，请重新查询')
        return result


def preview_inputs(results):
    """Freeze only valuation inputs on the GUI thread, not two whole model copies."""
    from copy import deepcopy
    fields=('A','D','E','G','K','L','V','S','AU','_source','_must_sell','_inventory_analysis_price','_inventory_analysis_unit','_inventory_analysis_source')
    meta_fields=('product_revision','routing','export_file','export_sheet','export_row','export_updated')
    out={}
    for store,result in results.items():
        out[store]={'must_list':deepcopy(result.get('must_list')),'must_list_audit':deepcopy(result.get('must_list_audit')),
                    'rows':[{**{k:deepcopy(r[k]) for k in fields if k in r},'_meta':{k:deepcopy(r.get('_meta',{})[k]) for k in meta_fields if k in r.get('_meta',{})}} for r in result.get('rows',[])]}
    return out


def preview(results,day,view='stock',progress=lambda _:None,cancel=None):
    rounds=[];tokens=[]
    for store,result in results.items():
        checkpoint(cancel);head={'day':day,'store':store,'number':'本轮预览','scope':'本次导入范围（未证明全店）','partial':False}
        rows=facts(result,head,cancel=cancel);rounds.append(_round(head,rows,None,[],view));tokens.append([store,analytics.digest(rows)])
    return finish_report(query_spec({'from':day,'to':day,'view':view}),rounds,analytics.digest(tokens),True)
