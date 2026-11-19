"""Automatic association from explicit identity or globally unique complete facts.

An unmarked workbook does not prove a JST company. Local source associations
therefore have a separate namespace and never create company/store aliases.
"""
import copy
from decimal import Decimal
import payment_matching as pm
from runtime_support import checkpoint

HEAD_KEYS=('oid','revision','warehouse','number','store','digest')

def same_head(a,b):return all(a.get(k)==b.get(k) for k in HEAD_KEYS)
def local_key(internal,name):return pm.mapping_key('document',None,pm.checksum([str(internal),pm.normalized(name)]))

def result(state,reason,choices=(),*,automatic=False,proof=None,exact=False,matches=(),blocked=False):
    return {'state':state,'reason':reason,'candidates':list(choices),'rejected':[],
            'automatic':automatic,'proof':proof,'exact':exact,'prevent_rebind':blocked,
            'source_scope':None,'exact_numbers':[h['number'] for h in matches],
            'trace':[['完整匹配（跨全部相关日期）',len(matches)]]}

def matching_heads(path,rows,heads,progress,cancel,*,quantities_only=False):
    incoming=pm.facts(rows)
    if not incoming['complete']:return []
    pm.load_features(path,heads,progress,cancel,required_sku=str(rows[0]['sku']))
    possible=[h for h in heads if h.get('features') and h['features']['count']==incoming['count']
              and h['features']['quantity']==incoming['quantity']
              and (quantities_only or h['features']['amount']==incoming['amount'])]
    matches=[]
    for i,h in enumerate(possible):
        checkpoint(cancel);progress(f'自动认单：完整核对候选 {i+1}/{len(possible)}，包含全部相关日期')
        ok,_=pm.exact(path,rows,h,cancel,quantities_only=quantities_only)
        if ok:matches.append(h)
    return matches

def prior_links(rows,index,source_scope):
    r=rows[0];internal=r.get('document_number');name=pm.normalized((r.get('source_order') or {}).get('店铺名称'))
    values=[];local=index['mappings'].get(local_key(internal,name))
    if local:values.append(local)
    for key,value in index['mappings'].items():
        if not key.startswith(pm.PREFIX+'order.') or value.get('internal')!=internal:continue
        if source_scope is not None:
            if value.get('source_scope')==source_scope:values.append(value)
        elif pm.normalized(value.get('original_name'))==name:values.append(value)
    return values

def differences(path,rows,heads,cancel,progress=lambda _:None):
    """At most three full reads for explanation; never an association rule."""
    import jst_orders as jst,order_lifecycle as lc
    pm.load_features(path,heads,progress,cancel)
    incoming=pm.facts(rows);details={}
    shortlist=sorted((h for h in heads if h.get('features')),key=lambda h:(abs(h['features']['count']-incoming['count']),abs(Decimal(h['features']['amount'])-Decimal(incoming['amount'] or 0))))[:3]
    for h in shortlist:
        checkpoint(cancel);g=jst.get(path,h['generation_id']);items=copy.deepcopy(rows)
        try:lc.match_payment_rows(path,lc.full_head(path,h),items)
        except ValueError as exc:details[h['number']]=str(exc);continue
        expected={r['sku']:r for r in g['rows'] if Decimal(r['quantity'])>0};actual={r['sku']:r for r in items}
        missing=sorted(expected.keys()-actual.keys());extra=sorted(actual.keys()-expected.keys());common=expected.keys()&actual.keys()
        qty=[k for k in common if pm.number(actual[k]['base_quantity'],True)!=pm.number(expected[k]['quantity'],True)]
        money=[k for k in common if pm.number(actual[k]['amount'],True)!=pm.number(expected[k]['amount'],True)]
        details[h['number']]=f'少 {len(missing)} 款、多 {len(extra)} 款、数量不同 {len(qty)} 款、金额不同 {len(money)} 款'+('；缺少编码 '+ '、'.join(missing[:3]) if missing else '')+('；清单外编码 '+'、'.join(extra[:3]) if extra else '')
    return details

