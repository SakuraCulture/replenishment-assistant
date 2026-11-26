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
    'PREPARED':'待平台预览',
    'PREVIEWED':'已预览，待确认导入',
    'IMPORT_UNKNOWN':'导入结果待核',
    'IMPORT_FAILED':'导入失败',
    'MANUAL_HANDOFF':'人工处理待核',
    'IMPORTED':'已导入',
    'AUDIT_UNKNOWN':'审核结果待核',
    'AUDITED':'普通审核完成',
    'REVIEW':'异常待处理',
    'CANCELLED':'原单已取消',
    'WITHDRAWN':'未成单分配已撤销',
}
RELEASED = frozenset(('IMPORT_UNKNOWN','IMPORT_FAILED','MANUAL_HANDOFF','IMPORTED','AUDIT_UNKNOWN','AUDITED','REVIEW','CANCELLED'))
UNRESOLVED = ('IMPORT_UNKNOWN','AUDIT_UNKNOWN')
HISTORY = 'jst_auto_order_history'
CANCELLATION_HOLDS = 'jst_cancellation_holds'
LEGACY_CANCELLATION = """stage='REVIEW' AND internal IS NOT NULL
    AND (reason LIKE '聚水潭状态为已取消%' OR reason LIKE '聚水潭当前为“已取消”%')"""

# rc13 kept the native observation even when a later failed lookup replaced
# the display note. The event was written after checking the saved binding.
# Corroborate it with the signed-order snapshot and exact generation.
# Keep the scoped checks in purchase-host-guard.mjs in step with this query.
LEGACY_HISTORY_TABLES = frozenset(('jst_auto_orders', 'jst_generations', 'jst_events', 'lifecycle_versions'))
LEGACY_HISTORY = """
    FROM jst_auto_orders AS a JOIN jst_generations AS g
      ON g.gid=a.gid AND g.oid=a.oid AND g.warehouse=a.warehouse
    WHERE {scope} AND a.internal IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM jst_events AS e WHERE e.day=g.day AND e.oid=a.oid AND e.warehouse=a.warehouse
          AND e.action='聚水潭原单核对' AND json_valid(e.detail)
          AND json_extract(e.detail,'$.native_status')='已取消'
          AND json_extract(e.detail,'$.online')=a.online AND json_extract(e.detail,'$.gid')=a.gid
          AND json_extract(e.detail,'$.internal')=a.internal
      )
      AND g.revision=json_extract(a.payload,'$.head.revision')
      AND g.digest=json_extract(a.payload,'$.generation.digest')
      AND json_extract(a.proof,'$.binding.warehouse')=a.warehouse
      AND json_extract(a.proof,'$.binding.shop')=json_extract(a.payload,'$.shop.name')
      AND length(json_extract(a.proof,'$.binding.coid'))>0
      AND length(json_extract(a.proof,'$.binding.uid'))>0
      AND length(json_extract(a.proof,'$.binding.shop_id'))>0
      AND EXISTS (
        SELECT 1 FROM lifecycle_versions AS v
        WHERE v.oid=a.oid AND v.warehouse=a.warehouse AND v.revision=g.revision AND v.stage='jst_order'
          AND json_extract(CAST(v.payload AS TEXT),'$.pending')=1
          AND json_extract(CAST(v.payload AS TEXT),'$.native_status')='已取消'
          AND json_extract(CAST(v.payload AS TEXT),'$.reference.online')=a.online
          AND json_extract(CAST(v.payload AS TEXT),'$.reference.internal')=a.internal
          AND json_extract(CAST(v.payload AS TEXT),'$.reference.gid')=a.gid
          AND json_extract(CAST(v.payload AS TEXT),'$.reference.order_digest')=a.digest
          AND json_extract(CAST(v.payload AS TEXT),'$.head.digest')=json_extract(a.payload,'$.head.digest')
          AND json_extract(CAST(v.payload AS TEXT),'$.head.number')=json_extract(a.payload,'$.head.number')
          AND json_extract(CAST(v.payload AS TEXT),'$.head.store')=json_extract(a.payload,'$.head.store')
      )
"""


def legacy_cancellation_history(db, head, tables=None):
    if tables is None:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if LEGACY_HISTORY_TABLES <= tables:
        return db.execute('SELECT a.online,a.internal,a.gid' + LEGACY_HISTORY.format(
            scope='a.oid=? AND a.warehouse=? AND g.revision=?'),
            (head['oid'], head['warehouse'], head['revision'])).fetchone()
    return None


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
    historical = False
    if row is None and exists(db, HISTORY):
        row = db.execute('SELECT '+','.join(fields)+' FROM '+HISTORY+' WHERE online=?', (online,)).fetchone()
        historical = row is not None
    if row is None:
        return None
    value=dict(zip(fields,row))
    if historical:
        value['historical'] = True
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


