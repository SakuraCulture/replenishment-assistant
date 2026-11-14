"""Add-only order-graph merge into an isolated copy; never opens a browser.

No local preferences, live stock pointer, daily draft or connection is replaced.
The caller owns the safe-exit/backup/directory-swap transaction. Unknown owned
records and divergent shared dependencies isolate the whole affected component.
"""
from __future__ import annotations
import hashlib,json,os,re,sqlite3,shutil,copy,tempfile
from pathlib import Path
from contextlib import closing
from dataclasses import dataclass,field
from runtime_support import checkpoint,Cancelled

DBS=('workbench.sqlite3','aoxiang/workflows.sqlite3','aoxiang/jobs.sqlite3','aoxiang/component-state/state.sqlite')
EVENTS={'normal_events','lifecycle_events','jst_events','purchase_events','automation_events'}
DIRECT={'normal_orders','normal_revisions','normal_events','merchant_feedback','lifecycle_routes','lifecycle_versions','lifecycle_events','followup_notes','archive_visibility','jst_generations','jst_active','jst_locks','jst_events','order_component_packages','warehouse_execution_heads','warehouse_execution_revisions','warehouse_execution_retirements','deleted_warehouse_tokens','warehouse_component_packages'}
MAIN=DIRECT|{'jst_headers','jst_reservations','jst_generation_batches','jst_batches','jst_batch_stock','jst_batch_members','jst_batch_retired','jst_runs','jst_steps','jst_tasks','purchase_documents','purchase_versions','purchase_events','purchase_link_index','automation_tasks','automation_events','scheduled_runs','scheduled_values','scheduled_events','deleted_jst_steps'}
WEAK={'normal_work_revisions','jst_baselines','retired_stock','source_files','lifecycle_files','jst_files','purchase_files','snapshot_chunks'}
GLOBAL={'jobs','stores','presets','stocking_lists','replenishment_plans','replenishment_plan_events','scheduled_arrangements','settings_history','store_admission','admission_events','global_category_mapping','global_category_mapping_events','lifecycle_mappings','purchase_mappings','maintenance_chunk_refs','snapshot_storage_meta','jst_warehouses','catalog_sync_applies','deleted_order_tokens','deletion_generations','deletion_component_outbox','warehouse_deletion_generations','sqlite_sequence','order_merge_receipts'}
NODE={'purchase_creates','purchase_create_events','purchase_receipts','purchase_receipt_events','purchase_runs','retired_host_orders','retired_host_warehouses','retired_purchase_tokens','retired_purchase_runs','retirement_files','order_owned_files','warehouse_owned_files','machine_commands','machine_events','machine_retired','assets'}
NODE_GLOBAL={'metadata','runs','items','attempts','publishes','imported_pending','migration_imports','conflicts','retired_collections','collection_reexports','collection_consumers','machine_protocol'}
PARENTS={'normal_orders':('oid',),'jst_generations':('gid','operation'),'jst_batches':('batch_id',),'jst_runs':('runid',),'jst_tasks':('task',),'purchase_documents':('docid','identity'),'automation_tasks':('id','operation_key'),'scheduled_runs':('id',),'ax_workflows':('id',),'ax_jobs':('id',),'purchase_creates':('id',),'purchase_runs':('id',),'purchase_receipts':('id',),'machine_commands':('id',),'runs':('id',),'items':('id',),'attempts':('id',),'publishes':('id',),'assets':('asset_key',)}
LINKS={'oid','gid','batch_id','runid','docid','task','run_id','id','item_id','command_id','purchase_id','hostOrderId','hostPurchaseId','hostOrderIds','rebuildOf','successorId','operation'}
ASSETS={'source_files','lifecycle_files','jst_files','purchase_files'}
ACTIVE={'QUEUED','RUNNING','YIELDED','PAUSING','PAUSE_REQUESTED','PREPARING','READY','VERIFYING','SUBMITTING'}

def encoded(v):return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def digest(v):return hashlib.sha256(encoded(v).encode('utf-8')).hexdigest()
def file_hash(p):
    with Path(p).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()
def q(s):return '"'+s.replace('"','""')+'"'
def strings(v):
    if isinstance(v,str):yield v
    elif isinstance(v,dict):
        for k,x in v.items():yield str(k);yield from strings(x)
    elif isinstance(v,(tuple,list)):
        for x in v:yield from strings(x)
def typed(v):
    if isinstance(v,bytes):return {'bytes_sha256':hashlib.sha256(v).hexdigest(),'size':len(v)}
    if isinstance(v,dict):return ['dict',[[str(k),typed(x)] for k,x in v.items()]]
    if isinstance(v,(list,tuple)):return ['list',[typed(x) for x in v]]
    return [type(v).__name__,v]

@dataclass
class Row:
    db:str;table:str;rid:int;values:dict;pk:tuple;refs:set;owners:set=field(default_factory=set);weak:bool=False;error:str='';blob:dict=field(default_factory=dict);origin_file:str|None=None
    @property
    def key(self):return self.db,self.table,self.rid
    def business(self):
        if self.table in ASSETS:return {'sha':self.values['sha'],'content':self.values['content']}
        if self.table=='snapshot_chunks':return {k:self.values[k] for k in ('hash','size')}
        return {k:v for k,v in self.values.items() if not (self.table in EVENTS and k=='id') and not (self.table=='ax_jobs' and k=='updated')}

