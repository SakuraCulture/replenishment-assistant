"""Business-labelled projections of exact historical snapshots. No live config lookup."""
from contextlib import closing
import io
import openpyxl
import storage_chunks as chunks
import source_archive
import parameter_schema
import result_semantics as sem
import normal_exports
from runtime_support import checkpoint

LABELS=dict(zip('A B C D E F G H I J K L M N O S U V'.split(),['门店名称','门店编码','供应商编码','商品名称','商品编码','条码','库存单位','14天日均销量','近7天销量','近30天销量','可用库存','在途库存','在途库存天数','原始缺货天数','原始箱规','商品状态','权重分类','基本售价']))
LABELS.update({'AS':'原计算建议量','_base_qty':'例外前建议量','_candidate_qty':'预算前候选量','_execution_qty':'签发执行量','_execution_amount':'签发金额','_budget_gap':'预算未满足量','_meta':'来源与校验','_audit':'一箱例外审计','effective_settings':'实际生效设置','parameter_config':'完整参数方案','parameters':'模型参数','weight_profiles':'分类权重','category_profiles':'分类映射','warehouses':'仓周期','exception':'一箱例外','budget':'门店预算','supply_route':'主从仓设置','primary':'主仓','secondary':'从仓','preset':'参数方案','config':'完整配置','sources':'输入来源','hashes':'文件校验值','new_focus':'关注清单','new_products':'首铺选择','BA':'有效缺货天数','AN':'基准预测日均','AM':'趋势系数','AL':'安全库存量','AG':'备货天数','AH':'到货天数','AR':'常规规整量','BQ':'最低持有量','BR':'约束前需求量','BU':'资金允许数量','BP':'断货恢复后数量','BI':'常规补货资格','BD':'有效箱规','AU':'需求归属仓','AW':'计算状态','BM':'模型解释','BB':'输入诊断','_must_sell':'是否必卖','_source':'资料匹配状态','alpha':'趋势敏感度','w1':'最近7天权重','w2':'此前7天权重','w3':'此前16天权重'})
LABELS.update({k:v[0] for k,v in parameter_schema.HELP.items()})

def fields(value,prefix=''):
    if isinstance(value,dict):
        if not value:yield [prefix,'空记录','记录']
        for k,v in value.items():yield from fields(v,(prefix+' / ' if prefix else '')+LABELS.get(k,str(k)))
    elif isinstance(value,(list,tuple)):
        if not value:yield [prefix,'空清单','清单']
        for i,v in enumerate(value):yield from fields(v,prefix+f' / {i+1}')
    else:
        kind='缺失' if value is None else '开关' if isinstance(value,bool) else '数字' if isinstance(value,(int,float)) else '文字'
        text='未提供' if value is None else '空文本' if value=='' else '是' if value is True else '否' if value is False else str(value)
        yield [prefix,text,kind]

def work_parts(repo,order,paths):
    rev=order['payload'].get('work_revision')
    with closing(repo.work.connect()) as db:
        row=db.execute('SELECT payload FROM normal_work_revisions WHERE revision=?',(rev,)).fetchone()
        if not row:return {}
        return chunks.load(row[0],db,paths=paths)

