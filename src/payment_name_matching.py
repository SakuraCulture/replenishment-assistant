"""Exact optional shop names narrow identities, never audit payment quantities."""
import payment_shop_names as names

HEAD_KEYS=('oid','revision','warehouse','number','store','digest')

def context(rows,index,eligible,source_scope=None):
    name=names.normalized((rows[0].get('source_order') or {}).get('店铺名称'))
    configured=names.name_evidence(index.get('shop_names',{}),name)
    pairs=configured['pairs'];basis='configured_name' if pairs else None
    if not pairs and name:
        stores={v['store'] for k,v in index['mappings'].items()
                if k.startswith('payment_match.store.') and names.normalized(v.get('original_name'))==name
                and (source_scope is None or v.get('source_scope')==source_scope)}
        if stores:
            pairs=sorted({(h['store'],h['warehouse']) for h in index['heads'] if h['store'] in stores});basis='confirmed_name'
        else:
            pairs=sorted({(h['store'],h['warehouse']) for h in index['heads'] if names.normalized(h['name'])==name})
            if pairs:basis='application_name'
    if not basis:return None
    pairs=[list(p) for p in pairs];matching=[h for h in eligible if [h['store'],h['warehouse']] in pairs]
    return {'configured':configured,'basis':basis,'pairs':pairs,
            'identities':[{k:h[k] for k in HEAD_KEYS} for h in matching]}

def candidates(context,eligible):
    pairs=context['pairs']
    return [h for h in eligible if [h['store'],h['warehouse']] in pairs]

def validate(db,context,request):
    import work_repository as wr
    names.validate_evidence(db,context['configured'])
    if context['configured']['name']!=names.normalized(request['original_name']):raise ValueError('店铺名称识别与原付款名称不一致')
    if not any(all(h[k]==request['head'][k] for k in HEAD_KEYS) for h in context['identities']):
        raise wr.Conflict('所选订单不在店铺名称对应的候选范围，请重新识别')
    if request.get('automatic_proof') in ('configured_name','confirmed_name','application_name'):
        if request['automatic_proof']!=context['basis'] or len(context['identities'])!=1:
            raise ValueError('店铺名称尚有多个有效候选，不能按唯一订单保存')
