"""Previewed permanent removal of complete store/day orders, in one transaction.

Warehouse children share a signed parent: selection is explicitly expanded to
that parent before confirmation. Shared immutable source snapshots are retained
only while another live order references them. This is not forensic disk wiping.
"""
import copy, hashlib, sqlite3, uuid, re, zlib
from pathlib import Path
from contextlib import closing
from decimal import Decimal
import work_repository as wr
import storage_chunks as chunks
import normal_orders as orders
import archive_orders as archive
from runtime_support import checkpoint,CommitProgress

def tables(db):return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
def digest(value):return hashlib.sha256(wr.dumps(value).encode()).hexdigest()

def snapshot(db,oids,cancel=None):
    available=tables(db);parents=[];versions=[];evidence=[];external=[];related_docs=set()
    for oid in oids:
        checkpoint(cancel)
        row=db.execute('SELECT day,store,number,current FROM normal_orders WHERE oid=?',(oid,)).fetchone()
        if not row:raise wr.Conflict('订单已删除或变化，请刷新')
        day,store,number,current=row
        raw=db.execute('SELECT digest,payload FROM normal_revisions WHERE oid=? AND revision=?',(oid,current)).fetchone()
        payload=chunks.load(raw[1],db,binding=raw[0]);whs=archive.scopes(payload['result']['rows'])
        parents.append(dict(oid=oid,day=day,store=store,number=number,revision=current,digest=raw[0],warehouses=whs,name=payload['result'].get('store_name',store)))
        for table in sorted(available):
            if table.startswith('sqlite_'):continue
            columns=[r[1] for r in db.execute(f'PRAGMA table_info("{table}")')]
            if 'oid' in columns:
                for values in db.execute(f'SELECT * FROM "{table}" WHERE oid=? ORDER BY rowid',(oid,)):
                    evidence.append([table,[hashlib.sha256(v).hexdigest() if isinstance(v,bytes) else v for v in values]])
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
            if linked-set(oids):raise ValueError('存在跨订单共享的旧版采购资料，请一并选择全部关联正常单再删除')
            evidence.extend(['purchase_versions',list(r)] for r in db.execute('SELECT * FROM purchase_versions WHERE docid=? ORDER BY version',(did,)))
    for table in ('jst_tasks','jst_warehouses','jst_runs'):
        if table in available:evidence.extend([table,list(r)] for r in db.execute(f'SELECT * FROM {table} ORDER BY rowid'))
    for day in dict.fromkeys(h['day'] for h in parents):
        evidence.extend(['jobs',list(r)] for r in db.execute('SELECT day,revision,digest FROM jobs WHERE day=?',(day,)))
    ax=[]
    if 'ax' in [r[1] for r in db.execute('PRAGMA database_list')]:
        for id,state,raw in db.execute('SELECT id,state,payload FROM ax.ax_workflows ORDER BY rowid'):
            job=wr.loads(raw);matched=[i for i in job['items'] if (i.get('head') or i.get('basis') or {}).get('oid') in oids]
            if not matched:continue
            if state in ('QUEUED','RUNNING','YIELDED','PAUSED') or any(i['state'] in ('RUNNING','REVIEW','PENDING') for i in matched):raise ValueError('所选订单仍有翱象运行、未完成或待核任务，请先安全结束/核对：'+id)
            if any(i.get('package') for i in matched):external.append(id+' 含原平台采购身份；本地删除不会撤销平台采购')
            ax.append([id,raw]);evidence.append(['ax',id,raw])
    return {'parents':parents,'docs':sorted(related_docs),'external':external,'ax':ax,'token':digest(evidence)}

def connect(path):
    db=sqlite3.connect(path,timeout=.2);db.execute('PRAGMA secure_delete=ON')
    ax=Path(path).parent/'aoxiang/workflows.sqlite3'
    if ax.exists():db.execute('ATTACH DATABASE ? AS ax',(str(ax),))
    for n,name in enumerate(('archive_index.sqlite3','lifecycle_index.sqlite3')):
        cache=Path(path).parent/name
        if cache.exists():db.execute(f'ATTACH DATABASE ? AS cache{n}',(str(cache),))
    return db

