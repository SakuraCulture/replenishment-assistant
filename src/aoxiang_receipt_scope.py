"""Cheap live local eligibility for receipt UI, queue, schedules and dispatch."""
import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from lifecycle_store import decode, latest_version, version_row
from lifecycle_rules import DOCUMENT_SCHEMA
from purchase_io import number


PAYMENT_REVIEW = '待人工核对：正式付款金额已更正，请对照原采购与现款后沿原采购保存实收。'
PAYMENT_CHANGED = '原付款版本已变化，暂停沿用原采购关联'
PAYMENT_SCOPE_REVIEW = '付款商品或数量已手动更正。原采购单和实收待办保留，请按实际收货单核对登记。'


def _payment_rows(document):
    payload = document['payload']
    if (payload.get('schema') != DOCUMENT_SCHEMA or payload.get('pending')
            or not payload.get('basis', {}).get('ready') or payload.get('missing')):
        return None
    values = {}
    zero = set()
    for row in payload['rows']:
        sku = row['sku']
        quantity = number(row.get('base_quantity'))
        amount = number(row.get('amount'), optional=True)
        if (sku in values or row.get('quantity_origin') in ('missing','blank')
                or row.get('currency','') not in ('','CNY','人民币','RMB')
                or (Decimal(quantity) > 0 and amount is None)
                or (Decimal(quantity) == 0 and amount is not None and Decimal(amount) != 0)):
            return None
        values[sku] = ((Decimal(quantity), row['unit']), Decimal(amount) if amount is not None else None)
        if (Decimal(quantity) == 0 and amount is not None and Decimal(amount) == 0
                and row.get('quantity_origin') == 'zero' and row.get('quantity') is not None
                and Decimal(number(row['quantity'])) == 0 and len(row.get('sources', ())) == 1):
            zero.add(sku)
    return values, zero


def amount_review(db, head, original, current=None):
    """Only a formal amount-only correction can use the original receipt path."""
    current = latest_version(db,head,'payment',effective=True) if current is None else current
    if not current or tuple(current) == (original['version'],original['digest']):
        return None
    from jst_cancellation import read as cancellation
    if cancellation(db,head):
        return None
    old = decode(version_row(db,head,'payment',original['version']))
    new = decode(version_row(db,head,'payment',current[0]))
    if not old or old['digest'] != original['digest'] or not new or new['digest'] != current[1]:
        return None
    if old['payload']['basis'] != new['payload']['basis']:
        return None
    old_rows, new_rows = _payment_rows(old), _payment_rows(new)
    if not old_rows or not new_rows:
        return None
    before, _ = old_rows
    after, explicit_zero = new_rows
    added = after.keys() - before.keys()
    if not before or not before.keys() <= after.keys() or not added <= explicit_zero:
        return None
    delta = []
    for sku, (identity, old_amount) in before.items():
        new_identity, new_amount = after[sku]
        if identity != new_identity:
            return None
        if old_amount != new_amount:
            if old_amount is None or new_amount is None:
                return None
            delta.append([sku, str(identity[0]), str(old_amount), str(new_amount), str(new_amount-old_amount)])
    if not delta:
        return None
    totals = [sum((v[1] for v in rows.values() if v[1] is not None),Decimal(0)) for rows in (before,after)]
    return {'original':original, 'current':{'version':current[0],'digest':current[1]},
            'old_amount':str(totals[0]), 'new_amount':str(totals[1]), 'difference':str(totals[1]-totals[0]),
            'rows':delta, 'added_zero_skus':sorted(added)}


def review_stamp(change, purchase_id, order_id, manifest_sha):
    return {'original':change['original'], 'current':change['current'], 'purchase_id':purchase_id,
            'order_id':order_id, 'observation_sha':manifest_sha}


def validate_review(db, head, review):
    """Final write-lock check of the specific original and current paid facts."""
    from jst_cancellation import read as cancellation
    from work_repository import Conflict
    old = version_row(db,head,'payment',review['original']['version'])
    current = review['current']
    if (cancellation(db,head) or not old or old[2] != review['original']['digest']
            or latest_version(db,head,'payment',effective=True) != (current['version'],current['digest'])):
        raise Conflict('原采购或付款版本在实收核对后变化，请重新核对')