def form(repo,order,scope,sku,cancel=None):
    p=order['payload'];r=p['result']
    if scope=='单据概况':
        data={'补货单号':order['number'],'业务日期':order['day'],'门店编码':order['store'],'门店名称':r['store_name'],'签发修订':order['revision'],'归档时间':order['saved'],'签发金额':r['summary']['amount'],'签发款数':r['summary']['positive'],'说明':'原签发建议只读；门店回传独立保存'}
    elif scope=='当时参数':data={'实际生效设置':r.get('effective_settings'),'当时完整参数方案':p.get('preset'),'模型实际配置':r.get('config'),'关注清单':p.get('new_focus'),'首铺选择':r.get('new_products'),'输入来源':r.get('sources')}
    elif scope in ('商品建议与销量库存','商品完整计算依据'):
        d=next((x for x in r['rows'] if x['E']==sku),None)
        if d is None:return [['商品','请输入本单内的精确商品编码','提示']]
        if scope=='商品完整计算依据':data=d
        else:
            data={k:d.get(k) for k in 'A B D E G H I J K L N BA BD V AU AS _base_qty _candidate_qty _execution_qty _execution_amount _budget_gap'.split()}
            # Execution is budget/status-aware; _execution_* alone is only a
            # pre-budget first-fill override, absent on ordinary moving goods.
            data['_execution_qty']=sem.order_quantity(d);data['_execution_amount']=sem.order_amount(d)
            data.update(核心理由=normal_exports.decision(d),当时资料与来源=d.get('_meta'),一箱例外=d.get('_audit'))
            if d.get('_inbound_cap'):data['在途保底限制']=d['_inbound_cap']
            risk=sem.inbound_risk(d)
            if risk:data['需人工复核']=risk['text']
    elif scope=='当时商品资料快照':
        parts=work_parts(repo,order,[('catalogs',),('loaded','intake','products_snapshot')]);catalogs=parts.get(('catalogs',),{});data={}
        for wh,c in catalogs.items():
            row=c.get('items',{}).get(sku)
            data[wh]={'当时资料版本':c.get('revision'),'原文件':c.get('source_path'),'工作表':c.get('sheet'),'当时选取字段':row or '本仓未匹配该商品'}
            with closing(repo.work.connect()) as db:stored=source_archive.read(db,c.get('source_sha256'))
            if stored and row:
                book=openpyxl.load_workbook(io.BytesIO(stored[1]),read_only=True,data_only=True)
                try:
                    ws=book[c['sheet']];ws.reset_dimensions();it=ws.values;headers=next(it,())
                    for line,values in enumerate(it,2):
                        checkpoint(cancel)
                        if line==row['_row']:
                            data[wh]['原表全部列']={f'{i+1}列 · {h}':values[i] if i<len(values) else None for i,h in enumerate(headers)};break
                finally:book.close()
            data[wh]['原文件留存']='完整原文件已留存，可下载' if stored else '历史原文件未留存；已有字段快照可查'
        if not catalogs:
            raw=parts.get(('loaded','intake','products_snapshot'),[]) or []
            data={'旧版混合资料':next((x for x in raw if str(x.get('商品编码'))==sku),None),'提示':'仅展示签发时保存的字段；不使用当前商品资料补填'}
    else:raise ValueError('未知业务表单')
    return list(fields(data))

def attachments(repo,order):
    parts=work_parts(repo,order,[('catalogs',),('loaded','intake','hashes')]);out=[]
    for wh,c in parts.get(('catalogs',),{}).items():out.append([wh,c['source_path'],c['source_sha256']])
    for path,sha in parts.get(('loaded','intake','hashes'),{}).items():out.append(['销售 / 混合资料',path,sha])
    profile=order['payload'].get('new_focus') or {}
    if profile.get('source_sha256'):out.append(['关注清单',profile['source_path'],profile['source_sha256']])
    with closing(repo.work.connect()) as db:
        for row in out:
            exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='source_files'").fetchone()
            found=exists and db.execute('SELECT 1 FROM source_files WHERE sha=?',(row[2],)).fetchone()
            row.append('已留存' if found else '历史原文件未留存')
    return out

def event_rows(records,heads):
    rows=[]
    for r in records:
        head=heads.get(r['oid'],{});detail=r['detail']
        rows.append([r['at'],head.get('number',detail.get('number','')),head.get('store',detail.get('store','')),r['revision'],r['action'],'；'.join(k+'：'+v for k,v,t in fields(detail))])
    return rows

def events(path,oid,progress=lambda _:None,cancel=None):
    from history_read import repository
    repo=repository(path);progress('读取日志并关联补货单号…');records=repo.events(oid);checkpoint(cancel);heads={h['oid']:h for h in repo.headers()}
    return {'records':records,'rows':event_rows(records,heads)}
