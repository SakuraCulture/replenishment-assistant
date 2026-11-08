"""Lazy, rebuildable payment candidate index. Source facts are never inferred or rewritten."""
import copy,hashlib,json,re,sqlite3,unicodedata
from contextlib import closing
from datetime import date,datetime
from decimal import Decimal
from pathlib import Path
import work_repository as wr
from purchase_io import number
from runtime_support import checkpoint

PREFIX='payment_match.'

def checksum(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()
def normalized(value):return re.sub(r'\s+','',unicodedata.normalize('NFKC',str(value or '')))
def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2);db.execute('PRAGMA query_only=ON');return db
def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}

def accounts(path):
    """Only stable, previously verified JST company identifiers; no browser or login."""
    file=Path(path).parent/'jst_catalog_sync/jobs.json'
    if not file.is_file():return {'choices':[],'token':checksum([]),'reason':'尚无已核验的聚水潭企业连接；可人工关联，本次不记住来源单号'}
    if file.stat().st_size>32*1024*1024:return {'choices':[],'token':checksum(['oversize']),'reason':'来源连接记录过大，未整体加载；可人工关联，自动来源识别待维护'}
    try:value=json.loads(file.read_text('utf-8'))
    except (OSError,ValueError):return {'choices':[],'token':checksum(['unreadable']),'reason':'聚水潭来源连接记录无法读取；可人工关联，不自动记录内部单号'}
    raw=value.get('accounts',{}) if isinstance(value,dict) else {}
    values=list(raw.values()) if isinstance(raw,dict) else raw if isinstance(raw,list) else [];choices={};versions=[]
    for a in values:
        if not isinstance(a,dict):continue
        coid=str(a.get('expectedCoid',''))
        if a.get('mappingVerified') is not True or not re.fullmatch(r'[1-9]\d*',coid) or not a.get('expectedCompany'):continue
        item=choices.setdefault(coid,{'coid':coid,'name':a['expectedCompany'],'warehouses':[]})
        item['warehouses'].append(str(a.get('warehouse') or '已核验连接'));versions.append([str(a.get('id') or ''),coid,str(a.get('expectedUid') or ''),str(a.get('bindingVersion') or ''),a.get('expectedCompany')])
    return {'choices':list(choices.values()),'token':checksum(sorted(versions)),'reason':'' if choices else '当前连接缺少已核验企业身份，不猜测来源企业'}

def mappings(db):
    if 'lifecycle_mappings' not in tables(db):return {}
    return {name:wr.loads(raw) for name,raw in db.execute("SELECT name,payload FROM lifecycle_mappings WHERE stage='payment' AND name LIKE 'payment_match.%' ORDER BY name")}
def mapping_key(kind,scope,value):return PREFIX+kind+'.'+checksum([scope,str(value)])

def pool_token(db):
    """Metadata only: additions, deletions, revisions, generation and downstream changes."""
    known=tables(db);parts=[]
    for table,sql in [
        ('normal_orders','SELECT o.oid,o.day,o.store,o.number,o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current ORDER BY o.oid'),
        ('archive_visibility','SELECT oid,warehouse,deleted,token FROM archive_visibility ORDER BY oid,warehouse'),
        ('jst_active','SELECT a.oid,a.warehouse,a.gid,g.digest FROM jst_active a JOIN jst_generations g ON a.gid=g.gid ORDER BY a.oid,a.warehouse'),
        ('lifecycle_versions',"SELECT oid,revision,warehouse,stage,MAX(version) FROM lifecycle_versions GROUP BY oid,revision,warehouse,stage ORDER BY oid,revision,warehouse,stage")]:
        parts.append([table,list(db.execute(sql)) if table in known else []])
    return checksum(parts)

def facts(rows):
    values=[];seen=set();complete=True
    for row in rows:
        sku=str(row['sku']);q=row.get('base_quantity',row.get('quantity'));a=row.get('amount')
        q=number(q,True);a=number(a,True)
        if sku in seen:complete=False
        seen.add(sku);complete=complete and q is not None and a is not None
        values.append([sku,q,a])
    return {'signature':checksum(sorted(values)) if complete else None,
            'quantity_signature':checksum(sorted([sku,q] for sku,q,_ in values)) if complete else None,'count':len(values),
            'quantity':str(sum((Decimal(r[1]) for r in values),Decimal(0))) if all(r[1] is not None for r in values) else None,
            'amount':str(sum((Decimal(r[2]) for r in values),Decimal(0))) if all(r[2] is not None for r in values) else None,
            'complete':complete}