class Graph:
    def __init__(self,root,progress,cancel,*,file_fallback=False):
        self.root=Path(root).resolve();self.rows=[];self.sql={};self.columns={};self.indexes={};self.contracts={};self.order={};self.connections={};self.cancel=cancel;self.progress=progress;self.paths=set();self.deleted=set();self.receipts={};self.files={};self.map={};self.bad={};self.scratch=[];self.file_tokens={};self.deleted_business=set();self.revision_names={};self.deleted_warehouses=set();self.active_warehouses={};self.legacy_warehouses={}
        marker=self.root/'迁入原文件映射.json';self.contents={}
        if marker.is_file():
            record=json.loads(marker.read_text('utf-8'));self.map=record.get('paths',{});self.contents=record.get('contents',{})
        try:
            for name in DBS:
                p=self.root/name
                if not p.is_file():continue
                before=_db_identity(p);self.file_tokens[p]=before
                readpath=p
                # Read-only SQLite can create -shm beside a WAL database. Read
                # stable copies of WAL/journal sets so preview never writes
                # anything into the caller's source or target directories.
                if before[1] is not None or before[2] is not None:
                    from stockout_common import resource_check
                    resource_check(self.root.parent,sum(v[2] for v in before if v is not None))
                    temporary=tempfile.TemporaryDirectory(prefix='.order-merge-read-',dir=self.root.parent);self.scratch.append(temporary)
                    readpath=Path(temporary.name)/p.name
                    for suffix,identity in zip(('','-wal','-journal'),before):
                        if identity is not None:shutil.copyfile(Path(str(p)+suffix),Path(str(readpath)+suffix))
                    if _db_identity(p)!=before:raise ValueError('订单资料在读取期间变化，请重新核对')
                    db=sqlite3.connect(readpath)
                else:db=sqlite3.connect(p.as_uri()+'?mode=ro&immutable=1',uri=True)
                db.row_factory=sqlite3.Row;self.connections[name]=db;db.execute('BEGIN')
                if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('合并资料库完整性校验失败：'+name)
                self._read(name,db)
            self._owners()
            self._files()
            self._declared_files(file_fallback)
            if any(_db_identity(p)!=token for p,token in self.file_tokens.items()):raise ValueError('订单资料在读取期间变化，请重新核对')
        except BaseException:self.close();raise
    def close(self):
        for db in self.connections.values():db.close()
        for temporary in self.scratch:temporary.cleanup()
    def _read(self,name,db):
        schemas=list(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"))
        for table,sql in schemas:
            checkpoint(self.cancel);self.sql[name,table]=sql
            cols=list(db.execute('PRAGMA table_info('+q(table)+')'));self.columns[name,table]=[r['name'] for r in cols];pk=tuple(r['name'] for r in sorted(cols,key=lambda r:r['pk']) if r['pk'])
            self.contracts[name,table]=_table_contract(db,table,cols)
            self.indexes[name,table]=[(r['name'],[x['name'] for x in db.execute('PRAGMA index_info('+q(r['name'])+')')],r['partial']) for r in db.execute('PRAGMA index_list('+q(table)+')') if r['unique']]
            if table=='deleted_order_tokens':self.deleted.update(r[0] for r in db.execute('SELECT oid FROM '+q(table)))
            if table in ('warehouse_execution_retirements','deleted_warehouse_tokens','retired_host_warehouses'):self.deleted_warehouses.update(tuple(r) for r in db.execute('SELECT oid,warehouse FROM '+q(table)))
            if table=='warehouse_execution_heads':
                for oid,warehouse,state in db.execute('SELECT oid,warehouse,state FROM '+q(table)):
                    self.active_warehouses.setdefault(oid,set())
                    if state=='active':self.active_warehouses[oid].add(warehouse)
                    if state=='deleted':self.deleted_warehouses.add((oid,warehouse))
            if table=='deletion_generations':self.deleted_business.update((r[0],r[1]) for r in db.execute('SELECT day,store FROM deletion_generations WHERE generation>0'))
            if table=='retired_host_orders':self.deleted.update(r[0] for r in db.execute('SELECT oid FROM '+q(table)))
            if table=='order_merge_receipts':
                self.receipts={r['oid']:dict(r) for r in db.execute('SELECT * FROM '+q(table))};continue
            # Asset bytes remain in SQLite, never accumulated with decoded trees.
            asset=table in ASSETS or table=='snapshot_chunks';omit={'content','data'} if asset else set()
            select=','.join(q(c['name']) for c in cols if c['name'] not in omit)
            for raw in db.execute('SELECT rowid AS _merge_rid,'+select+' FROM '+q(table)+' ORDER BY rowid'):
                checkpoint(self.cancel);values=dict(raw);rid=values.pop('_merge_rid');refs=set();error='';blobs={}
                for c in cols:
                    if c['name'] in omit:
                        length=db.execute('SELECT length('+q(c['name'])+') FROM '+q(table)+' WHERE rowid=?',(rid,)).fetchone()[0]
                        blobs[c['name']]=length;values[c['name']]={'blob':length,'sha':values.get('sha',values.get('hash'))}
                try:
                    for key,v in list(values.items()):
                        if isinstance(v,bytes) and key=='payload':
                            import storage_chunks as chunks
                            binding=values.get('snapshot_digest') if table=='warehouse_execution_revisions' else values.get('digest') if table=='normal_revisions' else None
                            m=chunks.require_manifest(v);decoded=chunks.load(v,db,binding=binding)
                            if table=='normal_revisions':
                                import normal_orders
                                if normal_orders.fingerprint_owned(decoded['result'],decoded.get('new_focus'))!=values['digest']:raise ValueError('归档业务摘要不一致')
                                self.revision_names[values['oid'],values['revision']]=decoded['result'].get('store_name')
                                import warehouse_execution
                                self.legacy_warehouses.setdefault(values['oid'],set()).update(warehouse_execution.warehouses(decoded))
                            if table=='warehouse_execution_revisions':
                                import normal_orders
                                if normal_orders.fingerprint_owned(decoded['result'],decoded.get('new_focus'))!=values['snapshot_digest']:raise ValueError('分仓签发业务摘要不一致')
                            refs.update(strings(decoded));refs.update(_chunk_refs(m['tree']))
                        elif isinstance(v,str):
                            refs.add(v)
                            if v[:1] in ('{','['):refs.update(strings(json.loads(v)))
                    self._validate(table,values)
                except Cancelled:raise
                except Exception as exc:error=str(exc)
                weak=table in WEAK
                row=Row(name,table,rid,values,pk,refs,weak=weak,error=error,blob=blobs);self.rows.append(row)
                self.paths.update(x for x in refs if _absolute(x))
                if table=='normal_orders':self.order[values['oid']]=values
    def _validate(self,table,values):
        if table=='lifecycle_versions':
            from order_lifecycle import decode
            decode(tuple(values[k] for k in ('version','saved','digest','payload')))
        elif table=='purchase_versions':
            from purchases import fingerprint
            if fingerprint(json.loads(values['payload']))!=values['digest']:raise ValueError('采购快照摘要不一致')
        elif table in ('jst_baselines','jst_generations','jst_runs','jst_batches'):
            from jst_orders import decode
            decode((values['payload'],values['digest']))
    def _owners(self):
        alloids=set(self.order);strong=[r for r in self.rows if not r.weak and not (r.db==DBS[0] and r.table in GLOBAL)]
        parent={}
        for r in strong:
            r.owners=set()
            r.owners.update(r.refs&alloids)
            if r.values.get('oid') in alloids:r.owners.add(r.values['oid'])
            for col in PARENTS.get(r.table,()):
                value=r.values.get(col)
                if isinstance(value,str) and value:parent.setdefault(value,set())
        # Edge identifiers propagate both ways only across mutable shared objects.
        changed=True
        while changed:
            changed=False
            for r in strong:
                old=set(r.owners)
                for ref in r.refs:r.owners.update(parent.get(ref,()))
                for ref in r.refs&parent.keys():
                    if not r.owners<=parent[ref]:parent[ref].update(r.owners);changed=True
                if old!=r.owners:changed=True
        weak_lookup={}
        for r in self.rows:
            if r.weak:
                key=r.values.get('sha') or r.values.get('hash') or r.values.get('bid') or (r.values.get('revision') if r.table=='normal_work_revisions' else None)
                if key:weak_lookup.setdefault(key,[]).append(r)
        # Frozen work/baseline nodes can share bytes without forcing order groups.
        changed=True
        while changed:
            changed=False
            for r in self.rows:
                if not r.owners:continue
                for ref in r.refs:
                    for dep in weak_lookup.get(ref,()):
                        if not r.owners<=dep.owners:dep.owners.update(r.owners);changed=True
        for r in self.rows:
            known=(r.table in MAIN|WEAK if r.db==DBS[0] else r.table in {'ax_workflows','ax_workflow_events','ax_schedules','ax_workflow_meta','ax_jobs','ax_meta'} if r.db in DBS[1:3] else r.table in NODE|NODE_GLOBAL)
            if r.db==DBS[0] and r.table in GLOBAL:known=True
            if not known:
                # No guessed attribution: a new nonempty table without a known
                # ownership contract is not silently lost from an imported order.
                for oid in r.owners or alloids:self.bad.setdefault(oid,[]).append('存在暂不支持合并且不能安全归属的资料表：'+r.table)
            if r.owners and r.table in ASSETS:
                try:
                    with self.connections[r.db].blobopen(r.table,'content',r.rid,readonly=True) as blob:
                        h=hashlib.sha256()
                        while block:=blob.read(512*1024):checkpoint(self.cancel);h.update(block)
                    if h.hexdigest()!=r.values['sha']:raise ValueError('原件内容与SHA不一致')
                except Cancelled:raise
                except Exception as exc:r.error=str(exc)
            if r.error:
                for oid in r.owners:self.bad.setdefault(oid,[]).append(r.table+'校验失败：'+r.error)
        self.groups=[set(r.owners) for r in strong if len(r.owners)>1]
        self.by_order={oid:[r for r in self.rows if oid in r.owners] for oid in self.order}
        # A header without its complete version/dependency cannot be displayed
        # or resumed, even if SQLite itself passes quick_check.
        revisions={(r.values['oid'],r.values['revision']) for r in self.rows if r.table=='normal_revisions'}
        child_revisions={(r.values['oid'],r.values['warehouse'],r.values['source_revision']) for r in self.rows if r.table=='warehouse_execution_revisions'}
        works={r.values['revision'] for r in self.rows if r.table=='normal_work_revisions'}
        generations={r.values['gid'] for r in self.rows if r.table=='jst_generations'}
        batches={r.values['batch_id'] for r in self.rows if r.table=='jst_batches'}
        bases={r.values['bid'] for r in self.rows if r.table=='jst_baselines'}
        for oid,head in self.order.items():
            if (oid,head['current']) not in revisions:self.bad.setdefault(oid,[]).append('订单当前签发修订缺失')
        for row in self.rows:
            if not row.owners:continue
            issue=None
            if row.table=='normal_revisions' and not row.error:
                import storage_chunks
                decoded=storage_chunks.load(row.values['payload'],self.connections[row.db],binding=row.values['digest'])
                if decoded.get('work_revision') not in works:issue='签发引用的原工作修订缺失'
            elif row.table=='warehouse_execution_heads' and row.values['state']=='active' and (row.values['oid'],row.values['warehouse'],row.values['source_revision']) not in child_revisions:issue='分仓当前签发快照缺失'
            elif row.table=='warehouse_execution_revisions' and not row.error:
                import storage_chunks
                decoded=storage_chunks.load(row.values['payload'],self.connections[row.db],binding=row.values['snapshot_digest'])
                if decoded.get('work_revision') not in works:issue='分仓签发引用的原工作修订缺失'
            elif row.table in ('jst_active','jst_locks','jst_generation_batches') and row.values['gid'] not in generations:issue='原库存分配生成记录缺失'
            elif row.table=='jst_generation_batches' and row.values['batch_id'] not in batches:issue='原库存分配批次缺失'
            elif row.table=='jst_batch_stock' and row.values['bid'] not in bases:issue='原库存分配基线缺失'
            if issue:
                for oid in row.owners:self.bad.setdefault(oid,[]).append(issue)
    def _files(self):
        # Optional export-recovery files are order-owned mutable shared objects.
        for p in sorted((self.root/'export_receipts').glob('*.json')):
            checkpoint(self.cancel);value=json.loads(p.read_text('utf-8'));refs=set(strings({k:v for k,v in value.items() if k!='database'}));owners=refs&self.order.keys()
            if owners:
                rel=p.relative_to(self.root).as_posix();self.files[rel]=(owners,refs,value)
                if len(owners)>1:self.groups.append(set(owners))
        for rel,(owners,refs,value) in self.files.items():
            for oid in owners:self.paths.update(x for x in refs if _absolute(x))
    def _declared_files(self,allow_fallback):
        """Declared originals must exist, not merely have a surviving SHA label.

        A verified ZIP original may reconstruct the identical missing asset row
        in the private target. No signed/lifecycle payload is rewritten, and
        missing declarations in a legacy payload are not invented.
        """
        expected={'merchant_feedback':'source_files','lifecycle_versions':'lifecycle_files','purchase_versions':'purchase_files'}
        assets={(r.table,r.values.get('sha')):r for r in self.rows if r.table in ASSETS}
        for parent in list(self.rows):
            if not parent.owners or parent.table not in expected or parent.error:continue
            payload=json.loads(parent.values['payload']);files=payload.get('files')
            if files is None:continue
            if not isinstance(files,list):
                for oid in parent.owners:self.bad.setdefault(oid,[]).append('单据原件清单格式无效：'+parent.table)
                continue
            for entry in files:
                checkpoint(self.cancel)
                h=entry.get('sha') if isinstance(entry,dict) else None
                if not isinstance(h,str) or not re.fullmatch('[a-f0-9]{64}',h):
                    for oid in parent.owners:self.bad.setdefault(oid,[]).append('单据明确列出的原件缺少有效SHA：'+parent.table)
                    continue
                table=expected[parent.table];key=table,h
                if key in assets:
                    asset=assets[key]
                    for oid in parent.owners-asset.owners:self.by_order[oid].append(asset)
                    asset.owners.update(parent.owners)
                    continue
                candidate=None
                if allow_fallback:
                    possible=list(self.contents.get(h,()))
                    if entry.get('path') in self.map:possible.insert(0,self.map[entry['path']])
                    for relative in dict.fromkeys(possible):
                        p=(self.root/relative).resolve()
                        if p.is_relative_to(self.root) and p.is_file() and file_hash(p)==h:candidate=p;break
                if candidate is None:
                    for oid in parent.owners:self.bad.setdefault(oid,[]).append('单据原件缺失：'+str(entry.get('name') or h)+'；库内及迁移包均未保留对应内容')
                    continue
                # A verified immutable ZIP original is enough to recreate this
                # content-addressed row; its bytes and SHA remain unchanged.
                dbname=DBS[0];schema_key=dbname,table
                if schema_key not in self.sql:
                    saved=',saved TEXT NOT NULL' if table=='source_files' else ''
                    required='' if table=='lifecycle_files' else ' NOT NULL'
                    sql='CREATE TABLE '+q(table)+'(sha TEXT PRIMARY KEY,name TEXT'+required+saved+',content BLOB'+required+')'
                    with closing(sqlite3.connect(':memory:')) as temp:
                        temp.row_factory=sqlite3.Row;temp.execute(sql);columns=list(temp.execute('PRAGMA table_info('+q(table)+')'))
                        self.sql[schema_key]=sql;self.columns[schema_key]=[r['name'] for r in columns];self.contracts[schema_key]=_table_contract(temp,table,columns);self.indexes[schema_key]=[]
                values={'sha':h,'name':entry.get('name') or candidate.name}
                if table=='source_files':values['saved']=parent.values.get('saved','')
                values['content']={'blob':candidate.stat().st_size,'sha':h}
                values={k:values[k] for k in self.columns[schema_key]}
                row=Row(dbname,table,-len(self.rows)-1,values,('sha',),{h},owners=set(parent.owners),weak=True,blob={'content':candidate.stat().st_size},origin_file=candidate.relative_to(self.root).as_posix())
                self.rows.append(row);assets[key]=row
                for oid in row.owners:self.by_order[oid].append(row)
    def owner_hash(self,oid):
        rows=[]
        for r in self.by_order[oid]:rows.append([r.db,r.table,typed(r.business())])
        files=[]
        for rel,(owners,refs,value) in self.files.items():
            if oid in owners:
                v={k:x for k,x in value.items() if k!='database'};files.append([rel,typed(v)])
        return digest({'rows':sorted(rows,key=encoded),'files':files})

def _absolute(s):
    return isinstance(s,str) and len(s)<32768 and ('\x00' not in s) and (Path(s).is_absolute() or bool(re.match(r'^[A-Za-z]:[\\/]',s)))
def _db_identity(path):
    values=[]
    for suffix in ('','-wal','-journal'):
        try:
            s=Path(str(path)+suffix).stat();values.append((s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns))
        except FileNotFoundError:values.append(None)
    return tuple(values)
def _sql_contract(sql):
    # Whitespace/case outside quoted literals is SQL syntax, not data. Preserve
    # quoted values (and expression indexes) rather than dropping WHERE guards.
    parts=re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")",sql or '')
    return ''.join(re.sub(r'\s+','',p).lower() if i%2==0 else p for i,p in enumerate(parts))
