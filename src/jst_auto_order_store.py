"""Persisted 聚水潭 order intent and acknowledgements in the workbench DB."""
from contextlib import closing
from hashlib import sha256
from pathlib import Path
import sqlite3
from uuid import uuid4

from runtime_support import checkpoint, CommitProgress
import work_repository as wr
from sqlite_write import begin


STAGES = {
    'PREPARED':'尚未导入',
    'PREVIEWED':'尚未导入',
    'IMPORT_UNKNOWN':'导入结果待核',
    'IMPORTED':'已导入',
    'AUDIT_UNKNOWN':'审核结果待核',
    'AUDITED':'普通审核完成',
    'REVIEW':'异常待处理',
}
RELEASED = frozenset(('IMPORT_UNKNOWN','IMPORTED','AUDIT_UNKNOWN','AUDITED','REVIEW'))
UNRESOLVED = ('IMPORT_UNKNOWN','AUDIT_UNKNOWN')


def profile_schema(db):
    db.execute('''CREATE TABLE IF NOT EXISTS jst_auto_profiles(
        store TEXT NOT NULL, warehouse TEXT NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(store,warehouse))''')


def schema(db):
    profile_schema(db)
    db.execute('''CREATE TABLE IF NOT EXISTS jst_auto_orders(
        online TEXT PRIMARY KEY, oid TEXT NOT NULL, warehouse TEXT NOT NULL, gid TEXT NOT NULL,
        stage TEXT NOT NULL, sequence INTEGER NOT NULL, created TEXT NOT NULL, updated TEXT NOT NULL,
        payload TEXT NOT NULL, digest TEXT NOT NULL, file_sha TEXT NOT NULL, workbook BLOB NOT NULL,
        proof TEXT, internal TEXT, platform_status TEXT, reason TEXT NOT NULL, UNIQUE(oid,warehouse))''')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS jst_auto_bound_internal ON jst_auto_orders(warehouse,internal) WHERE internal IS NOT NULL')


def exists(db, table='jst_auto_orders'):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def connection(path):
    return sqlite3.connect(Path(path).resolve().as_uri()+'?mode=rw',uri=True,timeout=.15)


def token(payload):
    return sha256(wr.dumps(payload).encode('utf-8')).hexdigest()


def state(db, head):
    if not exists(db):
        return None
    return db.execute('SELECT stage,internal FROM jst_auto_orders WHERE oid=? AND warehouse=?',
                      (head['oid'],head['warehouse'])).fetchone()


def read(db, online, *, workbook=False):
    if not exists(db):
        return None
    fields=('online','gid','stage','sequence','created','updated','payload','digest','file_sha',
            'proof','internal','platform_status','reason')
    if workbook:
        fields+=('workbook',)
    row=db.execute('SELECT '+','.join(fields)+' FROM jst_auto_orders WHERE online=?',(online,)).fetchone()
    if row is None:
        return None
    value=dict(zip(fields,row))
    value['payload']=wr.loads(value['payload'])
    need(value['payload'],value['digest'],value['online'],value['gid'])
    value['proof']=wr.loads(value['proof']) if value['proof'] else None
    if workbook and sha256(value['workbook']).hexdigest()!=value['file_sha']:
        raise ValueError('原提交 Excel 已变化，需核对原任务')
    return value


def read_warehouse_order(db, head):
    if not exists(db):
        return None
    row = db.execute('SELECT online FROM jst_auto_orders WHERE oid=? AND warehouse=?',
                     (head['oid'],head['warehouse'])).fetchone()
    return read(db,row[0]) if row else None


def need(payload, digest, online, gid):
    if token(payload)!=digest or payload['online']!=online or payload['gid']!=gid:
        raise ValueError('聚水潭自动下单资料摘要不符，需核对原任务')


def profile(db, store, warehouse):
    if not exists(db,'jst_auto_profiles'):
        return None
    return _profile(db,store,warehouse)


def _profile(db, store, warehouse):
    row=db.execute('SELECT payload FROM jst_auto_profiles WHERE store=? AND warehouse=?',(store,warehouse)).fetchone()
    return wr.loads(row[0]) if row else None


def profiles(db):
    if not exists(db,'jst_auto_profiles'):
        return {}
    return {(code,warehouse):wr.loads(raw) for code,warehouse,raw in
            db.execute('SELECT store,warehouse,payload FROM jst_auto_profiles')}


def _put_profile(db, store, warehouse, values, expected):
    current=_profile(db,store,warehouse)
    if (current or {}).get('version')!=expected:
        raise wr.Conflict(f'{store} / {warehouse} 的收货资料已更新，请重新核对后保存')
    record={'version':uuid4().hex, 'values':values}
    db.execute('INSERT INTO jst_auto_profiles VALUES(?,?,?) ON CONFLICT(store,warehouse) DO UPDATE SET payload=excluded.payload',
               (store,warehouse,wr.dumps(record)))
    return record


def save_profile(path, store, warehouse, values, expected, cancel=None):
    with closing(connection(path)) as db,db:
        begin(db,cancel)
        profile_schema(db)
        return _put_profile(db,store,warehouse,values,expected)


def save_profiles(path, changes, progress=lambda _:None, cancel=None):
    if not changes:
        return {}
    saved={}
    with closing(connection(path)) as db,db:
        begin(db,cancel)
        profile_schema(db)
        for change in changes:
            checkpoint(cancel)
            key=change['store'],change['warehouse']
            saved[key]=_put_profile(db,*key,change['after'],change['version'])
        checkpoint(cancel)
        progress(CommitProgress(True))
    return saved


