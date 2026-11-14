"""Independent warehouse execution over shared, immutable calculation evidence.

``revision`` remains the legacy source revision used by existing documents.
``child_revision`` is the warehouse issue sequence. They are never substituted.
Read-only legacy fallback does not upgrade or rewrite a user's database.
"""
import hashlib
from contextlib import closing, nullcontext
import work_repository as wr
import storage_chunks as chunks
from app_defaults import WAREHOUSES
from runtime_support import checkpoint


def exists(db):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE name='warehouse_execution_heads'").fetchone())


def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS warehouse_execution_heads(oid TEXT,warehouse TEXT,source_revision INTEGER NOT NULL,child_revision INTEGER NOT NULL,state TEXT NOT NULL,epoch INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(oid,warehouse))')
    db.execute('CREATE TABLE IF NOT EXISTS warehouse_execution_revisions(oid TEXT,warehouse TEXT,source_revision INTEGER,child_revision INTEGER NOT NULL,identity_digest TEXT NOT NULL,snapshot_digest TEXT NOT NULL,saved TEXT NOT NULL,payload BLOB NOT NULL,PRIMARY KEY(oid,warehouse,source_revision))')
    db.execute('CREATE TABLE IF NOT EXISTS warehouse_execution_retirements(oid TEXT,warehouse TEXT,operation_id TEXT NOT NULL,PRIMARY KEY(oid,warehouse))')


def has_parent(db, oid):
    return exists(db) and bool(db.execute('SELECT 1 FROM warehouse_execution_heads WHERE oid=?', (oid,)).fetchone())


def state(db, oid, warehouse):
    if not has_parent(db, oid):
        return None
    row = db.execute('SELECT source_revision,child_revision,state,epoch FROM warehouse_execution_heads WHERE oid=? AND warehouse=?', (oid,warehouse)).fetchone()
    return dict(zip(('source_revision','child_revision','state','epoch'),row)) if row else {'state':'not_participating','epoch':0}


def is_active(db, oid, warehouse):
    value = state(db,oid,warehouse)
    return value is None or value['state']=='active'


def project(payload, warehouse):
    import archive_orders as archive
    from result_semantics import summarize
    result=payload['result']
    rows=[row for row in result['rows'] if archive.warehouse(row)==warehouse]
    selected={row['E'] for row in rows}
    scoped={**result,'rows':rows,'summary':summarize(rows)}
    for name in ('must_list_audit','allocation_audit'):
        if name in scoped:scoped[name]=[a for a in result[name] if not a.get('sku') or a['sku'] in selected]
    if result.get('new_products'):
        scoped['new_products']={**result['new_products'],'selection':{k:v for k,v in result['new_products'].get('selection',{}).items() if k in selected}}
    if result.get('monthly_first_fill'):
        audit=result['monthly_first_fill'];scoped['monthly_first_fill']={**audit,'skipped':{k:v for k,v in audit.get('skipped',{}).items() if k in selected}}
        if 'selected' in audit:scoped['monthly_first_fill']['selected']=len(scoped.get('new_products',{}).get('selection',{}))
    if result.get('preissue'):
        state=result['preissue'];edits={k:v for k,v in state.get('edits',{}).items() if k in selected}
        refs={v.get('list_revision') for v in edits.values()}
        risks=[r for r in state.get('risks',[]) if (not r.get('sku') or r['sku'] in selected) and (not r.get('warehouse') or r['warehouse']==warehouse)]
        ids={r['id'] for r in risks}
        scoped['preissue']={**state,'edits':edits,'lists':{k:v for k,v in state.get('lists',{}).items() if k in refs},'risks':risks,'confirmed':[x for x in state.get('confirmed',[]) if x in ids]}
        # Budget/original_summary/source are shared calculation evidence. Do
        # not recalculate their values or imply each child owns the whole pot.
    projected={**payload,'result':scoped}
    if 'item_ids' in projected:
        projected['item_ids']={key:value for key,value in payload['item_ids'].items() if key in selected}
    return projected


def warehouses(payload):
    import normal_exports, archive_orders
    found={archive_orders.warehouse(row) for values in normal_exports.groups(payload['result'],payload.get('new_focus')).values() for row in values}
    return [w for w in (*WAREHOUSES,archive_orders.LEGACY) if w in found]


def _digest(payload):
    import normal_orders
    return normal_orders.fingerprint_owned(payload['result'],payload.get('new_focus'))


def _store_snapshot(db,oid,warehouse,source_revision,child_revision,identity_digest,saved,payload,progress,cancel):
    projected=project(payload,warehouse);digest=_digest(projected)
    encoded=chunks.prepare(projected,chunks.known(db),progress,binding=digest)
    db.execute('INSERT OR IGNORE INTO warehouse_execution_revisions VALUES(?,?,?,?,?,?,?,?)',
        (oid,warehouse,source_revision,child_revision,identity_digest,digest,saved,chunks.put(db,encoded,cancel=cancel)))
    return digest


