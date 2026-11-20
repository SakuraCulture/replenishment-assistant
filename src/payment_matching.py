"""Identify paid orders and attach recognition evidence to a lifecycle preview."""
import copy
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

import lifecycle_batch
import payment_match_commit
import payment_shop_names
import work_repository as wr
from payment_auto_matching import analyze as analyze_automatic, exact
from payment_evidence import (
    HEAD_KEYS, PREFIX, MatchRequest, PaymentMatchEvidence, checksum, facts,
    mapping_key, normalized, number, related, source_day,
)
from payment_match_index import Cache, accounts, build, load_features, mappings, readonly, scope_evidence
from runtime_support import checkpoint


def build_identification(path,scope,progress=lambda _:None,cancel=None):
    return build(path,progress,cancel,identity_only=True,scope=scope)


def analyze(path,document,index,scope=None,source_scope=None,mode='first',progress=lambda _:None,cancel=None):
    maintained=payment_shop_names.name_evidence(index.get('shop_names',{}),(document['scan']['rows'][0].get('source_order') or {}).get('店铺名称'))['pairs']
    if source_scope is None or maintained or any(r.get('signed_number') for r in document['scan']['rows']):
        return analyze_automatic(path,document,index,scope,source_scope,progress,cancel)
    if source_scope is not None and source_scope not in {a['coid'] for a in index['accounts']['choices']}:
        return {'candidates':[],'rejected':[],'automatic':False,'state':'来源企业未核验','reason':'所选来源企业没有已核验身份，请刷新连接记录后重新选择','prevent_rebind':True,'source_scope':source_scope}
    rows=document['scan']['rows'];origin=rows[0].get('source_order') or {};day=source_day(rows);name=normalized(origin.get('店铺名称'))
    internal=rows[0].get('document_number')
    mapped=index['mappings'];link=mapped.get(mapping_key('order',source_scope,internal)) if source_scope and internal else None
    if source_scope is None and any(v.get('internal')==internal for k,v in mapped.items() if k.startswith(PREFIX+'order.')):
        return {'candidates':[],'rejected':[],'automatic':False,'state':'请确认来源企业','reason':'该内部订单号已有已确认企业范围的原关联；请先选择本次来源企业，不能忽略原关联改绑','prevent_rebind':True,'source_scope':None}
    alias=mapped.get(mapping_key('store',source_scope,name)) if source_scope and name else None
    all_heads=[h for h in index['heads'] if related(rows,h)];allowed={h['number'] for h in scope} if scope is not None else None
    source=next(a for a in index['accounts']['choices'] if a['coid']==source_scope)
    warehouses=set(source['warehouses'])&{'山东仓','河北仓'}
    if warehouses:all_heads=[h for h in all_heads if h['warehouse'] in warehouses]
    if link:
        candidates=[h for h in all_heads if all(h[k]==link['head'][k] for k in ('oid','revision','warehouse','digest','number'))]
        if len(candidates)!=1:return {'candidates':[],'rejected':[],'automatic':False,'state':'原关联失效','reason':'内部订单号已有关联，但原订单、修订或仓单已失效；不会改绑其他单','prevent_rebind':True,'source_scope':source_scope}
        target=candidates[0]
        if allowed is not None and target['number'] not in allowed:return {'candidates':[],'rejected':[],'automatic':False,'state':'原关联超出范围','reason':'来源内部单号对应原仓单不在本次勾选范围；请从原订单进入','prevent_rebind':True,'source_scope':source_scope}
        load_features(path,[target],progress,cancel)
        ok,reason=exact(path,rows,target,cancel)
        return {'candidates':[target],'rejected':[],'automatic':target.get('generation_id')==link.get('generation_id'),'state':'已识别原内部单号','reason':'已有企业范围及内部订单号对应；'+reason,'source_scope':source_scope,'exact':ok,'day':day,'trace':[]}
    if alias:all_heads=[h for h in all_heads if h['store']==alias['store']]
    name_stores={h['store'] for h in all_heads if name and normalized(h['name'])==name}
    confirmed_store=alias.get('store') if alias else next(iter(name_stores)) if len(name_stores)==1 else None
    feature=facts(rows);matching=[];reasons={}
    considered=[h for h in all_heads if confirmed_store is None or h['store']==confirmed_store]
    load_features(path,considered,progress,cancel)
    summary=[h for h in considered if h['features'] and h['features']['count']==feature['count'] and h['features']['quantity']==feature['quantity'] and h['features']['amount']==feature['amount']]
    for i,h in enumerate(summary):
        checkpoint(cancel);progress(f'核对付款候选 {i+1}/{len(summary)}：完整明细和跨日唯一性')
        ok,reason=exact(path,rows,h,cancel);reasons[h['number']]=reason
        if ok:matching.append(h)
    related_matches=[h for h in matching if confirmed_store is None or h['store']==confirmed_store]
    # Date/lifecycle rank the view; uniqueness always includes every relevant day.
    def rank(h):
        pending=h.get('return_version') is not None and h.get('generation_id') and h.get('payment_version') is None
        lifecycle=0 if (pending if mode=='first' else h.get('payment_version') is not None if mode=='correction' else True) else 1
        distance=abs((date.fromisoformat(h['day'])-date.fromisoformat(day)).days) if day else 0
        return (0 if h in related_matches else 1,lifecycle,0 if h['store']==confirmed_store else 1,distance,h['day'],h['number'])
    choices=sorted([h for h in all_heads if allowed is None or h['number'] in allowed],key=rank)
    load_features(path,choices,progress,cancel)
    automatic=bool(source_scope and len(related_matches)==1 and related_matches[0] in choices)
    if automatic:
        target=related_matches[0];choices=[target,*[h for h in choices if h is not target]];state='唯一完整匹配';reason='已核对指定企业供货仓及全部相关日期的逐商品明细；自动识别门店和仓，无需确认别称'
    else:
        state='需要核对来源／候选';reason=('尚未确认来源企业；' if not source_scope else '')+('门店名称对应待确认；' if not confirmed_store else '')+(f'完整匹配 {len(related_matches)} 张，不能默认第一张' if len(related_matches)!=1 else '有内容匹配候选，仍需确认来源及门店对应')
    trace=[['有效仓单',len(all_heads)],['待付款优先',sum(bool(h.get('return_version') and h.get('generation_id') and h.get('payment_version') is None) for h in all_heads)],['同日优先',sum(h['day']==day for h in all_heads) if day else '来源无明确日期'],['汇总相同',len(summary)],['完整匹配（跨全部相关日期）',len(related_matches)]]
    return {'candidates':choices,'rejected':[],'automatic':automatic,'state':state,'reason':reason,'source_scope':source_scope,'day':day,'trace':trace,'exact_numbers':[h['number'] for h in matching],'candidate_reasons':reasons}