def assess(path, items, *, require_payment=True):
    path=Path(path).resolve();out={}
    if not path.is_file():
        return {r.get('key',r.get('id')):{'eligible':False,'reason':'本机订单数据库不存在'} for r in items}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for item in items:
            key=item.get('key',item.get('id'));basis=item.get('head') or item.get('basis') or {};reason='';head=None
            try:
                import warehouse_orders as wo
                original=basis
                if not basis.get('oid'):
                    # Legacy component intents have a frozen host package. Read
                    # one at a time; never identify a recreated order by number alone.
                    import json
                    from aoxiang_purchase import fingerprint
                    id=item.get('purchase_id') or item.get('id') or item.get('key')
                    if not id or '/' in id or '\\' in id:raise ValueError('原采购缺少有效本机身份')
                    proof=path.parent/'aoxiang/purchase-packages'/id/'付款采购依据.json'
                    try:plan=json.loads(proof.read_text('utf-8'))
                    except (OSError,ValueError):raise ValueError('旧采购缺少原冻结订单身份，不能按同号新订单继续') from None
                    if fingerprint(plan)!=original.get('planFingerprint'):raise ValueError('原冻结采购依据摘要不符')
                    h=plan['head']
                    if any(h[k]!=original.get(k) for k in ('number','store','warehouse')):raise ValueError('原冻结订单与采购身份不符')
                    basis={**original,'oid':h['oid'],'revision':h['revision'],'signedDigest':h['digest'],'visibilityToken':h.get('visibility_token',0)}
                if 'normal_orders' not in tables:raise ValueError('本机原订单已删除')
                parent,warehouse=wo.resolve(db,basis.get('number',''))
                if warehouse!=basis.get('warehouse') or parent[1]!=basis.get('store'):raise ValueError('原采购关联的门店或仓单身份不符')
                row=db.execute('SELECT oid,current FROM normal_orders WHERE number=?',(parent[2],)).fetchone()
                oid,revision=row
                if basis.get('oid') and basis['oid']!=oid:raise ValueError('原订单身份已失效，不能按同号重建的订单继续')
                import warehouse_execution as execution
                child=execution.state(db,oid,warehouse)
                if child is not None:
                    if child['state']!='active':raise ValueError('本机原仓单已删除或本轮未参与')
                    revision=child['source_revision']
                if basis.get('revision') is not None and basis['revision']!=revision:raise ValueError('原签发修订已失效')
                if 'archive_visibility' in tables:
                    deleted=db.execute('SELECT deleted,token FROM archive_visibility WHERE oid=? AND warehouse=?',(oid,warehouse)).fetchone()
                    if deleted and deleted[0]:raise ValueError('本机原仓单已移入回收站')
                    if basis.get('visibilityToken') is not None and (deleted[1] if deleted else 0)!=basis['visibilityToken']:raise ValueError('原仓单可见性已改变，请重新核对原关联')
                digest=db.execute('SELECT identity_digest FROM warehouse_execution_revisions WHERE oid=? AND warehouse=? AND source_revision=?',(oid,warehouse,revision)).fetchone() if child is not None else db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(oid,revision)).fetchone()
                if not digest or basis.get('signedDigest') and digest[0]!=basis['signedDigest']:raise ValueError('原签发快照已改变')
                head={'oid':oid,'revision':revision,'warehouse':warehouse,'store':parent[1],'number':basis['number'],'day':parent[0],
                      'current_revision':revision,'digest':digest[0],
                      **({'execution_epoch':basis.get('executionEpoch',child['epoch'])} if child is not None else {}),
                      **{k:basis[k] for k in ('signedDigest','visibilityToken') if k in basis}}
                from work_repository import Conflict
                from deletion_barrier import SourceDeleted
                try:execution.validate(db,head)
                except (Conflict,SourceDeleted) as exc:raise ValueError(str(exc)) from exc
                if require_payment:
                    if 'lifecycle_versions' not in tables:raise ValueError('原已确认付款依据不存在')
                    payment=latest_version(db,head,'payment',effective=True)
                    pv=basis.get('paymentVersion');pd=basis.get('paymentDigest')
                    if not payment or pv is None or not pd:raise ValueError('原采购缺少可复核的付款版本，需核对原任务')
                    if tuple(payment)!=(pv,pd):
                        from jst_cancellation import read as cancellation_case
                        if cancellation_case(db,head):
                            out[key]={'eligible':False,'head':head,'retain_receipt':True,
                                      'reason':'原采购对应的付款版本已变化。原单与实收待办保留，请按原采购和原付款核对实收。'}
                            continue
                        review = amount_review(db,head,{'version':pv,'digest':pd},payment)
                        if review:
                            out[key]={'eligible':True,'head':head,'payment_review':review,'reason':PAYMENT_REVIEW}
                            continue
                        original=version_row(db,head,'payment',pv)
                        latest=decode(version_row(db,head,'payment',payment[0]))
                        if original and original[2]==pd and latest and latest['payload'].get('manual_correction'):
                            out[key]={'eligible':False,'head':head,'retain_receipt':True,'reason':PAYMENT_SCOPE_REVIEW}
                            continue
                        raise ValueError(PAYMENT_CHANGED)
            except (KeyError,TypeError,IndexError):reason='原关联身份资料不完整，请核对原任务，不能按同号新单继续'
            except ValueError as exc:reason=str(exc)
            out[key]={'eligible':not reason,'reason':reason,'head':head}
    return out


def require(path, items):
    states=assess(path,items)
    missing=[str(k)+'：'+v['reason'] for k,v in states.items() if not v['eligible']]
    if missing:raise ValueError('以下采购单的本地依据不再有效，未执行同步：\n'+'\n'.join(missing))
    return states


