"""Technical deletion fences. No retained product/payment/receipt payloads."""
import json,sqlite3,uuid
from contextlib import closing
from pathlib import Path

class SourceDeleted(ValueError):
    code='SOURCE_ORDER_DELETED'

def has(db,table):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone())

def schema(db):
    db.execute('CREATE TABLE IF NOT EXISTS deleted_order_tokens(oid TEXT PRIMARY KEY,operation_id TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS deletion_generations(day TEXT,store TEXT,generation INTEGER NOT NULL,PRIMARY KEY(day,store))')
    db.execute('CREATE TABLE IF NOT EXISTS deletion_component_outbox(operation_id TEXT PRIMARY KEY,oids TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL DEFAULT 0)')
    if 'generation' not in {r[1] for r in db.execute('PRAGMA table_info(deletion_component_outbox)')}:db.execute('ALTER TABLE deletion_component_outbox ADD COLUMN generation INTEGER NOT NULL DEFAULT 0')
    db.execute('CREATE TABLE IF NOT EXISTS deleted_jst_steps(operation TEXT PRIMARY KEY,runid TEXT NOT NULL,warehouse TEXT NOT NULL,bid TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS order_component_packages(package_id TEXT PRIMARY KEY,oid TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS deleted_warehouse_tokens(oid TEXT,warehouse TEXT,operation_id TEXT NOT NULL,PRIMARY KEY(oid,warehouse))')
    db.execute('CREATE TABLE IF NOT EXISTS warehouse_deletion_generations(day TEXT,store TEXT,warehouse TEXT,generation INTEGER NOT NULL,PRIMARY KEY(day,store,warehouse))')
    if 'scopes' not in {r[1] for r in db.execute('PRAGMA table_info(deletion_component_outbox)')}:db.execute("ALTER TABLE deletion_component_outbox ADD COLUMN scopes TEXT NOT NULL DEFAULT '[]'")
    db.execute('CREATE TABLE IF NOT EXISTS warehouse_component_packages(package_id TEXT PRIMARY KEY,oid TEXT NOT NULL,warehouse TEXT NOT NULL)')

def active(db,oid,warehouse=None):
    if has(db,'deleted_order_tokens') and db.execute('SELECT 1 FROM deleted_order_tokens WHERE oid=?',(oid,)).fetchone():return False
    if warehouse:
        if has(db,'deleted_warehouse_tokens') and db.execute('SELECT 1 FROM deleted_warehouse_tokens WHERE oid=? AND warehouse=?',(oid,warehouse)).fetchone():return False
        import warehouse_execution
        if not warehouse_execution.is_active(db,oid,warehouse):return False
    return has(db,'normal_orders') and bool(db.execute('SELECT 1 FROM normal_orders WHERE oid=?',(oid,)).fetchone())

def require(db,oid,warehouse=None):
    if not active(db,oid,warehouse):raise SourceDeleted('来源仓单已删除或本轮未参与；该任务停止，不恢复订单或关联记录')

def epochs(db,day,codes):
    if not has(db,'deletion_generations'):return {s:0 for s in codes}
    return {s:(db.execute('SELECT generation FROM deletion_generations WHERE day=? AND store=?',(day,s)).fetchone() or [0])[0] for s in codes}

def read_epochs(path,day,codes):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:return epochs(db,day,codes)

def basis(payload):
    codes=set()
    for k in ('states','results','store_presets','store_ui','overrides','default_by_store','preissue_drafts','new_product_bases','_calculation_settings'):
        if isinstance(payload.get(k),dict):codes.update(payload[k])
    for k in ('stores','blocked'):codes.update((payload.get('loaded') or {}).get(k,{}) or {})
    scopes={}
    def add_scope(store,warehouse):
        if warehouse in ('河北仓','山东仓','旧版未标仓'):scopes.setdefault(store,set()).add(warehouse)
    def edits_scope(store,value):
        for item in (value.get('edits') or {}).values():add_scope(store,item.get('warehouse'))
        for item in value.get('risks') or []:add_scope(store,item.get('warehouse'))
    for store,result in (payload.get('results') or {}).items():
        for row in result.get('rows',[]):
            add_scope(store,row.get('AU'))
        edits_scope(store,result.get('preissue') or {})
    for store,value in (payload.get('preissue_drafts') or {}).items():edits_scope(store,value)
    for store,warehouses in (payload.get('execution_warehouses') or {}).items():scopes.setdefault(store,set()).update(warehouses)
    saved=payload.get('warehouse_deletion_generations',{})
    return {'day':payload['day'],'generations':{s:payload.get('deletion_generations',{}).get(s,0) for s in codes},
            'warehouse_generations':{s:{w:saved.get(s,{}).get(w,0) for w in sorted(warehouses)} for s,warehouses in scopes.items()}}

def validate(db,guard):
    from work_repository import Conflict
    current=epochs(db,guard['day'],guard['generations'])
    if current!=guard['generations']:raise Conflict('部分门店已删除或数据代次变化；旧草稿/签发预览不能恢复，请重新计算相关门店')
    expected=guard.get('warehouse_generations',{})
    if warehouse_epochs(db,guard['day'],expected)!=expected:raise Conflict('部分仓单已删除或数据代次变化；旧草稿不能恢复被删仓明细，请重新读取当前仓单后继续')