def ensure_parent(db,oid,progress=lambda _:None,cancel=None):
    """Convert one legacy parent's complete history inside its caller's transaction."""
    if has_parent(db,oid):return
    row=db.execute('SELECT current FROM normal_orders WHERE oid=?',(oid,)).fetchone()
    if row is None:raise wr.Conflict('来源订单已删除，请刷新')
    schema(db);current=row[0];latest={};counts={}
    for revision,digest,saved,blob in db.execute('SELECT revision,digest,saved,payload FROM normal_revisions WHERE oid=? ORDER BY revision',(oid,)):
        checkpoint(cancel);payload=chunks.load(blob,db,binding=digest,progress=progress)
        if _digest(payload)!=digest:raise ValueError('原签发快照校验失败')
        for warehouse in warehouses(payload):
            counts[warehouse]=counts.get(warehouse,0)+1
            _store_snapshot(db,oid,warehouse,revision,counts[warehouse],digest,saved,payload,progress,cancel)
            if revision==current:latest[warehouse]=(revision,counts[warehouse])
    for warehouse,(revision,child_revision) in latest.items():
        db.execute("INSERT INTO warehouse_execution_heads VALUES(?,?,?,?,'active',0)",(oid,warehouse,revision,child_revision))


def publish(db,oid,source_revision,identity_digest,saved,payload,selected,progress=lambda _:None,cancel=None):
    """Publish only the explicitly selected children; leave other heads exact."""
    schema(db)
    if not selected or len(selected)!=len(set(selected)) or any(w not in warehouses(payload) for w in selected):
        raise ValueError('请选择该计算方案中有效且不重复的执行仓')
    result=[]
    for warehouse in selected:
        checkpoint(cancel);old=state(db,oid,warehouse)
        if old and old['state']=='deleted':raise wr.Conflict('该仓单已永久删除；不能由旧草稿或默认勾选恢复，请新建本仓业务任务')
        previous=db.execute('SELECT snapshot_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(oid,warehouse,old['source_revision'])).fetchone() if old and old['state']=='active' else None
        projected=project(payload,warehouse);snapshot_digest=_digest(projected)
        if previous and previous[0]==snapshot_digest:
            continue
        if old and old['state']=='active':
            import jst_orders
            if jst_orders.exists(db) and db.execute('SELECT 1 FROM jst_active WHERE oid=? AND warehouse=?',(oid,warehouse)).fetchone():
                raise wr.Conflict('所选仓已有聚水潭生成记录，请通过该仓原单更正；另一仓不受影响：'+warehouse)
        child_revision=(old.get('child_revision',0) if old else 0)+1
        _store_snapshot(db,oid,warehouse,source_revision,child_revision,identity_digest,saved,payload,progress,cancel)
        db.execute("INSERT INTO warehouse_execution_heads VALUES(?,?,?,?,'active',0) ON CONFLICT(oid,warehouse) DO UPDATE SET source_revision=excluded.source_revision,child_revision=excluded.child_revision,state='active'",(oid,warehouse,source_revision,child_revision))
        result.append(warehouse)
    return result


def needs_publish(db,oid,source_digest,selected):
    if not has_parent(db,oid):return False
    for warehouse in selected:
        current=state(db,oid,warehouse)
        if not current or current['state']!='active':return True
        row=db.execute('SELECT identity_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(oid,warehouse,current['source_revision'])).fetchone()
        if not row or row[0]!=source_digest:return True
    return False


def _head(parent,warehouse,revision,digest,saved,child_revision=None,snapshot_digest=None,epoch=0):
    from warehouse_orders import child_number
    import archive_orders
    return {**{k:v for k,v in parent.items() if k not in ('current','warehouse_heads','payload')},'parent_number':parent['number'],'number':child_number(parent['number'],warehouse) if warehouse in WAREHOUSES else archive_orders.number(parent['number'],warehouse),
        'warehouse':warehouse,'revision':revision,'current_revision':revision,'source_parent_revision':revision,
        'digest':digest,'saved':saved,'child_revision':child_revision or revision,'snapshot_digest':snapshot_digest,'execution_epoch':epoch}


def heads(db,parent_headers=None,*,history=False):
    selected=None if parent_headers is None else {h['oid'] for h in parent_headers}
    parents=[dict(zip(('oid','day','store','number','current'),r)) for r in db.execute("SELECT oid,day,store,number,current FROM normal_orders WHERE kind='normal_model' ORDER BY day DESC,store")]
    result=[]
    for parent in parents:
        oid=parent['oid']
        if selected is not None and oid not in selected:continue
        if has_parent(db,oid):
            for warehouse,current,child,state_value,epoch in db.execute('SELECT warehouse,source_revision,child_revision,state,epoch FROM warehouse_execution_heads WHERE oid=?',(oid,)):
                if state_value!='active':continue
                sql='SELECT source_revision,identity_digest,saved,child_revision,snapshot_digest,payload FROM warehouse_execution_revisions WHERE oid=? AND warehouse=?'+('' if history else ' AND source_revision=?')+' ORDER BY source_revision DESC'
                for revision,digest,saved,seq,snapshot_digest,blob in db.execute(sql,(oid,warehouse) if history else (oid,warehouse,current)):
                    h=_head(parent,warehouse,revision,digest,saved,seq,snapshot_digest,epoch);h['current_revision']=current
                    names=chunks.load(blob,db,paths=[('result','store_name')],binding=snapshot_digest)
                    h['name']=names.get(('result','store_name'),parent['store']);result.append(h)
        else:
            sql='SELECT revision,digest,saved,payload FROM normal_revisions WHERE oid=?'+('' if history else ' AND revision=?')+' ORDER BY revision DESC'
            for revision,digest,saved,blob in db.execute(sql,(oid,) if history else (oid,parent['current'])):
                payload=chunks.load(blob,db,paths=[('result','rows'),('result','store_name'),('new_focus',)],binding=digest)
                value={'result':{'rows':payload[('result','rows')]},'new_focus':payload.get(('new_focus',))}
                for warehouse in warehouses(value):
                    h=_head(parent,warehouse,revision,digest,saved);h['current_revision']=parent['current'];h['name']=payload.get(('result','store_name'),parent['store']);result.append(h)
    rank={w:i for i,w in enumerate(WAREHOUSES)}
    return sorted(result,key=lambda h:(h['day'],h['store'],-rank.get(h['warehouse'],99),h['revision']),reverse=True)


def validate(db,head):
    import deletion_barrier,archive_orders
    try:deletion_barrier.require(db,head['oid'])
    except deletion_barrier.SourceDeleted as exc:raise wr.Conflict(str(exc)) from exc
    warehouse=head.get('warehouse')
    if not warehouse:raise ValueError('请选择具体仓单')
    archive_orders.require_active(db,head['oid'],warehouse)
    if has_parent(db,head['oid']):
        current=state(db,head['oid'],warehouse)
        if not current or current['state']!='active':raise deletion_barrier.SourceDeleted('来源仓单已删除或本轮未参与，旧任务不能恢复')
        row=db.execute('SELECT identity_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(head['oid'],warehouse,head['revision'])).fetchone()
        if not row or row[0]!=head['digest'] or current['source_revision']!=head.get('current_revision',head['revision']):raise wr.Conflict('该仓签发版本已变化，请刷新核对；另一仓不受影响')
        if head.get('execution_epoch',current['epoch'])!=current['epoch']:raise wr.Conflict('该仓数据代次变化，请刷新')
    else:
        row=db.execute('SELECT o.current,r.digest FROM normal_orders o JOIN normal_revisions r ON o.oid=r.oid WHERE o.oid=? AND r.revision=?',(head['oid'],head['revision'])).fetchone()
        if not row or row!=(head.get('current_revision',head['revision']),head['digest']):raise wr.Conflict('签发修订已更新，请重新读取并核对关联')


def get_order(repository,head,view=None,progress=lambda _:None,*,db=None):
    warehouse=head.get('warehouse')
    if warehouse:
        with (closing(repository.work.connect()) if db is None else nullcontext(db)) as db:
            if has_parent(db,head['oid']):
                if not is_active(db,head['oid'],warehouse):raise wr.Conflict('来源仓单已删除或未参与')
                row=db.execute('SELECT identity_digest,snapshot_digest,saved,payload FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(head['oid'],warehouse,head['revision'])).fetchone()
                if not row or row[0]!=head['digest']:raise wr.Conflict('仓单原签发依据已变化，请刷新')
                payload=chunks.load(row[3],db,binding=row[1],progress=progress)
                if _digest(payload)!=row[1]:raise ValueError('仓单业务快照校验失败')
                return {**head,'number':head.get('parent_number') or db.execute('SELECT number FROM normal_orders WHERE oid=?',(head['oid'],)).fetchone()[0],
                        'saved':row[2],'payload':payload,'snapshot_digest':row[1]}
    order=repository.get(head['day'],head['store'],head['revision'],view=view,progress=progress)
    if not order or order['digest']!=head['digest']:raise wr.Conflict('原签发依据已变化，请刷新')
    return {**order,**({'warehouse':warehouse} if warehouse else {})}


def retire(db,oid,warehouse,operation):
    schema(db)
    db.execute('INSERT OR REPLACE INTO warehouse_execution_retirements VALUES(?,?,?)',(oid,warehouse,operation))
    db.execute("UPDATE warehouse_execution_heads SET state='deleted',epoch=epoch+1 WHERE oid=? AND warehouse=?",(oid,warehouse))
    db.execute('DELETE FROM warehouse_execution_revisions WHERE oid=? AND warehouse=?',(oid,warehouse))
