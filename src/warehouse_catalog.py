"""Externally labelled warehouse snapshots and demand ownership, not stock reservation."""
from __future__ import annotations
import copy,hashlib,json
from collections import Counter
from datetime import datetime
from pathlib import Path
import openpyxl
from openpyxl.cell import WriteOnlyCell
import engine
from priority_list import atomic_json

from app_defaults import WAREHOUSES
from data_contracts import InputBatch
REQUIRED=['商品编码','分类','基本售价','出库箱规','商品状态','可用数']
FIELDS=REQUIRED+['商品名称','商品等级','补货模型分类','实际库存数','采购在途数','订单占有数']

def revision(items):return hashlib.sha256(json.dumps(items,ensure_ascii=False,sort_keys=True,allow_nan=False).encode()).hexdigest()[:16]

def validate(data):
    if not isinstance(data,dict) or data.get('schema')!=1 or data.get('warehouse') not in WAREHOUSES or not isinstance(data.get('items'),dict) or not data['items']:raise ValueError('仓资料快照格式错误')
    for sku,row in data['items'].items():
        if not isinstance(row,dict) or engine.adapter.identifier(row.get('商品编码'))!=(sku,None):raise ValueError('仓资料存在无效商品编码')
        if not all(k in row for k in REQUIRED):raise ValueError('仓资料缺少必需字段')
    if revision(data['items'])!=data.get('revision'):raise ValueError('仓资料版本与内容不一致，请重新导入')
    return data

def import_catalog(path,warehouse,progress=lambda s:None,cancel=None):
    if warehouse not in WAREHOUSES:raise ValueError('请明确选择河北仓或山东仓')
    path=Path(path).resolve();before=engine.adapter.digest(path);sheet,heads=engine.sheet_name(path,'products')
    progress('读取'+warehouse+'商品资料…');rows,info=engine.adapter.read_table(path,sheet,REQUIRED)
    if set(FIELDS)&set(info['duplicate_unused_headers']):raise ValueError('商品资料存在重复业务表头')
    items={}
    for raw in rows:
        engine.checkpoint(cancel);sku,error=engine.adapter.identifier(raw.get('商品编码'))
        if error:raise ValueError(f"第{raw['_row']}行商品编码待核：{error}；未更新旧资料")
        if sku in items:raise ValueError(f"本仓商品编码重复：{sku}，第{items[sku]['_row']}和{raw['_row']}行；请处理后导入")
        row={k:raw.get(k) for k in FIELDS};row.update(商品编码=sku,_row=raw['_row']);items[sku]=row
    if before!=engine.adapter.digest(path):raise ValueError('读取期间文件发生变化，请重新导入')
    data={'schema':1,'warehouse':warehouse,'source_path':str(path),'source_sha256':before,'sheet':sheet,'imported_at':datetime.now().isoformat(timespec='seconds'),'items':items,'revision':revision(items)}
    return validate(data)

def metadata(catalog):return {k:v for k,v in catalog.items() if k!='items'}|{'count':len(catalog['items'])}

class Repository:
    def __init__(self,folder):self.folder=Path(folder)
    def catalogs(self):
        result={}
        for wh in WAREHOUSES:
            p=self.folder/'warehouse_catalogs'/(wh+'.json')
            if p.exists():
                data=validate(json.loads(p.read_text(encoding='utf-8')))
                if data['warehouse']!=wh:raise ValueError('仓资料标签与保存位置不一致')
                result[wh]=data
        return result
    def save(self,data):
        validate(data);atomic_json(self.folder/'warehouse_catalogs'/(data['warehouse']+'.json'),data)
    def routes(self):
        p=self.folder/'store_supply.json'
        if not p.exists():return {}
        data=json.loads(p.read_text(encoding='utf-8'))
        if not isinstance(data,dict):raise ValueError('门店主从仓设置格式错误')
        for route in data.values():validate_route(route,WAREHOUSES)
        return data
    def save_route(self,store,route):
        validate_route(route,WAREHOUSES);data=self.routes();data[store]=route;atomic_json(self.folder/'store_supply.json',data)

def validate_route(route,catalogs):
    if not isinstance(route,dict) or route.get('primary') not in WAREHOUSES:raise ValueError('请选择本店主仓')
    primary=route['primary'];secondary=route.get('secondary')
    if secondary not in (None,*WAREHOUSES) or secondary==primary:raise ValueError('从仓必须与主仓不同，单仓供货请选择“无从仓”')
    for wh in [primary,secondary]:
        if wh and wh not in catalogs:raise ValueError(wh+'资料尚未导入，请先维护该仓资料')
    return route