def analyze(path,document,index,scope=None,source_scope=None,progress=lambda _:None,cancel=None):
    from lifecycle_diagnostics import binding
    rows=document['scan']['rows'];heads=index['heads'];allowed={h['number'] for h in scope} if scope is not None else None
    if source_scope is not None and source_scope not in {a['coid'] for a in index['accounts']['choices']}:
        return result('来源企业未核验','指定的来源企业没有已核验身份；请恢复自动识别或修正来源设置。',blocked=True)
    # Do not recursively read a previous UI result as fresh evidence.
    raw={k:v for k,v in document.items() if k!='payment_match'}
    evidence=binding(raw,heads);eligible=evidence['candidates']
    if source_scope is not None:
        source=next(a for a in index['accounts']['choices'] if a['coid']==source_scope)
        warehouses=set(source['warehouses'])&{'山东仓','河北仓'}
        if warehouses:eligible=[h for h in eligible if h['warehouse'] in warehouses]
        if not eligible and evidence['candidates']:return result('指定企业与供货仓不一致','文件对应的供货仓不属于指定来源企业；请恢复自动模式或修正来源设置。',blocked=True)
    numbers={r['signed_number'] for r in rows if r.get('signed_number')}
    explicit=bool(numbers)
    links=prior_links(rows,index,source_scope)
    linked=[]
    for link in links:
        candidates=[h for h in heads if same_head(h,link['head'])]
        if len(candidates)!=1:
            return result('原订单已失效','此内部单号曾保存到 '+link['head']['number']+'，原订单已删除或重新签发；不能套到同号新订单。',blocked=True)
        if candidates[0].get('generation_id')!=link.get('generation_id'):
            return result('原分配已变化','此内部单号的原库存分配已变更；当前文件不能自动替换为另一轮分配。',blocked=True)
        if not any(same_head(candidates[0],h) for h in linked):linked.append(candidates[0])
    import payment_name_matching as naming
    name_context=naming.context(rows,index,eligible,source_scope)
    named=naming.candidates(name_context,eligible) if name_context else eligible
    # Immutable document identities precede optional name narrowing. Complete
    # facts remain a fallback, never a universal requirement to bind a payment.
    strong=explicit or bool(linked)
    if not strong and name_context and len(named)==1 and allowed is not None and named[0]['number'] not in allowed:
        return result('识别订单超出本次范围','文件完整对应 '+named[0]['number']+'，但该订单不在本次勾选范围内；未改绑其他订单。',blocked=True)
    matches=matching_heads(path,rows,eligible if strong else named,progress,cancel)
    target=None;method=None;reason='';is_exact=False
    if explicit:
        if len(numbers)!=1 or not all(r.get('signed_number') for r in rows) or len(eligible)!=1:
            return result('签发单号无法对应',evidence['reason'],blocked=True)
        target=eligible[0]
        if linked and any(not same_head(target,h) for h in linked):
            return result('单号与原关联冲突','新增签发单号 '+target['number']+' 与该付款内部单号已保存的原订单不一致。',blocked=True)
        # A clearly different complete order is stronger evidence of a mistyped
        # explicit number, not permission to silently rewrite that number.
        if not matches:
            other=matching_heads(path,rows,[h for h in heads if h['number']!=target['number']],progress,cancel)
            if other:
                descriptions='；'.join(h['name']+' / '+h['warehouse']+' / '+h['number'] for h in other)
                return result('签发单号与明细冲突','填写单号属于 '+target['name']+' / '+target['warehouse']+'；文件明细完整对应 '+descriptions+'。请更正文件中的错误单号后重新读取。',matches=other,blocked=True)
        method='explicit_number';is_exact=bool(matches)
        reason='已按文件签发单号识别 '+target['name']+' / '+target['warehouse']+'；'+('商品、数量、金额全部一致。' if is_exact else '订单身份已确定，数量或金额差异会在付款核对中逐项显示。')
    elif linked:
        if len(linked)!=1:return result('原单号存在多个关联','该内部单号存在不同的已确认原单，请先核对原关联；不能默认取其中一张。',blocked=True)
        target=linked[0]
        if matches and any(not same_head(target,h) for h in matches):
            return result('内部单号与明细冲突','该内部单号已有原订单关联，当前明细却完整匹配另一张订单；不能自动改绑。',matches=matches,blocked=True)
        method='previous_source';is_exact=any(same_head(target,h) for h in matches)
        pm.load_features(path,[target],progress,cancel)
        a=pm.facts(rows)['amount'];b=(target.get('features') or {}).get('amount')
        difference=f'金额较分配结果变化 {Decimal(a)-Decimal(b):+.2f} 元；' if a is not None and b is not None else ''
        reason=f"已自动识别原内部单号对应的 {target['name']} / {target['warehouse']}；{difference}数量和金额差异保留到核对页。"
    elif name_context and len(named)==1:
        target=named[0];method=name_context['basis'];is_exact=bool(matches)
        reason='店铺名称对应 '+target['name']+' / '+target['warehouse']+'，全部相关日期内只有这一张有效仓单；数量和金额差异进入付款核对。'
    elif len(matches)==1:
        target=matches[0];method='full_detail';is_exact=True
        reason='已自动识别 '+target['name']+' / '+target['warehouse']+'：商品编码、逐商品数量及金额完整一致，全部相关日期内仅此一单。'
    else:
        if name_context and not named:
            return result('店铺名称暂无有效订单','已找到店仓名称对应，但没有符合文件身份的当前有效仓单；请核对签发和文件中的门店、仓。',blocked=True)
        details=differences(path,rows,named,cancel,progress)
        hint='；'.join(h['name']+' / '+h['warehouse']+'：'+details[h['number']] for h in named if h['number'] in details)
        choices=[h for h in named if allowed is None or h['number'] in allowed]
        day=pm.source_day(rows)
        choices.sort(key=lambda h:(0 if h in matches else 1,0 if h['day']==day else 1,0 if h.get('payment_version') is None else 1,h['day'],h['number']))
        state='店铺名称对应多个订单' if name_context else '存在多张完整匹配订单' if len(matches)>1 else '没有完整匹配订单'
        reason=(f'名称对应 {len(named)} 张有效仓单；请选择业务日期和仓单。' if name_context else '没有唯一完整明细匹配；请明确选择原单。')+'数量或金额差异在下一页核对，不妨碍确认单据身份。'+hint
        outcome=result(state,reason,choices,matches=matches)
        outcome['candidate_reasons']=details
        if name_context:outcome['name_context']=name_context
        return outcome
    if allowed is not None and target['number'] not in allowed:
        return result('识别订单超出本次范围','文件完整对应 '+target['number']+'，但该订单不在本次勾选范围内；未改绑其他订单。',blocked=True)
    # Explicit row metadata and immutable hidden references must hold even for
    # previously remembered local associations.
    if not any(same_head(target,h) for h in eligible):
        return result('文件身份冲突','原订单与文件中的门店、仓或签发修订不一致。',blocked=True)
    if name_context and not any(same_head(target,h) for h in named):
        return result('店铺名称与原单身份冲突','文件名称对应的店仓与明确单号或已确认原关联不一致，请核对文件名称及独立名称配置。',blocked=True)
    pm.load_features(path,[target],progress,cancel)
    outcome=result('已自动识别原内部单号' if method=='previous_source' else '已自动识别',reason,[target],automatic=True,proof=method,exact=is_exact,matches=matches)
    if name_context:outcome['name_context']=name_context
    return outcome

def relationship_update(existing,updates,request):
    """Local file evidence only; never claims or saves an inferred company."""
    key=local_key(request['internal'],request['original_name']);old=existing.get(key)
    if old and not same_head(old['head'],request['head']):
        raise pm.wr.Conflict('该来源内部单号已有本机原关联，不能改绑另一张订单')
    # Known company-scoped identities remain immutable, including on a default
    # automatic import. Distinct source names alone do not become company IDs.
    for k,v in existing.items():
        if k.startswith(pm.PREFIX+'order.') and v.get('internal')==request['internal'] and pm.normalized(v.get('original_name'))==pm.normalized(request['original_name']) and not same_head(v['head'],request['head']):
            raise pm.wr.Conflict('该内部单号已有原企业订单关联，不能通过自动识别改绑')
    if not request['remember']:return
    value=old or {k:copy.deepcopy(request[k]) for k in ('internal','original_name','head','generation_id','quantity_signature')}
    value={**value,'kind':'local_document','company_inferred':False}
    if key in updates and updates[key]!=value:raise ValueError('同一来源内部单号在本批次对应多张订单；不能保存矛盾关系')
    updates[key]=value
