"""Read-only payment candidate facts and their disposable feature cache.

When _db is supplied, reuse the caller's snapshot and leave the connection
and transaction with the caller. The derived cache uses a separate database.
"""
from contextlib import closing, nullcontext
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

import archive_orders as ao
import jst_order_store as jst
import payment_shop_names
import storage_chunks as chunks
import warehouse_execution as execution
import warehouse_orders as wo
import work_repository as wr
from export_progress import RowProgress
from normal_orders import read_repository
from payment_evidence import PREFIX, checksum, facts, mapping_key, normalized
from performance_log import begin
from runtime_support import checkpoint


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2);db.execute('PRAGMA query_only=ON');return db


def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def accounts(path):
    """Only stable, previously verified JST company identifiers; no browser or login."""
    file=Path(path).parent/'jst_catalog_sync/jobs.json'
    if not file.is_file():return {'choices':[],'token':checksum([]),'reason':'尚无已核验的聚水潭企业连接；自动模式仍可认单，仅记住本张付款的原单对应，不推断企业'}
    if file.stat().st_size>32*1024*1024:return {'choices':[],'token':checksum(['oversize']),'reason':'来源连接记录过大，未整体加载；可在自动模式认单，不推断企业'}
    try:value=json.loads(file.read_text('utf-8'))
    except (OSError,ValueError):return {'choices':[],'token':checksum(['unreadable']),'reason':'聚水潭来源连接记录无法读取；自动模式仍可认单，仅记住本张付款的原单对应'}
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
    import jst_payment_links
    saved={name:wr.loads(raw) for name,raw in db.execute("SELECT name,payload FROM lifecycle_mappings WHERE stage='payment' AND name LIKE 'payment_match.%' ORDER BY name")} if 'lifecycle_mappings' in tables(db) else {}
    return jst_payment_links.merge(db,saved)


def pool_token(db):
    """Metadata only: additions, deletions, revisions, generation and downstream changes."""
    known=tables(db);parts=[]
    for table,sql in [
        ('normal_orders','SELECT o.oid,o.day,o.store,o.number,o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current ORDER BY o.oid'),
        ('archive_visibility','SELECT oid,warehouse,deleted,token FROM archive_visibility ORDER BY oid,warehouse'),
        ('warehouse_execution_heads','SELECT oid,warehouse,source_revision,child_revision,state,epoch FROM warehouse_execution_heads ORDER BY oid,warehouse'),
        ('jst_active','SELECT a.oid,a.warehouse,a.gid,g.digest FROM jst_active a JOIN jst_generations g ON a.gid=g.gid ORDER BY a.oid,a.warehouse'),
        ('lifecycle_versions',"SELECT oid,revision,warehouse,stage,MAX(version) FROM lifecycle_versions GROUP BY oid,revision,warehouse,stage ORDER BY oid,revision,warehouse,stage")]:
        parts.append([table,list(db.execute(sql)) if table in known else []])
    return checksum(parts)


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


def _generation_key(head):
    return checksum(['generation-v2',head['generation_id'],head['generation_digest']])


def _generation_features(db,cache,head,counts,progress,cancel,current,total):
    gid,gd=head['generation_id'],head['generation_digest']
    key=_generation_key(head)
    features=cache.get(key)
    if features is not None:
        counts['generation_cache_hits']+=1
        return features
    counts['generation_cache_misses']+=1
    progress(RowProgress('读取必要的分配快照以建立付款索引',current,total))
    checkpoint(cancel)
    raw=db.execute('SELECT payload,digest FROM jst_generations WHERE gid=?',(gid,)).fetchone()
    if not raw or raw[1]!=gd:raise wr.Conflict('原分配快照已变化，请重新识别付款单')
    generation=jst.decode(raw)
    if any(generation['head'][k]!=head[k] for k in ('oid','revision','warehouse','digest')):
        raise wr.Conflict('生成单与当前签发身份不符')
    features=facts(r for r in generation['rows'] if Decimal(r['quantity'])>0)
    cache.put(key,features)
    return features


def _index_counts():
    return dict.fromkeys(('candidates','selected','signed_cache_hits','signed_cache_misses',
                          'generation_cache_hits','generation_cache_misses','generation_prefilter_skipped'),0)


def build(path,progress=lambda _:None,cancel=None,*,_db=None,identity_only=False,scope=None):
    """Index current identities. Interactive matching loads generation facts on demand."""
    counts=_index_counts()
    with begin('payment_matching.index') as metrics:
        def report(message):
            metrics.progress(message);progress(message)
        try:
            result=_build(path,report,cancel,_db,identity_only,counts)
            counts['candidates']=len(result['heads'])
            allowed={h['number'] for h in scope} if scope is not None else None
            counts['selected']=counts['candidates'] if allowed is None else sum(h['number'] in allowed for h in result['heads'])
            return result
        finally:metrics.payment_counts(counts)