def latest(record, receipt=None):
    """Latest verified observation wins; failed queries never replace evidence."""
    receipt=receipt or {};summary=receipt.get('summary') or {}
    status=summary.get('status') or (record.get('binding') or {}).get('status') or '尚未查询'
    return {**record,'receipt_terminal':bool(receipt.get('terminal')) or status in ('已完成','已作废'),
            'receipt_state':receipt.get('state','未查询'),'platform_status':status,
            'observed_at':summary.get('observedAt') or (record.get('binding') or {}).get('verifiedAt'),
            'receipt_summary':summary}


def formal_receipt_exists(path,basis):
    state=assess(path,[{'key':'item','basis':basis}]).get('item')
    if not state or not (state['eligible'] or state.get('retain_receipt')):return False
    h=state['head'];path=Path(path).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        return db.execute("SELECT 1 FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='receipt' LIMIT 1",(h['oid'],h['revision'],h['warehouse'])).fetchone() is not None


def saved_observations(folder,ids):
    """Small local projection, shared by batch/status views; never opens a browser."""
    import json
    path=Path(folder)/'component-state/state.sqlite';out={}
    if not path.is_file() or not ids:return out
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='purchase_receipts'").fetchone():return out
        for id in set(ids):
            row=db.execute('SELECT payload FROM purchase_receipts WHERE id=?',(id,)).fetchone()
            if row:
                record=json.loads(row[0])
                if record.get('summary'):out[id]={**record['summary'],'receipt_state':record['state'],'hostConflict':record.get('hostConflict')}
    return out


def restorable_payment_receipts(folder, schedules):
    """Confirm old source-retirement projections against the same native PO."""
    path = Path(folder)/'component-state/state.sqlite'
    if not schedules or not path.is_file():
        return {}
    observed = {}
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('BEGIN')
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'purchase_creates','purchase_receipts'} <= tables:
            return {}
        for schedule in schedules:
            last_error = schedule.get('last_error') or {}
            retired = (schedule.get('retired_reason') == PAYMENT_CHANGED if schedule.get('retired') else
                       (last_error.get('code'),last_error.get('message')) == ('RECEIPT_SOURCE_RETIRED',PAYMENT_CHANGED))
            if (not retired or schedule.get('last_state') in ('SYNCED','VOIDED','VOIDED_REVIEW')
                    or (schedule.get('summary') or {}).get('status') == '已作废'):
                continue
            row = db.execute('''SELECT p.payload,r.payload FROM purchase_creates p
                JOIN purchase_receipts r ON r.id=p.id
                WHERE p.id=? AND p.state='BOUND' AND p.platform_order=?''',
                             (schedule['id'],schedule['order_id'])).fetchone()
            if not row:
                continue
            original, receipt = map(json.loads,row)
            intent, scope = original.get('intent') or {}, receipt.get('scope') or {}
            if (original.get('id') != schedule['id'] or receipt.get('id') != schedule['id']
                    or intent.get('basis') != schedule['basis'] or receipt.get('basis') != schedule['basis']
                    or (original.get('binding') or {}).get('orderId') != schedule['order_id']
                    or scope.get('orderId') != schedule['order_id'] or scope.get('store') != schedule['basis']['store']
                    or scope.get('identity') != intent.get('identity') or scope.get('supplier') != intent.get('supplierCode')
                    or receipt.get('state') in ('SYNCED','VOIDED','VOIDED_REVIEW')
                    or receipt.get('terminalKind') == 'VOIDED' or (receipt.get('summary') or {}).get('status') == '已作废'
                    or receipt.get('hostReceipt') or receipt.get('hostConflict')):
                continue
            observed[schedule['id']] = receipt
    return observed


def tracking_observations(path,heads):
    """Read the tiny host projection; never scan component intents or snapshots."""
    import json
    file=Path(path).parent/'aoxiang/workflows.sqlite3';out={}
    if not file.is_file() or not heads:return out
    by_key={(h['oid'],h['revision'],h['warehouse']):h for h in heads}
    with closing(sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='ax_schedules'").fetchone():return out
        for row in db.execute('SELECT payload FROM ax_schedules'):
            value=json.loads(row[0]);h=value.get('local_head') or {};key=(h.get('oid'),h.get('revision'),h.get('warehouse'))
            if key not in by_key or value.get('retired') or not (value.get('summary') or value.get('payment_review')):continue
            summary=value.get('summary') or {};old=out.get(key)
            rank = lambda r: (bool(r.get('payment_review')),r.get('observedAt') or '')
            if not old or rank({**summary,'payment_review':value.get('payment_review')}) > rank(old):
                out[key]={'status':'尚未查询',**summary,'orderId':value['order_id'],'receipt_state':value.get('last_state'),
                          'payment_review':bool(value.get('payment_review')), 'purchase_id':value['id']}
    return out