class Cache:
    def __init__(self,path):
        self.path=Path(path).parent/'payment_matching/index-v1.sqlite3'
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,timeout=.2)
        try:self.db.execute('CREATE TABLE IF NOT EXISTS candidates(k TEXT PRIMARY KEY,payload TEXT NOT NULL,checksum TEXT NOT NULL)')
        except sqlite3.DatabaseError as exc:
            self.db.close()
            if getattr(exc,'sqlite_errorcode',None) not in (sqlite3.SQLITE_CORRUPT,sqlite3.SQLITE_NOTADB):raise
            # This disposable index is never a business database. Keep the
            # damaged bytes for diagnosis and rebuild only this exact file.
            from uuid import uuid4
            self.path.rename(self.path.with_name('index-v1-damaged-'+uuid4().hex+'.sqlite3'))
            self.db=sqlite3.connect(self.path,timeout=.2)
            self.db.execute('CREATE TABLE candidates(k TEXT PRIMARY KEY,payload TEXT NOT NULL,checksum TEXT NOT NULL)')
        self.writes=0
    def close(self):
        try:self.db.commit()
        finally:self.db.close()
    def get(self,key):
        row=self.db.execute('SELECT payload,checksum FROM candidates WHERE k=?',(key,)).fetchone()
        if not row:return None
        try:value=wr.loads(row[0])
        except (ValueError,TypeError):return None
        return value if checksum(value)==row[1] else None
    def put(self,key,value):
        self.db.execute('INSERT INTO candidates VALUES(?,?,?) ON CONFLICT(k) DO UPDATE SET payload=excluded.payload,checksum=excluded.checksum',(key,wr.dumps(value),checksum(value)))
        self.writes+=1
        if self.writes%32==0:self.db.commit()

def build(path,progress=lambda _:None,cancel=None):
    """Only current revisions, one decoded snapshot at a time; warm reads use metadata."""
    import archive_orders as ao
    import warehouse_orders as wo
    from history_read import repository
    import jst_orders as jst
    import order_lifecycle as lc
    repo=repository(path);out=[];decoded=0
    with closing(readonly(path)) as db,closing(Cache(path)) as cache:
        db.execute('BEGIN');token=pool_token(db);known=tables(db);visibility=ao.states(db)
        heads=[dict(zip(('day','store','oid','number','revision','digest','saved'),r)) for r in db.execute('SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current ORDER BY o.day DESC,o.store')]
        active={(oid,wh):(gid,revision,digest) for oid,wh,gid,revision,digest in db.execute('SELECT a.oid,a.warehouse,a.gid,g.revision,g.digest FROM jst_active a JOIN jst_generations g ON a.gid=g.gid')} if 'jst_active' in known else {}
        for i,head in enumerate(heads):
            checkpoint(cancel);progress(f'建立/刷新付款识别索引 {i+1}/{len(heads)}：仅当前签发，支持取消')
            key=checksum(['signed',head['oid'],head['revision'],head['digest']]);meta=cache.get(key)
            if meta is None:
                checkpoint(cancel);order=repo.get(head['day'],head['store'],head['revision'],view='export');result=order['payload']['result']
                meta={'name':result['store_name'],'warehouses':[wh for wh in ao.scopes(result['rows']) if wh in wo.WAREHOUSES]}
                cache.put(key,meta);decoded+=1;del order,result
            for wh in meta['warehouses']:
                checkpoint(cancel);v=visibility.get((head['oid'],wh),{})
                if v.get('deleted'):continue
                h={**head,'parent_number':head['number'],'number':wo.child_number(head['number'],wh),'warehouse':wh,'name':meta['name'],
                   'current_revision':head['revision'],'visibility_token':v.get('token',0)}
                gid,revision,gd=active.get((head['oid'],wh),(None,None,None))
                if revision!=h['revision']:gid=None;gd=None
                features=None
                if gid:
                    ck=checksum(['generation-v2',gid,gd]);features=cache.get(ck)
                    if features is None:
                        checkpoint(cancel);generation=jst.decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(gid,)).fetchone())
                        if any(generation['head'][k]!=h[k] for k in ('oid','revision','warehouse','digest')):raise wr.Conflict('生成单与当前签发身份不符')
                        exported=[r for r in generation['rows'] if Decimal(r['quantity'])>0]
                        features=facts(exported);cache.put(ck,features);decoded+=1;del exported,generation
                paid=db.execute("SELECT version FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='payment' ORDER BY version DESC LIMIT 1",lc.key(h)).fetchone() if 'lifecycle_versions' in known else None
                returned=db.execute("SELECT version FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='return' ORDER BY version DESC LIMIT 1",lc.key(h)).fetchone() if 'lifecycle_versions' in known else None
                out.append({**h,'generation_id':gid,'generation_digest':gd,'basis_kind':'generation' if gid else 'return' if returned else 'signed',
                            'payment_version':paid[0] if paid else None,'return_version':returned[0] if returned else None,'features':features})
        if pool_token(db)!=token:raise wr.Conflict('索引读取期间业务依据变化，请刷新')
        mapping=mappings(db)
    return {'heads':out,'pool_token':token,'mapping_token':checksum(mapping),'mappings':mapping,'accounts':accounts(path),'decoded_snapshots':decoded}