def write_catalog(catalog,path):
    book=openpyxl.Workbook(write_only=True);ws=book.create_sheet('商品资料');ws.append(FIELDS+['仓归属'])
    for row in catalog['items'].values():
        cells=[]
        for value in [row.get(k) for k in FIELDS]+[catalog['warehouse']]:
            cell=WriteOnlyCell(ws,value)
            if isinstance(value,str):cell.data_type='s'
            cells.append(cell)
        ws.append(cells)
    book.save(path);book.close()

def prepare_warehouses(sales_path,catalogs,tmp,progress,cancel):
    """Run the existing adapter separately per warehouse, preventing cross-warehouse duplicate keys."""
    results={};base=None
    for wh,catalog in catalogs.items():
        validate(catalog);path=Path(tmp)/(wh+'.xlsx');write_catalog(catalog,path)
        loaded=engine.load_inputs(sales_path,path,progress,cancel)
        for d,m in loaded['normalized']:
            row=catalog['items'].get(d['E'])
            m.update(product_file=catalog['source_path'],product_sheet=catalog['sheet'],product_revision=catalog['revision'])
            if row:
                m.update(product_row=row['_row'],warehouse_available=engine.adapter.numeric(row.get('可用数')))
            if d['_source']=='资料匹配':d['AU']=wh
        results[wh]=loaded['normalized']
        if base is None:base=loaded
        else:base['config']['category_profiles'].update(loaded['config']['category_profiles'])
    if base is None:raise ValueError('请至少导入一份带仓库标签的商品资料')
    lists=list(results.values());preview=[]
    for i in range(len(lists[0])):
        pairs=[rs[i] for rs in lists]
        preview.append(copy.deepcopy(next((pair for pair in pairs if pair[0]['_source']=='资料匹配'),pairs[0])))
    base.update(normalized=preview,warehouse_rows=results,catalogs={wh:metadata(c) for wh,c in catalogs.items()},warehouses=list(catalogs))
    return base

def assessment(pair):
    d,m=pair
    if m['match']!='商品编码精确匹配':return '无该商品'
    if str(d.get('S') or '').strip()=='禁用':return '禁用'
    available=m.get('warehouse_available')
    if not engine.numeric(available):return '可用数待核'
    return '有货' if available>0 else '无货'

def route_store(loaded:InputBatch,store:str,route) -> InputBatch:
    validate_route(route,loaded['catalogs']);primary=route['primary'];secondary=route.get('secondary')
    variants={w:[x for x in rows if x[0]['B']==store] for w,rows in loaded['warehouse_rows'].items()};base=[p for p in loaded['normalized'] if p[0]['B']==store];rows=[]
    for i,original in enumerate(base):
        p=variants[primary][i];state=assessment(p);target=primary
        if state in {'禁用','无货','无该商品'} and secondary:target=secondary
        d,m=copy.deepcopy(variants[target][i]);known=any(v[i][1]['match']=='商品编码精确匹配' for v in variants.values())
        child=assessment(variants[secondary][i]) if secondary else '未配置'
        reason=f'主仓{state}，需求归属'+('从仓' if target!=primary else '主仓')
        if target!=primary:reason+=f'；从仓{child}'
        if state=='可用数待核':reason='主仓可用数待核，保留主仓归属；未将缺失库存当成0'
        if known and m['match']!='商品编码精确匹配':
            d['_source']='匹配待核';m['blocking']=[s for s in m['blocking'] if s!='待补商品资料'];m['blocking'].append('归属仓未匹配商品资料，价格与箱规待核')
        d['AU']='地采' if d['_source']=='地采商品' else target
        if not known:reason='已导入的仓资料均未匹配，标记地采' if d['_source']=='地采商品' else '商品编码或资料匹配待核'
        m['routing']={'primary':primary,'secondary':secondary,'assigned':d['AU'],'primary_state':state,'secondary_state':child,'primary_stock':p[1].get('warehouse_available'),'secondary_stock':variants[secondary][i][1].get('warehouse_available') if secondary else None,'reason':reason,'version':'0.5.0'}
        rows.append((d,m))
    return {**loaded,'normalized':rows,'supply_route':copy.deepcopy(route)}