def identify_documents(path,documents,index,scope,source_scope,mode,progress=lambda _:None,cancel=None):
    """Identify paid spreadsheets as a separately timed background operation."""
    results=[]
    for document in documents:
        checkpoint(cancel)
        scan=document['scan']
        result=(analyze(path,document,index,scope,source_scope,mode,progress,cancel)
                if scan['stage']=='payment' and scan['rows'][0].get('adapter')=='paid_order' else None)
        results.append(result)
    return results


def attach(path,plan,documents,index,source_scope,remember=True,*,progress=lambda _:None,cancel=None):
    """Freeze sidecar relationship operations; never alter payment payload/digest."""
    if not index:return plan
    selected=[d for d in documents if d.get('selected',True) and d['scan']['stage']=='payment' and d['scan']['rows'][0].get('adapter')=='paid_order']
    if not selected:return plan
    requests: list[MatchRequest] = []
    for d in selected:
        target=d.get('target');numbers={r.get('signed_number') or (target or {}).get('number') for r in d['scan']['rows']}
        if len(numbers)!=1:raise ValueError('付款来源单必须先明确关联一张仓单')
        number_value=next(iter(numbers));p=next(p for p in plan['plans'] if p['stage']=='payment' and p['head']['number']==number_value)
        internal={r['document_number'] for r in d['scan']['rows']}
        if len(internal)!=1:raise ValueError('同一完整付款来源组存在多个内部订单号')
        origin=d['scan']['rows'][0]['source_order'];h=p['head'];candidate=next((v for v in index['heads'] if v['number']==h['number']),None)
        if not candidate:raise wr.Conflict('所选仓单已不在本次有效索引中，请重新读取')
        proof=d.get('payment_match') or {}
        incoming=facts(p['payload']['raw_rows'])
        constraints={checksum(v):v for row in d['scan']['rows'] for v in [{k:row.get(k) for k in ('signed_number','store','warehouse')}]}
        requests.append({'internal':next(iter(internal)),'source_scope':source_scope,'original_name':str(origin.get('店铺名称') or ''),'remember':bool(remember),
            'automatic_proof':proof.get('proof') if proof.get('automatic') and proof['candidates'][0]['number']==number_value else None,
            'name_context':copy.deepcopy(proof.get('name_context')),'manual_selection':not proof.get('automatic',False),
            'explicit_identity':any(r.get('signed_number') for r in d['scan']['rows']),
            'quantity_signature':incoming['quantity_signature'],'facts_signature':incoming['signature'],'identity_constraints':list(constraints.values()),
            'head':{k:h[k] for k in HEAD_KEYS},'generation_id':candidate.get('generation_id')})
    metadata: PaymentMatchEvidence = {'pool_token':index['pool_token'],'mapping_token':index['mapping_token'],'accounts_token':index['accounts']['token'],'requests':requests}
    for r in requests:r['name_evidence']=payment_shop_names.name_evidence(index.get('shop_names',{}),r['original_name'])
    metadata['scope_evidence'] = scope_evidence(path, index, requests, progress, cancel)
    plan['payment_matching']=metadata;plan['payment_matching_digest']=checksum(metadata)
    with closing(readonly(path)) as db:
        payment_match_commit.validate(path, db, metadata, plan['payment_matching_digest'], plan['plans'])
    return plan