def put_prepared(db, payload, workbook, expected):
    online = payload['online']
    head = payload['head']
    old = read_warehouse_order(db,head)
    if old and old['online'] != online:
        raise wr.Conflict('原下单任务号已变化，请刷新并核对原任务')
    if not old and db.execute('SELECT 1 FROM jst_auto_orders WHERE online=?',(online,)).fetchone():
        raise wr.Conflict('线上单号已关联其他仓单，请核对原自动下单任务')
    if old and old['stage'] in RELEASED:
        return old
    if (old or {}).get('sequence')!=expected:
        raise wr.Conflict('原下单任务已变化，请重新读取')
    digest=token(payload)
    if old and old['digest']==digest:
        return old
    stamp=wr.now()
    db.execute('''INSERT INTO jst_auto_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(online) DO UPDATE SET gid=excluded.gid,stage=excluded.stage,sequence=excluded.sequence,
        updated=excluded.updated,payload=excluded.payload,digest=excluded.digest,file_sha=excluded.file_sha,
        workbook=excluded.workbook,proof=NULL,internal=NULL,platform_status=NULL,reason=excluded.reason''',
        (online,head['oid'],head['warehouse'],payload['gid'],'PREPARED',0 if old is None else old['sequence']+1,
         stamp,stamp,wr.dumps(payload),digest,sha256(workbook).hexdigest(),workbook,None,None,None,'等待核对'))
    return read(db,online)


def update(db, order, stage, *, proof=None, internal=None, status=None, reason=''):
    current=db.execute('SELECT sequence,stage FROM jst_auto_orders WHERE online=?',(order['online'],)).fetchone()
    if current!=(order['sequence'],order['stage']):
        raise wr.Conflict('下单任务状态已变化，请刷新原记录')
    db.execute('''UPDATE jst_auto_orders SET stage=?,sequence=sequence+1,updated=?,
        proof=?,internal=?,platform_status=?,reason=? WHERE online=?''',
        (stage,wr.now(),wr.dumps(proof if proof is not None else order['proof']),
         internal or order['internal'],status or order['platform_status'],reason,order['online']))
    return read(db,order['online'])


def list_orders(path):
    with closing(wr.read_connection(path)) as db:
        if not exists(db):return []
        return [{'online':online,'stage':stage,'sequence':sequence,'store':store,'name':name,'warehouse':wh,
                 'number':num,'shop':shop,'sku_count':count,'quantity':qty,'amount':amount,'preview_amount':preview,
                 'internal':internal,'status':status,'reason':reason}
                for online,stage,sequence,store,name,wh,num,shop,count,qty,amount,preview,internal,status,reason in db.execute(
                    '''SELECT online,stage,sequence,json_extract(payload,'$.head.store'),json_extract(payload,'$.head.name'),
                       warehouse,json_extract(payload,'$.head.number'),json_extract(payload,'$.shop.name'),
                       json_extract(payload,'$.stats.sku_count'),json_extract(payload,'$.stats.quantity'),
                       json_extract(payload,'$.stats.amount'),json_extract(proof,'$.amount'),
                       internal,platform_status,reason FROM jst_auto_orders ORDER BY created DESC,rowid''')]


def _deletion_scope(oid, warehouse):
    if warehouse is None:
        return 'oid=?', (oid,)
    return 'oid=? AND warehouse=?', (oid,warehouse)


def pending_deletion_orders(db, scopes, cancel=None):
    """Read the original tasks that block removal of the selected warehouse orders."""
    pending=[]
    if not exists(db):
        return pending
    fields=('number','warehouse','stage','online','internal','sequence','reason')
    for oid,warehouse in dict.fromkeys(scopes):
        checkpoint(cancel)
        condition,arguments=_deletion_scope(oid,warehouse)
        for row in db.execute(
                "SELECT coalesce(json_extract(payload,'$.head.number'),oid),warehouse,stage,online,internal,sequence,reason "
                "FROM jst_auto_orders WHERE "+condition+" AND stage IN (?,?) ORDER BY warehouse",
                (*arguments,*UNRESOLVED)):
            pending.append(dict(zip(fields,row)))
    return pending


def require_resolved_deletion(db, scopes, cancel=None):
    blocked=[]
    for task in pending_deletion_orders(db,scopes,cancel):
        identity='内部单号 '+task['internal'] if task['internal'] else '线上单号 '+task['online']
        status=STAGES[task['stage']]
        details=f"{task['number']} · {task['warehouse']} · {identity} · {status}"
        blocked.append(details+(' · '+task['reason'] if task['reason'] else ''))
    if blocked:
        raise wr.Conflict('以下仓单的聚水潭原单仍待核。再次点击“彻底删除勾选订单”会自动回查。\n'+'\n'.join(sorted(blocked)))


def delete_orders(db, scopes):
    if not exists(db):
        return
    scopes=tuple(scopes)
    require_resolved_deletion(db,scopes)
    for oid,warehouse in scopes:
        condition,arguments=_deletion_scope(oid,warehouse)
        for table in ('jst_fulfillment_checks','jst_fulfillment_sources'):
            if exists(db,table):
                db.execute('DELETE FROM '+table+' WHERE online IN (SELECT online FROM jst_auto_orders WHERE '+condition+')',arguments)
        db.execute('DELETE FROM jst_auto_orders WHERE '+condition,arguments)
