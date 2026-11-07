"""One query contract for cards, cuts, detail and exports; weighted ratios only."""
from datetime import date
from stockout_common import VIEWS, today, encoded
from stockout_facts import exclusion

DIMENSIONS={'store':('s.store','门店编码'),'warehouse':('f.warehouse','供货仓'),
            'source':('f.source','供货来源'),'mandatory':('f.mandatory','必采属性'),
            'category':('f.category','商品类目'),'supplier':('f.supplier','仓库上游供货商'),'day':('s.day','观测日期'),
            'product_status':('f.product_status','仓商品状态'),'replen_status':('f.replen_status','翱象补货状态'),
            'inbound_status':('f.inbound_status','在途情况'),
            'tag_status':("CASE WHEN f.retired IS NULL OR f.assessment IS NULL THEN '标签待核' ELSE '标签已取得' END",'标签依据'),
            'sku':('f.sku','商品编码')}
LINKS='''current_days d JOIN publications p ON p.id=d.publication
 JOIN run_stores r ON r.run=p.run JOIN store_snapshots s ON s.id=r.snapshot AND s.day=d.day'''
AGG_KEYS=('base','zero','bad','unknown_tags','unknown_zero','removed_base','removed_zero')


def cut_columns(dimensions):
    """Names supplement exact code groups; they never become grouping/filter keys."""
    columns=[]
    for dimension in dimensions:
        columns.append((dimension,DIMENSIONS[dimension][1]))
        if dimension in ('store','sku'):
            columns.append((dimension+'_name','门店名称' if dimension=='store' else '商品名称'))
    return columns


def validate(query):
    query=dict(query or {})
    start=query.setdefault('start',today());end=query.setdefault('end',start)
    if date.fromisoformat(start)>date.fromisoformat(end):
        raise ValueError('起始日期不能晚于结束日期')
    if (date.fromisoformat(end)-date.fromisoformat(start)).days>365:
        raise ValueError('单次查询最多366天，请缩小日期范围')
    if query.setdefault('view','all') not in VIEWS:
        raise ValueError('剔除视角无效')
    for key in ('exclude_stopped','exclude_disabled'):
        if key in query and type(query[key]) is not bool:raise ValueError('剔除开关必须为勾选或未勾选')
    filters=query.setdefault('filters',{})
    if not isinstance(filters,dict) or set(filters)-set(DIMENSIONS):
        raise ValueError('未知筛选维度')
    for values in filters.values():
        if not isinstance(values,list) or len(values)>256:
            raise ValueError('每个维度筛选最多256项')
    if len(encoded(query).encode('utf-8'))>1024*1024:
        raise ValueError('筛选内容过长，请减少选中分组后查询；原始商品数据未截断')
    return query


def where(query):
    query=validate(query)
    conditions=['s.day BETWEEN ? AND ?',"s.state='READY'"]
    args=[query['start'],query['end']]
    for key,values in query['filters'].items():
        if values:
            field=DIMENSIONS[key][0]
            parts=[]
            regular=[v for v in values if v is not None]
            if regular:
                parts.append(field+' IN ('+','.join('?' for _ in regular)+')');args.extend(regular)
            if None in values:parts.append(field+' IS NULL')
            conditions.append('('+' OR '.join(parts)+')')
    return ' AND '.join(conditions),args


def excluded_sql(options, prefix='f.'):
    return exclusion(options['view'],prefix,exclude_stopped=options.get('exclude_stopped',False),
                     exclude_disabled=options.get('exclude_disabled',False))


def view_label(options):
    names=[] if options['view']=='all' else [VIEWS[options['view']]]
    if options.get('exclude_stopped'):names.append('剔除禁采（含停采）')
    if options.get('exclude_disabled'):names.append('剔除禁用')
    return '、'.join(names) or '全量'


