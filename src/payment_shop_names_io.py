"""Dedicated four-column template; no complete-parameter import or save calls."""
import hashlib,os,tempfile
from pathlib import Path
import openpyxl
from runtime_support import checkpoint
import payment_shop_names as service

HEADERS=['门店编码','应用门店名称','山东仓聚水潭店铺名称','河北仓聚水潭店铺名称']
FIELDS=dict(zip(HEADERS[2:],service.WAREHOUSES))

def sha(path):
    with Path(path).open('rb') as f:return hashlib.file_digest(f,'sha256').hexdigest()

def export(path,data,codes=None,progress=lambda _:None,cancel=None):
    from export_files import publish_new
    path=Path(path)
    if path.exists() or path.suffix.lower()!='.xlsx':raise ValueError('请选择新的.xlsx文件，保留已有文件')
    codes=list(data['names']) if codes is None else list(codes)
    if any(c not in data['names'] for c in codes):raise ValueError('导出门店已不在当前范围')
    path.parent.mkdir(parents=True,exist_ok=True);fd,tmp=tempfile.mkstemp(dir=path.parent,suffix='.xlsx');os.close(fd)
    book=openpyxl.Workbook();sheet=book.active;sheet.title='聚水潭店铺名称';sheet.append(HEADERS);sheet.freeze_panes='C2'
    for col,width in zip('ABCD',(24,38,42,42)):sheet.column_dimensions[col].width=width
    try:
        for code in codes:
            checkpoint(cancel);sheet.append([code,data['names'][code],*[(data['records'].get(service.key(code,w)) or {}).get('name','') for w in service.WAREHOUSES]])
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
            row[0].number_format='@'
        sheet.auto_filter.ref=sheet.dimensions
        note=book.create_sheet('填写说明');note.append(['事项','说明'])
        for row in [('用途','仅维护两仓聚水潭店铺名称，不修改补货参数。'),('必填','门店编码；名称从对应仓付款表的“店铺名称”列复制。'),('空白','空白保持原值；清空使用维护窗口中的明确清空操作。'),('应用门店名称','只用于核对，不修改应用门店名称。'),('其他列','不参与名称更新。'),('免维护','没有配置也能导入付款，存在多仓候选时请选择原单。')]:note.append(row)
        book.save(tmp);checkpoint(cancel);publish_new(tmp,path)
    finally:book.close();Path(tmp).unlink(missing_ok=True)
    return str(path)

def read(path,data,progress=lambda _:None,cancel=None):
    path=Path(path);before=sha(path);book=openpyxl.load_workbook(path,read_only=True,data_only=False,keep_links=False)
    rows=[];seen={};notices=[];candidates=[]
    try:
        for sheet in book:
            for n,cells in enumerate(sheet.iter_rows(),1):
                if n>20:break
                heads=[str(c.value or '').strip() for c in cells]
                if '门店编码' in heads and any(h in FIELDS for h in heads):candidates.append((sheet.title,n,heads))
        if len(candidates)!=1:raise ValueError('需要且只能有一张含“门店编码”和两仓名称列的数据表，请使用专用模板')
        title,n,heads=candidates[0]
        if any(heads.count(k)>1 for k in ['门店编码',*FIELDS]):raise ValueError('名称模板表头重复')
        extras=[h for h in heads if h and h not in HEADERS and h!='门店名称']
        if extras:notices.append('以下列不参与保存：'+'、'.join(extras))
        sheet=book[title];sheet.reset_dimensions()
        for rownum,cells in enumerate(sheet.iter_rows(min_row=n+1),n+1):
            checkpoint(cancel)
            if not any(c.value is not None for c in cells):continue
            def cell(k):return cells[heads.index(k)] if k in heads and heads.index(k)<len(cells) else None
            c=cell('门店编码');code='' if c is None or c.value is None else str(c.value).strip();error='';patches=[]
            if c is not None and c.data_type=='f':error='门店编码不能使用公式'
            elif not code:error='门店编码必填'
            elif code not in data['names']:error='未知门店编码，请先核对门店目录'
            elif code in seen:
                error='本批门店编码重复；请保留一行';rows[seen[code]]['error']=error
            if code not in seen:seen[code]=len(rows)
            for field,wh in FIELDS.items():
                c=cell(field)
                if c is None or c.value is None or not str(c.value).strip():continue
                if c.data_type=='f':error=error or '店铺名称不能使用公式';continue
                if not isinstance(c.value,str):error=error or '店铺名称须为文本';continue
                patches.append({'store':code,'warehouse':wh,'value':c.value})
            changes=[]
            if not error:
                try:changes=service.prepare(data,patches)['changes']
                except ValueError as exc:error=str(exc)
            added=sum(not (c['before'] or {}).get('name') for c in changes)
            status=f'新增 {added} 项，修改 {len(changes)-added} 项' if changes else '保持原值'
            rows.append({'row':rownum,'store':code,'name':data['names'].get(code,''),'patches':patches,'error':error,'status':status})
    finally:book.close()
    if sha(path)!=before:raise ValueError('读取期间名称文件变化，请重新读取')
    return {'path':str(path.resolve()),'sha256':before,'rows':rows,'notices':notices}

def prepare(data,imported,selected,progress=lambda _:None,cancel=None):
    if sha(imported['path'])!=imported['sha256']:raise ValueError('预览后名称文件变化，请重新导入')
    patches=[]
    for i in selected:
        checkpoint(cancel);row=imported['rows'][i]
        if row['error']:raise ValueError(f"第{row['row']}行：{row['error']}；请明确排除此行后保存")
        patches.extend(row['patches'])
    return service.prepare(data,patches,origin='import',cancel=cancel)
