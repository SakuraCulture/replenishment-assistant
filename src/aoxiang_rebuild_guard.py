"""Read durable component evidence before ending a host purchase task."""
import json
from contextlib import closing, nullcontext
from pathlib import Path
import sqlite3

from lifecycle_store import has_formal_document
import work_repository as wr


MESSAGE = '旧采购的上传、受理或收货事实尚未核清，请继续核对原采购。原收货待办已保留。'
UNSENT = frozenset(('RESERVED','PAUSED','FAILED_PRECHECK','CANCELLED_BEFORE_UPLOAD','SUPERSEDED'))


def replaceable(original, observed):
    if (original.get('state') in UNSENT and original.get('uploadAttempted') is False
            and not any(original.get(k) for k in ('receipt','binding','additionalTaskIds'))):
        return True
    intent = original.get('intent') or {}
    expected, bound = intent.get('basis') or {}, original.get('binding') or {}
    observed = observed or {}
    scope, summary = observed.get('scope') or {}, observed.get('summary') or {}
    return bool(bound.get('orderId') and not original.get('additionalTaskIds')
                and observed.get('state') == 'VOIDED' and observed.get('terminalKind') == 'VOIDED'
                and observed.get('terminal') is True and observed.get('finalSync') == 'NOT_DUE'
                and summary.get('status') == '已作废' and summary.get('issueCount') == 0
                and not (observed.get('hostReceipt') or observed.get('hostConflict'))
                and scope.get('orderId') == bound['orderId'] and scope.get('store') == expected.get('store')
                and scope.get('supplier') == intent.get('supplierCode')
                and scope.get('identity') == intent.get('identity') and observed.get('basis') == expected)


def require_rebuildable(folder, business_path, purchase_ids, scopes):
    component = Path(folder)/'component-state/state.sqlite'
    purchase_ids = set(purchase_ids)
    if not component.is_file():
        if purchase_ids:
            raise ValueError(MESSAGE)
        return
    with closing(sqlite3.connect(component.resolve().as_uri()+'?mode=ro',uri=True,timeout=2)) as db:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'purchase_creates' not in tables:
            if purchase_ids:
                raise ValueError(MESSAGE)
            return
        rows = db.execute('''SELECT id,payload FROM purchase_creates
            WHERE id IN (SELECT value FROM json_each(?))
               OR json_extract(payload,'$.intent.basis.oid') IN (SELECT value FROM json_each(?))''',
                          (json.dumps(sorted(purchase_ids)),json.dumps(sorted({oid for oid,_ in scopes}))))
        seen = set()
        with (closing(wr.read_connection(business_path)) if business_path else nullcontext()) as host:
            for id, raw in rows:
                item = json.loads(raw)
                basis = item['intent']['basis']
                key = basis.get('oid'),basis.get('warehouse')
                if id not in purchase_ids and key not in scopes:
                    continue
                seen.add(id)
                observed = None
                if 'purchase_receipts' in tables and item.get('binding'):
                    observed = db.execute('SELECT payload FROM purchase_receipts WHERE id=?',(id,)).fetchone()
                if (not replaceable(item,json.loads(observed[0]) if observed else None)
                        or observed and host and has_formal_document(host,*key,'receipt')):
                    binding = item.get('binding') or {}
                    task = item.get('receipt') or {}
                    raise ValueError(MESSAGE+' '+str(binding.get('orderId') or task.get('taskId') or id))
        if not purchase_ids <= seen:
            raise ValueError(MESSAGE+' 缺少原任务登记。')
