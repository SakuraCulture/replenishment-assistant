"""One combination per sheet, whole filtered result, typed values, no page truncation."""
from pathlib import Path
import os,uuid
from xlsx_exports import stream_book,write_table
from export_files import publish_new
from runtime_support import checkpoint
from stockout_common import now,resource_check
import warehouse_stockout as metrics
from warehouse_stockout_views import combinations

DETAIL=[('day','观测日期'),('warehouse','供货仓'),('sku','商品编码'),('name','商品名称'),('stock_raw','可用库存原值'),
        ('stock','可用库存'),('status','商品状态'),('virtual_tags','虚拟分类'),('product_tags','商品标签'),('category','商品分类'),
        ('category1','一级分类'),('category2','二级分类'),('category3','三级分类'),('supplier_code','供应商编码'),('supplier','供应商名称'),
        ('excluded','已剔除'),('bad','待核'),('reason','说明'),('observed','观测时间')]
COUNTS=[('base','剩余商品支数'),('zero','缺货商品支数'),('rate','大仓缺货率'),('excluded','剔除支数'),('pending','待核支数')]

DETAIL_MODES={'all':'全部商品（含剔除）','included':'纳入基数','excluded':'已剔除','pending':'待核'}
ROWS_PER_SHEET=1_000_000


def selected_combinations(values,include_detail):
    if values==[] and include_detail:return []
    if values==[]:raise ValueError('请至少勾选一个切面，或附带商品明细')
    return combinations(values)


def export(repo,options,destination,combos,*,include_detail=True,detail_mode=None,cancel=None,progress=lambda _:None):
    options=metrics.validate(options);combos=selected_combinations(combos,include_detail);target=Path(destination).resolve()
    if target.suffix.lower()!='.xlsx' or target.exists():raise ValueError('请选择尚不存在的.xlsx文件')
    checkpoint(cancel);target.parent.mkdir(parents=True,exist_ok=True);resource_check(target.parent)
    temp=target.with_name('.warehouse-'+uuid.uuid4().hex+'.xlsx')
    try:
        with stream_book() as book:
            write_report(book,repo,options,combos,include_detail=include_detail,detail_mode=detail_mode,cancel=cancel,progress=progress)
            checkpoint(cancel);progress('数据已写入，正在保存大仓Excel…');book.save(temp);checkpoint(cancel);publish_new(temp,target)
    finally:temp.unlink(missing_ok=True)
    return str(target)


def write_report(book,repo,options,combos,*,include_detail=True,detail_mode=None,cancel=None,progress=lambda _:None):
    import re
    options=metrics.validate(options);combos=selected_combinations(combos,include_detail)
    detail_options=metrics.validate({**options,'mode':detail_mode if detail_mode is not None else options.get('mode','all')})
    totals=metrics.query(repo,options)
    names=set()
    def write(name,headers,rows):
        clean=re.sub(r'[\\/*?:\[\]\x00-\x1f]','_',name).strip(" '") or '切面';base=clean[:31];name=base;n=1
        while name.casefold() in names:
            n+=1;suffix='_'+str(n);name=base[:31-len(suffix)]+suffix
        widths={i+1:96 if h=='说明' else 42 if h in ('商品名称','供应商名称') else 28 if h=='日期范围' else 22 for i,h in enumerate(headers)}
        names.add(name.casefold());write_table(book,name,headers,rows,cancel=cancel,progress=progress,style='single',widths=widths,formats={'大仓缺货率':'0.00%'})
    def paged(name,headers,rows):
        from itertools import chain,islice
        iterator=iter(rows);index=1
        while True:
            first=next(iterator,None)
            if first is None:
                if index==1:write(name,headers,[])
                break
            write(name+str(index),headers,chain([first],islice(iterator,ROWS_PER_SHEET-1)));index+=1
    write('范围汇总',['日期范围']+[v for _,v in COUNTS],[[options['start']+' 至 '+options['end']]+[totals[k] for k,_ in COUNTS]])
    for combination in combos:
        keys=metrics.dimensions(combination['dimensions']);columns=keys+[k for k,_ in COUNTS]
        paged(combination['name'],[metrics.DIMENSIONS[k][1] for k in keys]+[v for _,v in COUNTS],([r[k] for k in columns] for r in metrics.iter_cuts(repo,options,keys,cancel=cancel)))
    if include_detail:
        # Roll over explicitly rather than silently losing Excel overflow.
        paged('商品明细',[v for _,v in DETAIL],([row.get(k) for k,_ in DETAIL] for row in metrics.iter_details(repo,detail_options,cancel=cancel)))
    filters=[[metrics.DIMENSIONS[k][1],'、'.join(v)] for k,v in options.get('filters',{}).items() if v]
    write('口径与覆盖',['项目','说明'],[
        ['导出时间',now()],['日期范围',options['start']+' 至 '+options['end']],['搜索条件',options.get('search','').strip() or '不限'],
        *[['筛选：'+key,value] for key,value in filters],
        ['筛选规则','同一维度多选为或，不同维度为且；包含全部匹配结果，不受页面200行限制。'],
        ['已选切面','、'.join(c['name'] for c in combos) or '仅商品明细'],
        ['商品明细范围',DETAIL_MODES[detail_options['mode']] if include_detail else '不附带'],
        ['汇总商品范围',DETAIL_MODES[options.get('mode','all')]],
        ['明细与汇总','明细范围只限制商品明细；范围汇总和切面仍按日期、搜索及多维筛选统计。'],
        ['分母','剔除虚拟分类所有淘汰品及精确淘便考核、仓禁用品、商品标签精确日配后的商品支数；不要求有月销。'],
        ['统计口径版本',metrics.VERSION],
        ['分子','分母范围中可用库存≤0的商品支数；负库存计缺货。'],['待核','资料缺失不当作0；存在影响统计的待核行时不出百分比。'],
        ['期间','按仓商品日加权；不同日期不合并为一次库存，未采日期不补0。'],
        ['已观测范围','列出日期内已有快照；不是筛选后的商品数，也不表示所有日期均已采集。'],
        ['独立性','原件和分析库独立，不更新补货业务或门店缺货率资料。']])
    write('已观测范围',['观测日期','供货仓','观测时间','原件商品支数'],([r[k] for k in ('day','warehouse','observed','rows')] for r in totals['coverage']))
