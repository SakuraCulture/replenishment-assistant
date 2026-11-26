"""Reconcile a verified original import and retain the evidence for its remainder."""
import hashlib
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import lifecycle_store
import purchases
import work_repository as wr
from aoxiang_receipts import checked_json
from platform_import_files import author
from purchase_io import code, number
from runtime_support import checkpoint
from sqlite_write import begin


ACTION = '保存平台差额核对'


def _digest(value):
    return hashlib.sha256(wr.dumps(value).encode('utf-8')).hexdigest()


def _indexed(rows):
    values={}
    for row in rows:
        sku=code(row['sku'])
        if sku in values:
            raise ValueError('平台明细有重复商品编码，须核对来源及单位：'+sku)
        values[sku]={**row,'quantity':number(row['quantity'])}
    return values


def compare(original, actual, *, eligible, require_unit):
    """Report exact base-quantity differences without crossing units or SKUs."""
    original,actual=_indexed(original),_indexed(actual)
    rows=[]
    for sku, before in original.items():
        observed=actual.get(sku)
        wanted=Decimal(before['quantity']); done=Decimal(observed['quantity']) if observed else Decimal(0)
        old_unit=before.get('unit'); actual_unit=observed.get('unit') if observed else None
        if require_unit and (not old_unit or (observed and old_unit!=actual_unit)):
            remainder=None; state='基础单位待核'
        else:
            remainder=format(max(wanted-done,Decimal(0)),'f')
            state=('平台超出原量' if done>wanted else '已覆盖原量' if done==wanted else
                   '待确认未入单数量' if eligible else '待核 SKU 级失败证据')
        rows.append({'sku':sku,'request':str(wanted),'actual':str(done),'unit':old_unit,'actual_unit':actual_unit,
                     'remainder':remainder,'status':state})
    for sku, row in actual.items():
        if sku not in original:
            rows.append({'sku':sku,'request':None,'actual':row['quantity'],'unit':None,'actual_unit':row.get('unit'),
                         'remainder':None,'status':'平台新增商品'})
    return rows


def aoxiang(path, head, package, record, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    binding=record.get('binding') or {}; result=record.get('importResult') or {}
    proposal=record.get('bindingProposal') or {}; manifest=proposal.get('manifest') or {}
    if (record.get('state')!='BOUND' or not binding.get('manualRevision') or proposal.get('state')!='CONFIRMED'
            or proposal.get('id')!=binding.get('proposalId') or manifest.get('sha256')!=binding.get('proofSha256')):
        raise ValueError('先核对并确认关联原采购单，再查看差额')
    if record.get('additionalTaskIds'):
        raise ValueError('原提交存在多个受理号，先核对原任务')
    intent=checked_json(package['package_file'],package['package_sha256'])
    proof=checked_json(manifest['file'],manifest['sha256'])
    evidence=proof['evidence']; native=evidence['main']; basis=intent['basis']
    if (intent['id']!=record['id'] or proof.get('purchaseId')!=record['id'] or record['basis']!=basis
            or proof['basis']!=basis or native['orderId']!=binding['orderId'] or native['status']=='已作废'
            or native['store']!=head['store'] or native['supplier']!=intent['supplierCode']
            or native['marker']!=intent['note'] or proof.get('uniqueMarkedOrders')!=[binding['orderId']]
            or (basis['oid'],basis['warehouse'],basis['revision'],basis['signedDigest'])!=
               (head['oid'],head['warehouse'],head['revision'],head['digest'])):
        raise wr.Conflict('原仓单、采购执行或平台原单归属不一致')
    if any((r['orderId'],r['store'],r['supplier'],r['note']) !=
           (binding['orderId'],head['store'],intent['supplierCode'],intent['note']) for r in evidence['rows']):
        raise wr.Conflict('采购商品与原单归属不一致')
    files=evidence.get('files',[])
    if [item.get('source') for item in files]!=['D16','D17']:
        raise wr.Conflict('原采购主单及商品明细文件未齐全')
    for item in files:
        checkpoint(cancel)
        with Path(item['file']).open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=item['sha256']:
                raise wr.Conflict('平台采购原件已变化，请核对原采购任务')
    units=proof['request']['original_units']
    original=[{**r,'unit':units.get(r['sku'])} for r in intent['rows']]
    eligible=result.get('kind')=='partial' and result.get('taskId')==record.get('taskId') and bool(result.get('failCount'))
    with closing(purchases.connect(path)) as db:purchases.validate_head(db,head)
    return {'head':head,'platform':'翱象','stage':'payment','date':evidence['observedAt'],
            'source':record['id'],'platform_order':binding['orderId'],'task':record.get('taskId'),
            'basis_version':basis['paymentVersion'],'evidence_sha':manifest['sha256'],
            'rows':compare(original,evidence['rows'],eligible=eligible,require_unit=True)}


def jst(path,online,progress=lambda _:None,cancel=None):
    checkpoint(cancel)
    from jst_manual_order import record
    from jst_auto_order_store import token
    order=record(path,online); head=order['payload']['head']
    old=(order.get('proof') or {}).get('manual_binding') or {}
    proof={k:v for k,v in old.items() if k not in ('version','digest','at','operator','reason','proposal_id')}
    if (not old or order.get('historical') or order['stage']=='CANCELLED' or old.get('digest')!=token(proof)
            or (old['internal'],old['online'],old['gid'],old['order_digest'])!=
               (order['internal'],online,order['gid'],order['digest'])):
        raise wr.Conflict('先核对并确认关联原聚水潭仓单')
    # The current JST import receipt counts orders. It does not prove failed SKUs.
    original=[{'sku':sku,'quantity':quantity,'unit':'库存单位'} for sku,quantity,_ in order['payload']['lines']]
    return {'head':head,'platform':'聚水潭','stage':'jst_order','date':old['at'],
            'source':online,'platform_order':old['internal'],'task':order['gid'],'basis_version':old['version'],
            'evidence_sha':old['digest'],'rows':compare(original,old['rows'],eligible=False,require_unit=False)}


def save(path, report, operator, reason, progress=lambda _:None, cancel=None):
    checked=author(operator,reason);head=report['head'];stage=report['stage']; version=report['basis_version']
    detail={k:v for k,v in report.items() if k!='head'}
    identity=_digest(detail)
    with closing(purchases.connect(path)) as db,db:
        begin(db,cancel);purchases.validate_head(db,head);lifecycle_store.schema(db)
        args=(*lifecycle_store.key(head), stage, version, ACTION,identity)
        old=db.execute('SELECT detail FROM lifecycle_events WHERE oid=? AND revision=? AND warehouse=? AND stage=? '
                       "AND version=? AND action=? AND json_extract(detail,'$.id')=?",args).fetchone()
        if old:return wr.loads(old[0])
        result={**detail,**checked,'id':identity,'at':wr.now()}
        lifecycle_store.insert_event(db,result['at'],head,stage,version,ACTION,wr.dumps(result))
        checkpoint(cancel)
    return result