def source_day(rows):
    days=set()
    for row in rows:
        value=(row.get('source_order') or {}).get('下单时间')
        if isinstance(value,datetime):days.add(value.date().isoformat())
        elif isinstance(value,date):days.add(value.isoformat())
        elif isinstance(value,str):
            try:days.add(date.fromisoformat(value.strip()[:10]).isoformat())
            except ValueError:pass
    return next(iter(days)) if len(days)==1 else None

def related(rows,head):
    return all(not r.get(field) or r[field]==head[target] for r in rows for field,target in [('signed_number','number'),('store','store'),('warehouse','warehouse')])

def exact(path,rows,head,cancel=None,*,quantities_only=False):
    """Real generation and signed barcode proof, never a cache digest alone."""
    import jst_orders as jst,order_lifecycle as lc
    if not head.get('generation_id'):return False,'没有有效生成明细，下一步按回传／签发核对'
    checkpoint(cancel)
    with closing(readonly(path)) as db:generation=jst.decode(db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(head['generation_id'],)).fetchone())
    if not generation:raise wr.Conflict('原生成单已删除')
    if jst.digest(generation)!=head['generation_digest']:raise wr.Conflict('生成明细已变化')
    full=lc.full_head(path,head);incoming=copy.deepcopy(rows)
    with closing(readonly(path)) as db:
        if lc.upstream(db,full,'payment')!=generation['basis']:return False,'原生成单的回传或签发依据已变化，需先核对生成业务'
    try:lc.match_payment_rows(path,full,incoming)
    except ValueError as exc:return False,str(exc)
    expected=[r for r in generation['rows'] if Decimal(r['quantity'])>0]
    a,b=facts(incoming),facts(expected)
    key='quantity_signature' if quantities_only else 'signature'
    ok=bool(a['complete'] and b['complete'] and a[key]==b[key])
    return ok,('逐商品编码、基础数量'+('' if quantities_only else '及金额')+'完全一致' if ok else '数量、金额、缺行或清单外商品存在差异，下一步保留核对')

