"""Streaming, independent Excel reports with explicit metric and coverage evidence."""
from pathlib import Path
import os
import uuid
from stockout_common import VIEWS,VERSION,TAG_VERSION,now,resource_check
from runtime_support import checkpoint
import stockout_metrics as metrics
from stockout_facts import exclusion


def export(repo,options,destination,*,include_detail=False,dimensions=('category',),cancel=None,progress=lambda _:None):
    import openpyxl
    target=Path(destination).resolve()
    if target.exists() or target.suffix.lower()!='.xlsx':
        raise ValueError('请选择尚不存在的.xlsx文件，不覆盖原报表')
    target.parent.mkdir(parents=True,exist_ok=True)
    resource_check(target.parent)
    temp=target.with_name('.stockout-'+uuid.uuid4().hex+'.xlsx')
    book=openpyxl.Workbook(write_only=True)
    try:
        _write(book,repo,options,include_detail,dimensions,cancel,progress)
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


def _write(book,repo,options,include_detail,dimensions,cancel,progress):
    from openpyxl.cell import WriteOnlyCell
    def append(sheet,row):
        cells=[]
        for index,value in enumerate(row):
            cell=WriteOnlyCell(sheet,value)
            if isinstance(value,str):cell.data_type='s'
            headers=getattr(sheet,'_stockout_headers',[])
            if isinstance(value,(int,float)) and index<len(headers) and headers[index]=='门店缺货率':cell.number_format='0.00%'
            cells.append(cell)
        sheet.append(cells)
    def sheet(name,headers):
        ws=book.create_sheet(name);ws._stockout_headers=headers;append(ws,headers);ws.freeze_panes='A2';return ws
    totals=metrics.query(repo,options,cancel=cancel)
    meta=sheet('口径与覆盖',['项目','内容'])
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
    append(meta,['观测日期','日报修订','已完成门店','所选门店','发布时间'])
    for row in totals['coverage']:append(meta,[row[k] for k in ('day','revision','completed','selected','created')])
    evidence=sheet('快照依据',['观测日期','门店编码','门店名称','观测时间','原件哈希','设置版本','仓资料哈希集合','日报修订','采集任务','当时应用与规则版本'])
    with repo.connect() as db:
        for r in db.execute(f'''SELECT s.*,p.id AS revision,r.run,u.external FROM {metrics.LINKS} JOIN runs u ON u.id=r.run
                             WHERE s.day BETWEEN ? AND ? ORDER BY s.day,s.store''',(totals['query']['start'],totals['query']['end'])):
            checkpoint(cancel)
            if totals['query']['filters'].get('store') and r['store'] not in totals['query']['filters']['store']:continue
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
    for title,dims in [('门店汇总',('store',)),('四类分层',('source','mandatory')),('当前切面',tuple(dimensions))]:
        columns=metrics.cut_columns(dims)
        ws=sheet(title,[*[title for _,title in columns],*measure]);offset=0
        while True:
            checkpoint(cancel);data=metrics.cuts(repo,options,dims,offset=offset,cancel=cancel)
            for row in data['items']:append(ws,[*[row[d] for d,_ in columns],*[row[k] for k in keys]])
            offset+=len(data['items'])
            if offset>=data['total'] or not data['items']:break
    if include_detail:
        query=metrics.validate(options);condition,args=metrics.where(query)
        # A streaming cursor, not OFFSET pagination, keeps large exports linear.
        fields=['day','store','store_name','sku','name','month_raw','stock_raw','inbound_raw','source','warehouse',
                'mandatory','category','supplier','tags','retired','assessment','eligible','zero','bad','excluded','observed','source_sha','revision','rownum','error','product_status','replen_status']
        headers=['观测日期','门店编码','门店名称','商品编码','商品名称','原月销','原可用库存','原在途','供货来源','归属仓','必采标记',
                 '类目','仓库上游供货商','虚拟分类原文','淘汰标记','考核标记','有销资格','缺货标记','指标待核','本视角剔除','观测时间','原件哈希','日报修订','原件行号','说明','仓商品状态','翱象补货状态']
        part=1;count=0;ws=sheet('商品明细1',headers)
        with repo.connect() as db:
            if cancel:db.set_progress_handler(lambda:int(cancel.is_set()),1000)
            cursor=db.execute(f'''SELECT s.day,s.store,s.name AS store_name,s.observed,s.source_sha,p.id AS revision,
                ({metrics.excluded_sql(query)}) AS excluded,f.* FROM {metrics.LINKS} JOIN facts f ON f.snapshot=s.id
                WHERE {condition} ORDER BY s.day,s.store,f.sku''',args)
            for row in cursor:
                checkpoint(cancel)
                if count and count%1000000==0:
                    part+=1;ws=sheet('商品明细'+str(part),headers)
                append(ws,[row[k] for k in fields]);count+=1
                if count%1000==0:
                    resource_check(repo.folder);progress(f'已导出{count}条明细；原补货报表保持不变')