def _table_contract(db,table,columns):
    unique=[]
    for index in db.execute('PRAGMA index_list('+q(table)+')'):
        if not index['unique']:continue
        keys=[(r['name'],r['coll'],r['desc']) for r in db.execute('PRAGMA index_xinfo('+q(index['name'])+')') if r['key']]
        sql=(db.execute("SELECT sql FROM sqlite_master WHERE type='index' AND name=?",(index['name'],)).fetchone() or [None])[0]
        expression=''
        if any(k[0] is None for k in keys):expression=re.split(r'\bON\b',sql or '',maxsplit=1,flags=re.I)[-1]
        elif index['partial']:expression=re.split(r'\bWHERE\b',sql or '',maxsplit=1,flags=re.I)[-1]
        unique.append([keys,_sql_contract(expression)])
    return {'columns':[(r['name'],r['type'].upper(),r['notnull'],r['dflt_value'],r['pk']) for r in columns],
            'unique':sorted(unique,key=encoded),'foreign':sorted([tuple(r)[1:] for r in db.execute('PRAGMA foreign_key_list('+q(table)+')')],key=encoded)}
def _contract_issue(src,dst,row):
    key=row.db,row.table
    if key in dst.contracts and src.contracts[key]!=dst.contracts[key]:return '关联表字段类型、主键或唯一约束不兼容：'+row.table
    return None
