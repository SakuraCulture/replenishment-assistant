"""Read-only platform references for the warehouse-order tracking list."""
import sqlite3

from analytics_identifiers import external,registrations
from lifecycle_stages import SHIPMENT_CHECK, WAITING_SHIPMENT
from runtime_support import checkpoint


def payment_numbers(document):
    """Only the native payment sheet identifies JST's internal order number."""
    payload=document['payload']
    return list(dict.fromkeys(str(row['document_number']) for row in payload.get('raw_rows',payload['rows'])
                              if row.get('adapter')=='paid_order' and row.get('document_number')))


def matches(record,terms,fields=('day','store','name','warehouse','number')):
    tracking=record.get('platform_tracking',{})
    haystack=' '.join([*(str(record[k]) for k in fields),*tracking.get('jst_numbers',[]),*tracking.get('ax_numbers',[])]).lower()
    return all(term in haystack for term in terms)


def attach(path,db,records,cancel=None):
    if not records:return
    local_errors=[]
    try:registered=registrations(db,records,cancel)
    except (sqlite3.Error,ValueError,KeyError,TypeError):
        registered={};local_errors.append('聚水潭登记需核对')
    try:links=external(path,records,cancel,tracking=True)
    except (sqlite3.Error,OSError,ValueError,KeyError,TypeError):
        links={};local_errors.append('翱象关联读取待核')
    for head in records:
        checkpoint(cancel)
        tracking=head['platform_tracking'];number=head['number']
        if number in registered:tracking.update(jst_numbers=[str(registered[number])],jst_source='本机登记')
        elif tracking['jst_numbers']:tracking['jst_source']='聚水潭原生付款表'
        else:tracking['jst_source']='未提供'
        purchases=links.get(number,[])
        tracking['ax_numbers']=list(dict.fromkeys(p['number'] for p in purchases if p['number']))
        tracking['await_binding']=any(not p['number'] for p in purchases)
        statuses=[];notes=list(local_errors)
        for p in purchases:
            if not p['number']:continue
            status=p['status']
            if (p['payment_version'],p['payment_digest'])!=tracking['payment_basis'] or p['state']!='BOUND':
                statuses.append('关联待核');notes.append(p['number']+' · 原采购关联或付款依据待核')
            elif status=='已作废':statuses.append('已作废')
            elif p['receipt_state'] in ('NEEDS_REVIEW','VOIDED_REVIEW'):statuses.append('实收待核')
            elif status=='已完成':statuses.append('已完成待同步')
            elif p['observed_at']:statuses.append(WAITING_SHIPMENT if head.get('next')==SHIPMENT_CHECK else '待收货')
            else:statuses.append(status if status=='待提交' else '尚未查询')
            notes.append(p['number']+' · '+status+(' · 观察 '+p['observed_at'] if p['observed_at'] else ''))
        if tracking['receipt_saved']:
            if head['health'].get('receipt'):
                state='实收已归档'+(' · 作废待核' if '已作废' in statuses else '')
            else:state='实收待核'
        elif statuses:state='、'.join(dict.fromkeys(statuses))
        elif tracking['await_binding']:state='待绑定'
        else:state='收货记录待核' if local_errors else '未记录收货'
        tracking['receipt_status']=state
        tracking['detail']='\n'.join(dict.fromkeys(notes))