def _build(path,progress,cancel,_db,identity_only,counts):
    repo=read_repository(path);out=[]
    with (closing(readonly(path)) if _db is None else nullcontext(_db)) as db,closing(Cache(path)) as cache:
        if not db.in_transaction:db.execute('BEGIN')
        token=pool_token(db);known=tables(db);visibility=ao.states(db)
        dependencies={}
        if 'lifecycle_versions' in known:
            for oid,rev,wh,stage,version in db.execute('SELECT oid,revision,warehouse,stage,MAX(version) FROM lifecycle_versions GROUP BY oid,revision,warehouse,stage'):
                dependencies.setdefault((oid,rev,wh),{})[stage]=version
        heads=[dict(zip(('day','store','oid','number','revision','digest','saved'),r)) for r in db.execute('SELECT o.day,o.store,o.oid,o.number,o.current,r.digest,r.saved FROM normal_orders o JOIN normal_revisions r ON r.oid=o.oid AND r.revision=o.current ORDER BY o.day DESC,o.store')]
        effective=[]
        for head in heads:
            effective.extend(execution.heads(db,[head],include_material=True) if execution.has_parent(db,head['oid']) else [head])
        heads=effective
        active={(oid,wh):(gid,revision,digest) for oid,wh,gid,revision,digest in db.execute('SELECT a.oid,a.warehouse,a.gid,g.revision,g.digest FROM jst_active a JOIN jst_generations g ON a.gid=g.gid')} if 'jst_active' in known else {}
        for i,head in enumerate(heads):
            checkpoint(cancel);progress(RowProgress('读取当前签发身份',i+1,len(heads)))
            if identity_only and head.get('warehouse'):
                # The active split warehouse identity and frozen store name are
                # already projected by warehouse_execution.heads().
                meta={'name':head['name'],'warehouses':[head['warehouse']] if head['warehouse'] in wo.WAREHOUSES else []}
            else:
                key=checksum(['signed',head['oid'],head['revision'],head['digest'],head.get('warehouse'),head.get('snapshot_digest')])
                meta=cache.get(key)
                if meta is None:
                    counts['signed_cache_misses']+=1
                    progress(RowProgress('读取签发快照以建立付款索引',i+1,len(heads)))
                    if identity_only:
                        # Legacy parent-only snapshots carry warehouse membership
                        # in their rows. Other fields are unnecessary for the index.
                        raw=db.execute('SELECT payload FROM normal_revisions WHERE oid=? AND revision=?',(head['oid'],head['revision'])).fetchone()[0]
                        fields=chunks.load(raw,db,paths=[('result','rows'),('result','store_name')],binding=head['digest'])
                        name=fields[('result','store_name')];warehouses=ao.scopes(fields[('result','rows')]);del fields
                    else:
                        result=execution.get_order(repo,head,view='export',db=db)['payload']['result']
                        name=result['store_name'];warehouses=ao.scopes(result['rows']);del result
                    meta={'name':name,'warehouses':[wh for wh in warehouses if wh in wo.WAREHOUSES]}
                    cache.put(key,meta)
                else:counts['signed_cache_hits']+=1
            for wh in meta['warehouses']:
                checkpoint(cancel);v=visibility.get((head['oid'],wh),{})
                if v.get('deleted'):continue
                h={**head,'parent_number':head.get('parent_number',head['number']),'number':head['number'] if head.get('warehouse') else wo.child_number(head['number'],wh),'warehouse':wh,'name':meta['name'],
                   'current_revision':head['revision'],'visibility_token':v.get('token',0)}
                gid,revision,gd=active.get((head['oid'],wh),(None,None,None))
                if revision!=h['revision']:gid=None;gd=None
                document_versions=dependencies.get((h['oid'],h['revision'],wh),{})
                h.update(generation_id=gid,generation_digest=gd,basis_kind='generation' if gid else 'return' if 'return' in document_versions else 'signed',
                         payment_version=document_versions.get('payment'),return_version=document_versions.get('return'),features=None,
                         lifecycle_versions=document_versions)
                if gid and not identity_only:h['features']=_generation_features(db,cache,h,counts,progress,cancel,i+1,len(heads))
                out.append(h)
        if pool_token(db)!=token:raise wr.Conflict('索引读取期间业务依据变化，请刷新')
        mapping=mappings(db)
        shop_names=payment_shop_names.records(db)
    return {'heads':out,'pool_token':token,'mapping_token':checksum(mapping),'mappings':mapping,'accounts':accounts(path),
            'decoded_snapshots':counts['signed_cache_misses']+counts['generation_cache_misses'],'shop_names':shop_names}


