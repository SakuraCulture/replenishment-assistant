"""Shared local task evidence rules. No browser calls or business writes."""
import copy
from aoxiang_purchase_messages import waiting, voided

ACTIVE = ('QUEUED', 'RUNNING', 'YIELDED', 'PAUSED')
SUCCESS = ('PREPARED', 'BOUND', 'SYNCED')


def local_preparation(item, kind):
    """Only preparation which never reached a durable purchase identity is local."""
    result = item.get('result') or {}
    code = (item.get('error') or {}).get('code', '')
    evidence = ('purchase_id','order_id','orderId','uploadAttempted','binding','taskId','receipt','additionalTaskIds')
    if kind != 'prepare' or any(record.get(k) for record in
            (item, result, item.get('purchase_observation') or {}) for k in evidence):
        return False
    # reserve() rejects a DIFFERENT intent before inserting it. A frozen local
    # package is not a reservation; the original intent remains independently guarded.
    if code == 'PURCHASE_ALREADY_RESERVED' and item['state'] in ('REVIEW','FAILED','SKIPPED'):
        return True
    return not item.get('package') and not code.startswith(('SUBMISSION_', 'UPLOAD_', 'PURCHASE_'))


def can_end_local(job, item):
    return (job['state'] != 'RUNNING' and item['state'] in ('PENDING','FAILED','REVIEW')
            and local_preparation(item, job['kind']))


def deletion_issue(job, item):
    """Browser disconnected is never evidence that an upload was not accepted."""
    if job['state'] == 'RUNNING' or item['state'] == 'RUNNING':
        return '先停止', '批次仍在执行，请等待当前动作安全退出'
    if item['state']=='SKIPPED' and item.get('ended_locally') and local_preparation(item,job['kind']):
        return None
    if job['state'] in ACTIVE:
        return ('结束本地任务', '未执行完的本地准备，可明确结束后重新核对') if can_end_local(job,item) else (
            '先处理任务', '批次仍可继续执行，请打开原任务安全结束或核对')
    if local_preparation(item,job['kind']):
        if item['state'] in ('FAILED','REVIEW','SKIPPED') or (job['state']=='CANCELLED' and item['state']=='PENDING'):
            return None
        return '结束本地任务', '本地准备尚未结束，请先明确结束'
    if item['state'] in ('BOUND','SYNCED','OBSERVED','PREPARED','SKIPPED'):
        return None
    return '平台结果待核', '已有采购身份或平台任务，结果尚未核实；不能按未上传清理'


def counts(job):
    items=job['items']
    return {'total':len(items),'success':sum(i['state'] in SUCCESS and not voided(i) for i in items),
            'voided':sum(voided(i) for i in items),
            'review':sum(i['state'] in ('REVIEW','FAILED') and not waiting(i,job['kind']) for i in items),
            'waiting':sum(waiting(i,job['kind']) for i in items),
            'pending':sum(i['state'] in ('PENDING','RUNNING') for i in items),
            'observed':sum(i['state']=='OBSERVED' for i in items),
            'skipped':sum(i['state']=='SKIPPED' for i in items)}


def summary(job):
    c=counts(job)
    prefix='正在处理' if job['state']=='RUNNING' else '等待处理' if c['pending'] and job['state']!='CANCELLED' else '检查结束'
    success='预览就绪' if job['kind']=='prepare' else '已关联采购' if job['kind']=='upload' else '实收已同步'
    wait=f"，已受理待核对{c['waiting']}单" if job['kind']=='upload' else ''
    void=f"，已核实作废{c['voided']}单" if c['voided'] else ''
    return f"{prefix}：{success}{c['success']}单{wait}{void}，待处理{c['review']}单，未处理{c['pending']}单，已观察{c['observed']}单，已结束/跳过{c['skipped']}单"


def reconcile(job):
    """Rebuild batch metadata only; surviving item payloads remain untouched."""
    out=copy.deepcopy(job)
    states=[i['state'] for i in out['items']]
    if out['state'] not in ACTIVE and out['state']!='CANCELLED':
        if any(s in ('PENDING','RUNNING') for s in states):out['state']='PAUSED'
        elif any(s in ('REVIEW','FAILED') for s in states):out['state']='COMPLETED_WITH_ERRORS'
        elif states and all(s=='SKIPPED' for s in states):out['state']='CANCELLED'
        elif out['kind']=='prepare' and 'PREPARED' in states:out['state']='READY'
        else:out['state']='COMPLETED'
    out['message']=summary(out)
    return out
