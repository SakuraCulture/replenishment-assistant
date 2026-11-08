"""An editable local JST number; immutable payment documents stay unchanged."""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

import order_lifecycle as lc
import purchases
import work_repository as wr
from runtime_support import checkpoint, CommitProgress

PREFIX = 'jst_order_reference.'
ACTION = '维护聚水潭内部订单号'
HEAD_KEYS = ('oid', 'revision', 'warehouse', 'number', 'store', 'digest')


def digest(value):
    return hashlib.sha256(wr.dumps(value).encode('utf-8')).hexdigest()


def reference_key(head):
    return PREFIX + digest([head[k] for k in HEAD_KEYS])


def state(db, head):
    purchases.validate_head(db, head)
    payment = lc.current(db, head, 'payment')
    if not payment:
        raise ValueError('请先确认付款，再维护聚水潭内部订单号')
    raw = payment['payload'].get('raw_rows', payment['payload']['rows'])
    original = sorted({str(r.get('document_number') or '') for r in raw} - {''})
    if len(original) > 1:
        raise ValueError('原付款含多个内部订单号，请先核对原付款单')
    found = db.execute("SELECT payload FROM lifecycle_mappings WHERE stage='payment' AND name=?", (reference_key(head),)).fetchone()
    registered = wr.loads(found[0]) if found else None
    number = registered['number'] if registered else next(iter(original), '')
    identity = {k: head[k] for k in HEAD_KEYS}
    token = digest([identity, payment['version'], payment['digest'], registered])
    return {'head': dict(head), 'number': number, 'token': token, 'payment_version': payment['version'],
            'source': '本机登记' if registered else '原付款文件' if number else '尚未填写'}


def read(path, head, progress=lambda _: None, cancel=None):
    checkpoint(cancel)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.execute('BEGIN')
        result = state(db, head)
        rows = db.execute('SELECT at,detail FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND action=? ORDER BY id DESC', (*lc.key(head), ACTION))
        result['history'] = []
        for at, raw in rows:
            checkpoint(cancel); detail = wr.loads(raw)
            result['history'].append([at, detail['old_number'] or '未填写', detail['number']])
        return result


def save(path, expected, number, progress=lambda _: None, cancel=None):
    number = str(number).strip()
    if not 1 <= len(number) <= 100 or any(c.isspace() or ord(c) < 32 for c in number):
        raise ValueError('请填写聚水潭内部订单号，最多100字，不含空格或换行')
    checkpoint(cancel)
    current = read(path, expected['head'], cancel=cancel)
    if current['token'] != expected['token']:
        raise wr.Conflict('订单、付款或内部号已更新，请重新打开后维护')
    if number == current['number']:
        return {'changed': False, 'number': number}
    import storage_chunks
    storage_chunks.backup_before_write(Path(path), progress)
    with closing(sqlite3.connect(path, timeout=.2)) as db, db:
        lc.begin(db, cancel)
        current = state(db, expected['head'])
        if current['token'] != expected['token']:
            raise wr.Conflict('订单、付款或内部号已更新，本次未保存，请重新打开')
        checkpoint(cancel); progress(CommitProgress(True))
        head = {k: current['head'][k] for k in HEAD_KEYS}
        value = {'head': head, 'number': number, 'saved': wr.now()}
        db.execute("INSERT INTO lifecycle_mappings VALUES('payment',?,?) ON CONFLICT(stage,name) DO UPDATE SET payload=excluded.payload", (reference_key(head), wr.dumps(value)))
        detail = {**value, 'old_number': current['number']}
        db.execute('INSERT INTO lifecycle_events(at,oid,revision,warehouse,stage,version,action,detail) VALUES(?,?,?,?,?,?,?,?)',
                   (value['saved'], *lc.key(head), 'payment', current['payment_version'], ACTION, wr.dumps(detail)))
    return {'changed': True, 'number': number}