def prepare(path,heads,progress=lambda _:None,cancel=None):
    oids=list(dict.fromkeys(h['oid'] for h in heads))
    if not oids:raise ValueError('请勾选要彻底删除的订单')
    progress('读取订单与关联数据，核对删除范围…')
    with closing(connect(path)) as db:
        db.execute('BEGIN');plan=snapshot(db,oids,cancel)
        for h in heads:
            p=next(p for p in plan['parents'] if p['oid']==h['oid'])
            if h['revision']!=p['revision'] or h['digest']!=p['digest']:raise wr.Conflict('原签发已变化，请刷新再删除')
        plan['oids']=oids
    return plan

def strip_work(value,codes):
    p=copy.deepcopy(value)
    for k in ('states','results','store_presets','store_ui','overrides','default_by_store','preissue_drafts','new_product_bases','_calculation_settings'):
        if isinstance(p.get(k),dict):p[k]={s:v for s,v in p[k].items() if s not in codes}
    loaded=p.get('loaded')
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
    return p

def references(node):
    if node[0]=='ref':yield node[1]
    elif node[0]=='dict':
        for _,child in node[1]:yield from references(child)
    elif node[0] in ('array','map'):
        for child in node[1]:yield from references(child)

def collect_shared(db,folder,cancel=None):
    """Reclaim unreferenced blocks and files; validate every live manifest first."""
    live=set();work_ids=set();texts=[];available=tables(db)
    if 'normal_revisions' in available:
        for raw, in db.execute('SELECT payload FROM normal_revisions'):
            checkpoint(cancel);part=chunks.load(raw,db,paths=[('work_revision',)])
            if part.get(('work_revision',)):work_ids.add(part[('work_revision',)])
    for rev, in db.execute('SELECT revision FROM normal_work_revisions').fetchall():
        if rev not in work_ids:db.execute('DELETE FROM normal_work_revisions WHERE revision=?',(rev,))
    for table in ('normal_revisions','normal_work_revisions','jobs'):
        for raw, in db.execute(f'SELECT payload FROM {table}'):
            checkpoint(cancel);m=chunks.manifest(raw)
            if m:live.update(references(m['tree']))
            else:texts.append(wr.dumps(chunks.load(raw,db)))
    if 'snapshot_chunks' in available:
        for h, in db.execute('SELECT hash FROM snapshot_chunks').fetchall():
            if h not in live:db.execute('DELETE FROM snapshot_chunks WHERE hash=?',(h,))
    # Shared raw imports may also be used by saved catalogs, presets, and other stores.
    # Reclaim lifecycle attachments only after all surviving documents are inspected.
    for table in ('lifecycle_versions','purchase_versions','merchant_feedback'):
        if table in available:texts.extend(r[0] for r in db.execute(f'SELECT payload FROM {table}'))
    if 'snapshot_chunks' in available:
        for raw, in db.execute('SELECT data FROM snapshot_chunks'):texts.append(zlib.decompress(raw).decode('utf-8'))
    for table in ('jst_baselines','jst_generations','stocking_lists'):
        if table in available:
            col='data' if table=='stocking_lists' else 'payload'
            texts.extend(r[0] for r in db.execute(f'SELECT {col} FROM {table}'))
    # Maintained catalogs/lists (including explicit version history) are live owners.
    for name in ('warehouse_catalogs','must_sell.json','new_product_focus.json'):
        p=Path(folder)/name
        for f in p.rglob('*.json') if p.is_dir() else ([p] if p.exists() else []):
            texts.append(f.read_text('utf-8'))
    hashes=set()
    for text in texts:hashes.update(re.findall(r'(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])',text))
    for table in ('lifecycle_files','purchase_files','source_files','jst_files'):
        if table in available:
            for sha, in db.execute(f'SELECT sha FROM {table}').fetchall():
                if sha not in hashes:db.execute(f'DELETE FROM {table} WHERE sha=?',(sha,))

