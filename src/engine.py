"""Desktop input, calculation and export service. No network or reference workbook."""
from __future__ import annotations
import copy,json,math,os,uuid,warnings
from pathlib import Path
from datetime import datetime
from collections import Counter
import openpyxl
import model
import input_adapter as adapter
from one_box_exception import apply_one_box_exception
import allocation

from version_info import VERSION,MODEL_VERSION,diagnostic
from runtime_support import Cancelled,checkpoint
from result_semantics import numeric,fmt,issue_text,exclusion_reason,order_quantity,order_amount,orderable,order_status,summarize,quantity,amount_value,coverage,explanation,routing
from display_fields import COLUMNS,IMPORT_HEADERS,import_audit_rows
from xlsx_exports import export_xlsx
from app_defaults import DEFAULT_WAREHOUSES,load_default_config
from data_contracts import InputBatch,CalculationResult,WarehouseCycles
def config():return load_default_config()
def category_of(d,m):
    if m['match']=='商品编码精确匹配':return '资料匹配'
    if m['match']=='资料未匹配' and not any('编码' in b or '重复' in b for b in m['blocking'] if b!='待补商品资料'):return '地采商品'
    return '匹配待核'
def sheet_name(path,kind):
    warnings.filterwarnings('ignore',message='Workbook contains no default style')
    book=openpyxl.load_workbook(path,read_only=True,data_only=True,keep_links=False)
    try:
        required={'商品编码','日销','周销','月销','仓库/门店编码'} if kind=='sales' else {'商品编码','基本售价','分类','出库箱规'}
        candidates=[]
        for ws in book.worksheets:
            ws.reset_dimensions();row=next(ws.iter_rows(values_only=True),())
            heads={str(x).strip() for x in row if x is not None}
            if required<=heads:candidates.append((ws.title,heads))
        if len(candidates)!=1:
            if not candidates:raise ValueError(('补货建议' if kind=='sales' else '商品资料')+'未找到唯一可用工作表。必需列：'+ '、'.join(sorted(required)))
            raise ValueError('发现多个可用工作表：'+ '、'.join(x[0] for x in candidates)+'；请保留本次要使用的一张表。')
        return candidates[0]
    finally:book.close()

def load_inputs(sales,products,progress=lambda s:None,cancel=None) -> InputBatch:
    sales,products=Path(sales).resolve(),Path(products).resolve()
    if sales==products:raise ValueError('补货建议和商品资料必须选择两份不同的文件。')
    for p in [sales,products]:
        if not p.is_file() or p.suffix.lower()!='.xlsx':raise ValueError(f'请选择有效的 .xlsx 文件：{p.name}')
    before={str(p):adapter.digest(p) for p in [sales,products]};checkpoint(cancel)
    progress('正在识别工作表与字段…')
    ss,sh=sheet_name(sales,'sales');ps,ph=sheet_name(products,'products')
    cfg=_input_config(ss,ps,sh)
    progress('正在读取商品资料…');checkpoint(cancel)
    pr,_=adapter.read_table(products,ps,['商品编码','分类','基本售价','出库箱规'])
    warehouses,profile_overrides=_master_config(cfg,pr)
    progress('正在按商品编码匹配销量与资料…');checkpoint(cancel)
    norm,info=adapter.prepare(sales,products,cfg)
    norm,stores=_decorate_inputs(norm,warehouses,profile_overrides)
    checkpoint(cancel)
    if any(adapter.digest(p)!=h for p,h in before.items()):raise ValueError('读取期间源文件发生变化，请保存Excel后重新导入。')
    return {'normalized':norm,'config':cfg,'info':info,'hashes':before,'sales':str(sales),'products':str(products),'stores':stores,
            'warehouses':sorted({d['AU'] for d,m in norm if d['_source']=='资料匹配'}),'loaded_at':datetime.now().isoformat(timespec='seconds')}

