"""Read-only, frozen correspondence between a signed warehouse order and external IDs."""
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from runtime_support import checkpoint

HEADERS = ['签发单号', '聚水潭内部订单号', '翱象采购单号']
LINK_HEADERS = ['业务日期','门店编码','门店名称','供货仓',*HEADERS,'聚水潭单号来源','翱象平台状态（本机记录）','翱象关联说明','采购所据付款版本','本地采购任务']


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()


def registered(db, head):
    from payment_references import reference_key
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mappings'").fetchone():return None
    row=db.execute("SELECT payload FROM lifecycle_mappings WHERE stage='payment' AND name=?",(reference_key(head),)).fetchone()
    if not row:return None
    return verified_registration(row[0],head)


def verified_registration(raw,head):
    value=json.loads(raw)
    if any(value['head'].get(k)!=head[k] for k in ('oid','revision','warehouse','number','store','digest')):
        raise ValueError('聚水潭内部号登记与原仓单身份不符，请核对：'+head['number'])
    return value


def registrations(db,heads,cancel=None):
    from payment_references import reference_key
    from query_scope import oid_batches
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='lifecycle_mappings'").fetchone():return {}
    lookup={reference_key(h):h for h in heads};result={}
    for placeholders,keys in oid_batches(lookup):
        checkpoint(cancel)
        sql="SELECT name,payload FROM lifecycle_mappings WHERE stage='payment' AND name IN ("+placeholders+")"
        for name,raw in db.execute(sql,keys):
            head=lookup[name];result[head['number']]=verified_registration(raw,head)['number']
    return result


def local(db, head, payment):
    value=registered(db,head)
    numbers=[value['number']] if value else list(dict.fromkeys(str(r['document_number']) for r in (payment['payload'].get('raw_rows',payment['payload']['rows']) if payment else []) if r.get('document_number')))
    return {'signed_number':head['number'],'jst_numbers':numbers,'jst_source':'本机登记' if value else '付款原表' if numbers else '未提供',
            'purchases':[], 'payment_version':payment['version'] if payment else None,'payment_digest':payment['digest'] if payment else None}


def source_basis(path, id, basis):
    if basis.get('oid'):return basis
    # Older intents keep their full local identity in the frozen host package.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*',id):return None
    file=Path(path).parent/'aoxiang/purchase-packages'/id/'付款采购依据.json'
    if not file.is_file():return None
    from aoxiang_purchase import fingerprint
    plan=json.loads(file.read_text('utf-8'))
    if fingerprint(plan)!=basis.get('planFingerprint'):raise ValueError('原采购冻结依据摘要不符：'+id)
    h=plan['head']
    if any(h[k]!=basis.get(k) for k in ('number','store','warehouse')):raise ValueError('原采购与冻结仓单身份不符：'+id)
    return {**basis,'oid':h['oid'],'revision':h['revision'],'signedDigest':h['digest']}