def _chunk_refs(node):
    if node[0]=='ref':yield node[1]
    elif node[0]=='dict':
        for _,child in node[1]:yield from _chunk_refs(child)
    elif node[0] in ('array','map'):
        for child in node[1]:yield from _chunk_refs(child)

def _check_asset(graph,row):
    if row.table in ASSETS:
        with graph.connections[row.db].blobopen(row.table,'content',row.rid,readonly=True) as blob:
            h=hashlib.sha256()
            while block:=blob.read(512*1024):checkpoint(graph.cancel);h.update(block)
        if h.hexdigest()!=row.values['sha']:raise ValueError('原件内容与SHA不一致')
    elif row.table=='snapshot_chunks':
        import zlib
        raw=graph.connections[row.db].execute('SELECT data FROM snapshot_chunks WHERE rowid=?',(row.rid,)).fetchone()[0];plain=zlib.decompress(raw)
        if len(plain)!=row.values['size'] or hashlib.sha256(plain).hexdigest()!=row.values['hash']:raise ValueError('快照内容块摘要不一致')

def _protocol_issues(src,dst,oid):
    reasons=[];owned={r.db for r in src.by_order[oid]}
    for name in (DBS[1],DBS[2]):
        if name not in owned:continue
        table='ax_workflow_meta' if name==DBS[1] else 'ax_meta'
        for g in (src,dst):
            if name in g.connections:
                if (name,table) not in g.sql or [r[0] for r in g.connections[name].execute('SELECT version FROM '+q(table))]!=[1]:reasons.append('翱象队列协议版本不兼容：'+name)
    name=DBS[3]
    if name in owned:
        for g in (src,dst):
            if name not in g.connections:continue
            if (name,'metadata') not in g.sql:reasons.append('组件协议身份缺失');continue
            meta=dict(g.connections[name].execute('SELECT key,value FROM metadata'))
            if meta.get('schemaVersion') not in ('1','2'):reasons.append('组件状态版本不兼容')
            for key,value in meta.items():
                if (key.endswith('Protocol') or key=='collectionIsolation') and value!='1':reasons.append('组件协议不兼容：'+key)
            if (name,'machine_protocol') in g.sql and [r[0] for r in g.connections[name].execute('SELECT version FROM machine_protocol')]!=[1]:reasons.append('组件命令协议版本不兼容')
    return reasons

