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
    db.execute('CREATE TABLE IF NOT EXISTS deletion_component_outbox(operation_id TEXT PRIMARY KEY,oids TEXT NOT NULL,completed INTEGER NOT NULL DEFAULT 0)')
    db.execute('CREATE TABLE IF NOT EXISTS deleted_jst_steps(operation TEXT PRIMARY KEY,runid TEXT NOT NULL,warehouse TEXT NOT NULL,bid TEXT NOT NULL)')

def active(db,oid):
    if has(db,'deleted_order_tokens') and db.execute('SELECT 1 FROM deleted_order_tokens WHERE oid=?',(oid,)).fetchone():return False
    return has(db,'normal_orders') and bool(db.execute('SELECT 1 FROM normal_orders WHERE oid=?',(oid,)).fetchone())

def require(db,oid):
    if not active(db,oid):raise SourceDeleted('来源订单已删除；该任务停止，不恢复订单或关联记录')

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
    return {'day':payload['day'],'generations':{s:payload.get('deletion_generations',{}).get(s,0) for s in codes}}

def validate(db,guard):
    from work_repository import Conflict
    current=epochs(db,guard['day'],guard['generations'])
    if current!=guard['generations']:raise Conflict('部分门店已删除或数据代次变化；旧草稿/签发预览不能恢复，请重新计算相关门店')

def mark(db,parents):
    schema(db);operation=uuid.uuid4().hex
    for h in parents:
        db.execute('INSERT INTO deleted_order_tokens VALUES(?,?)',(h['oid'],operation))
        db.execute('INSERT INTO deletion_generations VALUES(?,?,1) ON CONFLICT(day,store) DO UPDATE SET generation=generation+1',(h['day'],h['store']))
    db.execute('INSERT INTO deletion_component_outbox VALUES(?,?,0)',(operation,json.dumps([h['oid'] for h in parents])))
    return operation

def pending(path):
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        if not has(db,'deletion_component_outbox'):return []
        return [{'operation_id':o,'oids':json.loads(ids)} for o,ids in db.execute('SELECT operation_id,oids FROM deletion_component_outbox WHERE completed=0')]

def acknowledge(path,operation):
    with closing(sqlite3.connect(path,timeout=.2)) as db,db:db.execute('UPDATE deletion_component_outbox SET completed=1 WHERE operation_id=?',(operation,))