def mark(db,parents):
    schema(db);operation=uuid.uuid4().hex
    for h in parents:
        db.execute('INSERT INTO deleted_order_tokens VALUES(?,?)',(h['oid'],operation))
        db.execute('INSERT INTO deletion_generations VALUES(?,?,1) ON CONFLICT(day,store) DO UPDATE SET generation=generation+1',(h['day'],h['store']))
    db.execute('INSERT INTO deletion_component_outbox(operation_id,oids) VALUES(?,?)',(operation,json.dumps([h['oid'] for h in parents])))
    return operation

def mark_warehouses(db,heads):
    """Retire exact child identities without invalidating a surviving sibling."""
    schema(db);operation=uuid.uuid4().hex;scopes=[]
    for head in heads:
        oid,warehouse=head['oid'],head['warehouse']
        db.execute('INSERT INTO deleted_warehouse_tokens VALUES(?,?,?)',(oid,warehouse,operation))
        db.execute('INSERT INTO warehouse_deletion_generations VALUES(?,?,?,1) ON CONFLICT(day,store,warehouse) DO UPDATE SET generation=generation+1',(head['day'],head['store'],warehouse))
        scopes.append({'oid':oid,'warehouse':warehouse})
    db.execute('INSERT INTO deletion_component_outbox(operation_id,oids,scopes) VALUES(?,?,?)',(operation,json.dumps(list(dict.fromkeys(h['oid'] for h in heads))),json.dumps(scopes)))
    return operation

def warehouse_epochs(db,day,scopes):
    return {store:{warehouse:(db.execute('SELECT generation FROM warehouse_deletion_generations WHERE day=? AND store=? AND warehouse=?',(day,store,warehouse)).fetchone() or [0])[0] if has(db,'warehouse_deletion_generations') else 0 for warehouse in warehouses} for store,warehouses in scopes.items()}

def read_warehouse_epochs(path,day,scopes):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as db:return warehouse_epochs(db,day,scopes)

def pending(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        if not has(db,'deletion_component_outbox'):return []
        generation='generation' if 'generation' in {r[1] for r in db.execute('PRAGMA table_info(deletion_component_outbox)')} else '0'
        scopes='scopes' if 'scopes' in {r[1] for r in db.execute('PRAGMA table_info(deletion_component_outbox)')} else "'[]'"
        return [{'operation_id':o,'oids':json.loads(ids),'generation':g,**({'scopes':json.loads(s)} if json.loads(s) else {})} for o,ids,g,s in db.execute('SELECT operation_id,oids,'+generation+','+scopes+' FROM deletion_component_outbox WHERE completed=0')]

def acknowledge(path,operation,generation):
    with closing(sqlite3.connect(path,timeout=.2)) as db,db:db.execute('UPDATE deletion_component_outbox SET completed=1 WHERE operation_id=? AND generation=?',(operation,generation))

def register_package(path,oid,package,warehouse=None):
    with closing(sqlite3.connect(path,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE');require(db,oid,warehouse)
        if warehouse is None and has(db,'deleted_warehouse_tokens') and db.execute('SELECT 1 FROM deleted_warehouse_tokens WHERE oid=?',(oid,)).fetchone():raise SourceDeleted('该订单已有仓单删除；旧任务缺少仓身份，请从仍有效仓单重新进入')
        db.execute('CREATE TABLE IF NOT EXISTS order_component_packages(package_id TEXT PRIMARY KEY,oid TEXT NOT NULL)')
        db.execute('INSERT INTO order_component_packages(package_id,oid) VALUES(?,?)',(package,oid))
        if warehouse:
            db.execute('CREATE TABLE IF NOT EXISTS warehouse_component_packages(package_id TEXT PRIMARY KEY,oid TEXT NOT NULL,warehouse TEXT NOT NULL)')
            db.execute('INSERT INTO warehouse_component_packages VALUES(?,?,?)',(package,oid,warehouse))

def package_finished(path,oid,package,warehouse=None):
    # A local writer may finish after component cleanup was acknowledged. Re-arm
    # the durable outbox so its late files are removed on the next local pass.
    with closing(sqlite3.connect(path,timeout=10)) as db,db:
        db.execute('BEGIN IMMEDIATE')
        if warehouse is None and has(db,'warehouse_component_packages'):
            match=db.execute('SELECT warehouse FROM warehouse_component_packages WHERE oid=? AND package_id=?',(oid,package)).fetchone()
            if match:warehouse=match[0]
        row=db.execute('SELECT operation_id FROM deleted_order_tokens WHERE oid=?',(oid,)).fetchone() if has(db,'deleted_order_tokens') else None
        if not row and warehouse and has(db,'deleted_warehouse_tokens'):row=db.execute('SELECT operation_id FROM deleted_warehouse_tokens WHERE oid=? AND warehouse=?',(oid,warehouse)).fetchone()
        if row:db.execute('UPDATE deletion_component_outbox SET completed=0,generation=generation+1 WHERE operation_id=?',(row[0],))
        elif warehouse is None and has(db,'deleted_warehouse_tokens'):
            # Older in-flight packages did not persist a child identity. Re-arm
            # exact scoped cleanups; this does not retire the surviving sibling.
            db.execute('UPDATE deletion_component_outbox SET completed=0,generation=generation+1 WHERE operation_id IN (SELECT operation_id FROM deleted_warehouse_tokens WHERE oid=?)',(oid,))