def aggregation(options):
    ex=excluded_sql(options)
    cases=[f'eligible * NOT ({ex})',f'zero * NOT ({ex})',f'bad * NOT ({ex})',
           f'eligible * (retired IS NULL OR assessment IS NULL) * NOT ({ex})',
           f'zero * (retired IS NULL OR assessment IS NULL) * NOT ({ex})',
           f'eligible * ({ex})',f'zero * ({ex})']
    return ','.join(f'coalesce(sum({case}),0) AS {name}' for case,name in zip(cases,AGG_KEYS))


def fact_source(options,dimensions=()):
    return 'facts' if 'sku' in dimensions or options['filters'].get('sku') else 'aggregate_facts'


def rate(row):
    value=dict(row)
    value['rate']=value['zero']/value['base'] if value['base'] and not value['bad'] else None
    value['status']='指标依据待核' if value['bad'] else '当前视角无有销商品' if not value['base'] else '含标签待核' if value['unknown_tags'] else '已核对'
    return value


def bounded_page(cursor,convert=dict,budget=4*1024*1024):
    rows=[];size=0
    for row in cursor:
        value=convert(row);cost=len(encoded(value).encode('utf-8'))
        if rows and size+cost>budget:break
        rows.append(value);size+=cost
    return rows


def query(repo, options, *, cancel=None):
    options=validate(options)
    condition,args=where(options)
    with repo.connect() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        if not options['filters'] and not options.get('exclude_stopped') and not options.get('exclude_disabled'):
            fields=','.join('coalesce(sum(m.'+k+'),0) AS '+k for k in AGG_KEYS)
            total=db.execute(f'SELECT {fields} FROM {LINKS} JOIN summaries m ON m.snapshot=s.id AND m.view=? WHERE {condition}',[options['view'],*args]).fetchone()
        else:
            total=db.execute(f'SELECT {aggregation(options)} FROM {LINKS} JOIN {fact_source(options)} f ON f.snapshot=s.id WHERE {condition}',args).fetchone()
        # Existing immutable snapshots keep their old derived values until a
        # local recalculation publishes a new revision. Do not relabel them v2.
        needs_recalculation=bool(total['bad'] and db.execute(f'''SELECT 1 FROM {LINKS}
          JOIN facts f ON f.snapshot=s.id WHERE {condition} AND NOT ({excluded_sql(options)})
          AND f.month>0 AND f.stock<0 LIMIT 1''',args).fetchone())
        # Coverage describes acquisition scope, not the number of filtered SKU rows.
        selected_filter=options['filters'].get('store',[])
        scope='';scope_args=[]
        if selected_filter:
            scope=' AND r.store IN ('+','.join('?' for _ in selected_filter)+')';scope_args=selected_filter
        coverage=[dict(r) for r in db.execute(f'''SELECT d.day,p.id AS revision,p.created,count(*) AS selected,
          sum(CASE WHEN s.state='READY' AND s.day=d.day THEN 1 ELSE 0 END) AS completed
          FROM current_days d JOIN publications p ON p.id=d.publication JOIN run_stores r ON r.run=p.run
          LEFT JOIN store_snapshots s ON s.id=r.snapshot WHERE d.day BETWEEN ? AND ? {scope}
          GROUP BY d.day,p.id ORDER BY d.day''',[options['start'],options['end'],*scope_args])]
    return {'total':rate(total),'coverage':coverage,'query':options,
            'needs_negative_stock_recalculation':needs_recalculation}