def _compatible(src,dst,row):
    """Return a clear conflict for PK/unique collisions; never REPLACE a row."""
    db=dst.connections.get(row.db)
    if db is None or (row.db,row.table) not in dst.sql:return None
    issue=_contract_issue(src,dst,row)
    if issue:return issue
    if row.table in EVENTS:return None
    keys=[list(row.pk)] if row.pk else []
    keys += [k for _,k,partial in src.indexes[row.db,row.table] if k and not partial]
    if row.table=='purchase_creates':
        # SQLite partial unique indexes and the stronger same-merchant host rule.
        p=json.loads(row.values['payload']);i=p.get('intent',{});basis=i.get('basis',{});merchant=i.get('identity',{}).get('merchantCode')
        for old in db.execute('SELECT * FROM purchase_creates'):
            v=json.loads(old['payload']);intent=v.get('intent',{});b=intent.get('basis',{})
            external=row.values.get('platform_order') and row.values.get('platform_order')==old['platform_order'] and (row.values.get('identity_key')==old['identity_key'] or merchant and merchant==intent.get('identity',{}).get('merchantCode'))
            host=merchant and merchant==intent.get('identity',{}).get('merchantCode') and all(b.get(k)==basis.get(k) for k in ('number','warehouse','store'))
            if external or (host and v.get('state') not in ('CANCELLED_BEFORE_UPLOAD','SUPERSEDED')):
                if old['id']!=row.values['id']:return '平台采购或原订单已有其他关联，未覆盖：'+str(row.values.get('platform_order') or basis.get('number'))
        keys.append(['order_key'])
    for columns in keys:
        if any(c not in row.values or row.values[c] is None for c in columns):continue
        old=db.execute('SELECT rowid AS _merge_rid,* FROM '+q(row.table)+' WHERE '+' AND '.join(q(c)+' IS ?' for c in columns),tuple(row.values[c] for c in columns)).fetchone()
        if old:
            target=next(r for r in dst.rows if r.db==row.db and r.table==row.table and r.rid==old['_merge_rid'])
            if row.table in ASSETS|{'snapshot_chunks'}:
                if row.values.get('sha',row.values.get('hash'))==target.values.get('sha',target.values.get('hash')):
                    try:_check_asset(dst,target)
                    except Cancelled:raise
                    except Exception as exc:return '本机相同身份原件或内容块校验失败：'+str(exc)
                    continue
            if typed(row.business())!=typed(target.business()):return '相同关联身份内容不同：'+row.table
    return None

