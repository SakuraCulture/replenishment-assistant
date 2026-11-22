"""Read the exact paid warehouse selection and any original procurement task."""

import json
import sqlite3
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path

from aoxiang_payment_preflight import preflight_payment
from runtime_support import checkpoint

MAX_ORDERS = 256
CLOSED_RESERVATIONS = {'CANCELLED_BEFORE_UPLOAD', 'SUPERSEDED'}


def payment_heads(receipt):
    return [{**item['payment_head'],
             'preview_payment': {'version': item['version'], 'digest': item['payment_digest']}}
            for item in receipt or () if item['stage'] == 'payment' and 'payment_head' in item]


def _scope(head):
    return head.get('number'), head.get('warehouse'), head.get('store')


def _warehouse(head):
    return head['oid'], head['warehouse']


@contextmanager
def _index(path):
    if not path.is_file():
        yield None
        return
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
        db.execute('BEGIN')
        yield db


def _batches(db, numbers, cancel):
    if db is None:
        return {}
    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'ax_workflows' not in tables:
        if tables:
            raise ValueError('翱象批次记录需核对，请在原批次页检查后再选单。')
        return {}
    matches = {}
    sql = """
        SELECT w.id, i.value
          FROM ax_workflows w, json_each(w.payload,'$.items') i
         WHERE json_extract(w.payload,'$.kind') IN ('prepare','upload')
           AND json_extract(w.payload,'$.successor') IS NULL
           AND json_extract(i.value,'$.key') IN (SELECT value FROM json_each(?))
         ORDER BY CASE json_extract(w.payload,'$.kind') WHEN 'upload' THEN 0 ELSE 1 END, w.rowid DESC
    """
    for batch, raw in db.execute(sql, (numbers,)):
        checkpoint(cancel)
        item = json.loads(raw)
        if item.get('source_deleted') or item.get('ended_locally') or item.get('rebuild_to'):
            continue
        if item['state'] == 'SKIPPED' and not item.get('cancelled_unstarted'):
            continue
        head = item.get('head') or item.get('basis') or {}
        purchase_id = (item.get('package') or {}).get('id') or (item.get('result') or {}).get('purchase_id')
        matches.setdefault(_scope(head), []).append({
            'head': head, 'batch': batch, 'purchase_id': purchase_id,
            'order_id': ((item.get('result') or {}).get('binding') or {}).get('orderId')})
    return matches


def _reservations(db, numbers, cancel):
    if db is None:
        return {}
    tables = {name for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'purchase_creates' not in tables:
        if 'purchase_create_events' in tables or ('metadata' in tables and db.execute(
                "SELECT 1 FROM metadata WHERE key='purchaseCreateProtocol'").fetchone()):
            raise ValueError('翱象原采购登记需核对，请检查原任务后再选单。')
        return {}
    matches = {}
    sql = """
        SELECT id,state,platform_order,json_extract(payload,'$.intent.basis')
          FROM purchase_creates
         WHERE json_extract(payload,'$.intent.basis.number') IN (SELECT value FROM json_each(?))
         ORDER BY rowid DESC
    """
    for purchase_id, state, order_id, raw in db.execute(sql, (numbers,)):
        checkpoint(cancel)
        head = json.loads(raw or '{}')
        matches.setdefault(_scope(head), []).append({
            'head': head, 'purchase_id': purchase_id, 'state': state, 'order_id': order_id})
    return matches


def existing_tasks(path, folder, heads, *, db=None, cancel=None):
    """Project task identities in bulk, without opening a platform session."""
    if not heads:
        return {}
    folder = Path(folder)
    numbers = json.dumps(list(dict.fromkeys(head['number'] for head in heads)))
    with (_index(folder/'workflows.sqlite3') if db is None else nullcontext(db)) as workflow:
        batches = _batches(workflow, numbers, cancel)
    with _index(folder/'component-state'/'state.sqlite') as component:
        reservations = _reservations(component, numbers, cancel)

    result = {}
    for head in heads:
        checkpoint(cancel)
        key = _scope(head)
        records = reservations.get(key, ())
        retired = {r['purchase_id'] for r in records if r['state'] in CLOSED_RESERVATIONS}
        live = [r for r in records if r['state'] not in CLOSED_RESERVATIONS]
        tasks = [r for r in batches.get(key, ()) if r['purchase_id'] not in retired]
        if len(live) > 1:
            result[_warehouse(head)] = {'state': 'blocked', 'message': '有多条同号原采购记录，请核对原任务。'}
            continue
        record = live[0] if live else None
        if record:
            tasks = [r for r in tasks if r['purchase_id'] == record['purchase_id']]
        task = tasks[0] if tasks else None
        if not record and not task:
            continue
        owner = (task or record)['head']
        if not owner.get('oid') and record:
            from analytics_identifiers import source_basis
            owner = source_basis(path, record['purchase_id'], owner) or {}
        if owner.get('oid') != head['oid'] or (record and record['head'].get('oid') not in (None, '', head['oid'])):
            result[_warehouse(head)] = {'state': 'blocked', 'message': '同号原采购与当前仓单身份待核。'}
            continue
        order_id = (record or task)['order_id'] or (task or {}).get('order_id')
        result[_warehouse(head)] = {
            'state': 'linked' if order_id else 'original',
            'message': ('已关联采购单 '+str(order_id) if order_id else
                        '已有原采购批次，继续原任务。' if task else '已有原采购记录，继续核对。'),
            'order_id': order_id,
            'batch': task['batch'] if task else None,
            'purchase_id': (record or task)['purchase_id'],
        }
    return result


def inspect_scope(path, folder, heads, progress=lambda _: None, cancel=None):
    """Recheck selected signing identity and effective payment in one read snapshot."""
    tasks = existing_tasks(path, folder, heads, cancel=cancel)
    result = []
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=2)) as db:
        db.execute('BEGIN')
        for head in heads:
            checkpoint(cancel)
            state = tasks.get(_warehouse(head))
            if state is None:
                try:
                    if head['warehouse'] not in ('山东仓', '河北仓'):
                        raise ValueError('请选择供货仓明确的仓单。')
                    facts = preflight_payment(path, head, expected=head.get('preview_payment'),
                                              require_quantity=True, cancel=cancel, db=db)
                    state = {'state': 'ready', 'message': '可生成采购预览', 'payment': facts['payment']}
                except ValueError as exc:
                    state = {'state': 'blocked', 'message': str(exc)}
            result.append({'head': head, **state})
    return result


def enqueue(store, heads, progress=lambda _: None, cancel=None):
    rows = inspect_scope(store.business_path, store.folder, heads, progress, cancel)
    changed = [r['head']['number'] for r in rows if r['state'] != 'ready']
    if changed:
        raise ValueError('采购范围已变化，请刷新并核对：'+'、'.join(changed))
    payments = {r['head']['number']: r['payment'] for r in rows}
    checkpoint(cancel)
    return store.prepare(heads, payments=payments)
