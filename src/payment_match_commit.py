"""Verify frozen payment recognition evidence and save its relationships.

Lifecycle commit owns the write lock and transaction. Preview may call the
same validation using a read-only connection. Neither entry point opens a
second business connection to replace the caller's current facts.
"""
import copy

import payment_name_matching as naming
import payment_shop_names
import work_repository as wr
from lifecycle_store import save_mapping
from payment_evidence import (
    HEAD_KEYS, PREFIX, MatchRequest, PaymentMatchEvidence, checksum, facts,
    local_key, mapping_key, normalized, same_head,
)
from payment_match_index import accounts, build, mappings, pool_token, scope_evidence


def _local_relationship_update(existing, updates, request: MatchRequest):
    """Local file evidence only; never claims or saves an inferred company."""
    key=local_key(request['internal'],request['original_name']);old=existing.get(key)
    if old and not same_head(old['head'],request['head']):
        raise wr.Conflict('该来源内部单号已有本机原关联，不能改绑另一张订单')
    # Known company-scoped identities remain immutable, including on a default
    # automatic import. Distinct source names alone do not become company IDs.
    for k,v in existing.items():
        if k.startswith(PREFIX+'order.') and v.get('internal')==request['internal'] and normalized(v.get('original_name'))==normalized(request['original_name']) and not same_head(v['head'],request['head']):
            raise wr.Conflict('该内部单号已有原企业订单关联，不能通过自动识别改绑')
    if not request['remember']:return
    value=old or {k:copy.deepcopy(request[k]) for k in ('internal','original_name','head','generation_id','quantity_signature')}
    value={**value,'kind':'local_document','company_inferred':False}
    if key in updates and updates[key]!=value:raise ValueError('同一来源内部单号在本批次对应多张订单；不能保存矛盾关系')
    updates[key]=value


def _relationship_updates(db, metadata: PaymentMatchEvidence):
    existing=mappings(db);updates={}
    for r in metadata['requests']:
        scope=r['source_scope']
        if not scope:
            original=existing.get(local_key(r['internal'],r['original_name']))
            if original and not same_head(original['head'],r['head']):raise wr.Conflict('该来源内部单号已有本机原关联，不能通过手选订单改绑')
            if r.get('automatic_proof') or r.get('manual_selection'):
                _local_relationship_update(existing, updates, r)
                continue
            if any(v.get('internal')==r['internal'] for k,v in existing.items() if k.startswith(PREFIX+'order.')):raise wr.Conflict('该内部单号已有来源范围，请先确认本次来源企业，不能忽略原关联')
            continue
        key=mapping_key('order',scope,r['internal']);old=existing.get(key)
        if old and any(old['head'].get(k)!=r['head'][k] for k in HEAD_KEYS):
            raise wr.Conflict('来源企业及内部订单号已有原关联；原修订失效或对象不同，不能改绑')
        alias_key=mapping_key('store',scope,normalized(r['original_name']));alias=existing.get(alias_key)
        if alias and alias['store']!=r['head']['store']:raise wr.Conflict('该企业中的门店名称已对应另一门店，请先核对门店名称对应关系')
        if not r['remember']:continue
        order=old or {'source_scope':scope,'internal':r['internal'],'original_name':r['original_name'],'head':r['head'],'generation_id':r['generation_id']}
        if key in updates and updates[key]!=order:raise ValueError('同一来源内部订单号分到多张仓单，本期不能记为唯一关系；请取消记住对应后人工核对')
        updates[key]=order
        if normalized(r['original_name']):
            value={'source_scope':scope,'original_name':r['original_name'],'normalized_name':normalized(r['original_name']),'store':r['head']['store']}
            if alias_key in updates and updates[alias_key]['store']!=value['store']:raise ValueError('同一来源门店名对应多家门店，不能记住为唯一门店')
            updates[alias_key]=alias or value
    return {k:v for k,v in updates.items() if existing.get(k)!=v}


def validate(path, db, metadata: PaymentMatchEvidence, digest, document_plans):
    """Recheck recognition against current facts using the supplied connection."""
    if digest!=checksum(metadata):raise ValueError('付款识别预览已变化，请重新核对')
    changed_pool=pool_token(db)!=metadata['pool_token'] or checksum(mappings(db))!=metadata['mapping_token']
    if changed_pool:
        expected=metadata.get('scope_evidence')
        if not expected:raise wr.Conflict('本次订单、匹配候选、生成范围或来源对应关系已变化，请重新识别；未保存')
        index=build(path,_db=db,identity_only=True)
        evidence=scope_evidence(path, index, metadata['requests'], db=db)
        if evidence!=expected:
            raise wr.Conflict('本次订单、匹配候选、生成范围或来源对应关系已变化，请重新识别；未保存')
    current_accounts=accounts(path)
    if current_accounts['token']!=metadata['accounts_token']:raise wr.Conflict('聚水潭企业连接身份已变化，请重新核对来源')
    scopes={a['coid'] for a in current_accounts['choices']}
    for r in metadata['requests']:
        if r.get('name_evidence'):
            payment_shop_names.validate_evidence(db,r['name_evidence'])
        if r['source_scope'] is not None and r['source_scope'] not in scopes:raise ValueError('付款来源企业没有已核验的稳定身份，不能记住内部单号')
        if r['source_scope'] is not None:
            source=next(a for a in current_accounts['choices'] if a['coid']==r['source_scope'])
            warehouses=set(source['warehouses'])&{'山东仓','河北仓'}
            if warehouses and r['head']['warehouse'] not in warehouses:raise ValueError('所选来源企业与订单供货仓不一致；不能保存付款对应')
        matches=[p for p in document_plans if p['stage']=='payment' and all(p['head'].get(k)==r['head'][k] for k in r['head'])]
        if len(matches)!=1:raise ValueError('识别对应关系与付款预览不是同一仓单')
        raw=matches[0]['payload']['raw_rows']
        if not raw or any(row.get('document_number')!=r['internal'] or str((row.get('source_order') or {}).get('店铺名称') or '')!=r['original_name'] for row in raw):raise ValueError('识别对应关系与原付款来源不符')
        if r.get('automatic_proof') and (r['automatic_proof'] not in ('full_detail','explicit_number','previous_source','configured_name','confirmed_name','application_name') or r.get('quantity_signature')!=facts(raw)['quantity_signature']):raise ValueError('自动识别依据与付款明细不一致')
        if r.get('name_context'):
            naming.validate(db,r['name_context'],r)
    _relationship_updates(db, metadata)


def apply(db, metadata: PaymentMatchEvidence):
    """Save relationships on the same transaction as the lifecycle versions."""
    for key,value in _relationship_updates(db, metadata).items():
        save_mapping(db, 'payment', key, value)
