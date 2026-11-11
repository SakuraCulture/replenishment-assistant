"""Streaming, independent Excel reports with explicit metric and coverage evidence."""
from pathlib import Path
from contextlib import closing
import os
import re
import uuid
from stockout_common import VIEWS,VERSION,TAG_VERSION,now,resource_check
from runtime_support import checkpoint
import stockout_metrics as metrics
from stockout_views import combinations as validate_combinations

SHEET_ROWS=1000000  # Data rows; below Excel's 1,048,576 row limit, including the header.


def export(repo,options,destination,*,include_detail=False,dimensions=('category',),combinations=None,cancel=None,progress=lambda _:None):
    import openpyxl
    options=metrics.validate(options)
    combinations=validate_combinations(combinations) if combinations is not None else [
        {'name':title,'dimensions':list(dims)} for title,dims in
        [('门店汇总',('store',)),('四类分层',('source','mandatory')),('当前切面',metrics.dimension_keys(dimensions))]]
    target=Path(destination).resolve()
    if target.exists() or target.suffix.lower()!='.xlsx':
        raise ValueError('请选择尚不存在的.xlsx文件，不覆盖原报表')
    target.parent.mkdir(parents=True,exist_ok=True)
    resource_check(target.parent)
    temp=target.with_name('.stockout-'+uuid.uuid4().hex+'.xlsx')
    book=openpyxl.Workbook(write_only=True)
    try:
        _write(book,repo,options,include_detail,combinations,cancel,progress)
        resource_check(target.parent,sum(Path(ws._writer.out).stat().st_size for ws in book.worksheets if ws._writer))
        checkpoint(cancel);book.save(temp);checkpoint(cancel)
        os.link(temp,target)
        return str(target)
    finally:
        # write_only worksheets own temporary XML files even before save().
        # Close generators and remove only this workbook's temporary outputs.
        for ws in book.worksheets:
            if not ws.closed:ws.close()
            writer=ws._writer
            if writer and Path(writer.out).exists():writer.cleanup()
        book.close();temp.unlink(missing_ok=True)