def _path_plan(src,dst,oid):
    references=set().union(*(r.refs for r in src.by_order[oid]))
    for owners,refs,value in src.files.values():
        if oid in owners:references.update(refs)
    result={};reasons=[]
    for old in sorted(references&src.map.keys()):
        rel=src.map[old];p=(src.root/rel).resolve()
        if not p.is_relative_to(src.root) or not p.is_file():reasons.append('关联原件不存在：'+old);continue
        h=file_hash(p)
        if _absolute(old) and Path(old).is_file() and file_hash(old)!=h:reasons.append('原文件路径在本机已有不同内容：'+old);continue
        existing=dst.map.get(old)
        if existing:
            local=(dst.root/existing).resolve()
            if not local.is_relative_to(dst.root) or not local.is_file() or file_hash(local)!=h:reasons.append('本机原件路径映射冲突：'+old);continue
        elif old in dst.paths and not Path(old).is_file():reasons.append('原件路径被本机旧资料引用且来源不明：'+old);continue
        result[old]={'source':rel,'sha256':h,'target':'migration-assets/'+h+'/'+p.name}
    return result,reasons

def _inspect(src,dst):
    results=[];paths={};hashes={};groups=src.groups
    for oid,head in src.order.items():
        checkpoint(src.cancel);sh=src.owner_hash(oid);hashes[oid]=sh;reasons=list(src.bad.get(oid,()))
        reasons.extend(issue for row in src.by_order[oid] if (issue:=_contract_issue(src,dst,row)))
        reasons.extend(_protocol_issues(src,dst,oid))
        reasons.extend('本机关联资料校验失败：'+v for v in dst.bad.get(oid,()))
        if oid in src.deleted:reasons.append('来源包同时存在订单与删除或退役标记，状态矛盾；此订单未迁入，保留本机资料')
        incoming=src.active_warehouses.get(oid,src.legacy_warehouses.get(oid,set()))
        retired={warehouse for owner,warehouse in dst.deleted_warehouses if owner==oid}
        if incoming & {warehouse for owner,warehouse in src.deleted_warehouses if owner==oid}:reasons.append('来源包同一仓单同时有效与退役，不能自动迁入')
        if oid in dst.deleted or incoming & retired or (head['day'],head['store']) in dst.deleted_business and not any(h['day']==head['day'] and h['store']==head['store'] for h in dst.order.values()):
            state='DELETED';reasons.append('本机已删除此订单，旧迁移包不恢复')
        else:
            collisions=[o for o,h in dst.order.items() if o==oid or (h['day'],h['store'],h['kind'])==(head['day'],head['store'],head['kind']) or h['number']==head['number']]
            if collisions:
                receipt=dst.receipts.get(oid);same=oid in dst.order and typed(head)==typed(dst.order[oid])
                target_hash=dst.owner_hash(oid) if oid in dst.order else None
                same=same and (target_hash==sh or bool(receipt and receipt['source_digest']==sh and receipt['target_digest']==target_hash))
                state='DUPLICATE' if same and not reasons else 'CONFLICT'
                if not same:reasons.append('本机已有同日同店、同签发单号或同身份订单，完整关联内容不同；保留本机')
            else:
                state='ADD'
                for row in src.by_order[oid]:
                    issue=_compatible(src,dst,row)
                    if issue:reasons.append(issue)
                for rel,(owners,refs,value) in src.files.items():
                    if oid in owners and rel in dst.files:
                        before=dst.files[rel][2]
                        if typed({k:v for k,v in before.items() if k!='database'})!=typed({k:v for k,v in value.items() if k!='database'}):reasons.append('导出恢复记录身份已被本机使用且内容不同：'+rel)
                paths[oid],issues=_path_plan(src,dst,oid);reasons.extend(issues)
                if reasons:state='CONFLICT'
        results.append({k:head[k] for k in ('oid','number','day','store')}|{'name':src.revision_names.get((oid,head['current'])) or head['store'],'status':state,'reason':'；'.join(dict.fromkeys(reasons)),'source_digest':sh})
    byid={r['oid']:r for r in results};changed=True
    while changed:
        changed=False
        for group in groups:
            blocked=[o for o in group if o not in byid or byid[o]['status'] in ('CONFLICT','DELETED')]
            if blocked:
                for oid in group:
                    if oid in byid and byid[oid]['status']=='ADD':
                        byid[oid].update(status='CONFLICT',reason='共享分配批次、采购单或作业关联的其他订单有冲突，保留整个关联组；不影响无关订单');changed=True
    counts={k:sum(r['status']==v for r in results) for k,v in [('added','ADD'),('duplicate','DUPLICATE'),('conflict','CONFLICT'),('deleted','DELETED')]}
    target={o:dst.owner_hash(o) for o in dst.order if o in src.order}
    # Fingerprint only graph facts involved in this preview, not unrelated jobs.
    value={'version':1,'counts':counts,'orders':results,'source_digest':digest(hashes),'target_digest':digest(target),'paths':paths}
    value['fingerprint']=digest(value);return value