def cuts(repo, options, dimensions=('store',), *, offset=0, limit=200, cancel=None):
    options=validate(options)
    if not 1<=len(dimensions)<=3 or len(set(dimensions))!=len(dimensions) or any(k not in DIMENSIONS for k in dimensions):
        raise ValueError('请选择1至3个不同的分析维度')
    condition,args=where(options)
    fields=','.join(DIMENSIONS[k][0]+' AS '+k for k in dimensions)
    for dimension,field in (('store','s.name'),('sku','f.name')):
        if dimension in dimensions:
            name=f"nullif({field},'')"
            fields+=f",coalesce(min({name}),'名称未提供') || CASE WHEN count(DISTINCT {name})>1 THEN '（同编码多名称，详见明细）' ELSE '' END AS {dimension}_name"
    group=','.join(DIMENSIONS[k][0] for k in dimensions)
    with repo.connect() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        source=fact_source(options,dimensions)
        sql=f'SELECT {fields},{aggregation(options)} FROM {LINKS} JOIN {source} f ON f.snapshot=s.id WHERE {condition} GROUP BY {group}'
        count=db.execute(f'SELECT count(*) FROM (SELECT {group} FROM {LINKS} JOIN {source} f ON f.snapshot=s.id WHERE {condition} GROUP BY {group})',args).fetchone()[0]
        records=bounded_page(db.execute(sql+' ORDER BY zero DESC,base DESC,'+group+' LIMIT ? OFFSET ?',[*args,min(200,max(1,limit)),max(0,offset)]),rate)
    return {'items':records,'total':count,'dimensions':list(dimensions),'offset':max(0,offset),'next_offset':offset+len(records) if offset+len(records)<count else None}


def detail(repo, options, *, offset=0, limit=200, only_zero=False, excluded=False, sku='', cancel=None):
    options=validate(options);condition,args=where(options)
    ex=excluded_sql(options)
    condition+=f' AND ({ex})' if excluded else f' AND NOT ({ex})'
    if only_zero:condition+=' AND f.zero>0'
    if sku:condition+=' AND f.sku=?';args.append(sku)
    source='facts' if sku else fact_source(options)
    counter='count(*)' if source=='facts' else 'coalesce(sum(f.zero),0)' if only_zero else 'coalesce(sum(f.rows),0)'
    scope,scope_args=where({**options,'filters':{k:v for k,v in options['filters'].items() if k in ('day','store')}})
    offset=max(0,offset);remaining=min(200,max(1,limit));skip=offset;rows=[];size=0
    with repo.connect() as db:
        if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
        count=db.execute(f'SELECT {counter} FROM {LINKS} JOIN {source} f ON f.snapshot=s.id WHERE {condition}',args).fetchone()[0]
        snapshots=db.execute(f'SELECT s.id,s.day,s.store,s.name AS store_name,s.observed,s.source_sha,p.id AS revision FROM {LINKS} WHERE {scope} ORDER BY s.day,s.store',scope_args)
        for snapshot in snapshots:
            part=f'FROM {source} f JOIN store_snapshots s ON s.id=f.snapshot WHERE {condition} AND f.snapshot=?'
            amount=db.execute(f'SELECT {counter} '+part,[*args,snapshot['id']]).fetchone()[0]
            if skip>=amount:skip-=amount;continue
            cursor=db.execute(f'SELECT f.* FROM facts f JOIN store_snapshots s ON s.id=f.snapshot WHERE {condition} AND f.snapshot=? ORDER BY f.sku LIMIT ? OFFSET ?',
                              [*args,snapshot['id'],remaining,skip])
            skip=0
            for record in cursor:
                row={**dict(snapshot),**dict(record)};cost=len(encoded(row).encode('utf-8'))
                if rows and size+cost>4*1024*1024:remaining=0;break
                rows.append(row);size+=cost;remaining-=1
            if remaining==0:break
    return {'items':rows,'total':count,'offset':max(0,offset),'next_offset':offset+len(rows) if offset+len(rows)<count else None}


def trend(repo,options,*,cancel=None):
    options=validate(options)
    # Daily queries keep missing dates absent. UI renders gaps instead of filling zeros.
    with repo.connect() as db:
        days=[r[0] for r in db.execute('SELECT day FROM current_days WHERE day BETWEEN ? AND ? ORDER BY day',(options['start'],options['end']))]
    result=[]
    for day in days:
        value=query(repo,{**options,'start':day,'end':day},cancel=cancel)
        result.append({'day':day,'total':value['total'],'coverage':value['coverage']})
    return result


def dates(repo):
    with repo.connect() as db:
        return [r[0] for r in db.execute('SELECT day FROM current_days ORDER BY day DESC LIMIT 3660')]
