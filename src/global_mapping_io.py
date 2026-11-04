"""Named-header global mapping workbook, deliberately without store identifiers."""
import os,tempfile
from pathlib import Path
from openpyxl import Workbook,load_workbook
from runtime_support import checkpoint
from store_parameter_import import digest


def read(path,progress=lambda _:None,cancel=None):
    before=digest(path);result={};wb=load_workbook(path,read_only=True,data_only=False)
    try:
        if '全局分类映射' not in wb.sheetnames:raise ValueError('缺少“全局分类映射”工作表，请使用本版模板')
        ws=wb['全局分类映射'];ws.reset_dimensions();header=None
        for rn,cells in enumerate(ws.iter_rows(),1):
            checkpoint(cancel)
            vals=[c.value for c in cells]
            if header is None:
                if rn>20:break
                names=[str(v or '').strip() for v in vals]
                if not {'商品分类','目标分类组'}<=set(names):continue
                if len([h for h in names if h])!=len(set(h for h in names if h)):raise ValueError('重复表头')
                if set(names)-{'商品分类','目标分类组',''}:raise ValueError('全局映射只填写商品分类和目标分类组，不填写门店编码')
                header=names;continue
            if all(v in (None,'') for v in vals):continue
            if any(c.data_type in ('f','e') for c in cells):raise ValueError(f'第{rn}行不能使用公式或错误值')
            if any(v not in (None,'') and (i>=len(header) or not header[i]) for i,v in enumerate(vals)):raise ValueError(f'第{rn}行数据列缺少表头')
            pair=[str(vals[header.index(h)] or '').strip() if header.index(h)<len(vals) else '' for h in ('商品分类','目标分类组')]
            if not all(pair):raise ValueError(f'第{rn}行商品分类和目标分类组都必须填写')
            if pair[0] in result:raise ValueError(f'第{rn}行商品分类重复：'+pair[0])
            result[pair[0]]=pair[1]
        if not result:raise ValueError('没有可导入的映射')
    finally:wb.close()
    if digest(path)!=before:raise ValueError('读取期间文件变化，请重新导入')
    return result


def write(path,mapping=None,progress=lambda _:None,cancel=None):
    target=Path(path)
    if target.exists():raise FileExistsError('目标文件已存在，请另存')
    wb=Workbook();ws=wb.active;ws.title='全局分类映射';ws.append(['商品分类','目标分类组'])
    for k,v in (mapping or {}).items():ws.append([k,v])
    for row in ws:
        for cell in row:
            if isinstance(cell.value,str):cell.data_type='s'
    ws.freeze_panes='A2';ws.column_dimensions['A'].width=35;ws.column_dimensions['B'].width=35
    note=wb.create_sheet('填写说明');note.append(['必填','商品分类、目标分类组；所有门店共用，不填门店编码。'])
    note.append(['导入方式','导入填充完整映射表，核对后保存；未列出的分类不再映射。未知分类继续使用原无分类规则。'])
    note.column_dimensions['B'].width=110
    fd,tmp=tempfile.mkstemp(dir=target.parent,suffix='.xlsx');os.close(fd)
    try:wb.save(tmp);checkpoint(cancel);os.link(tmp,target)
    finally:wb.close();Path(tmp).unlink(missing_ok=True)
    return str(target)
