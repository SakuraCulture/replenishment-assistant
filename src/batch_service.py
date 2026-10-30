"""Batch orchestration around the unchanged single-store business engine."""
from __future__ import annotations
import copy,json,multiprocessing,os,time
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor,wait,FIRST_COMPLETED
from pathlib import Path
import engine
import warehouse_catalog
from version_info import diagnostic
from xlsx_exports import write_sheet,export_batch
from data_contracts import InputBatch,StoreSettings,CalculationResult

def intake(paths,products,progress=lambda s:None,cancel=None):
    paths=list(dict.fromkeys(str(Path(p).resolve()) for p in paths))
    catalogs=copy.deepcopy(products) if isinstance(products,dict) else None
    if catalogs:
        for wh,catalog in catalogs.items():
            warehouse_catalog.validate(catalog)
            if wh!=catalog['warehouse']:raise ValueError('仓资料标签不一致')
    elif catalogs is not None:raise ValueError('请先导入河北仓或山东仓资料')
    products=str(Path(products).resolve()) if catalogs is None else None
    if not paths:raise ValueError('请至少添加一个补货建议文件。')
    if products in paths:raise ValueError('商品资料不能同时作为补货建议。')
    product_hash=engine.adapter.digest(Path(products)) if products else None
    product_snapshot=None
    if products:
        product_sheet,_=engine.sheet_name(products,'products')
        product_snapshot,_=engine.adapter.read_table(products,product_sheet,['商品编码','基本售价','分类','出库箱规'])
    groups=defaultdict(list);files=[];stores={};headers=[];hashes={products:product_hash} if products else {}
    from file_intake import checked_files
    for checked in checked_files(paths,progress,cancel):
        report=checked['report'];files.append(report)
        if report['error']:continue
        path=report['path'];staged=checked['staged'];names=checked['names']
        for key,record in staged:groups[key].append(record)
        stores.update({k:v for k,v in names.items() if k not in stores})
        for h in checked['headers']:
            if h not in headers:headers.append(h)
        hashes[path]=checked['hash']
    if not groups:raise ValueError('没有可读取的补货建议：\n'+'\n'.join(Path(f['path']).name+'：'+f['error'] for f in files))
    duplicates=0;conflicts=[];selected=[]
    for (store,sku),records in groups.items():
        variants=[];seen=set()
        for rec in records:
            fingerprint=json.dumps({k:v for k,v in rec['values'].items() if v is not None},sort_keys=True,ensure_ascii=False,default=str)
            if fingerprint in seen:duplicates+=1;continue
            seen.add(fingerprint);variants.append(rec)
        if len(variants)==1:selected.append(variants[0])
        else:conflicts.append({'id':len(conflicts),'store':store,'sku':sku,'variants':variants,'choice':None})
    if products and engine.adapter.digest(Path(products))!=product_hash:raise ValueError('商品资料读取期间发生变化，请重试。')
    return {'files':files,'stores':stores,'headers':headers,'hashes':hashes,'products':products,'products_snapshot':product_snapshot,'catalogs':catalogs,'selected':selected,'conflicts':conflicts,'duplicates':duplicates}

def prepare(data,progress=lambda s:None,cancel=None):
    """Project deduplicated records through the exact historical XLSX cell boundary."""
    for p,h in data['hashes'].items():
        if not data.get('frozen') and engine.adapter.digest(Path(p))!=h:raise ValueError('源文件已变化，请重新检查：'+Path(p).name)
    records=list(data['selected']);blocked=defaultdict(int)
    for c in data['conflicts']:
        if c['choice'] is None:blocked[c['store']]+=1
        else:records.append(c['variants'][c['choice']])
    if not records:raise ValueError('所有记录均有冲突，请先选择保留记录。')
    from table_records import bridge_table
    sales_table=bridge_table(data['headers'],(r['values'] for r in records),'补货建议',checkpoint=lambda:engine.checkpoint(cancel))
    from source_binding import bindings,restore
    origins=bindings(sales_table,records,data['headers'])
    if data.get('catalogs'):
        loaded=warehouse_catalog.prepare_records(sales_table,data['catalogs'],progress,cancel)
    else:
        products=data['products']
        if data.get('frozen'):
            raw=data.get('products_snapshot')
            if not raw:raise ValueError('旧作业没有商品资料快照，请重新选择原资料')
            heads=[k for k in raw[0] if k!='_row']
            product_table=bridge_table(heads,raw,'商品资料',header_text=True)
            from source_binding import master_bindings,restore_master
            master_origins=master_bindings(product_table,raw)
            products='memory:master.xlsx'
        else:
            ps,_=engine.sheet_name(products,'products')
            product_table=engine.adapter.read_table(products,ps,['商品编码','分类','基本售价','出库箱规'])
        loaded=engine.load_records(sales_table,product_table,sales_ref='memory:batch.xlsx',product_ref=str(products),progress=progress,cancel=cancel)
        if data.get('frozen'):
            for d,m in loaded['normalized']:
                if m.get('product_row'):m['product_row']=restore_master(master_origins,d['E'],m['product_row'])
    # Bind provenance before conversion, rather than interpreting a bridge row as
    # a list offset after conversion. Validate identity on every returned row.
    for pairs in [loaded['normalized'],*loaded.get('warehouse_rows',{}).values()]:
        for d,m in pairs:
            r=restore(origins,d,m)
            m.update(export_row=r['row'],export_file=r['path'],export_sheet=r['sheet'])
    loaded.update(hashes=dict(data['hashes']),sales=[f['path'] for f in data['files'] if not f['error']],stores=dict(data['stores']),blocked=dict(blocked),intake=data)
    for wh,c in loaded.get('catalogs',{}).items():loaded['hashes'][wh+'资料快照 '+c['source_path']+' #'+c['revision']]=c['source_sha256']
    for p,h in data['hashes'].items():
        if not data.get('frozen') and engine.adapter.digest(Path(p))!=h:raise ValueError('读取期间源文件变化，请重新检查：'+Path(p).name)
    return loaded

