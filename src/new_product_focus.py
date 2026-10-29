"""Explicit monthly focus-list import. Membership never changes model or budget priority."""
import hashlib,json,copy
from datetime import date
from pathlib import Path
import openpyxl
import input_adapter as adapter
import work_repository as wr
from priority_list import atomic_json

TRUE={'关注','已关注','是','1','true','√','✓'}
FALSE={'','未关注','不关注','否','0','false'}

def inspect(path,progress=lambda s:None,cancel=None):
    from engine import checkpoint
    path=Path(path);before=adapter.digest(path);book=openpyxl.load_workbook(path,read_only=True,data_only=True,keep_links=False);choices={}
    try:
        for ws in book:
            checkpoint(cancel);ws.reset_dimensions();headers=list(next(ws.values,()))
            if any(x is not None for x in headers):choices[ws.title]=[str(x).strip() for x in headers if x is not None and str(x).strip()]
    finally:book.close()
    if adapter.digest(path)!=before:raise ValueError('新品明细在读取期间变化，请重新导入')
    if not choices:raise ValueError('新品明细没有可识别的表头')
    return {'path':str(path),'sha256':before,'sheets':choices}

def revision(data):
    return hashlib.sha256(wr.dumps({k:v for k,v in data.items() if k not in {'revision','saved_at'}}).encode()).hexdigest()

def validate(data):
    if not isinstance(data,dict) or data.get('schema')!=1:raise ValueError('新品关注清单格式无效')
    month=data['month']
    if date.fromisoformat(month+'-01').strftime('%Y-%m')!=month:raise ValueError('关注清单月份格式须为YYYY-MM')
    if data.get('revision')!=revision(data):raise ValueError('新品关注清单校验不一致，请重新导入')
    codes=set()
    for item in data['items']:
        code,error=adapter.identifier(item['sku'])
        if error or code!=item['sku'] or code in codes or type(item['focused']) is not bool:raise ValueError('关注清单存在不可靠编码、重复或状态')
        codes.add(code)
    return data

def empty(month):
    data={'schema':1,'month':month,'name':'本月无关注新品（明确设置）','items':[],'source_sha256':'','source_path':'','mapping':{},'saved_at':wr.now()};data['revision']=revision(data);return validate(data)

def import_list(info,sheet,sku_field,focus_field,month,progress=lambda s:None,cancel=None):
    from engine import checkpoint
    path=Path(info['path']);checkpoint(cancel)
    if adapter.digest(path)!=info['sha256']:raise ValueError('文件在预览后变化，请重新导入')
    if sku_field==focus_field:raise ValueError('商品编码列和关注列不能相同')
    rows,meta=adapter.read_table(path,sheet,[sku_field,focus_field]);headers=meta['headers']
    if headers.count(sku_field)!=1 or headers.count(focus_field)!=1:raise ValueError('编码/关注表头不唯一')
    items={}
    for row in rows:
        checkpoint(cancel);sku,error=adapter.identifier(row.get(sku_field))
        if error:raise ValueError(f"第{row['_row']}行商品编码待核：{error}")
        raw=row.get(focus_field)
        token='' if raw is None else '1' if raw is True else '0' if raw is False else str(int(raw)) if isinstance(raw,(int,float)) and raw in (0,1) else str(raw).strip().lower()
        if token not in TRUE|FALSE:raise ValueError(f"第{row['_row']}行关注值无法识别：{raw!r}；请用关注/未关注、是/否或1/0")
        focused=token in TRUE
        if sku in items:
            if items[sku]['focused']!=focused:raise ValueError('同一商品关注状态冲突：'+sku)
            items[sku]['source_rows'].append(row['_row'])
        else:items[sku]={'sku':sku,'focused':focused,'name':str(row.get('商品名称') or ''),'source_rows':[row['_row']]}
    if not items:raise ValueError('新品明细没有商品；若本月无关注新品，请在界面明确设置')
    if adapter.digest(path)!=info['sha256']:raise ValueError('读取期间新品明细变化，请重新导入')
    data={'schema':1,'month':month,'name':path.stem,'items':list(items.values()),'source_path':str(path),'source_sha256':info['sha256'],'mapping':{'sheet':sheet,'sku_field':sku_field,'focus_field':focus_field},'saved_at':wr.now()};data['revision']=revision(data);return validate(data)

def members(data):return {x['sku'] for x in data['items'] if x['focused']} if data else set()

class Repository:
    def __init__(self,folder):self.path=Path(folder)/'new_product_focus.json'
    def load(self):return validate(json.loads(self.path.read_text(encoding='utf-8'))) if self.path.exists() else None
    def save(self,data):atomic_json(self.path,validate(data))


def require_for(data,day,results):
    from new_products import is_new
    from result_semantics import work_eligible
    if data is not None:validate(data)
    if any(is_new(d) and work_eligible(d) for r in results for d in r['rows']):
        if data is None:raise ValueError('请先导入新品明细的关注清单，或明确设置“本月无关注新品”；不能猜测新品分组')
        if data['month']!=day[:7]:raise ValueError('新品关注清单月份与业务日期不符，请更新后确认归档')
