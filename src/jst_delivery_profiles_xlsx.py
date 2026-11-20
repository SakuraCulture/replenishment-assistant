"""Spreadsheet for previewing patches to local, warehouse-specific delivery profiles."""
from contextlib import closing
from pathlib import Path
from shutil import copyfile
import tempfile
from zipfile import BadZipFile

import openpyxl
from openpyxl.utils import get_column_letter

from app_defaults import WAREHOUSES
from export_files import publish_new, sha256
from jst_auto_order_template import CONTACT
from jst_delivery_profiles import clean_profile
from runtime_support import checkpoint


SHEET = '配送资料'
IDENTITY = ('门店编码','门店名称','供货仓')
VERSION = '资料版本'
FIELDS = {title:key for key,(_,title) in CONTACT.items()}
HEADERS = (*IDENTITY,*FIELDS,VERSION)
UNCONFIGURED = '未配置'
TEMPLATE = Path(__file__).with_name('jst-delivery-profile-template.xlsx')


def _publish_workbook(path, write, cancel):
    path=Path(path)
    if path.suffix.lower()!='.xlsx' or path.exists():
        raise ValueError('请选择一个新的 .xlsx 文件')
    checkpoint(cancel)
    with tempfile.NamedTemporaryFile(dir=path.parent,suffix='.xlsx',delete=False) as stream:
        temporary=Path(stream.name)
    try:
        write(temporary)
        checkpoint(cancel)
        publish_new(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)
    return str(path)


def download_template(path, progress=lambda _:None, cancel=None):
    return _publish_workbook(path,lambda target:copyfile(TEMPLATE,target),cancel)


def export(path, data, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    with closing(openpyxl.Workbook()) as book:
        sheet=book.active
        sheet.title=SHEET
        sheet.append(HEADERS)
        for code,name in sorted(data['names'].items()):
            for warehouse in WAREHOUSES:
                checkpoint(cancel)
                record=data['records'].get((code,warehouse)) or {}
                values=record.get('values',{})
                sheet.append([code,name,warehouse,*(values.get(k,'') for k in CONTACT),
                              record.get('version',UNCONFIGURED)])
        for row in sheet:
            for cell in row:
                if isinstance(cell.value,str):
                    cell.data_type='s'
                cell.number_format='@'
        widths=(23,33,13,23,19,20,16,16,16,54,20)
        for index,width in enumerate(widths,1):
            sheet.column_dimensions[get_column_letter(index)].width=width
        sheet.column_dimensions[get_column_letter(len(HEADERS))].hidden=True
        sheet.freeze_panes='D2'
        sheet.auto_filter.ref=sheet.dimensions
        guide=book.create_sheet('填写说明')
        guide.column_dimensions['A'].width=18
        guide.column_dimensions['B'].width=92
        for row in (
            ('维护范围','每行按门店编码和供货仓更新一份配送资料。门店名称供核对。'),
            ('填写方式','使用文本填写买家帐号、手机和地址。留空的字段沿用软件内已保存的值。'),
            ('版本核对','保留模板中的资料版本列。发生变更冲突时，请重新导出并核对。'),
            ('保存方式','把修改后的文件导回软件，查看新旧值并确认保存。'),
        ):
            guide.append(row)
        return _publish_workbook(path,book.save,cancel)


def _columns(cells):
    heads=[str(cell.value or '').strip() for cell in cells]
    if any(heads.count(key)>1 for key in HEADERS):
        raise ValueError('配送资料表头有重复列')
    columns={name:index for index,name in enumerate(heads) if name in HEADERS}
    if not {'门店编码','供货仓'}<=columns.keys() or not FIELDS.keys()&columns.keys():
        raise ValueError('配送资料表需有门店编码、供货仓和至少一个配送字段')
    return columns


def _row_values(cells, columns):
    result={}
    for name,index in columns.items():
        if name=='门店名称' or index>=len(cells):
            continue
        cell=cells[index]
        if cell.value is None:
            continue
        if cell.data_type in ('f','e'):
            raise ValueError(f'{name}请填写实际文本，处理公式或单元格错误')
        if not isinstance(cell.value,str):
            raise ValueError(f'{name}请填写为文本，保留前导零')
        if cell.value.strip():
            result[name]=cell.value
    return result


def plan(path, data, progress=lambda _:None, cancel=None):
    checkpoint(cancel)
    before_file=sha256(path)
    try:
        book=openpyxl.load_workbook(path,read_only=True,data_only=False,keep_links=False)
    except (BadZipFile,openpyxl.utils.exceptions.InvalidFileException) as exc:
        raise ValueError('请选择 .xlsx 配送资料导入模板或导出的配送资料表') from exc
    changes=[]
    seen=set()
    with closing(book):
        if SHEET not in book:
            raise ValueError('找不到“配送资料”工作表，请使用配送资料表')
        rows=iter(book[SHEET].iter_rows())
        columns=_columns(next(rows,()))
        for rownum,cells in enumerate(rows,2):
            checkpoint(cancel)
            try:
                values=_row_values(cells,columns)
                if not values:
                    continue
                code=values.get('门店编码','').strip()
                warehouse=values.get('供货仓','').strip()
                key=code,warehouse
                if not code or code not in data['names']:
                    raise ValueError('门店编码未填写或不在本机门店目录中')
                if warehouse not in WAREHOUSES:
                    raise ValueError('供货仓请填写河北仓或山东仓')
                if key in seen:
                    raise ValueError(f'{code} / {warehouse} 重复填写，请保留一行')
                seen.add(key)
                patch={field:values[title] for title,field in FIELDS.items() if title in values}
                if not patch:
                    continue
                record=data['records'].get(key) or {}
                old={field:record.get('values',{}).get(field,'') for field in CONTACT}
                after=clean_profile(warehouse,{**old,**patch})
                if after==old:
                    continue
                version=record.get('version')
                if VERSION in columns and values.get(VERSION)!=(version or UNCONFIGURED):
                    raise ValueError(f'{code} / {warehouse} 的配送资料已更新或版本缺失，请重新导出后核对')
                changes.append({'row':rownum,'store':code,'warehouse':warehouse,'version':version,
                                'before':old,'after':after})
            except ValueError as exc:
                raise ValueError(f'配送资料第 {rownum} 行，{exc}') from None
    if sha256(path)!=before_file:
        raise ValueError('读取期间配送资料表已变化，请重新选择文件')
    return changes