def _input_config(ss,ps,sh):
    cfg=config();cfg['interface'].update(sales_sheet=ss,product_sheet=ps,blank_outbound_unit='one')
    cfg['sales_fields']['unit_type']='基础单位' if '基础单位' in sh else '库存单位' if '库存单位' in sh else '单位类型'
    cfg['weight_profiles']['无分类']=copy.deepcopy(cfg['weight_profiles']['快销'])
    return cfg


def _master_config(cfg,pr):
    warehouses={};categories=set();profile_overrides={}
    for r in pr:
        k,err=adapter.identifier(r.get('商品编码'))
        if not err:
            warehouses[k]=str(r.get('仓归属') or '未填写仓归属').strip()
            profile_overrides[k]=r.get('补货模型分类')
        categories.add(str(r.get('分类')))
    for c in categories:
        if c not in cfg['category_profiles']:cfg['category_profiles'][c]='无分类'
    return warehouses,profile_overrides


def _decorate_inputs(norm,warehouses,profile_overrides):
    stores={}
    for d,m in norm:
        m['profile_override']=profile_overrides.get(d['E'])
        d['_source']=category_of(d,m)
        if m['match']=='商品编码精确匹配':d['AU']=warehouses.get(d['E'],'未填写仓归属')
        elif d['_source']=='地采商品':d['AU']='地采'
        else:d['AU']='待核'
        if d.get('O')=='/':d['O']=1
        stores.setdefault(d['B'],d['A'] or d['B'])
    return norm,stores


def load_records(sales_table,product_table,*,sales_ref,product_ref,progress=lambda s:None,cancel=None) -> InputBatch:
    """Internal tables already read from Excel or explicitly bridge-projected.

    File entry keeps its file validation/hash checks. Batch callers retain original
    source hash checks and provenance before/after calling this adapter.
    """
    from table_records import require_headers
    sr,sinfo=sales_table;pr,pinfo=product_table
    checkpoint(cancel);progress('正在识别工作表与字段…')
    # Same single-sheet recognition performed by load_inputs on generated workbooks.
    for info,kind in [(sinfo,'sales'),(pinfo,'products')]:
        required={'商品编码','日销','周销','月销','仓库/门店编码'} if kind=='sales' else {'商品编码','基本售价','分类','出库箱规'}
        if not required<=set(info['headers']):raise ValueError(('补货建议' if kind=='sales' else '商品资料')+'未找到唯一可用工作表。必需列：'+ '、'.join(sorted(required)))
    cfg=_input_config(sinfo['sheet'],pinfo['sheet'],set(sinfo['headers']))
    progress('正在读取商品资料…');checkpoint(cancel)
    require_headers(pinfo,['商品编码','分类','基本售价','出库箱规'])
    warehouses,profile_overrides=_master_config(cfg,pr)
    progress('正在按商品编码匹配销量与资料…');checkpoint(cancel)
    norm,info=adapter.prepare_records(sr,sinfo,pr,pinfo,cfg)
    norm,stores=_decorate_inputs(norm,warehouses,profile_overrides);checkpoint(cancel)
    return {'normalized':norm,'config':cfg,'info':info,'hashes':{},'sales':sales_ref,'products':product_ref,'stores':stores,
            'warehouses':sorted({d['AU'] for d,m in norm if d['_source']=='资料匹配'}),'loaded_at':datetime.now().isoformat(timespec='seconds')}

def preview(loaded,store):
    rows=[]
    for d,m in loaded['normalized']:
        if d['B']!=store:continue
        x=copy.deepcopy(d);x['_meta']=copy.deepcopy(m);x.update(AS=None,BV=None,_base_qty=None,_base_amount=None,_audit={},_preview=True)
        rows.append(x)
    return rows