def analyze(path,document,index,scope=None,source_scope=None,mode='first',progress=lambda _:None,cancel=None):
    if source_scope is None or any(r.get('signed_number') for r in document['scan']['rows']):
        from payment_auto_matching import analyze as automatic
        return automatic(path,document,index,scope,source_scope,progress,cancel)
    if source_scope is not None and source_scope not in {a['coid'] for a in index['accounts']['choices']}:
        return {'candidates':[],'rejected':[],'automatic':False,'state':'来源企业未核验','reason':'所选来源企业没有已核验身份，请刷新连接记录后重新选择','prevent_rebind':True,'source_scope':source_scope}
    rows=document['scan']['rows'];origin=rows[0].get('source_order') or {};day=source_day(rows);name=normalized(origin.get('店铺名称'))
    internal=rows[0].get('document_number')
    mapped=index['mappings'];link=mapped.get(mapping_key('order',source_scope,internal)) if source_scope and internal else None
    if source_scope is None and any(v.get('internal')==internal for k,v in mapped.items() if k.startswith(PREFIX+'order.')):
        return {'candidates':[],'rejected':[],'automatic':False,'state':'请确认来源企业','reason':'该内部订单号已有已确认企业范围的原关联；请先选择本次来源企业，不能忽略原关联改绑','prevent_rebind':True,'source_scope':None}
    alias=mapped.get(mapping_key('store',source_scope,name)) if source_scope and name else None
    all_heads=[h for h in index['heads'] if related(rows,h)];allowed={h['number'] for h in scope} if scope is not None else None
    source=next(a for a in index['accounts']['choices'] if a['coid']==source_scope)
    warehouses=set(source['warehouses'])&{'山东仓','河北仓'}
    if warehouses:all_heads=[h for h in all_heads if h['warehouse'] in warehouses]
    if link:
        candidates=[h for h in all_heads if all(h[k]==link['head'][k] for k in ('oid','revision','warehouse','digest','number'))]
        if len(candidates)!=1:return {'candidates':[],'rejected':[],'automatic':False,'state':'原关联失效','reason':'内部订单号已有关联，但原订单、修订或仓单已失效；不会改绑其他单','prevent_rebind':True,'source_scope':source_scope}
        target=candidates[0]
        if allowed is not None and target['number'] not in allowed:return {'candidates':[],'rejected':[],'automatic':False,'state':'原关联超出范围','reason':'来源内部单号对应原仓单不在本次勾选范围；请从原订单进入','prevent_rebind':True,'source_scope':source_scope}
        ok,reason=exact(path,rows,target,cancel)
        return {'candidates':[target],'rejected':[],'automatic':target.get('generation_id')==link.get('generation_id'),'state':'已识别原内部单号','reason':'已有企业范围及内部订单号对应；'+reason,'source_scope':source_scope,'exact':ok,'day':day,'trace':[]}
    if alias:all_heads=[h for h in all_heads if h['store']==alias['store']]
    name_stores={h['store'] for h in all_heads if name and normalized(h['name'])==name}
    confirmed_store=alias.get('store') if alias else next(iter(name_stores)) if len(name_stores)==1 else None
    feature=facts(rows);matching=[];reasons={}
    summary=[h for h in all_heads if (confirmed_store is None or h['store']==confirmed_store) and h['features'] and h['features']['count']==feature['count'] and h['features']['quantity']==feature['quantity'] and h['features']['amount']==feature['amount']]
    for i,h in enumerate(summary):
        checkpoint(cancel);progress(f'核对付款候选 {i+1}/{len(summary)}：完整明细和跨日唯一性')
        ok,reason=exact(path,rows,h,cancel);reasons[h['number']]=reason
        if ok:matching.append(h)
    related_matches=[h for h in matching if confirmed_store is None or h['store']==confirmed_store]
    # Date/lifecycle rank the view; uniqueness always includes every relevant day.
    def rank(h):
        pending=h.get('return_version') is not None and h.get('generation_id') and h.get('payment_version') is None
        lifecycle=0 if (pending if mode=='first' else h.get('payment_version') is not None if mode=='correction' else True) else 1
        distance=abs((date.fromisoformat(h['day'])-date.fromisoformat(day)).days) if day else 0
        return (0 if h in related_matches else 1,lifecycle,0 if h['store']==confirmed_store else 1,distance,h['day'],h['number'])
    choices=sorted([h for h in all_heads if allowed is None or h['number'] in allowed],key=rank)
    automatic=bool(source_scope and len(related_matches)==1 and related_matches[0] in choices)
    if automatic:
        target=related_matches[0];choices=[target,*[h for h in choices if h is not target]];state='唯一完整匹配';reason='已核对指定企业供货仓及全部相关日期的逐商品明细；自动识别门店和仓，无需确认别称'
    else:
        state='需要核对来源／候选';reason=('尚未确认来源企业；' if not source_scope else '')+('门店名称对应待确认；' if not confirmed_store else '')+(f'完整匹配 {len(related_matches)} 张，不能默认第一张' if len(related_matches)!=1 else '有内容匹配候选，仍需确认来源及门店对应')
    trace=[['有效仓单',len(all_heads)],['待付款优先',sum(bool(h.get('return_version') and h.get('generation_id') and h.get('payment_version') is None) for h in all_heads)],['同日优先',sum(h['day']==day for h in all_heads) if day else '来源无明确日期'],['汇总相同',len(summary)],['完整匹配（跨全部相关日期）',len(related_matches)]]
    return {'candidates':choices,'rejected':[],'automatic':automatic,'state':state,'reason':reason,'source_scope':source_scope,'day':day,'trace':trace,'exact_numbers':[h['number'] for h in matching],'candidate_reasons':reasons}