def load(paths,products,progress=lambda s:None,cancel=None):
    data=intake(paths,products,progress,cancel)
    # UI must be able to resolve a dataset consisting entirely of conflicts.
    if not data['selected'] and data['conflicts']:return {'intake':data,'stores':data['stores'],'normalized':[],'blocked':{s:1 for s in data['stores']},'warehouses':[],'config':engine.config(),'hashes':data['hashes'],'catalogs':{w:warehouse_catalog.metadata(c) for w,c in (data.get('catalogs') or {}).items()}}
    return prepare(data,progress,cancel)

def calculate_store(loaded:InputBatch,store:str,settings:StoreSettings,profile) -> CalculationResult:
    started=time.time()
    if loaded.get('catalogs'):loaded=warehouse_catalog.route_store(loaded,store,settings.get('supply_route'))
    if settings.get('parameter_config'):
        import parameter_schema
        loaded=parameter_schema.apply(loaded,settings['parameter_config'])
    result=engine.run_calculation(loaded,store,settings['warehouses'],settings['exception'],settings['budget'],profile)
    if loaded.get('catalogs'):result.update(catalogs=loaded['catalogs'],supply_route=loaded['supply_route'])
    result['version']=diagnostic();result['task']={'pid':os.getpid(),'started':started,'finished':time.time()}
    result['effective_settings']=copy.deepcopy(settings)
    result['import_audit']=loaded.get('import_audit',{})
    return result

def run(loaded,jobs,profile,max_workers=2,notify=lambda s,state,result,error:None,cancel=None):
    """Submit only available slots: stop never starts queued stores; running stores finish."""
    if not 1<=max_workers<=4:raise ValueError('并发门店数必须为1至4。')
    # Only the computation contract crosses spawn. Raw intake/catalog originals
    # remain in the workbench for archiving; copying them here is redundant.
    keys=('normalized','config','stores','hashes','catalogs','warehouse_rows','blocked','import_audit','supply_route')
    snapshot=copy.deepcopy({k:loaded[k] for k in keys if k in loaded});data=loaded.get('intake')
    if data:snapshot['import_audit']={'files':data['files'],'identical_duplicates':data['duplicates'],'conflicts':[{'store':c['store'],'sku':c['sku'],'choice':c['choice'],'sources':[{'file':v['path'],'sheet':v['sheet'],'row':v['row']} for v in c['variants']]} for c in data['conflicts']]}
    pending=iter(jobs.items());active={};out={};stopped=False
    with ProcessPoolExecutor(max_workers=max_workers,mp_context=multiprocessing.get_context('spawn')) as pool:
        def fill():
            nonlocal stopped
            stopped=bool(cancel and cancel.is_set())
            if stopped:return
            while len(active)<max_workers:
                if cancel and cancel.is_set():return
                try:store,settings=next(pending)
                except StopIteration:break
                if snapshot.get('blocked',{}).get(store):
                    notify(store,'计算失败',None,'存在重复冲突，请先处理');out[store]=None;continue
                one={**snapshot,'normalized':[x for x in snapshot['normalized'] if x[0]['B']==store]}
                if snapshot.get('warehouse_rows'):one['warehouse_rows']={w:[x for x in rows if x[0]['B']==store] for w,rows in snapshot['warehouse_rows'].items()}
                notify(store,'计算中',None,'')
                active[pool.submit(calculate_store,one,store,copy.deepcopy(settings),copy.deepcopy(profile))]=store
        fill()
        while active:
            done,_=wait(active,timeout=.2,return_when=FIRST_COMPLETED)
            for future in done:
                store=active.pop(future)
                try:
                    result=future.result();out[store]=result;notify(store,'已完成',result,'')
                except Exception as exc:
                    from diagnostics import failure
                    out[store]=None;notify(store,'计算失败',None,failure(exc,stage='门店计算',store=store))
            fill()
        for store,_ in pending:notify(store,'已停止',None,'未启动，可重新计算')
    return out
