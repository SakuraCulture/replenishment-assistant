"""Read current lifecycle facts without preparing or saving documents.

Callers own any supplied database connection and snapshot. Read proofs only
reuse an already decoded projection after authenticating the current row.
"""
import hashlib
from decimal import Decimal

import archive_orders as ao
import jst_order_store as jst_orders
import lifecycle_routes as flow
import lifecycle_rules as rules
import lifecycle_store as store
import merchant_feedback
import result_semantics as sem
import warehouse_execution as execution
import work_repository as wr
from lifecycle_store import decode, exists
from normal_orders import read_repository as repository
from order_keys import warehouse_revision as key

SCHEMA = rules.DOCUMENT_SCHEMA


def current(db, head, stage, version=None,*,as_of=None,_proofs=None,_verified=None,include_superseded=False):
    if not exists(db): return None
    row=store.version_row(db, head, stage, version, as_of=as_of)
    if (row and version is None and not as_of and not include_superseded
            and not store.version_is_current(db,head,stage,row[0])):
        return None
    if type(_verified) is dict and row:
        from lifecycle_read_proofs import reuse
        reused=reuse(head,stage,row,_verified.get(stage))
        if reused is not None:
            if _proofs is not None:_proofs[stage]=_verified[stage]
            return reused
    document=decode(row)
    if stage==rules.JST_ORDER and document and not as_of:
        if document['payload']['reference']['gid']!=jst_orders.active(db,head):
            return None
    if _proofs is not None and document:
        from lifecycle_read_proofs import make
        proof=make(head,stage,row,document)
        if proof is not None:_proofs[stage]=proof
    return document


def _ready(document, tokens):
    if not document:
        return False
    payload=document['payload']
    return (payload.get('schema')!=SCHEMA
            or not payload.get('pending') and payload.get('basis',{}).get('dependencies')==tokens)


def upstream(db, head, stage, staged=None,*,_proofs=None,_verified=None):
    """Freeze every dependency; absence is also a token checked at commit."""
    route=flow.stages(db,head,staged,including=stage)
    index=route.index(stage); tokens={}; previous=None; valid=True
    for s in route[:index]:
        doc=(staged or {}).get((*key(head),s)) or current(db,head,s,_proofs=_proofs,_verified=_verified)
        if doc is None and s=='return':
            legacy=legacy_return(db,head,head['products'])
            if legacy and legacy['complete']:
                doc={'version':legacy['version'],'digest':legacy['digest'],'payload':{'schema':1,'rows':[{'sku':k,'name':head['products'][k].get('name'),'base_quantity':str(v)} for k,v in legacy['values'].items()]}}
        if s=='payment' and stage=='receipt' and valid:
            if not _ready(doc,tokens) or not any(
                    r.get('base_quantity') is not None and Decimal(str(r['base_quantity']))>0
                    for r in doc['payload']['rows']):
                order=current(db,head,rules.JST_ORDER)
                if _ready(order,tokens):
                    doc,s=order,rules.JST_ORDER
        valid=valid and _ready(doc,tokens)
        tokens[s]=rules.basis_token(doc)
        previous=doc
    products=head['products'] if stage=='return' else rules.positive(previous['payload']['rows']) if previous and valid else {}
    return {'stage':next(reversed(tokens)) if tokens else 'signed', 'dependencies':tokens, 'signed_digest':head['digest'], 'ready':stage=='return' or valid, 'products':products}


def legacy_return(db, head, products,*,as_of=None):
    return legacy_return_record(merchant_feedback.current(db,head['oid'],head['revision'],as_of=as_of),products)


def legacy_return_record(old, products):
    if not old: return None
    values = old['payload'].get('values', {}); required = [s for s, p in products.items() if p['quantity'] > 0]
    supplied = {s: q for s, q in values.items() if s in products}
    complete = bool(supplied) and all(s in supplied for s in required)
    return {'version': old['version'], 'saved': old['saved'], 'complete': complete, 'values': supplied, 'legacy': True,'digest':hashlib.sha256(wr.dumps(supplied).encode()).hexdigest()}


def full_head(path,head):
    if 'products' in head:return head
    order=execution.get_order(repository(path),head,view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照已变化')
    result=order['payload']['result']
    products={d['E']:{'sku':d['E'],'name':d.get('D'),'quantity':sem.order_quantity(d),'unit':d.get('G'),'price':d.get('V')} for rs in sem.signed_groups(result,order['payload'].get('new_focus')).values() for d in rs if ao.warehouse(d)==head['warehouse']}
    return {**head,'products':products}


def match_payment_rows(path, head, rows):
    if not any(r.get('adapter')=='paid_order' for r in rows):return
    identities={tuple(str(r['source_order'].get(k) or '') for k in ('内部订单号','线上订单号','店铺名称','买家账号','平台站点')) for r in rows if r.get('adapter')=='paid_order'}
    if len(identities)!=1:raise ValueError('付款文件含多个来源订单/店铺，请分别导入，不自动合并')
    order=execution.get_order(repository(path),head,view='export')
    if order['digest']!=head['digest']:raise wr.Conflict('签发快照已变化')
    for row in rows:
        if row.get('adapter')!='paid_order':continue
        original=row['sku']
        if original in head['products']:
            row['matching_basis']='商品编码精确匹配'
        else:row['matching_basis']='清单外商品，需确认'