def execute(path,plan,ack_external=False,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    if plan['external'] and not ack_external:raise ValueError('请明确核对外部采购/已下单影响；此操作不撤销外部订单')
    with closing(connect(path)) as db:
        import order_lifecycle as lc
        lc.begin(db,cancel)
        try:
            latest=snapshot(db,plan['oids'],cancel)
            if latest!= {k:plan[k] for k in latest}:raise wr.Conflict('关联数据在预览后变化，请重新核对删除范围')
            available=tables(db);oids=set(plan['oids']);progress('正在删除关联单据，提交前仍可取消…')
            # Locked external orders must not release stock in the currently selected baseline.
            if 'jst_active' in available:
                import jst_orders as jst
                db.execute('CREATE TABLE IF NOT EXISTS retired_stock(warehouse TEXT,bid TEXT,sku TEXT,quantity TEXT,PRIMARY KEY(warehouse,bid,sku))')
                for oid in oids:
                    for wh,gid in db.execute('SELECT warehouse,gid FROM jst_active WHERE oid=?',(oid,)).fetchall():
                        st=jst.state(db,wh)
                        if jst.locked(db,{'oid':oid,'warehouse':wh}) and st:
                            base=wr.loads(db.execute('SELECT payload FROM jst_baselines WHERE bid=?',(st[0],)).fetchone()[0])
                            if gid not in base.get('included',[]):
                                for sku,qty in db.execute('SELECT sku,quantity FROM jst_reservations WHERE gid=?',(gid,)):
                                    old=db.execute('SELECT quantity FROM retired_stock WHERE warehouse=? AND bid=? AND sku=?',(wh,st[0],sku)).fetchone()
                                    total=Decimal(qty)+(Decimal(old[0]) if old else 0)
                                    db.execute('INSERT OR REPLACE INTO retired_stock VALUES(?,?,?,?)',(wh,st[0],sku,str(total)))
                        db.execute('UPDATE jst_warehouses SET serial=serial+1 WHERE warehouse=?',(wh,))
                gids=[gid for oid in oids for gid, in db.execute('SELECT gid FROM jst_generations WHERE oid=?',(oid,))]
                for gid in gids:
                    for table in ('jst_headers','jst_reservations'):db.execute(f'DELETE FROM {table} WHERE gid=?',(gid,))
                for runid,raw in db.execute('SELECT runid,payload FROM jst_runs').fetchall():
                    p=wr.loads(raw);gone=[x for x in p['plans'] if x['head']['oid'] in oids]
                    skipped=[x for x in p.get('skipped',[]) if x.get('head',{}).get('oid') not in oids]
                    if not gone and skipped==p.get('skipped',[]):continue
                    p['plans']=[x for x in p['plans'] if x['head']['oid'] not in oids]
                    p['skipped']=skipped
                    for x in gone:db.execute('DELETE FROM jst_steps WHERE operation=?',(x['operation'],))
                    if p['plans'] or p['skipped']:db.execute('UPDATE jst_runs SET payload=?,digest=? WHERE runid=?',(wr.dumps(p),digest(p),runid))
                    else:db.execute('DELETE FROM jst_runs WHERE runid=?',(runid,))
            for task,raw in db.execute('SELECT task,head FROM jst_tasks').fetchall() if 'jst_tasks' in available else []:
                if wr.loads(raw)['oid'] in oids:db.execute('DELETE FROM jst_tasks WHERE task=?',(task,))
            for did in plan['docs']:
                for table in ('purchase_documents','purchase_versions','purchase_events','purchase_link_index'):db.execute(f'DELETE FROM {table} WHERE docid=?',(did,))
            for table in ('lifecycle_versions','lifecycle_events','merchant_feedback','followup_notes','archive_visibility','normal_events','normal_revisions','normal_orders','jst_active','jst_generations','jst_locks','jst_events'):
                if table in available:
                    for oid in oids:db.execute(f'DELETE FROM {table} WHERE oid=?',(oid,))
            for day in dict.fromkeys(h['day'] for h in plan['parents']):
                codes={h['store'] for h in plan['parents'] if h['day']==day}
                for rev,blob in db.execute('SELECT revision,payload FROM normal_work_revisions WHERE day=?',(day,)).fetchall():
                    old=chunks.load(blob,db);p=strip_work(old,codes)
                    if p!=old:db.execute('UPDATE normal_work_revisions SET payload=? WHERE revision=?',(chunks.put(db,chunks.prepare(p,chunks.known(db))),rev))
                raw=db.execute('SELECT payload FROM jobs WHERE day=?',(day,)).fetchone()
                if raw:
                    codes={h['store'] for h in plan['parents'] if h['day']==day};p=strip_work(chunks.load(raw[0],db),codes)
                    if not p.get('states'):db.execute('DELETE FROM jobs WHERE day=?',(day,))
                    else:
                        info,pack,sha=wr.prepare_job(day,p,progress,known=chunks.known(db))
                        db.execute('UPDATE jobs SET revision=?,digest=?,saved=?,summary=?,payload=? WHERE day=?',(uuid.uuid4().hex,sha,wr.now(),wr.dumps(info),chunks.put(db,pack),day))
            for id,raw in plan['ax']:
                p=wr.loads(raw);p['items']=[i for i in p['items'] if (i.get('head') or i.get('basis') or {}).get('oid') not in oids]
                db.execute('DELETE FROM ax.ax_workflow_events WHERE id=?',(id,))
                if not p['items']:db.execute('DELETE FROM ax.ax_workflows WHERE id=?',(id,))
                else:
                    for n,i in enumerate(p['items']):i['position']=n
                    p['sequence']+=1;p['message']='关联订单经确认删除；其余项保留'
                    db.execute('UPDATE ax.ax_workflows SET payload=? WHERE id=?',(wr.dumps(p),id))
            if 'ax' in [r[1] for r in db.execute('PRAGMA database_list')]:
                for id,raw in db.execute('SELECT id,payload FROM ax.ax_schedules').fetchall():
                    if wr.loads(raw).get('basis',{}).get('oid') in oids:db.execute('DELETE FROM ax.ax_schedules WHERE id=?',(id,))
            collect_shared(db,Path(path).parent,cancel)
            for _,name,_ in db.execute('PRAGMA database_list').fetchall():
                if name.startswith('cache'):
                    for table, in db.execute(f"SELECT name FROM {name}.sqlite_master WHERE type='table' AND name IN ('views','lifecycle_projection','jst_result_projection')").fetchall():
                        for oid in oids:db.execute(f'DELETE FROM {name}.{table} WHERE instr(k,?)>0 OR instr(payload,?)>0' if table!='views' else f'DELETE FROM {name}.{table} WHERE instr(k,?)>0 OR instr(projection,?)>0',(wr.dumps(oid),wr.dumps(oid)))
            checkpoint(cancel);progress(CommitProgress(True));db.commit()
        except BaseException:db.rollback();raise
        finally:progress(CommitProgress(False))
    return {'deleted':len(plan['parents']),'orders':[h['number'] for h in plan['parents']]}

def delete_job(path,day,revision,progress=lambda _:None,cancel=None):
    import order_lifecycle as lc
    with closing(connect(path)) as db:
        lc.begin(db,cancel)
        try:
            row=db.execute('SELECT revision FROM jobs WHERE day=?',(day,)).fetchone()
            if not row or row[0]!=revision:raise wr.Conflict('草稿已变化，请重新查看后删除')
            db.execute('DELETE FROM jobs WHERE day=?',(day,))
            if 'normal_orders' in tables(db):collect_shared(db,Path(path).parent,cancel)
            checkpoint(cancel);progress(CommitProgress(True));db.commit()
        except BaseException:db.rollback();raise
        finally:progress(CommitProgress(False))