def inspect(source_root,target_root,progress=lambda _:None,cancel=None):
    progress('核对来源完整订单链与本机订单，保留本机资料…')
    src=Graph(source_root,progress,cancel,file_fallback=True)
    try:
        dst=Graph(target_root,progress,cancel)
        try:return _inspect(src,dst)
        finally:dst.close()
    finally:src.close()

def _ensure_schema(src,dst,name,db,needed):
    needed=set(needed)
    if name==DBS[0]:
        # Readers test a family head table, then expect its companion tables.
        # Import empty schema companions too, without their global/local rows.
        families=('jst_','purchase_','lifecycle_','normal_','automation_','scheduled_','snapshot_','warehouse_execution_')
        prefixes={prefix for prefix in families if any(t.startswith(prefix) for t in needed)}
        needed.update(t for n,t in src.sql if n==name and any(t.startswith(prefix) for prefix in prefixes))
    for table in needed:
        if (name,table) in dst.sql:continue
        db.execute(src.sql[name,table])
        for typ,n,sql in src.connections[name].execute("SELECT type,name,sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL",(table,)):
            db.execute(sql)
    # Queue/component protocol tables are a structural unit, never source accounts.
    if name in DBS[1:]:
        for table in src.columns:
            n,t=table
            if n!=name or t in needed or (n,t) in dst.sql:continue
            db.execute(src.sql[n,t])
            for _,sql in src.connections[n].execute("SELECT name,sql FROM sqlite_master WHERE tbl_name=? AND type='index' AND sql IS NOT NULL",(t,)):db.execute(sql)
        if name in DBS[1:3]:
            table='ax_workflow_meta' if name==DBS[1] else 'ax_meta'
            if not db.execute('SELECT 1 FROM '+q(table)).fetchone():
                for row in src.connections[name].execute('SELECT * FROM '+q(table)):
                    db.execute('INSERT INTO '+q(table)+' VALUES('+','.join('?' for _ in row)+')',tuple(row))
            return
        for key,value in src.connections[name].execute('SELECT key,value FROM metadata'):
            if key=='generation':
                if not db.execute("SELECT 1 FROM metadata WHERE key='generation'").fetchone():db.execute('INSERT INTO metadata VALUES(?,?)',(key,'0'))
            elif key=='schemaVersion' or key.endswith('Protocol') or key=='collectionIsolation':
                old=db.execute('SELECT value FROM metadata WHERE key=?',(key,)).fetchone()
                if old and old[0]!=value:
                    if key=='schemaVersion' and {old[0],value}<={'1','2'}:
                        if value=='2':db.execute('UPDATE metadata SET value=? WHERE key=?',(value,key))
                    else:raise ValueError('组件协议不兼容，未合并：'+key)
                if not old:db.execute('INSERT INTO metadata VALUES(?,?)',(key,value))
        if (name,'machine_protocol') in src.sql:
            versions=src.connections[name].execute('SELECT version FROM machine_protocol').fetchall()
            local=db.execute('SELECT version FROM machine_protocol').fetchall()
            if local and [tuple(v) for v in local]!=[tuple(v) for v in versions]:raise ValueError('组件命令协议版本不兼容，未合并')
            if not local:
                for v in versions:db.execute('INSERT INTO machine_protocol VALUES(?)',(v[0],))

def _pause(row):
    out=dict(row.values)
    if row.table=='ax_workflows':
        v=json.loads(out['payload']);v['migration_paused']=True
        if v.get('state') in ACTIVE:v['state']='PAUSED';out['state']='PAUSED'
        out['payload']=encoded(v)
    elif row.table=='ax_schedules':
        v=json.loads(out['payload']);v.update(enabled=False,automation_paused=True,final_pending=False);out['payload']=encoded(v)
    elif row.table=='jst_tasks' and out.get('status') in ('pending','prepared'):
        out.update(status='paused',message='合并迁入后暂停，核对原分配批次后继续')
    elif row.table=='scheduled_runs':
        v=json.loads(out['payload']);v['migration_paused']=True
        if out.get('state') in ACTIVE:v['state']='PAUSED';out['state']='PAUSED'
        out['payload']=encoded(v)
    return out

def _pause_machine(row):
    out=dict(row.values);v=json.loads(out['payload'])
    if v.get('state') not in ('QUEUED','RUNNING','PAUSE_REQUESTED'):return out,None
    from stockout_common import now
    previous=v['state'];v.update(state='PAUSED' if previous=='QUEUED' else 'INTERRUPTED',sequence=v['sequence']+1,updated_at=now(),migration_paused=True)
    out.update(state=v['state'],sequence=v['sequence'],payload=encoded(v))
    event={'protocol_version':1,'command_id':v['id'],'sequence':v['sequence'],'state':v['state'],'kind':'MIGRATION_PAUSED','observed_at':v['updated_at'],'detail':{'previous_state':previous,'message':'合并迁入后暂停；保留原请求、任务号与受理事实，不自动派发'}}
    return out,event