def prepare(path,documents,scope,index,source_scope,remember=True,progress=lambda _:None,cancel=None):
    documents=copy.deepcopy(documents)
    # Head/dependency validation and attach() check the relevant candidate set.
    # An unrelated payment save must not invalidate this entire index first.
    for d in documents:
        if not d.get('selected',True) or d['scan']['stage']!='payment' or d['scan']['rows'][0].get('adapter')!='paid_order':continue
        result=analyze(path,d,index,scope,source_scope,progress=progress,cancel=cancel);d['payment_match']=result
        if result.get('prevent_rebind'):raise ValueError(result['reason'])
        if result['automatic'] and d.get('target') and d['target']['number']!=result['candidates'][0]['number']:raise ValueError('自动识别结果与所选订单不同；不能将完整付款单改绑其他订单')
        if d.get('target') is None and result['automatic']:d['target']=result['candidates'][0]
        if d.get('target') is None:raise ValueError(result['state']+'：'+result['reason'])
        if not any(all(h[k]==d['target'].get(k) for k in HEAD_KEYS) for h in result['candidates']):
            source=next((a for a in index['accounts']['choices'] if a['coid']==source_scope),None)
            if source and d['target']['warehouse'] not in source['warehouses']:raise ValueError('所选来源企业与订单供货仓不一致；请修正来源或恢复自动模式')
            raise ValueError('所选付款订单不在当前有效候选中，请重新识别')
    plan=lifecycle_batch.prepare(path,documents,scope,progress,cancel)
    checkpoint(cancel)
    return attach(path,plan,documents,index,source_scope,remember,progress=progress,cancel=cancel)


def aliases(path):
    with closing(readonly(path)) as db:
        return [{'key':k,'value':v,'token':checksum(v)} for k,v in mappings(db).items() if k.startswith(PREFIX+'store.')]


def forget_alias(path,key,token):
    """Explicit alias maintenance. Internal-order identities remain immutable."""
    if not key.startswith(PREFIX+'store.'):raise ValueError('只能解除门店名称对应，不能删除来源订单原关联')
    with closing(sqlite3.connect(path,timeout=.2)) as db:
        db.execute('BEGIN IMMEDIATE')
        try:
            value=mappings(db).get(key)
            if value is None or checksum(value)!=token:raise wr.Conflict('门店对应关系已变化，请刷新后再处理')
            backup=Path(path).parent/'payment_matching/alias-backups';backup.mkdir(parents=True,exist_ok=True)
            file=backup/(uuid4().hex+'.json')
            with file.open('x',encoding='utf-8') as f:json.dump({'key':key,'value':value},f,ensure_ascii=False,indent=2)
            db.execute("DELETE FROM lifecycle_mappings WHERE stage='payment' AND name=?",(key,));db.commit()
            return str(file)
        except Exception:db.rollback();raise