def _write(book,repo,options,include_detail,combinations,cancel,progress):
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font,PatternFill,Alignment
    from openpyxl.utils import get_column_letter
    heading=Font(bold=True,color='FFFFFF');fill=PatternFill('solid',fgColor='147D73')
    def append(sheet,row):
        cells=[]
        for index,value in enumerate(row):
            cell=WriteOnlyCell(sheet,value)
            if isinstance(value,str):cell.data_type='s'
            headers=getattr(sheet,'_stockout_headers',[])
            if isinstance(value,(int,float)) and index<len(headers) and headers[index]=='门店缺货率':cell.number_format='0.00%'
            if sheet._stockout_rows==0:cell.font=heading;cell.fill=fill;cell.alignment=Alignment(wrap_text=True,vertical='center')
            cells.append(cell)
        sheet.append(cells);sheet._stockout_rows+=1
    def sheet(name,headers,filterable=True):
        name=re.sub(r'[\\/*?:\[\]\x00-\x1f]','_',name).strip(" '") or '切面'
        base=name[:31];name=base;n=2
        while name.casefold() in {s.casefold() for s in book.sheetnames}:
            suffix='_'+str(n);name=base[:31-len(suffix)]+suffix;n+=1
        ws=book.create_sheet(name);ws._stockout_headers=headers;ws._stockout_rows=0;ws._stockout_filterable=filterable
        ws.freeze_panes='A2';ws.sheet_view.showGridLines=False;ws.row_dimensions[1].height=32
        for i,title in enumerate(headers,1):
            width=30 if any(k in title for k in ('名称','供应商','供货商','说明','原文')) else 24 if '编码' in title else max(14,len(title)*2+2)
            ws.column_dimensions[get_column_letter(i)].width=min(40,width)
        append(ws,headers);return ws
    totals=metrics.query(repo,options,cancel=cancel)
    meta=sheet('口径与覆盖',['项目','内容'],False)
    pending=totals['needs_negative_stock_recalculation']
    for row in [('指标','门店缺货率：当前视角中月销>0且门店可用库存≤0的店品数 / 月销>0店品数；负库存按0统计，原始值保留'),
                ('统计口径版本','含旧口径结果，需用留存原件重算' if pending else VERSION),('标签版本',TAG_VERSION),('视角',metrics.view_label(totals['query'])),
                ('旧日报处理','本报表含尚未按负库存计缺货的旧结果；到采集与快照用留存原件重算后再导出' if pending else '当前范围已按负库存按0统计'),
                ('起止日期',totals['query']['start']+' 至 '+totals['query']['end']),('生成时间',now()),
                ('说明','按每日实际观测时点；在途不抵扣；未知不当作0；期间率按店品日分子分母加总'),
                ('剔除说明','按虚拟分类精确识别标杆淘汰系列与淘便考核；同时剔除分子分母，双标去重；标签未知保留'),
                ('状态剔除','禁采按翱象补货状态“停采/禁采”；禁用按所选供货仓商品状态“禁用”；未知状态保留，不剔除备用；与标签剔除合并去重'),
                ('责任说明','供货商、类目、仓均是门店缺货的切面，不自动归因于仓或供货商'),
                ('查询范围',__import__('json').dumps(totals['query'],ensure_ascii=False))]:append(meta,row)
    append(meta,['筛选逻辑','同一维度多选为或，不同维度为且；各切面采用同一日期、剔除视角和筛选。各表是同一批数据的不同汇总，不跨表加总。'])
    append(meta,['地采显示','地采商品的供货仓统一显示地采；历史原始归属记录保持不变。'])
    append(meta,['预计销售天数','商品明细按（可用库存＋在途库存）÷月销×30计算；月销不大于0或必要字段缺失留空并说明，不混加不同商品的库存单位。'])
    coverage=sheet('日报覆盖',['观测日期','日报修订','已完成门店','所选门店','发布时间'])
    for row in totals['coverage']:append(coverage,[row[k] for k in ('day','revision','completed','selected','created')])
    evidence=sheet('快照依据',['观测日期','门店编码','门店名称','观测时间','原件哈希','设置版本','仓资料哈希集合','日报修订','采集任务','当时应用与规则版本'])
    with repo.connect() as db:
        for r in db.execute(f'''SELECT s.*,p.id AS revision,r.run,u.external FROM {metrics.LINKS} JOIN runs u ON u.id=r.run
                             WHERE s.day BETWEEN ? AND ? ORDER BY s.day,s.store''',(totals['query']['start'],totals['query']['end'])):
            checkpoint(cancel)
            if totals['query']['filters'].get('store') and r['store'] not in totals['query']['filters']['store']:continue
            if totals['query']['filters'].get('day') and r['day'] not in totals['query']['filters']['day']:continue
            versions=__import__('json').loads(r['external']).get('versions',{})
            append(evidence,[*[r[k] for k in ('day','store','name','observed','source_sha','basis','catalog_set','revision','run')],__import__('json').dumps(versions,ensure_ascii=False)])
    measure=['有销基数','缺货数','门店缺货率','指标待核数','标签待核有销数','标签待核缺货数','剔除有销数','剔除缺货数','说明']
    keys=['base','zero','rate','bad','unknown_tags','unknown_zero','removed_base','removed_zero','status']
    summary=sheet('总体',[*measure]);append(summary,[totals['total'][k] for k in keys])
    tags=sheet('剔除与标签待核',['视角',*measure])
    for view,name in VIEWS.items():
        checkpoint(cancel)
        value=metrics.query(repo,{**options,'view':view},cancel=cancel)['total']
        append(tags,[metrics.view_label({**options,'view':view}),*[value[k] for k in keys]])
    trend=sheet('每日趋势',['观测日期',*measure])
    for row in metrics.trend(repo,options,cancel=cancel):
        checkpoint(cancel);append(trend,[row['day'],*[row['total'][k] for k in keys]])
    for combination in combinations:
        title=combination['name'];dims=combination['dimensions'];checkpoint(cancel)
        columns=metrics.cut_columns(dims)
        headers=[*[name for _,name in columns],*measure];ws=sheet(title,headers);count=0;part=1
        append(meta,['切面 '+ws.title,'＋'.join(metrics.DIMENSIONS[k][1] for k in dims)])
        progress('正在导出切面：'+title+'（全部分组）')
        with closing(metrics.iter_cuts(repo,options,dims,cancel=cancel)) as records:
            for row in records:
                if count and count%SHEET_ROWS==0:
                    part+=1;ws=sheet(title[:24]+'_续'+str(part),headers);append(meta,['切面续表 '+ws.title,title+' 超过单表容量，全部分组继续保留'])
                append(ws,[*[metrics.dimension_value(row[d],d) for d,_ in columns],*[row[k] for k in keys]]);count+=1
                if count%1000==0:resource_check(repo.folder);progress(f'{title} 已导出 {count} 组')
        append(meta,['切面行数 '+title,count])
    if include_detail:
        query=metrics.validate(options);condition,args=metrics.where(query)
        # A streaming cursor, not OFFSET pagination, keeps large exports linear.
        fields=['day','store','store_name','sku','name','month_raw','stock_raw','inbound_raw','expected_sales_days','expected_sales_days_reason','source','warehouse',
                'mandatory','category','supplier','tags','retired','assessment','eligible','zero','bad','excluded','observed','source_sha','revision','rownum','error','product_status','replen_status']
        headers=['观测日期','门店编码','门店名称','商品编码','商品名称','原月销','原可用库存','原在途','预计销售天数','销售天数说明','供货来源','归属仓','必采标记',
                 '类目','仓库上游供货商','虚拟分类原文','淘汰标记','考核标记','有销资格','缺货标记','指标待核','本视角剔除','观测时间','原件哈希','日报修订','原件行号','说明','仓商品状态','翱象补货状态']
        part=1;count=0;ws=sheet('商品明细1',headers)
        with repo.connect() as db:
            if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
            cursor=db.execute(f'''SELECT s.day,s.store,s.name AS store_name,s.observed,s.source_sha,p.id AS revision,
                ({metrics.excluded_sql(query)}) AS excluded,f.* FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id
                WHERE {condition} ORDER BY s.day,s.store,f.sku''',args)
            for row in cursor:
                checkpoint(cancel)
                if count and count%SHEET_ROWS==0:
                    part+=1;ws=sheet('商品明细'+str(part),headers)
                row=metrics.canonical_record(row)
                append(ws,[metrics.dimension_value(row[k],k) if k=='source' else row[k] for k in fields]);count+=1
                if count%1000==0:
                    resource_check(repo.folder);progress(f'已导出{count}条明细；原补货报表保持不变')
    for ws in book.worksheets:
        if ws._stockout_filterable:ws.auto_filter.ref=f'A1:{get_column_letter(len(ws._stockout_headers))}{ws._stockout_rows}'