def attach(path,plan,documents,index,source_scope,remember=True):
    """Freeze sidecar relationship operations; never alter payment payload/digest."""
    if not index:return plan
    selected=[d for d in documents if d.get('selected',True) and d['scan']['stage']=='payment' and d['scan']['rows'][0].get('adapter')=='paid_order']
    if not selected:return plan
    requests=[]
    for d in selected:
        target=d.get('target');numbers={r.get('signed_number') or (target or {}).get('number') for r in d['scan']['rows']}
        if len(numbers)!=1:raise ValueError('付款来源单必须先明确关联一张仓单')
        number_value=next(iter(numbers));p=next(p for p in plan['plans'] if p['stage']=='payment' and p['head']['number']==number_value)
        internal={r['document_number'] for r in d['scan']['rows']}
        if len(internal)!=1:raise ValueError('同一完整付款来源组存在多个内部订单号')
        origin=d['scan']['rows'][0]['source_order'];h=p['head'];candidate=next((v for v in index['heads'] if v['number']==h['number']),None)
        if not candidate:raise wr.Conflict('所选仓单已不在本次有效索引中，请重新读取')
        proof=d.get('payment_match') or {}
        requests.append({'internal':next(iter(internal)),'source_scope':source_scope,'original_name':str(origin.get('店铺名称') or ''),'remember':bool(remember),
            'automatic_proof':proof.get('proof') if proof.get('automatic') and proof['candidates'][0]['number']==number_value else None,
            'quantity_signature':facts(p['payload']['raw_rows'])['quantity_signature'],
            'head':{k:h[k] for k in ('oid','revision','warehouse','number','store','digest')},'generation_id':candidate.get('generation_id')})
    metadata={'pool_token':index['pool_token'],'mapping_token':index['mapping_token'],'accounts_token':index['accounts']['token'],'requests':requests}
    plan['payment_matching']=metadata;plan['payment_matching_digest']=checksum(metadata)
    with closing(readonly(path)) as db:validate(path,db,plan)
    return plan

def operations(db,metadata):
    existing=mappings(db);updates={}
    for r in metadata['requests']:
        scope=r['source_scope']
        if not scope:
            from payment_auto_matching import local_key,same_head
            original=existing.get(local_key(r['internal'],r['original_name']))
            if original and not same_head(original['head'],r['head']):raise wr.Conflict('该来源内部单号已有本机原关联，不能通过手选订单改绑')
            if r.get('automatic_proof'):
                from payment_auto_matching import relationship_update
                relationship_update(existing,updates,r)
                continue
            if any(v.get('internal')==r['internal'] for k,v in existing.items() if k.startswith(PREFIX+'order.')):raise wr.Conflict('该内部单号已有来源范围，请先确认本次来源企业，不能忽略原关联')
            continue
        key=mapping_key('order',scope,r['internal']);old=existing.get(key)
        if old and any(old['head'].get(k)!=r['head'][k] for k in ('oid','revision','warehouse','number','store','digest')):
            raise wr.Conflict('来源企业及内部订单号已有原关联；原修订失效或对象不同，不能改绑')
        alias_key=mapping_key('store',scope,normalized(r['original_name']));alias=existing.get(alias_key)
        if alias and alias['store']!=r['head']['store']:raise wr.Conflict('该企业中的门店名称已对应另一门店，请先核对门店名称对应关系')
        if not r['remember']:continue
        order=old or {'source_scope':scope,'internal':r['internal'],'original_name':r['original_name'],'head':r['head'],'generation_id':r['generation_id']}
        if key in updates and updates[key]!=order:raise ValueError('同一来源内部订单号分到多张仓单，本期不能记为唯一关系；请取消记住对应后人工核对')
        updates[key]=order
        if normalized(r['original_name']):
            value={'source_scope':scope,'original_name':r['original_name'],'normalized_name':normalized(r['original_name']),'store':r['head']['store']}
            if alias_key in updates and updates[alias_key]['store']!=value['store']:raise ValueError('同一来源门店名对应多家门店，不能记住为唯一门店')
            updates[alias_key]=alias or value
    return {k:v for k,v in updates.items() if existing.get(k)!=v}