def preserve_cancellation_hold(db, online):
    # rc13 stored this evidence in the same column as the latest lookup note.
    # Freeze it in the transaction that would otherwise overwrite it.
    row = db.execute('SELECT online,oid,warehouse,gid,internal,updated,reason FROM jst_auto_orders '
                     'WHERE online=? AND '+LEGACY_CANCELLATION, (online,)).fetchone()
    if row:
        db.execute('''CREATE TABLE IF NOT EXISTS jst_cancellation_holds(
            online TEXT PRIMARY KEY,oid TEXT NOT NULL,warehouse TEXT NOT NULL,
            gid TEXT NOT NULL,internal TEXT NOT NULL,observed TEXT NOT NULL,signal TEXT NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS jst_cancellation_hold_scope ON jst_cancellation_holds(oid,warehouse)')
        db.execute('INSERT OR IGNORE INTO jst_cancellation_holds VALUES(?,?,?,?,?,?,?)', row)


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


def display_stage(stage, receipt):
    # Older builds still recognize the same guarded execution stage. The
    # exact rejection is a separate observation on the released intent.
    if stage=='IMPORT_UNKNOWN' and (receipt or {}).get('fail_count')==1:
        return 'IMPORT_FAILED'
    return stage


def manual_pending(order):
    proof=order.get('proof') or {}
    return not order['internal'] and bool(proof.get('manual_handoff') or proof.get('manual_proposal'))


def display_reason(stage, reason, receipt, not_sent=None):
    if stage=='IMPORT_FAILED' and receipt:
        summary=f"聚水潭导入失败（成功 {receipt['success_count']} 张、失败 {receipt['fail_count']} 张），可重新平台预览"
    elif stage=='IMPORT_UNKNOWN':
        if receipt and receipt.get('success_count')==1:
            summary='平台回执成功 1 张，请核对原单取得内部单号'
        elif not_sent:
            summary='最终导入未发送，可重新平台预览'
            if not_sent.get('reason'):
                summary+='。'+not_sent['reason']
        else:
            summary='尚无最终导入回执，可预览原文件。确认导入需先核实上次提交结果'
    else:
        return reason
    if reason and reason not in ('已核对本仓企业、店铺和导入入口','已保存导入意图，等待原单回查',
                                 '已取得导入回执，等待核对原线上单号',summary):
        return summary+'。'+reason
    return summary


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
    if not old and read(db, online):
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
    if (internal and exists(db, HISTORY) and db.execute(
            'SELECT 1 FROM '+HISTORY+' WHERE warehouse=? AND internal=?',
            (order['payload']['head']['warehouse'], internal)).fetchone()):
        raise wr.Conflict('回查内部单号已属于历史原单，请核对原订单，不能关联到后继任务')
    if order['stage'] == 'REVIEW':
        preserve_cancellation_hold(db,order['online'])
    db.execute('''UPDATE jst_auto_orders SET stage=?,sequence=sequence+1,updated=?,
        proof=?,internal=?,platform_status=?,reason=? WHERE online=?''',
        (stage,wr.now(),wr.dumps(proof if proof is not None else order['proof']),
         internal or order['internal'],status or order['platform_status'],reason,order['online']))
    return read(db,order['online'])


def archive_cancelled(db, order):
    """Move a terminal attempt out of the single live warehouse slot."""
    if order['stage'] != 'CANCELLED' or not order['internal'] or order.get('historical'):
        raise wr.Conflict('原单取消尚未核实，不能释放下单范围')
    _archive(db, order)


def archive_withdrawn(db, order, proof):
    if order.get('historical') or order['internal']:
        raise wr.Conflict('原下单任务已结束或已关联内部单号')
    saved = update(db, order, 'WITHDRAWN', proof=proof, reason='原分配已撤销，历史原件可核对')
    _archive(db, saved)
    if exists(db, 'jst_cancellations'):
        # The unsubmitted attempt can itself be the successor of a cancelled original.
        db.execute('UPDATE jst_cancellations SET successor=? WHERE successor=? AND oid=? AND warehouse=?',
                   (_withdrawal_successor(order['online']), order['online'],
                    order['payload']['head']['oid'], order['payload']['head']['warehouse']))


def _withdrawal_successor(online):
    return 'BH' + sha256(('replenishment-jst-withdrawn:' + online).encode()).hexdigest()[:24].upper()


def after_withdrawals(db, online):
    if exists(db, HISTORY):
        while db.execute('SELECT 1 FROM '+HISTORY+" WHERE online=? AND stage='WITHDRAWN'", (online,)).fetchone():
            online = _withdrawal_successor(online)
    return online


def _archive(db, order):
    if not exists(db, HISTORY):
        db.execute('CREATE TABLE '+HISTORY+' AS SELECT * FROM jst_auto_orders WHERE 0')
        db.execute('CREATE UNIQUE INDEX jst_auto_history_online ON '+HISTORY+'(online)')
        db.execute('CREATE UNIQUE INDEX jst_auto_history_internal ON '+HISTORY+'(warehouse,internal)')
    value = (order['online'], order['sequence'])
    db.execute('INSERT INTO '+HISTORY+' SELECT * FROM jst_auto_orders WHERE online=? AND sequence=?', value)
    if db.execute('DELETE FROM jst_auto_orders WHERE online=? AND sequence=?', value).rowcount != 1:
        raise wr.Conflict('原下单任务状态已变化，请重新核对')


def list_orders(path):
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        if not exists(db):return []
        rows = []
        rounds = {(o,w,r):g for o,w,r,g in db.execute('''SELECT r.oid,r.warehouse,r.revision,a.gid
            FROM jst_allocation_rounds r LEFT JOIN jst_active a ON a.oid=r.oid AND a.warehouse=r.warehouse''')
        } if exists(db,'jst_allocation_rounds') and exists(db,'jst_active') else {}
        for table in ('jst_auto_orders', HISTORY):
            if not exists(db, table):
                continue
            fields = ('online','stage','sequence','store','name','warehouse','number','shop',
                      'sku_count','quantity','amount','preview_amount','internal','status','reason','import_receipt','import_not_sent','manual_handoff','manual_proposal','oid','gid','revision')
            for row in db.execute('''SELECT online,stage,sequence,json_extract(payload,'$.head.store'),
                   json_extract(payload,'$.head.name'),warehouse,json_extract(payload,'$.head.number'),
                   json_extract(payload,'$.shop.name'),json_extract(payload,'$.stats.sku_count'),
                   json_extract(payload,'$.stats.quantity'),json_extract(payload,'$.stats.amount'),
                   json_extract(proof,'$.amount'),internal,platform_status,reason,
                   json_extract(proof,'$.import_receipt'),json_extract(proof,'$.import_not_sent'),json_extract(proof,'$.manual_handoff.id'),
                   json_extract(proof,'$.manual_proposal.id'),oid,gid,json_extract(payload,'$.head.revision')
                   FROM '''+table+' ORDER BY created DESC,rowid'):
                record=dict(zip(fields,row))
                raw=record.pop('import_receipt')
                receipt=wr.loads(raw) if raw else None
                raw=record.pop('import_not_sent')
                not_sent=wr.loads(raw) if raw else None
                record['stage']=display_stage(record['stage'],receipt)
                record['import_outcome']=('accepted' if receipt and receipt.get('success_count')==1 else
                                          'rejected' if receipt and receipt.get('fail_count')==1 else
                                          'not_sent' if not_sent else None)
                if (record['stage'] in ('IMPORT_UNKNOWN','IMPORT_FAILED') and
                        not record['internal'] and (record['manual_handoff'] or record['manual_proposal'])):
                    record['stage']='MANUAL_HANDOFF'
                record['reason']=display_reason(record['stage'],record['reason'],receipt,not_sent)
                record['stage_label']=STAGES[record['stage']]
                if record['stage']=='IMPORT_UNKNOWN':
                    record['stage_label']=('导入已受理，待核原单' if record['import_outcome']=='accepted' else
                                           '未发送，待重新预览' if not_sent else '导入结果待核')
                scope=(record.pop('oid'),record['warehouse'],record.pop('revision'))
                gid=record.pop('gid')
                if table!=HISTORY and scope in rounds and rounds[scope]!=gid:
                    record['allocation_superseded']=True
                    if record['stage'] in RELEASED:
                        record['reason']='旧分配已失效，可核对聚水潭原单。'+record['reason']
                    else:
                        record.update(stage_label='旧分配已失效',reason='请按有效分配重新制表' if rounds[scope] else '请重新分配后制表')
                rows.append({**record, 'historical':table == HISTORY})
        return rows


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
        for order_table in ('jst_auto_orders', HISTORY):
            if not exists(db, order_table):
                continue
            for table in ('jst_fulfillment_checks','jst_fulfillment_sources'):
                if exists(db,table):
                    db.execute('DELETE FROM '+table+' WHERE online IN (SELECT online FROM '+order_table+' WHERE '+condition+')',arguments)
            db.execute('DELETE FROM '+order_table+' WHERE '+condition,arguments)
        for table in ('jst_cancellation_reviews','jst_cancellations',CANCELLATION_HOLDS):
            if exists(db,table):
                db.execute('DELETE FROM '+table+' WHERE '+condition,arguments)