def merge(source_root,private_target,expected_report=None,progress=lambda _:None,cancel=None,*,final_root=None):
    source=Path(source_root).resolve();target=Path(private_target).resolve()
    if source==target or source in target.parents or target in source.parents:raise ValueError('合并必须使用两个独立目录')
    src=Graph(source,progress,cancel,file_fallback=True)
    try:
        dst=Graph(target,progress,cancel)
        try:
            report=_inspect(src,dst)
            if expected_report is not None and report['fingerprint']!=expected_report['fingerprint']:raise ValueError('合并预览依据已变化，未写入；请重新核对')
            chosen={r['oid'] for r in report['orders'] if r['status']=='ADD'}
            if not chosen:return report
            rows=[r for r in src.rows if r.owners&chosen];connections={}
            machines={r.values['id']:_pause_machine(r) for r in rows if r.table=='machine_commands'}
            # Release the private target's read snapshots before its writer
            # commits; they otherwise self-block rollback-journal SQLite.
            dst.close()
            try:
                for name in DBS:
                    subset=[r for r in rows if r.db==name]
                    if not subset:continue
                    p=target/name;p.parent.mkdir(parents=True,exist_ok=True);db=sqlite3.connect(p);connections[name]=db;db.execute('PRAGMA synchronous=FULL');db.execute('BEGIN IMMEDIATE')
                    _ensure_schema(src,dst,name,db,{r.table for r in subset})
                    for r in subset:
                        checkpoint(cancel);values=_pause(r)
                        if r.table=='machine_commands':values=machines[r.values['id']][0]
                        elif r.table=='ax_jobs':
                            if values.get('state') in ACTIVE|{'PREPARED'}:values['state']='PAUSED'
                            changed=machines.get(values['id'])
                            if changed and changed[1] is not None:values.update(record=changed[0]['payload'],state=changed[0]['state'],sequence=changed[0]['sequence'])
                        for col in r.blob:
                            raw=(source/r.origin_file).read_bytes() if r.origin_file else src.connections[name].execute('SELECT '+q(col)+' FROM '+q(r.table)+' WHERE rowid=?',(r.rid,)).fetchone()[0]
                            if r.table in ASSETS and hashlib.sha256(raw).hexdigest()!=r.values['sha']:raise ValueError('原件摘要校验失败：'+r.table)
                            if r.table=='snapshot_chunks':
                                import zlib
                                plain=zlib.decompress(raw)
                                if hashlib.sha256(plain).hexdigest()!=r.values['hash'] or len(plain)!=r.values['size']:raise ValueError('快照内容块校验失败')
                            values[col]=raw
                        if r.table in EVENTS:values.pop('id',None)
                        # A duplicate dependency is skipped only after explicit
                        # preflight equality, never a wildcard OR IGNORE.
                        if r.pk and not r.table in EVENTS:
                            old=db.execute('SELECT 1 FROM '+q(r.table)+' WHERE '+' AND '.join(q(c)+' IS ?' for c in r.pk),tuple(values[c] for c in r.pk)).fetchone()
                            if old:continue
                        columns=list(values);db.execute('INSERT INTO '+q(r.table)+' ('+','.join(q(c) for c in columns)+') VALUES ('+','.join('?' for _ in columns)+')',tuple(values[c] for c in columns))
                    if name==DBS[3]:
                        for ident,(values,event) in machines.items():
                            if event:db.execute('INSERT INTO machine_events VALUES(?,?,?)',(ident,event['sequence'],encoded(event)))
                    if db.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('合并副本完整性校验失败：'+name)
                for db in connections.values():db.commit()
            except BaseException:
                for db in connections.values():db.rollback()
                raise
            finally:
                for db in connections.values():db.close()
            # Both DB commits are private; caller discards this copy on any later
            # error and never exposes partially written component/business DBs.
            paths=dict(dst.map);contents={}
            oldmap=target/'迁入原文件映射.json'
            if oldmap.is_file():contents=json.loads(oldmap.read_text('utf-8')).get('contents',{})
            for oid in chosen:
                for old,entry in report['paths'].get(oid,{}).items():
                    checkpoint(cancel);p=target/entry['target'];p.parent.mkdir(parents=True,exist_ok=True)
                    if p.exists():
                        if file_hash(p)!=entry['sha256']:raise ValueError('迁入原件目标有不同内容，未覆盖')
                    else:shutil.copyfile(source/entry['source'],p)
                    if file_hash(p)!=entry['sha256']:raise ValueError('复制迁入原件校验失败')
                    paths[old]=entry['target'];contents.setdefault(entry['sha256'],[])
                    if entry['target'] not in contents[entry['sha256']]:contents[entry['sha256']].append(entry['target'])
            from stockout_common import atomic_json
            atomic_json(oldmap,{'version':1,'paths':paths,'contents':contents})
            for rel,(owners,refs,value) in src.files.items():
                if not owners&chosen:continue
                p=target/rel;p.parent.mkdir(parents=True,exist_ok=True);v=copy.deepcopy(value)
                if 'database' in v:v['database']=str((Path(final_root) if final_root else target)/'workbench.sqlite3')
                if p.exists() and encoded(json.loads(p.read_text('utf-8')))!=encoded(v):raise ValueError('导出恢复记录身份冲突，未覆盖')
                if not p.exists():atomic_json(p,v)
        finally:dst.close()
        # Record original vs paused-copy graph hashes; exact package replay is
        # duplicate only while the imported local order graph remains unchanged.
        after=Graph(target,progress,cancel)
        try:
            if any(after.bad.get(oid) for oid in chosen):raise ValueError('合并副本订单关联校验未通过：'+'；'.join(v for oid in chosen for v in after.bad.get(oid,())))
            target_hashes={oid:after.owner_hash(oid) for oid in chosen}
        finally:after.close()
        with closing(sqlite3.connect(target/'workbench.sqlite3')) as db,db:
            db.execute('CREATE TABLE IF NOT EXISTS order_merge_receipts(oid TEXT PRIMARY KEY,source_digest TEXT NOT NULL,target_digest TEXT NOT NULL)')
            for oid in chosen:db.execute('INSERT INTO order_merge_receipts VALUES(?,?,?)',(oid,src.owner_hash(oid),target_hashes[oid]))
        return report
    finally:src.close()