def external(path, heads, cancel=None, *, tracking=False):
    file=Path(path).parent/'aoxiang/component-state/state.sqlite'
    result={h['number']:[] for h in heads}
    if not file.is_file() or not heads:return result
    lookup={h['number']:h for h in heads}
    with closing(sqlite3.connect(file.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'purchase_creates' not in tables:return result
        # SQLite projects only small identity/binding metadata; product payloads
        # and native Excel files are never decoded or retained for this view.
        receipt_columns="json_extract(r.payload,'$.state'),json_extract(r.payload,'$.summary.status'),json_extract(r.payload,'$.summary.observedAt')" if 'purchase_receipts' in tables else 'NULL,NULL,NULL'
        join=' LEFT JOIN purchase_receipts r ON r.id=c.id' if 'purchase_receipts' in tables else ''
        accepted=" OR json_extract(c.payload,'$.taskId') IS NOT NULL" if tracking else ''
        query=("SELECT c.id,c.state,c.platform_order,json_extract(c.payload,'$.intent.basis'),json_extract(c.payload,'$.binding'),"+
               receipt_columns+" FROM purchase_creates c"+join+" WHERE (c.platform_order IS NOT NULL"+accepted+
               ") AND json_extract(c.payload,'$.intent.basis.number') IN (SELECT value FROM json_each(?)) ORDER BY c.rowid")
        for id,state,number,raw_basis,raw_binding,receipt_state,receipt_status,observed_at in db.execute(query,(json.dumps(list(lookup)),)):
            checkpoint(cancel); basis=json.loads(raw_basis or '{}')
            head=lookup.get(basis.get('number'))
            if not head:continue
            basis=source_basis(path,id,basis)
            if not basis:continue
            if any(basis.get(k)!=head[field] for k,field in (('oid','oid'),('revision','revision'),('warehouse','warehouse'),('store','store'),('signedDigest','digest'))):continue
            binding=json.loads(raw_binding or '{}')
            if tracking and number is None:
                result[head['number']].append({'number':None,'state':state,'status':'待绑定','purchase_id':id})
                continue
            if not number or binding.get('orderId')!=number:raise ValueError('已保存翱象采购单号与关联凭证不符：'+id)
            status=binding.get('status') or '平台状态未提供'
            status=receipt_status or ('已作废' if receipt_state=='VOIDED' else status)
            result[head['number']].append({'number':str(number),'purchase_id':id,'state':state,'status':status,
                'payment_version':basis.get('paymentVersion'),'payment_digest':basis.get('paymentDigest'),
                'proof':digest(binding)})
            if tracking:result[head['number']][-1].update(observed_at=observed_at,receipt_state=receipt_state)
    return result


def token(path,db,heads,cancel=None,*,links=None):
    locals=[]
    for head in heads:
        checkpoint(cancel);locals.append([head['number'],registered(db,head)])
    return digest([locals,external(path,heads,cancel) if links is None else links])


def attach(path,db,heads,entries,cancel=None):
    links=external(path,heads,cancel)
    for entry in entries:
        checkpoint(cancel); refs=entry['references']
        for purchase in links[entry['order']]:
            current=(purchase['payment_version'],purchase['payment_digest'])==(refs['payment_version'],refs['payment_digest'])
            note='已关联' if current and purchase['state']=='BOUND' else '原付款已变化，关联待复核' if not current else '原采购关联待复核'
            refs['purchases'].append({**purchase,'note':note})
    return token(path,db,heads,cancel,links=links)


def unique(rows):
    seen=set()
    for row in rows:
        if row['order'] not in seen:seen.add(row['order']);yield row


def summary(rows):
    return [{'order':r['order'],'references':r.get('references',{})} for r in unique(rows)]


def cells(rows):
    records=list(unique(rows)); values=[[],[],[]]
    for row in records:
        refs=row.get('references',{})
        for column,value in zip(values,(refs.get('signed_number',row['order']), '、'.join(refs.get('jst_numbers',[])) or '未提供',
                                       '、'.join(dict.fromkeys(p['number'] for p in refs.get('purchases',[]))) or '未关联')):column.append(value)
    text=['\n'.join(v) for v in values]
    if any(len(v)>30000 for v in text):return [f'涉及{len(records)}张签发单；完整对应见“单号关联明细”']*3
    return text


def link_rows(rows,cancel=None):
    for row in unique(rows):
        checkpoint(cancel);refs=row.get('references',{})
        for purchase in refs.get('purchases',[]) or [{}]:
            yield [row['day'],row['store'],row['store_name'],row['warehouse'],refs.get('signed_number',row['order']),
                   '、'.join(refs.get('jst_numbers',[])) or None,purchase.get('number'),refs.get('jst_source','未提供'),
                   purchase.get('status'),purchase.get('note','尚未关联翱象采购单'),purchase.get('payment_version'),purchase.get('purchase_id')]
