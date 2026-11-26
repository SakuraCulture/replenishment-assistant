"""Read-only platform proof and durable manual handling of one frozen JST import."""
from contextlib import closing
from decimal import Decimal
from hashlib import sha256
import re
from uuid import uuid4

import jst_auto_order as automatic
import jst_auto_order_store as store
import jst_order_confirmation as confirmation
import jst_order_payment as payment
import jst_order_store as generation
from platform_import_files import author, write_original
from runtime_support import checkpoint
from sqlite_write import begin, commit
import work_repository as wr


def _read(db, online, *, workbook=False):
    row=store.read(db,online,workbook=workbook)
    if not row:
        raise automatic.Check('原导入任务未留存')
    return row


def record(path, online):
    with closing(wr.read_connection(path)) as db:
        return _read(db,online)


def order_keys(path, head):
    with closing(wr.read_connection(path)) as db:
        rows=[]
        for table in ('jst_auto_orders',store.HISTORY):
            if store.exists(db,table):
                online_ids=db.execute(f'SELECT online FROM {table} WHERE oid=? AND warehouse=? ORDER BY created DESC,rowid',
                                      (head['oid'],head['warehouse']))
                for online, in online_ids:
                    original=_read(db,online)
                    if all(original['payload']['head'].get(k)==head[k] for k in automatic.HEAD_KEYS):
                        rows.append(online)
        return rows


def _source(db, order):
    head=order['payload']['head']
    original,digest=generation.read_generation(db,order['gid'])
    if (order.get('historical') or order['stage']=='CANCELLED'
            or generation.active(db,head)!=order['gid']
            or digest!=order['payload']['generation']['digest']
            or any(original['head'].get(k)!=head[k] for k in automatic.HEAD_KEYS)):
        raise wr.Conflict('原仓单或执行轮次已变化，历史原件仍可核对')
    automatic.require_generation(db,original,digest,historical=True)


_browser = automatic.browser_session
_lookup = automatic.lookup_scoped


def _ready_for_handoff(order):
    proof=order['proof'] or {}
    receipt=proof.get('import_receipt') or {}
    return (not order.get('historical') and not order['internal'] and not proof.get('manual_proposal') and (
        order['stage'] in ('PREPARED','PREVIEWED')
        or order['stage']=='IMPORT_UNKNOWN' and receipt.get('online')==order['online']
        and receipt.get('success_count')==0 and receipt.get('fail_count')==1))


def take_over(path, online, sequence, operator, reason, owner, progress=lambda _:None, cancel=None):
    note=author(operator,reason)
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        order=_read(db,online,workbook=True)
        _source(db,order)
        existing=(order['proof'] or {}).get('manual_handoff')
        if existing and not order['internal'] and not order['proof'].get('manual_proposal'):
            return existing
        if order['sequence']!=sequence or not _ready_for_handoff(order):
            raise automatic.Check('原任务仍在处理或结果待核，请核对原单后再接管')
        if not order.get('workbook') or sha256(order['workbook']).hexdigest()!=order['file_sha']:
            raise automatic.Check('原导入文件未留存或摘要不符')
        automatic._require_inputs(db,order['payload'])
    with _browser(owner,progress,cancel) as browser:
        progress('查询原线上单号')
        row, result=_lookup(browser,order)
        if result.get('found') is not False or result.get('exact_absence') is not True:
            raise automatic.Check('未核实原线上单号无单，请先核对并关联已有原单')
        handoff={**note,'id':uuid4().hex,'at':wr.now(),'online':online,'gid':order['gid'],
                 'file_sha':order['file_sha'],'binding':row['proof']['binding'],
                 'evidence':'confirmed_failure_and_exact_absence' if order['stage']=='IMPORT_UNKNOWN' else 'unsent_and_exact_absence',
                 'import_receipt':row['proof'].get('import_receipt')}
        with closing(store.connection(path)) as db,db:
            begin(db,cancel)
            current=_read(db,online)
            _source(db,current)
            if current['sequence']!=sequence or not _ready_for_handoff(current):
                raise wr.Conflict('原任务已更新，请刷新后核对')
            automatic._require_inputs(db,current['payload'])
            proof={**row['proof'],'manual_handoff':handoff}
            store.update(db,current,'IMPORT_UNKNOWN',proof=proof,reason='人工接管，等待按原线上单号核对')
            automatic._event(db,current,'聚水潭人工接管',handoff)
            commit(db,cancel)
    return handoff


def export_original(path, online, target, usage, progress=lambda _:None, cancel=None):
    if usage not in ('check','manual'):
        raise ValueError('原件导出用途无效')
    progress('核对聚水潭原导入文件')
    with closing(store.connection(path)) as db,db:
        begin(db,cancel)
        order=_read(db,online,workbook=True)
        handoff=(order['proof'] or {}).get('manual_handoff')
        if usage=='manual':
            _source(db,order)
            automatic._require_inputs(db,order['payload'])
            if (not handoff or order['stage']!='IMPORT_UNKNOWN' or order['internal'] or order['proof'].get('manual_proposal')
                    or handoff['gid']!=order['gid'] or handoff['file_sha']!=order['file_sha']):
                raise automatic.Check('请先确认接管，已关联的原件可导出核对')
        receipt={**write_original(order['workbook'],order['file_sha'],target,'.xls',cancel),
                 'usage':usage,'handoff_id':(handoff or {}).get('id') if usage=='manual' else None}
        head=order['payload']['head']
        detail=wr.dumps({'online':online,'gid':order['gid'],**receipt})
        event='导出聚水潭原导入单'
        if not db.execute('SELECT 1 FROM jst_events WHERE oid=? AND warehouse=? AND action=? AND detail=?',
                          (head['oid'],head['warehouse'],event,detail)).fetchone():
            automatic._event(db,order,event,receipt)
        db.commit()
    return receipt


