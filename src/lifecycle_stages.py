"""Document routes and warehouse-order progress shared by lifecycle readers."""
from lifecycle_rules import DOCUMENT_SCHEMA, JST_ORDER, basis_token

LEGACY = ('return', 'payment', 'receipt')
CURRENT = ('return', 'operations', 'payment', 'receipt')
PENDING = {
    'return': '待回传',
    'operations': '待运营签发',
    'payment': '待付款',
    'receipt': '待收货',
}
FINISHED = '已收齐'
TITLES = {
    'return': '门店回传单',
    'payment': '门店付款单',
    'receipt': '门店实际收货单',
    'operations': '运营签发单',
}
STATUSES = tuple(PENDING[stage] for stage in LEGACY) + (FINISHED,)


def route(operations_required):
    return CURRENT if operations_required else LEGACY


def presence_state(present):
    """Legacy presence check. Use progress_state for validated upstream readiness."""
    for i, stage in enumerate(LEGACY):
        if not present.get(stage): return STATUSES[i], stage
    return STATUSES[-1], None

def progress_state(summaries, legacy=None, operations_required=False, *, shipment=None):
    summaries=dict(summaries)
    stages=route(operations_required or summaries.get('operations'))
    if not summaries['return'] and legacy and legacy['complete']:
        summaries['return']={'version':legacy['version'],'digest':legacy['digest'],'schema':1,'positive':sum(v>0 for v in legacy['values'].values())}
    health={};tokens={}
    ordered=summaries.get(JST_ORDER)
    order_valid=False
    for s in stages:
        d=summaries.get(s)
        if s=='payment':
            order_valid=bool(ordered and not ordered['pending'] and ordered['positive']
                             and ordered['dependencies']==tokens and all(health.values()))
        expected=tokens
        ready=all(health.values())
        if s=='receipt' and d and JST_ORDER in d['dependencies']:
            expected={k:v for k,v in tokens.items() if k!='payment'}
            expected[JST_ORDER]=basis_token(ordered)
            ready=order_valid
        health[s]=bool(d) and (d['schema']==1 or not d['pending'] and d['dependencies']==expected and ready)
        tokens[s]=basis_token(d)
    if ordered:
        health[JST_ORDER]=order_valid
    order_note='已核实聚水潭原单，待人工确认付款' if order_valid and not health['payment'] else ''
    if not summaries['return'] and not (legacy and legacy['complete']):return PENDING['return'],'return','',health
    payment = summaries.get('payment')
    for i,s in enumerate(stages[1:],1):
        document=summaries.get(s)
        prev=ordered if s=='receipt' and order_valid else summaries.get(stages[i-1])
        if not document and prev and health[stages[i-1]] and prev['schema']==DOCUMENT_SCHEMA and prev['positive']==0 and not any(summaries.get(k) for k in stages[i:]):
            return STATUSES[-1],None,('无要货，无需后续运营、分配、付款或收货' if stages[i-1] in ('return','operations') else '无付款数量，无需收货'),health
        if s=='receipt' and not health[s] and shipment and shipment['payment_lock'] != basis_token(payment):
            status = shipment['status']
            observed = f'原单当前为“{status}”，' if status else ''
            reason = '付款版本待与聚水潭原单核对' if shipment['observed'] else '等待核实聚水潭原单发货中（打单拣货）'
            note = f"已保存付款 V{payment['version']:03d}，{observed}{reason}"
            return PENDING['payment'],'payment',note,health
        if not document:
            return PENDING[s],s,order_note,health
        if not health[s]:return PENDING[s],s,order_note if s=='payment' and order_note else TITLES[s]+'对照依据已更新或未就绪，请重新核对',health
    return STATUSES[-1],None,order_note or '单据已收齐；不代表数量完全一致',health