def run_calculation(loaded:InputBatch,store:str,warehouses:WarehouseCycles,enabled:bool,budget=None,profile=None,progress=lambda s:None,cancel=None) -> CalculationResult:
    rows=[copy.deepcopy(x) for x in loaded['normalized'] if x[0]['B']==store]
    if not rows:raise ValueError('所选门店没有商品。')
    p=model.Rules(**loaded['config']['parameters']);result=[];baseline=[]
    for i,(d,m) in enumerate(rows):
        checkpoint(cancel)
        if i%300==0:progress(f'正在计算 {i:,} / {len(rows):,} 件商品…')
        if d['_source']=='资料匹配':
            depth,lead=warehouses.get(d['AU'],(-1,-1))
            if depth<0 or lead<0 or depth+lead<=0:m['blocking'].append('所属仓的备货/到货天数未配置')
            else:d.update(AG=depth,AH=lead)
        b=adapter.execute([(d,m)],p)[0]
        x,a=apply_one_box_exception(b,m,enabled=enabled)
        baseline.append(b)
        if not a['applied'] and b!=x:raise RuntimeError('隔离检查失败：非例外商品发生变化')
        if a['applied']:
            assert b['AS']==0 and x['AS']==b['BD']
            assert all(b.get(k)==x.get(k) for k in set(b)|set(x) if k not in {'AS','BV','BM','BW'})
        x=copy.deepcopy(x);x.update(_audit=a,_base_qty=b['AS'],_base_amount=b['BV'],_preview=False)
        result.append(x)
    checkpoint(cancel);progress('正在核对数量、金额与例外隔离…')
    checks=adapter.business_checks(result,p)
    if checks['failures']:raise RuntimeError('结果核验失败：'+str(checks['failures'][:3]))
    if not any(numeric(d['AS']) for d in result) and not loaded.get('catalogs'):raise ValueError('没有可计算商品，请检查商品资料与仓参数。')
    report={'rows':result,'baseline':baseline,'store':store,'store_name':loaded['stores'][store],
        'warehouses':warehouses,'enabled':enabled,'config':loaded['config'],'sources':loaded['hashes'],
        'created':datetime.now().isoformat(timespec='seconds'),'checks':checks,'summary':summarize(result)}
    return apply_budget(report,budget,profile,progress,cancel)

def apply_budget(result,budget=None,profile=None,progress=lambda s:None,cancel=None):
    import priority_list
    if profile is not None:priority_list.validate_profile(profile)
    if budget is not None and profile is None:raise ValueError('启用预算前请先导入有效必卖清单')
    members={d['sku']:d for d in profile['items']} if profile else {}
    for d in result['rows']:
        for k in list(d):
            if k.startswith(('_budget_','_allocated_','_holding_','_candidate_','_must_')):d.pop(k)
        d['_must_sell']=d.get('E') in members
        d['_candidate_qty']=0 if exclusion_reason(d) else quantity(d)
        d['_candidate_amount']=0 if exclusion_reason(d) else amount_value(d)
    progress('正在按门店预算分配必卖与其他商品…' if budget is not None else '正在标记必卖清单…')
    result['budget']=allocation.apply(result['rows'],budget,lambda:checkpoint(cancel)) if budget is not None else {'enabled':False}
    by_code={d['E']:d for d in result['rows']}
    audit=[]
    for sku,item in members.items():
        d=by_code.get(sku)
        state='未出现在本次门店导出' if d is None else '已排除：'+exclusion_reason(d) if exclusion_reason(d) else '地采，资料未匹配' if d['_source']=='地采商品' else '数据待核' if issue_text(d) else '有补货需求' if numeric(d.get('AS')) and d['AS']>0 else '当前无补货建议'
        audit.append({'sku':sku,'name':item.get('name',''),'state':state})
    result['must_list']={'name':profile['name'],'revision':profile['revision'],'source_sha256':profile.get('source_sha256',''),'total':len(members),
        'matched':sum(d['_must_sell'] for d in result['rows']),'missing':sum(x['state']=='未出现在本次门店导出' for x in audit)} if profile else None
    result['must_list_audit']=audit
    result['summary']=summarize(result['rows'])
    return result