def validate(path,db,plan):
    metadata=plan.get('payment_matching')
    if not metadata:return
    if plan.get('payment_matching_digest')!=checksum(metadata):raise ValueError('付款识别预览已变化，请重新核对')
    if pool_token(db)!=metadata['pool_token']:raise wr.Conflict('订单、生命周期或生成范围在识别后变化，请重新读取；未保存')
    if checksum(mappings(db))!=metadata['mapping_token']:raise wr.Conflict('门店或来源单号对应关系已变化，请重新读取')
    current_accounts=accounts(path)
    if current_accounts['token']!=metadata['accounts_token']:raise wr.Conflict('聚水潭企业连接身份已变化，请重新核对来源')
    scopes={a['coid'] for a in current_accounts['choices']}
    for r in metadata['requests']:
        if r['source_scope'] is not None and r['source_scope'] not in scopes:raise ValueError('付款来源企业没有已核验的稳定身份，不能记住内部单号')
        if r['source_scope'] is not None:
            source=next(a for a in current_accounts['choices'] if a['coid']==r['source_scope'])
            warehouses=set(source['warehouses'])&{'山东仓','河北仓'}
            if warehouses and r['head']['warehouse'] not in warehouses:raise ValueError('所选来源企业与订单供货仓不一致；不能保存付款对应')
        matches=[p for p in plan['plans'] if p['stage']=='payment' and all(p['head'].get(k)==r['head'][k] for k in r['head'])]
        if len(matches)!=1:raise ValueError('识别对应关系与付款预览不是同一仓单')
        raw=matches[0]['payload']['raw_rows']
        if not raw or any(row.get('document_number')!=r['internal'] or str((row.get('source_order') or {}).get('店铺名称') or '')!=r['original_name'] for row in raw):raise ValueError('识别对应关系与原付款来源不符')
        if r.get('automatic_proof') and (r['automatic_proof'] not in ('full_detail','explicit_number','previous_source') or r.get('quantity_signature')!=facts(raw)['quantity_signature']):raise ValueError('自动识别依据与付款明细不一致')
    operations(db,metadata)

def apply(db,plan):
    metadata=plan.get('payment_matching')
    if not metadata:return
    for key,value in operations(db,metadata).items():
        db.execute("INSERT INTO lifecycle_mappings VALUES('payment',?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload",(key,wr.dumps(value)))

def prepare(path,documents,scope,index,source_scope,remember=True,progress=lambda _:None,cancel=None):
    import lifecycle_batch
    documents=copy.deepcopy(documents)
    if index:
        with closing(readonly(path)) as db:
            if pool_token(db)!=index['pool_token']:raise wr.Conflict('订单或生成范围已变化，请刷新后重新识别')
    for d in documents:
        if not d.get('selected',True) or d['scan']['stage']!='payment' or d['scan']['rows'][0].get('adapter')!='paid_order':continue
        result=analyze(path,d,index,scope,source_scope,progress=progress,cancel=cancel);d['payment_match']=result
        if result.get('prevent_rebind'):raise ValueError(result['reason'])
        if result['automatic'] and d.get('target') and d['target']['number']!=result['candidates'][0]['number']:raise ValueError('自动识别结果与所选订单不同；不能将完整付款单改绑其他订单')
        if d.get('target') is None and result['automatic']:d['target']=result['candidates'][0]
        if d.get('target') is None:raise ValueError(result['state']+'：'+result['reason'])
    plan=lifecycle_batch.prepare(path,documents,scope,progress,cancel)
    checkpoint(cancel)
    return attach(path,plan,documents,index,source_scope,remember)


def aliases(path):
    with closing(readonly(path)) as db:
        return [{'key':k,'value':v,'token':checksum(v)} for k,v in mappings(db).items() if k.startswith(PREFIX+'store.')]


def forget_alias(path,key,token):
    """Explicit alias maintenance. Internal-order identities remain immutable."""
    if not key.startswith(PREFIX+'store.'):raise ValueError('只能解除门店名称对应，不能删除来源订单原关联')
    with closing(sqlite3.connect(path,timeout=.2)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            value=mappings(db).get(key)
            if value is None or checksum(value)!=token:raise wr.Conflict('门店对应关系已变化，请刷新后再处理')
            from uuid import uuid4
            backup=Path(path).parent/'payment_matching/alias-backups';backup.mkdir(parents=True,exist_ok=True)
            file=backup/(uuid4().hex+'.json')
            with file.open('x',encoding='utf-8') as f:json.dump({'key':key,'value':value},f,ensure_ascii=False,indent=2)
            db.execute("DELETE FROM lifecycle_mappings WHERE stage='payment' AND name=?",(key,));db.commit()
            return str(file)
        except Exception:db.rollback();raise