def difference_rows(order, rows):
    before={sku:(qty,price) for sku,qty,price in order['payload']['lines']}
    after={row['sku']:row for row in rows}
    output=[]
    for sku in dict.fromkeys([*before,*after]):
        qty,price=before.get(sku,(None,None))
        new=after.get(sku)
        change='新增' if qty is None else '缺行' if new is None else (
            '数量或金额变化' if (Decimal(qty),Decimal(price))!=(Decimal(new['quantity']),Decimal(new['price'])) else '一致')
        output.append({'sku':sku,'old_quantity':qty,'old_price':price,'new':new,'change':change})
    return output


def _evidence(browser, order, internal, cancel):
    row,result=_lookup(browser,order)
    found=result.get('internal')
    if (result.get('found') is not True or not isinstance(found,str) or not re.fullmatch(r'[1-9]\d{0,39}',found)
            or (internal and internal!=found) or order['internal'] not in (None,found)):
        raise automatic.Check('按原线上单号未核实唯一内部单号，请核对原单')
    detail=browser.action('payment_detail',{**row,'internal':found})
    if (detail.get('found') is not True or detail.get('online')!=order['online'] or detail.get('internal')!=found
            or detail.get('binding')!=row['proof']['binding'] or detail.get('native_status') not in confirmation.PAYMENT_STATES
            or detail.get('native_status')!=result.get('native_status')):
        raise automatic.Check('原单身份或状态变化，完整商品明细仍待核')
    rows,totals=payment.manual_items(row,detail,cancel)
    changes=difference_rows(row,rows)
    for observed in (result,detail):
        if observed.get('buyer_check') in ('mismatch','masked_conflict'):
            raise automatic.Check(confirmation.buyer_issue(observed))
    issue=confirmation.buyer_issue(detail)
    if issue:
        if detail.get('buyer_check')!='unavailable' or any(r['change']!='一致' for r in changes):
            raise automatic.Check(issue)
        payment.verify_import_items({**row,'proof':{**row['proof'],
            'amount':row['proof'].get('amount') or row['payload']['stats']['amount']}},detail,cancel)
    evidence={'internal':found,'online':order['online'],'gid':order['gid'],'order_digest':order['digest'],
              'file_sha':order['file_sha'],'binding':row['proof']['binding'],
              'status':detail['native_status'],'buyer_check':detail.get('buyer_check'),'rows':rows,'totals':totals}
    return evidence,{**result,**detail},row['proof']


def preview(path, online, sequence, internal, operator, reason, owner, progress=lambda _:None, cancel=None):
    note=author(operator,reason)
    if internal and not re.fullmatch(r'[1-9]\d{0,39}',internal):
        raise ValueError('请输入完整的聚水潭内部单号')
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        order=_read(db,online)
        _source(db,order)
        if order['sequence']!=sequence:
            raise wr.Conflict('原下单任务已变化，请刷新后核对')
    with _browser(owner,progress,cancel) as browser:
        progress('按原线上单号核对完整商品明细')
        evidence,_,proof=_evidence(browser,order,internal,cancel)
    if order['internal']:
        return {'existing':True,'evidence':evidence,'differences':difference_rows(order,evidence['rows'])}
    proposal={**note,'id':uuid4().hex,'at':wr.now(),'evidence':evidence,'digest':store.token(evidence),'sequence':sequence+1}
    with closing(store.connection(path)) as db,db:
        begin(db,cancel)
        current=_read(db,online)
        _source(db,current)
        if current['sequence']!=sequence:
            raise wr.Conflict('原下单任务已变化，请重新核对')
        stage='IMPORT_UNKNOWN' if order['stage'] not in store.RELEASED else order['stage']
        saved=store.update(db,order,stage,proof={**proof,'manual_proposal':proposal},reason=order['reason'])
        commit(db,cancel)
    return {**proposal,'sequence':saved['sequence'],'differences':difference_rows(order,evidence['rows'])}


def confirm(path, online, proposal_id, digest, owner, progress=lambda _:None, cancel=None):
    with closing(wr.read_connection(path)) as db:
        db.execute('BEGIN')
        order=_read(db,online)
        _source(db,order)
        proof=order['proof'] or {}
        bound=proof.get('manual_binding')
        if bound and bound['proposal_id']==proposal_id and bound['digest']==digest:
            return bound
        proposal=proof.get('manual_proposal') or {}
        if order['internal'] or proposal.get('id')!=proposal_id or proposal.get('digest')!=digest or order['sequence']!=proposal.get('sequence'):
            raise wr.Conflict('核对草稿已变化，请重新核对原单')
    with _browser(owner,progress,cancel) as browser:
        progress('复核原线上单号、商品与金额')
        evidence,result,proof=_evidence(browser,order,proposal['evidence']['internal'],cancel)
        if store.token(evidence)!=digest or evidence!=proposal['evidence']:
            raise wr.Conflict('平台明细或状态已变化，请重新核对后确认')
        bound={**evidence,'version':1,'digest':digest,'at':wr.now(),'operator':proposal['operator'],
               'reason':proposal['reason'],'proposal_id':proposal_id}
        proof={**proof,'manual_binding':bound}
        confirmed=automatic._record_found(path,order,result,cancel,identity_only=True,manual_proof=proof)
        if confirmed.get('historical'):
            raise wr.Conflict('原执行轮次已退役，请核对历史记录')
        return bound
