"""One ordered revision comparison for confirmation, delivery and business forms."""
import normal_orders as orders
import work_repository as wr
import result_semantics as sem

def scalar(value):
    if value is None:return '未提供'
    if value is True:return '是'
    if value is False:return '否'
    if isinstance(value,(list,tuple)):return '、'.join(scalar(v) for v in value)
    if isinstance(value,dict):return '；'.join(str(k)+'：'+scalar(v) for k,v in value.items())
    return str(value)

def flatten(value,prefix=''):
    if isinstance(value,dict):
        for k,v in value.items():yield from flatten(v,prefix+' / '+str(k) if prefix else str(k))
    elif isinstance(value,(list,tuple)):
        for i,v in enumerate(value):yield from flatten(v,prefix+f' / {i+1}')
        if not value:yield prefix,'空清单'
    else:yield prefix,scalar(value)

def build(old,new,new_focus):
    before=old['payload']['result'] if old else None
    diff=orders.revision_diff(before,new)
    before_rows={d['E']:d for d in before['rows']} if before else {}
    after={d['E']:d for d in new['rows']};goods=[]
    for code,row in after.items():
        prior=before_rows.get(code);q=sem.order_quantity(row);money=sem.order_amount(row)
        if prior is None or wr.dumps(q)!=wr.dumps(sem.order_quantity(prior)) or wr.dumps(money)!=wr.dumps(sem.order_amount(prior)):
            goods.append([code,row.get('D',''),'新增' if prior is None else '执行变化',sem.order_quantity(prior) if prior else None,q,sem.order_amount(prior) if prior else None,money])
    for code,row in before_rows.items():
        if code not in after:goods.append([code,row.get('D',''),'移除',sem.order_quantity(row),None,sem.order_amount(row),None])
    return {'store':new['store'],'name':new['store_name'],'old_revision':old['revision'] if old else None,'new_revision':old['revision']+1 if old else 1,'changes':diff,'goods':goods,'before_amount':before['summary']['amount'] if before else None,'after_amount':new['summary']['amount'],'old_selection':before.get('new_products',{}) if before else None,'new_selection':new.get('new_products',{}),'old_focus':old['payload'].get('new_focus') if old else None,'new_focus':new_focus}

def rows(reviews):
    for r in reviews:
        store=r['name']+' ('+r['store']+')'
        for sku,name,kind,old,new,om,nm in r['goods']:
            yield [store,'商品执行量',sku,name,kind,scalar(old),scalar(new)]
            yield [store,'商品执行金额',sku,name,kind,scalar(om),scalar(nm)]
        for kind,before,after in [('生效参数',r['changes']['old_settings'],r['changes']['new_settings']),('来源',r['changes']['old_sources'],r['changes']['new_sources']),('首铺选择',r['old_selection'],r['new_selection']),('关注清单',r['old_focus'],r['new_focus'])]:
            a=dict(flatten(before));b=dict(flatten(after))
            for field in list(a)+[k for k in b if k not in a]:
                if a.get(field)!=b.get(field):yield [store,kind,'',field,'变更',a.get(field,'未提供'),b.get(field,'未提供')]
        for change in r['changes']['changed']:
            for field,v in change['fields'].items():
                yield [store,'商品字段',change['sku'],change['name'],field,scalar(v['before']) if v['before_present'] else '原记录无此字段',scalar(v['after']) if v['after_present'] else '新记录无此字段']

HEADERS=['门店','核对内容','商品编码','商品名称 / 参数','变化项','原值','新值']

def preview(reviews,limit=200):
    items=[];total=0;lines=[]
    for row in rows(reviews):
        total+=1
        if len(items)<limit:items.append(row)
    for r in reviews:
        lines.append(f"{r['name']}：V{r['old_revision'] or 0:03d} → V{r['new_revision']:03d}；金额 {scalar(r['before_amount'])} → {scalar(r['after_amount'])}；执行变化 {len(r['goods'])} 款")
    lines.extend(' | '.join(scalar(v) for v in row) for row in items[:limit])
    if total>limit:lines.append(f'【已截断：共{total}条，仅显示前{limit}条；请导出完整差异Excel】')
    return {'detail_text':'\n'.join(lines),'truncated':total>limit,'rows':items,'total':total}
