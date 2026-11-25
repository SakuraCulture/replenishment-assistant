"""Special-order guards and group receipts at the existing native platform boundaries."""
import json
from pathlib import Path

import jst_allocation
import lifecycle_store
import special_plans as sp
import special_settlement as settle
import warehouse_catalog
import work_repository as wr


def remember_jst(db,order,reply):
    if not sp.exists(db,'special_packages'):
        return
    oid=order['payload']['head']['oid']
    row=db.execute('SELECT 1 FROM special_packages WHERE oid=? AND gid=? AND online=?',
                   (oid,order['gid'],order['online'])).fetchone()
    if not row or not reply.get('found'):
        return
    p=settle.package(db,oid)
    b=reply.get('binding') or {}
    if (reply.get('internal')!=order['internal'] or reply.get('online')!=p['online']
            or (b.get('warehouse'),str(b.get('coid')),str(b.get('shop_id')))!=tuple(p['scope'][k] for k in ('warehouse','account','shop'))):
        raise ValueError('聚水潭原单回读的身份与冻结执行包不一致')
    rows=[{k:r.get(k) for k in ('sku','qty','amount')} for r in reply.get('items',())]
    doc=dict(internal=order['internal'],online=p['online'],gid=p['gid'],binding=b,status=reply.get('native_status'),
             rows=rows,final=reply.get('final_allocations') or [])
    sp.record(db,oid,'聚水潭原单回读',doc,'聚水潭原单查询')


def guard_jst(db,path,order):
    if not sp.exists(db,'special_packages'):
        return
    oid=order['payload']['head']['oid']
    row=db.execute('SELECT plan FROM special_packages WHERE oid=?',(oid,)).fetchone()
    if not row:
        return
    p=settle.package(db,oid)
    version=db.execute('SELECT version FROM special_runs WHERE run=?',(p['run'],)).fetchone()[0]
    frozen=sp.read(db,p['plan'],version)
    current=sp.read(db,p['plan'])
    if current['version']!=version or not sp.enabled(db,frozen['kind']):
        raise ValueError('专项已修订或暂停执行，请核对原包及未发余量')
    ids={c['line'] for i in p['items'] for c in i['claims']}
    stamp=sp.instant(sp.now())
    if any(not sp.in_window(frozen,r,stamp) for r in frozen['lines'] if r['id'] in ids):
        raise ValueError('原执行包已过窗，只能查询原任务')
    b=(order.get('proof') or {}).get('binding') or {}
    if str(b.get('coid') or '')!=p['scope']['account'] or str(b.get('shop_id') or '')!=p['scope']['shop'] or b.get('warehouse')!=p['scope']['warehouse']:
        raise ValueError('聚水潭预览账号、店铺或仓与冻结发布范围不符')
    wh=p['scope']['warehouse']
    cat=warehouse_catalog.Repository(Path(path).parent).snapshots((wh,))[0].get(wh) or {}
    items=cat.get('items',{})
    for r in p['items']:
        item=items.get(r['sku'])
        if not item or jst_allocation.status_issue(item):
            raise ValueError('原包有缺档或禁用商品，请复核 '+r['sku'])
    local=sp.latest(db,oid,'原单')
    if local:
        raise ValueError('该包已有原单依据，只能回查已有聚水潭原号')


def merge_purchase(db,plan):
    g=settle.group_for_oid(db,plan['head']['oid'])
    if not g:
        return
    frozen=sp.latest(db,g,'采购预览')
    if not frozen or frozen['body']['inputs'][0]['oid']!=plan['head']['oid']:
        raise ValueError('请从新店开业工作台核对并继续同一采购组')
    data=frozen['body']
    validate_group(db,plan,g,frozen['sha'],data)
    plan.update({k:data[k] for k in ('total_quantity','total_amount','payment_total_amount','rounding_delta')})
    plan['rows']=data['rows']
    plan['opening_group']=dict(id=g,sha=frozen['sha'],version=frozen['version'],inputs=data['inputs'],
                               freight=data['freight'],adjustment=data['adjustment'])


def validate_group(db,plan,group,sha,data=None):
    if data is None:
        frozen=sp.latest(db,group,'采购预览')
        if not frozen or frozen['sha']!=sha:
            raise ValueError('采购组预览已更新')
        data=frozen['body']
    if (plan['account_binding']['merchant_code'],plan['supplier_code'],plan['head']['store'],plan['head']['warehouse'])!=tuple(data['scope'][k] for k in ('merchant','supplier','store','warehouse')):
        raise ValueError('当前翱象商家、供应商、收货店或供货仓与采购组不一致')
    if sp.digest(settle.assemble(db,data['plan'],data['scope']))!=sha:
        raise ValueError('采购组子包付款或实配版本已变化，请重新核对')


def sync_group(path,package,manifest,configuration,suppliers):
    from aoxiang_purchase import validate_original_context
    from aoxiang_receipts import checked_json,frozen_purchase,validate_observation
    intent,frozen=frozen_purchase(package)
    o=validate_observation(checked_json(manifest['file'],manifest['sha256']),intent)
    group=frozen['opening_group']['id']
    rows=[dict(sku=r['sku'],unit=r['unit'],quantity=r['receivedQuantity'],amount=r['receivedAmount']) for r in o['rows']]
    with sp.connection(path,True) as db:
        validate_original_context(db,frozen,configuration,suppliers)
        for f in [*o['files'],manifest]:
            import hashlib
            raw=Path(f['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=f['sha256']:
                raise ValueError('翱象原件摘要不符')
            db.execute('INSERT OR IGNORE INTO lifecycle_files VALUES(?,?,?)',(f['sha256'],Path(f['file']).name,raw))
        posted=settle.post(db,group,intent['id'],o['orderId'],rows,manifest['sha256'],'翱象原采购实收')
        return lifecycle_store.save_receipt(frozen['head'],'receipt',posted['version'],True,posted['sha'])


def posted_group(db,head,index,record,group):
    from aoxiang_receipts import frozen_purchase,validate_observation
    if not sp.latest(db,group,'正式实收'):
        return None
    intent,frozen=frozen_purchase(json.loads(index.read_text('utf-8')))
    first=sp.latest(db,group,'正式实收',1)
    sha=(record.get('manifest') or {}).get('sha256')
    raw=lifecycle_store.file_row(db,sha)
    if (not raw or intent['id']!=record['id'] or intent['basis']!=record['basis'] or not sha or
        (frozen.get('opening_group') or {}).get('id')!=group or first['body']['evidence']!=sha):
        raise ValueError('组级原采购或原实收回执无法核对')
    import hashlib
    if hashlib.sha256(raw[0]).hexdigest()!=sha:
        raise ValueError('已存实收原件摘要不符')
    observed=validate_observation(json.loads(raw[0]),intent)
    if observed['orderId']!=record['scope']['orderId'] or first['body']['original']!=observed['orderId']:
        raise ValueError('原采购号不一致')
    return lifecycle_store.save_receipt(head,'receipt',first['version'],False,first['sha'])


def remember_ack(path,record):
    oid=(record.get('basis') or {}).get('oid')
    if not oid:
        return
    with sp.connection(path) as db:
        group=settle.group_for_oid(db,oid)
    if group:
        with sp.connection(path,True) as db:
            settle.acknowledge(db,group,record['id'],record['scope']['orderId'],record['manifest']['sha256'])
