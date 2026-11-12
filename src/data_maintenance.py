"""Previewed permanent removal of complete store/day orders, in one transaction.

Warehouse children share a signed parent: selection is explicitly expanded to
that parent before confirmation. Shared immutable source snapshots are retained
only while another live order references them. This is not forensic disk wiping.
"""
import copy, hashlib, sqlite3, uuid, re, zlib, tempfile, time
from pathlib import Path
from contextlib import closing
from decimal import Decimal
import work_repository as wr
import storage_chunks as chunks
import normal_orders as orders
import archive_orders as archive
from runtime_support import checkpoint,CommitProgress
from export_progress import RowProgress
import maintenance_storage as storage

def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def digest(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def snapshot(db,oids,cancel=None,progress=lambda _:None,inspect=False):
    available=tables(db);parents=[];evidence=storage.EvidenceDigest();external=[];related_docs=set();blockers=[];tasks=[]
    for oid in oids:
        checkpoint(cancel)
        row=db.execute('SELECT day,store,number,current FROM normal_orders WHERE oid=?',(oid,)).fetchone()
        if not row:raise wr.Conflict('订单已删除或变化，请刷新')
        day,store,number,current=row
        raw=db.execute('SELECT digest,payload FROM normal_revisions WHERE oid=? AND revision=?',(oid,current)).fetchone()
        with closing(sqlite3.connect(':memory:')) as scratch:
            editor=storage.Editor(db,scratch,progress,cancel);manifest=chunks.require_manifest(raw[1])
            if manifest.get('binding')!=raw[0] or editor.checksum(manifest['tree'])!=(manifest['digest'],manifest['size']):raise ValueError('归档快照校验失败')
            tree=manifest['tree']
            found=set()
            def rows(node):
                for part in node[1] if node[0]=='array' else [node]:
                    checkpoint(cancel)
                    for item in editor.value(part):found.add(archive.warehouse(item))
                return node
            editor.update(tree,('result','rows'),rows)
            whs=[w for w in (*archive.WAREHOUSES,archive.LEGACY) if w in found]
            name=editor.get(tree,('result','store_name'),store)
        parents.append(dict(oid=oid,day=day,store=store,number=number,revision=current,digest=raw[0],warehouses=whs,name=name))
        progress(RowProgress('核对订单范围',len(parents),len(oids)))
        if 'purchase_link_index' in available:
            related_docs.update(r[0] for r in db.execute('SELECT docid FROM purchase_link_index WHERE oid=?',(oid,)))
        if 'jst_locks' in available:
            external += [f'{number} / {w} 已标记下单' for w, in db.execute('SELECT warehouse FROM jst_locks WHERE oid=? AND locked=1',(oid,))]
    if 'purchase_versions' in available:
        for did,raw in db.execute('SELECT docid,payload FROM purchase_versions'):
            if any(link['head']['oid'] in oids for r in wr.loads(raw).get('rows',[]) for link in r.get('links',[])):related_docs.add(did)
    if related_docs:
        for did in related_docs:
            linked={r[0] for r in db.execute('SELECT oid FROM purchase_link_index WHERE docid=?',(did,))}
            for raw, in db.execute('SELECT payload FROM purchase_versions WHERE docid=?',(did,)):
                linked.update(link['head']['oid'] for r in wr.loads(raw).get('rows',[]) for link in r.get('links',[]))
            # Other links are retained and pruned by exact oid at commit.
            evidence.extend(['purchase_versions',list(r)] for r in db.execute('SELECT * FROM purchase_versions WHERE docid=? ORDER BY version',(did,)))
    ax=[]
    if 'ax' in [r[1] for r in db.execute('PRAGMA database_list')]:
        for id,state,raw in db.execute('SELECT id,state,payload FROM ax.ax_workflows ORDER BY rowid'):
            job=wr.loads(raw);matched=[i for i in job['items'] if (i.get('head') or i.get('basis') or {}).get('oid') in oids]
            if not matched:continue
            from aoxiang_workflow_state import deletion_issue,can_end_local
            for i in matched:
                issue=deletion_issue({**job,'state':state},i)
                h=i.get('head') or i.get('basis') or {}
                task={'id':id,'key':i['key'],'sequence':job['sequence'],'order':h.get('number') or i['key'],
                      'name':h.get('name') or h.get('store'),'warehouse':h.get('warehouse'),
                      'action':issue[0] if issue else '可随订单清理','reason':issue[1] if issue else '任务已停止；仅清理所选订单的关联记录',
                      'can_end':can_end_local(job,i),'oid':h.get('oid')}
                tasks.append(task)
                # Platform state is informational, never a deletion veto.
            if any(i.get('package') or i.get('purchase_id') or i.get('order_id') or (i.get('result') or {}).get('binding') for i in matched):external.append(id+' 含原平台采购身份；本地删除不会撤销平台采购')
            ax.append([id,raw]);evidence.append(['ax',id,state,raw])
        for id,raw in db.execute('SELECT id,payload FROM ax.ax_schedules ORDER BY rowid'):
            if wr.loads(raw).get('basis',{}).get('oid') in oids:evidence.append(['ax_schedule',id,raw])
    return {'parents':parents,'docs':sorted(related_docs),'external':external,'ax':ax,'token':digest(parents),'blockers':blockers,'tasks':tasks}

def connect(path):
    db=sqlite3.connect(path,timeout=.2);db.execute('PRAGMA secure_delete=ON')
    ax=Path(path).parent/'aoxiang/workflows.sqlite3'
    if ax.exists():db.execute('ATTACH DATABASE ? AS ax',(str(ax),))
    for n,name in enumerate(('archive_index.sqlite3','lifecycle_index.sqlite3','payment_matching/index-v1.sqlite3')):
        cache=Path(path).parent/name
        if cache.exists():db.execute(f'ATTACH DATABASE ? AS cache{n}',(str(cache),))
    return db

def require_atomic_journals(db,*,configure=True):
    """ATTACH crash atomicity requires disk main + rollback journals, not WAL."""
    from work_repository import Conflict
    modes={}
    for _,name,file in db.execute('PRAGMA database_list'):
        if name=='temp':continue
        mode=db.execute(f'PRAGMA {name}.journal_mode').fetchone()[0].lower();modes[name]=mode
        if not file or mode not in {'delete','truncate','persist'}:
            raise Conflict('尚未删除：跨库原子删除要求磁盘回滚日志；当前 '+name+'='+mode+'。请先在数据库维护中核对日志模式，不能在WAL下承诺断电原子性')
    if configure:
        for name in modes:
            db.execute(f'PRAGMA {name}.synchronous=FULL');db.execute(f'PRAGMA {name}.secure_delete=ON')
    return modes

def prune_purchases(db,docs,oids,cancel):
    import purchases
    for did in docs:
        retained=[];survives=False
        for version,old_digest,raw in db.execute('SELECT version,digest,payload FROM purchase_versions WHERE docid=? ORDER BY version',(did,)):
            checkpoint(cancel);p=wr.loads(raw)
            if purchases.fingerprint(p)!=old_digest:raise ValueError('采购快照校验失败，未删除')
            for row in p['rows']:
                row['links']=[l for l in row.get('links',[]) if l['head']['oid'] not in oids]
                survives=survives or bool(row['links'])
            retained.append((version,purchases.fingerprint(p),wr.dumps(p),p))
        if not survives:
            for t in ('purchase_link_index','purchase_events','purchase_versions','purchase_documents'):db.execute(f'DELETE FROM {t} WHERE docid=?',(did,))
            continue
        for version,h,raw,_ in retained:db.execute('UPDATE purchase_versions SET digest=?,payload=? WHERE docid=? AND version=?',(h,raw,did,version))
        cur=db.execute('SELECT current FROM purchase_documents WHERE docid=?',(did,)).fetchone()[0]
        current=next(p for version,_,_,p in retained if version==cur)
        db.execute('DELETE FROM purchase_link_index WHERE docid=?',(did,))
        for row in current['rows']:
            for link in row['links']:
                h=link['head'];db.execute('INSERT INTO purchase_link_index VALUES(?,?,?,?,?,?,?)',(did,row['line_id'],h['oid'],h['revision'],h['warehouse'],row['sku'],link['quantity']))

def scrub_workflow_events(db,id,gone):
    identities={i.get('item_id') for i in gone if i.get('item_id')}
    terms={v for i in gone for v in [i.get('key'),*((i.get('head') or i.get('basis') or {}).get(k) for k in ('oid','number','name','store'))] if isinstance(v,str) and v}
    for sequence,raw in db.execute('SELECT sequence,payload FROM ax.ax_workflow_events WHERE id=?',(id,)).fetchall():
        e=wr.loads(raw)
        if e.get('item_id') in identities:db.execute('DELETE FROM ax.ax_workflow_events WHERE id=? AND sequence=?',(id,sequence))
        elif any(t in str(e.get('message','')) for t in terms):
            e['message']='已清理所选订单内容；保留共享批次技术事件'
            db.execute('UPDATE ax.ax_workflow_events SET payload=? WHERE id=? AND sequence=?',(wr.dumps(e),id,sequence))

def prepare(path,heads,progress=lambda _:None,cancel=None,*,inspect=False):
    oids=list(dict.fromkeys(h['oid'] for h in heads))
    if not oids:raise ValueError('请勾选要彻底删除的订单')
    progress('读取订单与关联数据，核对删除范围…')
    with closing(connect(path)) as db:
        db.execute('BEGIN');chunks.require_current(db,cancel);plan=snapshot(db,oids,cancel,progress,inspect=inspect)
        for h in heads:
            p=next(p for p in plan['parents'] if p['oid']==h['oid'])
            if h['revision']!=p['revision'] or h['digest']!=p['digest']:raise wr.Conflict('原签发已变化，请刷新再删除')
        plan['oids']=oids
    return plan

def inspect_deletion(path,heads,progress=lambda _:None,cancel=None):
    return prepare(path,heads,progress,cancel,inspect=True)

def strip_work(value,codes,*,owned=False,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    p=dict(value) if owned else copy.deepcopy(value)
    for k in ('deletion_generations','states','results','store_presets','store_ui','overrides','default_by_store','preissue_drafts','new_product_bases','_calculation_settings'):
        if isinstance(p.get(k),dict):p[k]={s:v for s,v in p[k].items() if s not in codes}
    loaded=p.get('loaded')
    if loaded and owned:
        loaded=dict(loaded);p['loaded']=loaded
        if loaded.get('warehouse_rows') is not None:loaded['warehouse_rows']=dict(loaded['warehouse_rows'])
        if loaded.get('intake'):loaded['intake']=dict(loaded['intake'])
    if loaded:
        for k in ('stores','blocked'):
            if k in loaded:loaded[k]={s:v for s,v in loaded[k].items() if s not in codes}
        loaded['normalized']=[r for r in loaded.get('normalized',[]) if r[0].get('B') not in codes]
        for w,rows in loaded.get('warehouse_rows',{}).items():loaded['warehouse_rows'][w]=[r for r in rows if r[0].get('B') not in codes]
        data=loaded.get('intake') or {}
        data['stores']={s:v for s,v in data.get('stores',{}).items() if s not in codes}
        data['conflicts']=[c for c in data.get('conflicts',[]) if c['store'] not in codes]
        import engine
        field=(loaded.get('config') or engine.config())['sales_fields']['store_code']
        data['selected']=[r for r in data.get('selected',[]) if engine.adapter.identifier(r.get('values',{}).get(field))[0] not in codes]
    # Export logs can contain several stores and original paths: remove only affected entries.
    def affected(e):
        if isinstance(e,dict):return e.get('store') in codes or any(affected(v) for v in e.values())
        if isinstance(e,list):return any(affected(v) for v in e)
        return False
    p['export_log']=[e for e in p.get('export_log',[]) if not affected(e)]
    p['pending_copy']=None
    checkpoint(cancel)
    return p

def strip_current(value,codes,progress=lambda _:None,cancel=None):
    progress('仅移除受影响门店，保留其他门店参数与结果…')
    return strip_work(value,codes,owned=True,progress=progress,cancel=cancel)

def references(node):
    if node[0]=='ref':yield node[1]
    elif node[0]=='dict':
        for _,child in node[1]:yield from references(child)
    elif node[0] in ('array','map'):
        for child in node[1]:yield from references(child)

def collect_shared(db,folder,cancel=None,progress=lambda _:None,*,verified=None):
    """Reclaim unreferenced blocks and files; validate every live manifest first."""
    chunks.require_current(db,cancel)
    live=set();work_ids=set();hashes=set();available=tables(db);storage.cache_schema(db)
    progress('核对存活订单及共享资料引用…')
    if 'normal_revisions' in available:
        for raw, in db.execute('SELECT payload FROM normal_revisions'):
            checkpoint(cancel)
            revision=chunks.load(raw,db,paths=[('work_revision',)],progress=lambda s:checkpoint(cancel)).get(('work_revision',))
            if revision:work_ids.add(revision)
    for rev, in db.execute('SELECT revision FROM normal_work_revisions').fetchall():
        if rev not in work_ids:db.execute('DELETE FROM normal_work_revisions WHERE revision=?',(rev,))
    for table in ('normal_revisions','normal_work_revisions','jobs','scheduled_values'):
        if table not in available:continue
        for raw, in db.execute(f'SELECT payload FROM {table}'):
            checkpoint(cancel);m=chunks.require_manifest(raw)
            live.update(references(m['tree']))
    if 'snapshot_chunks' in available:
        for h, in db.execute('SELECT hash FROM snapshot_chunks').fetchall():
            checkpoint(cancel)
            if h not in live:db.execute('DELETE FROM snapshot_chunks WHERE hash=?',(h,))
    # Shared raw imports may also be used by saved catalogs, presets, and other stores.
    # Reclaim lifecycle attachments only after all surviving documents are inspected.
    for table in ('lifecycle_versions','purchase_versions','merchant_feedback'):
        if table in available:
            for raw, in db.execute(f'SELECT payload FROM {table}'):hashes.update(storage.hashes(raw,cancel))
    if 'snapshot_chunks' in available:
        storage.cache_schema(db);total=db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0]
        for i,(h,) in enumerate(db.execute('SELECT hash FROM snapshot_chunks'),1):
            checkpoint(cancel)
            cached=verified.execute('SELECT refs FROM verified_chunks WHERE hash=?',(h,)).fetchone() if verified is not None else None
            if cached is not None:hashes.update(wr.loads(cached[0]))
            else:
                size,raw=db.execute('SELECT size,data FROM snapshot_chunks WHERE hash=?',(h,)).fetchone()
                hashes.update(storage.references_for(db,h,size,raw,cancel));del raw
            if i%32==0 or i==total:progress(RowProgress('核对共享附件引用',i,total))
        db.execute('DELETE FROM maintenance_chunk_refs WHERE hash NOT IN (SELECT hash FROM snapshot_chunks)')
    for table in ('jst_baselines','jst_generations','stocking_lists'):
        if table in available:
            col='data' if table=='stocking_lists' else 'payload'
            for raw, in db.execute(f'SELECT {col} FROM {table}'):hashes.update(storage.hashes(raw,cancel))
    # Maintained catalogs/lists (including explicit version history) are live owners.
    for name in ('warehouse_catalogs','must_sell.json','new_product_focus.json'):
        p=Path(folder)/name
        for f in p.rglob('*.json') if p.is_dir() else ([p] if p.exists() else []):
            checkpoint(cancel);hashes.update(storage.hashes(f.read_bytes(),cancel))
    for table in ('lifecycle_files','purchase_files','source_files','jst_files'):
        if table in available:
            for sha, in db.execute(f'SELECT sha FROM {table}').fetchall():
                checkpoint(cancel)
                if sha not in hashes:db.execute(f'DELETE FROM {table} WHERE sha=?',(sha,))
    checkpoint(cancel)


def prepare_rewrites(db,plan,scratch,progress,cancel):
    """Prepare outside the writer lock. Recheck exact source blobs before use."""
    chunks.require_current(db,cancel)
    editor=storage.Editor(db,scratch,progress,cancel);prepared=[]
    records=[]
    for day in dict.fromkeys(h['day'] for h in plan['parents']):
        codes={h['store'] for h in plan['parents'] if h['day']==day}
        for rev, in db.execute('SELECT revision FROM normal_work_revisions WHERE day=?',(day,)):
            records.append(('normal_work_revisions',rev,day,codes))
        for _, in db.execute('SELECT day FROM jobs WHERE day=?',(day,)):
            records.append(('jobs',day,day,codes))
    rewritten={}
    for i,(table,key,day,codes) in enumerate(records,1):
        col='day' if table=='jobs' else 'revision'
        row=db.execute(f'SELECT payload'+(',digest' if table=='jobs' else '')+f' FROM {table} WHERE {col}=?',(key,)).fetchone()
        if row is None:raise wr.Conflict('当天共享作业在准备时变化，请重新核对删除范围')
        blob=row[0];expected=row[1] if table=='jobs' else None
        checkpoint(cancel);progress(RowProgress('准备当天作业清理',i-1,len(records)))
        source_hash=hashlib.sha256(blob).digest()
        # The current draft and signed work revisions can hold identical bytes.
        # Reuse only within the same day (hence the same selected stores).
        if (day,source_hash) not in rewritten:
            pack=editor.rewrite(blob,codes,expected)
            rewritten[day,source_hash]=pack,editor.original_digest
        pack,original_digest=rewritten[day,source_hash]
        if expected and expected!=original_digest:raise ValueError('作业快照与索引不一致')
        info={'original_digest':original_digest}
        if table=='jobs':
            states=editor.get(pack.tree,('states',),{})
            if states:
                from datetime import date
                if date.fromisoformat(day).isoformat()!=day:raise ValueError('日期须为YYYY-MM-DD')
                if editor.get(pack.tree,('schema',))!=1 or editor.get(pack.tree,('day',))!=day:raise ValueError('作业快照与日期不一致')
                info=editor.job_summary(pack.tree)
            else:info={'stores':{}}
        prepared.append((table,key,source_hash,pack,info))
        del blob,row
        progress(RowProgress('准备当天作业清理',i,len(records)))
    # Read existing receipts in place; stage only changed receipts. Every source
    # block is still verified, and no source cache writes precede the write lock.
    cache_source=db if 'maintenance_chunk_refs' in tables(db) else None
    scratch.execute('CREATE TABLE verified_chunks(hash TEXT PRIMARY KEY,refs TEXT NOT NULL)')
    if 'snapshot_chunks' in tables(db):
        total=db.execute('SELECT COUNT(*) FROM snapshot_chunks').fetchone()[0]
        for i,(h,size,data) in enumerate(db.execute('SELECT hash,size,data FROM snapshot_chunks'),1):
            refs=storage.references_for(scratch,h,size,data,cancel,cache_source=cache_source)
            scratch.execute('INSERT INTO verified_chunks VALUES(?,?)',(h,wr.dumps(refs)))
            if i%32==0 or i==total:progress(RowProgress('准备共享附件引用（首次需核对历史块）',i,total))
    # The former compatibility pass also validated every current manifest,
    # including orphan work and old revisions about to be removed. Retain that
    # integrity boundary independently of retired-format support.
    for table in ('normal_revisions','normal_work_revisions','jobs'):
        for blob, in db.execute(f'SELECT payload FROM {table}'):
            checkpoint(cancel);chunks.require_manifest(blob)
    scratch.commit();return prepared


def apply_rewrites(db,scratch,prepared,days,progress,cancel):
    chunks.schema(db)
    expected={(table,key):old for table,key,old,_,_ in prepared};current={}
    for day in days:
        current.update({('normal_work_revisions',rev):hashlib.sha256(blob).digest() for rev,blob in db.execute('SELECT revision,payload FROM normal_work_revisions WHERE day=?',(day,))})
        current.update({('jobs',day):hashlib.sha256(blob).digest() for blob, in db.execute('SELECT payload FROM jobs WHERE day=?',(day,))})
    if current!=expected:raise wr.Conflict('当天共享作业在准备后变化，请重新核对删除范围')
    # This exact set/content check already runs under BEGIN IMMEDIATE. There is
    # no intervening source mutation: do not read every payload a second time.
    for h,size,raw in scratch.execute('SELECT hash,size,data FROM snapshot_chunks'):
        checkpoint(cancel);db.execute('INSERT OR IGNORE INTO snapshot_chunks VALUES(?,?,?)',(h,size,raw))
    for table,key,old,pack,info in prepared:
        checkpoint(cancel)
        if table=='jobs':
            if not info['stores']:db.execute('DELETE FROM jobs WHERE day=?',(key,))
            else:db.execute('UPDATE jobs SET revision=?,digest=?,saved=?,summary=?,payload=? WHERE day=?',(uuid.uuid4().hex,pack.digest,wr.now(),wr.dumps(info),chunks.put(db,pack,cancel=cancel),key))
        else:
            # Preserve unchanged records byte-for-byte.
            if pack.digest!=info['original_digest']:db.execute('UPDATE normal_work_revisions SET payload=? WHERE revision=?',(chunks.put(db,pack,cancel=cancel),key))
    storage.publish_cache(db,scratch)
    progress('当天作业清理已写入事务，继续核对共享引用…')


def clear_indexes(db,oids,progress,cancel):
    """One streaming pass per index; match exactly the former substring rule."""
    needles=[wr.dumps(oid) for oid in oids]
    for _,name,_ in db.execute('PRAGMA database_list').fetchall():
        if not name.startswith('cache'):continue
        for table, in db.execute(f"SELECT name FROM {name}.sqlite_master WHERE type='table' AND name IN ('views','lifecycle_projection','jst_result_projection')").fetchall():
            col='projection' if table=='views' else 'payload';total=db.execute(f'SELECT COUNT(*) FROM {name}.{table}').fetchone()[0]
            # Only keys are read for the common case. Exceptional cross-order payload
            # references retain the old substring semantics, but scan each row once.
            for i,(key,) in enumerate(db.execute(f'SELECT k FROM {name}.{table}').fetchall(),1):
                checkpoint(cancel);remove=any(n in key for n in needles)
                if not remove:
                    rowid,kind=db.execute(f'SELECT rowid,typeof({col}) FROM {name}.{table} WHERE k=?',(key,)).fetchone()
                    if kind=='text':
                        with db.blobopen(table,col,rowid,readonly=True,name=name) as raw:remove=storage.contains_text(raw,needles,cancel)
                    else:
                        # Retain the original type-error behavior for malformed caches.
                        raw=db.execute(f'SELECT {col} FROM {name}.{table} WHERE k=?',(key,)).fetchone()[0]
                        remove=any(n in raw for n in needles);del raw
                if remove:db.execute(f'DELETE FROM {name}.{table} WHERE k=?',(key,))
                if i%16==0 or i==total:progress(RowProgress('清理订单索引',i,total))

def execute(path,plan,ack_external=False,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if plan['external'] and not ack_external:raise ValueError('请明确核对外部采购/已下单影响；此操作不撤销外部订单')
    with tempfile.TemporaryDirectory(prefix='yt-order-delete-') as temporary,closing(connect(path)) as db,closing(sqlite3.connect(Path(temporary)/'prepared.sqlite3')) as scratch:
        journal_modes=require_atomic_journals(db)
        initial_version=db.execute('PRAGMA main.data_version').fetchone()[0]
        prepared=prepare_rewrites(db,plan,scratch,progress,cancel)
        progress('准备完成，正在取得删除写锁…');started=time.monotonic()
        while True:
            checkpoint(cancel)
            try:db.execute('BEGIN IMMEDIATE');break
            except sqlite3.OperationalError as exc:
                if 'locked' not in str(exc).lower() or time.monotonic()-started>10:raise
                progress(f'等待其他操作释放数据库：{time.monotonic()-started:.1f} 秒（可取消）')
        # Any concurrent main-DB commit invalidates this operation-local verified
        # reference set. The fallback rescans exact bytes under the writer lock.
        verified=scratch if db.execute('PRAGMA main.data_version').fetchone()[0]==initial_version else None
        if verified is not None and not scratch.in_transaction:scratch.execute('BEGIN')
        db.set_progress_handler(lambda:int(cancel is not None and cancel.is_set()),10000)
        try:
            require_atomic_journals(db,configure=False)
            chunks.require_current(db,cancel)
            latest=snapshot(db,plan['oids'],cancel,progress)
            if latest['parents']!=plan['parents'] or latest['token']!=plan['token']:raise wr.Conflict('订单修订或确认范围已变化，请重新核对删除范围')
            if verified is None:
                # Rebase drafts on the latest committed shared payload, not the preview.
                for name, in scratch.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():scratch.execute('DROP TABLE "'+name+'"')
                scratch.commit();prepared=prepare_rewrites(db,latest,scratch,progress,cancel)
            import deletion_barrier
            operation_id=deletion_barrier.mark(db,latest['parents'])
            available=tables(db);oids=set(plan['oids']);progress('正在删除关联单据，提交前仍可取消…')
            # Payment recognition stores exact order bindings separately from
            # lifecycle versions. Store aliases are shared configuration.
            if 'lifecycle_mappings' in available:
                for stage,name,raw in db.execute('SELECT stage,name,payload FROM lifecycle_mappings').fetchall():
                    if wr.loads(raw).get('head',{}).get('oid') in oids:db.execute('DELETE FROM lifecycle_mappings WHERE stage=? AND name=?',(stage,name))
            aliases={r[1] for r in db.execute('PRAGMA database_list')}
            if 'cache2' in aliases and db.execute("SELECT 1 FROM cache2.sqlite_master WHERE name='candidates'").fetchone():
                for oid in oids:
                    keys=[digest(['signed',oid,rev,h]) for rev,h in db.execute('SELECT revision,digest FROM normal_revisions WHERE oid=?',(oid,))]
                    if 'jst_generations' in available:keys.extend(digest(['generation-v2',gid,h]) for gid,h in db.execute('SELECT gid,digest FROM jst_generations WHERE oid=?',(oid,)))
                    for key in keys:db.execute('DELETE FROM cache2.candidates WHERE k=?',(key,))
            # Locked external orders must not release stock in the currently selected baseline.
            if 'jst_active' in available:
                import jst_orders as jst
                import jst_batches
                db.execute('CREATE TABLE IF NOT EXISTS retired_stock(warehouse TEXT,bid TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(warehouse,bid,sku))')
                for oid in oids:
                    for wh,gid in db.execute('SELECT warehouse,gid FROM jst_active WHERE oid=?',(oid,)).fetchall():
                        batch_id=jst_batches.generation_batch(db,gid)
                        st=None if batch_id else jst.state(db,wh)
                        if jst.locked(db,{'oid':oid,'warehouse':wh}) and st:
                            base=wr.loads(db.execute('SELECT payload FROM jst_baselines WHERE bid=?',(st[0],)).fetchone()[0])
                            if gid not in base.get('included',[]):
                                for sku,qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gid,)):
                                    old=db.execute('SELECT quantity FROM retired_stock WHERE warehouse=? AND bid=? AND sku=?',(wh,st[0],sku)).fetchone()
                                    total=Decimal(qty)+(Decimal(old[0]) if old else 0)
                                    db.execute('INSERT OR REPLACE INTO retired_stock VALUES(?,?,?,?)',(wh,st[0],sku,str(total)))
                        # Deletion cannot reduce available capacity; preserve other orders' previews.
                    jst_batches.deleting_order(db,oid)
                gids=[gid for oid in oids for gid, in db.execute('SELECT gid FROM jst_generations WHERE oid=?',(oid,))]
                for gid in gids:
                    for table in ('jst_headers','jst_reservations'):db.execute(f'DELETE FROM {table} WHERE gid=?',(gid,))
                for runid, in db.execute('SELECT runid FROM jst_runs').fetchall():
                    raw=db.execute('SELECT payload FROM jst_runs WHERE runid=?',(runid,)).fetchone()[0]
                    p=wr.loads(raw);gone=[x for x in p['plans'] if x['head']['oid'] in oids]
                    skipped=[x for x in p.get('skipped',[]) if x.get('head',{}).get('oid') not in oids]
                    if not gone and skipped==p.get('skipped',[]):continue
                    p['plans']=[x for x in p['plans'] if x['head']['oid'] not in oids]
                    p['skipped']=skipped
                    for x in gone:
                        w=x['head']['warehouse'];step=db.execute('SELECT runid FROM jst_steps WHERE operation=?',(x['operation'],)).fetchone()
                        if step:db.execute('INSERT OR IGNORE INTO deleted_jst_steps VALUES(?,?,?,?)',(x['operation'],step[0],w,p['states'][w][0]))
                        db.execute('DELETE FROM jst_steps WHERE operation=?',(x['operation'],))
                    if p['plans'] or p['skipped']:db.execute('UPDATE jst_runs SET payload=?,digest=? WHERE runid=?',(wr.dumps(p),digest(p),runid))
                    else:db.execute('DELETE FROM jst_runs WHERE runid=?',(runid,))
            for task,raw in db.execute('SELECT task,head FROM jst_tasks').fetchall() if 'jst_tasks' in available else []:
                if wr.loads(raw)['oid'] in oids:db.execute('DELETE FROM jst_tasks WHERE task=?',(task,))
            prune_purchases(db,latest['docs'],oids,cancel)
            if 'automation_tasks' in available:
                for task,raw in db.execute('SELECT id,payload FROM automation_tasks').fetchall():
                    if wr.loads(raw).get('order_ref') in oids:
                        if 'automation_events' in available:db.execute('DELETE FROM automation_events WHERE task=?',(task,))
                        db.execute('DELETE FROM automation_tasks WHERE id=?',(task,))
            for table in ('lifecycle_routes','lifecycle_versions','lifecycle_events','merchant_feedback','followup_notes','archive_visibility','normal_events','normal_revisions','normal_orders','jst_active','jst_generations','jst_locks','jst_events'):
                if table in available:
                    for oid in oids:db.execute(f'DELETE FROM {table} WHERE oid=?',(oid,))
            apply_rewrites(db,scratch,prepared,dict.fromkeys(h['day'] for h in plan['parents']),progress,cancel)
            for id,raw in latest['ax']:
                p=wr.loads(raw);gone=[i for i in p['items'] if (i.get('head') or i.get('basis') or {}).get('oid') in oids];p['items']=[i for i in p['items'] if i not in gone]
                if not p['items']:
                    db.execute('DELETE FROM ax.ax_workflow_events WHERE id=?',(id,))
                    db.execute('DELETE FROM ax.ax_workflows WHERE id=?',(id,))
                else:
                    from aoxiang_workflow_state import reconcile
                    for n,i in enumerate(p['items']):i['position']=n
                    if 'dispatch_keys' in p:p['dispatch_keys']=[k for k in p['dispatch_keys'] if k in {i['key'] for i in p['items']}]
                    scrub_workflow_events(db,id,gone)
                    p=reconcile(p);p['sequence']+=1;p['updated_at']=time.time()
                    db.execute('UPDATE ax.ax_workflows SET state=?,payload=? WHERE id=?',(p['state'],wr.dumps(p),id))
                    db.execute('INSERT INTO ax.ax_workflow_events VALUES(?,?,?)',(id,p['sequence'],wr.dumps({'at':p['updated_at'],'event':'REMOVED_SELECTED_ORDER_ITEMS','state':p['state'],'message':p['message']})))
            if 'ax' in [r[1] for r in db.execute('PRAGMA database_list')]:
                for id,raw in db.execute('SELECT id,payload FROM ax.ax_schedules').fetchall():
                    if wr.loads(raw).get('basis',{}).get('oid') in oids:db.execute('DELETE FROM ax.ax_schedules WHERE id=?',(id,))
            collect_shared(db,Path(path).parent,cancel,progress,verified=verified)
            clear_indexes(db,oids,progress,cancel)
            checkpoint(cancel);progress(CommitProgress(True));db.set_progress_handler(None,0);db.commit()
        except BaseException:
            db.set_progress_handler(None,0);db.rollback();checkpoint(cancel);raise
        finally:progress(CommitProgress(False))
    return {'deleted':len(plan['parents']),'orders':[h['number'] for h in plan['parents']],'operation_id':operation_id,'component_cleanup_pending':True,'journal_modes':journal_modes}

def delete_job(path,day,revision,progress=lambda _:None,cancel=None):
    import order_lifecycle as lc
    with closing(connect(path)) as db:
        lc.begin(db,cancel)
        try:
            chunks.require_current(db,cancel)
            row=db.execute('SELECT revision FROM jobs WHERE day=?',(day,)).fetchone()
            if not row or row[0]!=revision:raise wr.Conflict('草稿已变化，请重新查看后删除')
            db.execute('DELETE FROM jobs WHERE day=?',(day,))
            if 'normal_orders' in tables(db):collect_shared(db,Path(path).parent,cancel,progress)
            checkpoint(cancel);progress(CommitProgress(True));db.commit()
        except BaseException:db.rollback();raise
        finally:progress(CommitProgress(False))