def _possible_generations(db,heads,sku,cancel):
    # An exact match requires this unchanged product code to exist. Let SQLite
    # check that single fact without materializing unrelated rows in Python.
    # Unrecognized or changed records still go through the original checks.
    ids=wr.dumps([h['generation_id'] for h in heads])
    rows=db.execute('''
        SELECT gid,digest,CASE
          WHEN NOT json_valid(payload) THEN 1
          WHEN json_type(payload,'$.rows') IS NOT 'array' THEN 1
          ELSE EXISTS (
            SELECT 1 FROM json_each(payload,'$.rows') AS item
            WHERE CASE
              WHEN item.type IS NOT 'object' THEN 1
              WHEN json_type(item.value,'$.sku') IS NOT 'text' THEN 1
              ELSE json_extract(item.value,'$.sku')=? END
          ) END
        FROM jst_generations WHERE gid IN (SELECT value FROM json_each(?))''',(sku,ids))
    excluded=set()
    for gid,digest,possible in rows:
        checkpoint(cancel)
        if not possible:excluded.add((gid,digest))
    return [h for h in heads if (h['generation_id'],h['generation_digest']) not in excluded]


def load_features(path,heads,progress=lambda _:None,cancel=None,*,_db=None,required_sku=None):
    pending=[h for h in heads if h.get('generation_id') and h.get('features') is None]
    if not pending:return
    counts=_index_counts();counts['candidates']=len(pending);counts.pop('selected')
    with begin('payment_matching.candidate_snapshots') as metrics:
        def report(message):
            metrics.progress(message);progress(message)
        try:
            with (closing(readonly(path)) if _db is None else nullcontext(_db)) as db,closing(Cache(path)) as cache:
                if not db.in_transaction:db.execute('BEGIN')
                if required_sku is not None and len(pending)>1:
                    cold=[]
                    for head in pending:
                        checkpoint(cancel)
                        features=cache.get(_generation_key(head))
                        if features is None:cold.append(head)
                        else:head['features']=features;counts['generation_cache_hits']+=1
                    pending=_possible_generations(db,cold,required_sku,cancel) if cold else cold
                    counts['generation_prefilter_skipped']=len(cold)-len(pending)
                for i,head in enumerate(pending,1):
                    checkpoint(cancel)
                    head['features']=_generation_features(db,cache,head,counts,report,cancel,i,len(pending))
        finally:metrics.payment_counts(counts)


def _evidence_candidates(index,requests):
    warehouse_scopes={a['coid']:set(a['warehouses'])&{'山东仓','河北仓'} for a in index['accounts']['choices']}
    for h in index['heads']:
        signatures=[]
        for r in requests:
            selected=all(h.get(k)==r['head'].get(k) for k in ('oid','warehouse')) or h['number']==r['head']['number']
            warehouses=warehouse_scopes.get(r['source_scope'],())
            eligible=(not warehouses or h['warehouse'] in warehouses) and all(not value or value==h[{'signed_number':'number'}.get(field,field)] for c in r.get('identity_constraints',[]) for field,value in c.items())
            proof=r.get('automatic_proof');context=r.get('name_context');automatic=not r.get('manual_selection',True)
            unique=eligible and automatic and not r.get('explicit_identity') and proof not in ('explicit_number','previous_source')
            named=bool(unique and context and [h['store'],h['warehouse']] in context['pairs'])
            possible_name=bool(unique and (not context or context['basis']=='application_name') and normalized(h['name'])==normalized(r['original_name']))
            if selected or named or possible_name:
                yield h,None
                break
            if unique and proof in (None,'full_detail'):
                signature='signature' if r.get('facts_signature') else 'quantity_signature'
                signatures.append((signature,r.get('facts_signature') or r['quantity_signature']))
        else:
            if signatures:yield h,signatures


def scope_evidence(path, index, requests, progress=lambda _: None, cancel=None, *, db=None):
    """Relevant candidate additions/removals count; unrelated payments do not."""
    candidates=list(_evidence_candidates(index,requests))
    load_features(path, (head for head, _ in candidates), progress, cancel, _db=db)
    relevant=[h for h,checks in candidates if checks is None or (
        h.get('features') and any(h['features'].get(k)==value for k,value in checks))]
    mapping={}
    keys=set();orders=set();aliases=set()
    # Hash request keys once, not once per historical mapping and request.
    for r in requests:
        scope=r['source_scope'];name=normalized(r['original_name'])
        keys.add(mapping_key('document',None,checksum([str(r['internal']),name])))
        if scope is not None:keys.update((mapping_key('order',scope,r['internal']),mapping_key('store',scope,name)))
        else:
            orders.add((r['internal'],name))
            if not r.get('name_context') or r['name_context']['basis']!='configured_name':aliases.add(name)
    for key,value in index['mappings'].items():
        if key in keys or (orders and key.startswith(PREFIX+'order.') and (value.get('internal'),normalized(value.get('original_name'))) in orders) or (aliases and key.startswith(PREFIX+'store.') and normalized(value.get('original_name')) in aliases):mapping[key]=value
    return {'version':2,'heads':checksum(sorted(relevant,key=lambda h:(h['oid'],h['warehouse']))),'mappings':checksum(mapping)}
