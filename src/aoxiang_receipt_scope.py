"""Cheap live local eligibility for receipt UI, queue, schedules and dispatch."""
import sqlite3
from contextlib import closing
from pathlib import Path


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
                if basis.get('revision') is not None and basis['revision']!=revision:raise ValueError('原签发修订已失效')
                if 'archive_visibility' in tables:
                    deleted=db.execute('SELECT deleted,token FROM archive_visibility WHERE oid=? AND warehouse=?',(oid,warehouse)).fetchone()
                    if deleted and deleted[0]:raise ValueError('本机原仓单已移入回收站')
                    if basis.get('visibilityToken') is not None and (deleted[1] if deleted else 0)!=basis['visibilityToken']:raise ValueError('原仓单可见性已改变，请重新核对原关联')
                if basis.get('signedDigest'):
                    digest=db.execute('SELECT digest FROM normal_revisions WHERE oid=? AND revision=?',(oid,revision)).fetchone()
                    if not digest or digest[0]!=basis['signedDigest']:raise ValueError('原签发快照已改变')
                head={'oid':oid,'revision':revision,'warehouse':warehouse,'store':parent[1],'number':basis['number'],'day':parent[0],
                      **{k:basis[k] for k in ('signedDigest','visibilityToken') if k in basis}}
                if require_payment:
                    if 'lifecycle_versions' not in tables:raise ValueError('原已确认付款依据不存在')
                    payment=db.execute("SELECT version,digest FROM lifecycle_versions WHERE oid=? AND revision=? AND warehouse=? AND stage='payment' ORDER BY version DESC LIMIT 1",(oid,revision,warehouse)).fetchone()
                    pv=basis.get('paymentVersion');pd=basis.get('paymentDigest')
                    if not payment or pv is None or not pd:raise ValueError('原采购缺少可复核的付款版本，需核对原任务')
                    if tuple(payment)!=(pv,pd):raise ValueError('原付款版本已变化，暂停沿用原采购关联')
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
    if not state or not state['eligible']:return False
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
            if key not in by_key or value.get('retired') or not value.get('summary'):continue
            summary=value['summary'];old=out.get(key)
            if not old or summary.get('observedAt','')>old.get('observedAt',''):
                out[key]={**summary,'orderId':value['order_id'],'receipt_state':value.get('last_state')}
    return out
