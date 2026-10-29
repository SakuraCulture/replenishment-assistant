"""Independent 0.6.0 oracle. Runs each source tree in a separate interpreter.

Never imports a candidate module into the baseline process. Exact types and values,
keyed business rows, full nested metadata and workbook presentation are compared.
Generated files and private settings belong exclusively to the explicitly supplied QA root.
"""
from __future__ import annotations
import argparse,copy,hashlib,json,os,pickle,subprocess,sys,zlib
from pathlib import Path
from datetime import datetime,date

BASE='e50072cc00c6997aed9fe1113012192210ad7eb3'
ROOT=Path(__file__).resolve().parents[1]
CORE=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json']
WHITELIST={
 'result.created':'calculation wall-clock timestamp',
 'result.task.pid':'spawn process identifier',
 'result.task.started':'spawn timing',
 'result.task.finished':'spawn timing',
 'result.version.version':'application packaging version, not model/routing/schema version',
 'result.version.source_commit':'build provenance',
 'result.version.build_id':'build provenance',
 'result.version.built_at':'build timestamp',
 'loaded.loaded_at':'import wall-clock timestamp',
 'loaded.sales / loaded.products':'only generated replenishment-* temporary file paths or memory:batch.xlsx/master.xlsx/河北仓.xlsx/山东仓.xlsx internal references',
 'Excel: 运行概览/计算时间; 版本及参数/导出时间':'clock cells only',
 'Excel: 软件版本; 构建信息.version/source_commit/build_id/built_at':'packaging metadata only',
 'Excel: 版本及参数/<store> / version, source_commit, build_id, built_at':'packaging metadata only',
}
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def save(p,obj):Path(p).write_bytes(zlib.compress(pickle.dumps(obj,protocol=5)))
def read(p):return pickle.loads(zlib.decompress(Path(p).read_bytes()))
def write_json(p,obj):Path(p).write_text(json.dumps(obj,ensure_ascii=False,indent=2,default=str),encoding='utf-8',newline='\n')
def source_hashes(source):return {str(p.relative_to(source)):sha(p) for p in sorted((source/'src').glob('*')) if p.is_file()}
def external_hashes():
    paths=[]
    for folder in ['交付_0.3','交付_0.4.0_rc2','交付_0.5.0_rc1','交付_0.6.0_rc2']:
        paths.extend(p for p in (ROOT/folder).rglob('*') if p.is_file())
    settings=Path(os.environ['LOCALAPPDATA'])/'YoutuanTools/补货助手/settings'
    paths.extend(p for p in settings.rglob('*') if p.is_file())
    return {str(p):sha(p) for p in paths}
def freeze(qa):
    inputs=qa/'inputs';inputs.mkdir(exist_ok=False)
    source=Path('D:/企业微信/WXWork/1688855943717538/Cache/File/2026-10')
    import shutil
    names=['补货建议导出服务.xlsx','混合仓商品资料.xlsx','商品资料_20261024103312_203178058_1.xlsx']
    originals={str(source/n):sha(source/n) for n in names}
    for n in names:shutil.copy2(source/n,inputs/n)
    sys.path[:0]=[str(qa/'baseline/src'),str(qa/'baseline')]
    import verify_warehouse,warehouse_catalog as wc,parameter_schema as ps,openpyxl
    verify_warehouse.fixtures(inputs)
    catalogs={w:wc.import_catalog(inputs/(w+'.xlsx'),w) for w in wc.WAREHOUSES}
    write_json(inputs/'catalogs.json',catalogs)
    from verify_batch import write_fixture
    # Deterministic irregular rows exercise missing, unknown, override, code and date semantics.
    sales,master=[],[]
    codes=['0000123','1234567890123456789012345',123,'=SKU','unknown_class','no_box','no_price','bad_sales','local','override','precision']
    for i,sku in enumerate(codes):
        sales.append({'仓库/门店编码':'EDGE','仓库/门店名称':'边界合成','商品编码':sku,'商品名称':'商品'+str(i),'日销':.3333333333333333,'周销':2,'月销':9,'可用库存':0,'在途库存':0,'缺货天数（天）':0,'补货状态':'正常','基础单位':'瓶','更新时间':datetime(2026,10,29,12,1,2,123000),'商品标签':''})
        if sku!='local':master.append({'商品编码':sku,'商品名称':'商品'+str(i),'分类':'不存在的分类' if sku=='unknown_class' else '饮品','基本售价':None if sku=='no_price' else 1.123456789012345,'出库箱规':None if sku=='no_box' else 1,'商品状态':'启用','仓归属':'河北仓','补货模型分类':'日百' if sku=='override' else None})
    sales[7]['日销']=None
    write_fixture(inputs/'edge.xlsx',list(sales[0]),sales);write_fixture(inputs/'edge_master.xlsx',list(master[0]),master)
    duplicate=copy.deepcopy(sales[0]);duplicate['可用库存']=8
    write_fixture(inputs/'conflict.xlsx',list(sales[0]),[sales[0],duplicate])
    for name,headers,values in [('duplicate_header',['商品编码','日销','日销','周销','月销','仓库/门店编码','可用库存','在途库存','缺货天数（天）'],['x',1,2,3,4,'EDGE',0,0,0]),('numeric_store',['商品编码','日销','周销','月销','仓库/门店编码','可用库存','在途库存','缺货天数（天）'],['x',1,2,3,1234567890123456,0,0,0])]:
        b=openpyxl.Workbook();w=b.active;w.append(headers);w.append(values);b.save(inputs/(name+'.xlsx'));b.close()
    config=ps.standard();custom=copy.deepcopy(config);custom['parameters']['probability']=.5;custom['weight_profiles']['快销'].update(alpha=.1,w1=.3,w2=.4)
    write_json(inputs/'parameters.json',{'standard':config,'custom':custom})
    shutil.copy2(qa/'baseline/src/default_must_sell.json',inputs/'must_sell.json')
    write_json(qa/'frozen.json',{'baseline_commit':BASE,'baseline_sources':source_hashes(qa/'baseline'),'original_inputs':originals,'frozen_inputs':{str(p):sha(p) for p in inputs.iterdir() if p.is_file()},'preserved_files':external_hashes(),'whitelist':WHITELIST})
    print('Frozen 0.6.0 sources, original/copy hashes, profiles, warehouse snapshots and preserved files.')

def capture(source,qa,out):
    out.mkdir(parents=True,exist_ok=False);sys.path[:0]=[str(source/'src'),str(source)]
    os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import engine as e,batch_service as bs,warehouse_catalog as wc,parameter_schema as ps,openpyxl
    inp=qa/'inputs';profiles=json.loads((inp/'parameters.json').read_text(encoding='utf-8'));must=json.loads((inp/'must_sell.json').read_text(encoding='utf-8'));cases=[]
    def put(name,value):save(out/(name+'.pklz'),value);cases.append(name)
    real=e.load_inputs(inp/'补货建议导出服务.xlsx',inp/'混合仓商品资料.xlsx');store=next(iter(real['stores']));put('real_loaded',real)
    for limit in [None,0,3000,5000,10000]:
        r=bs.calculate_store(real,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':limit,'parameter_config':profiles['standard']},must)
        put('real_'+str(limit),r)
        if limit is None:
            # All rows included in oracle; workbook samples include all statuses plus filtered scope.
            e.export_xlsx(r,out/'real.xlsx');e.export_xlsx(r,out/'filtered.xlsx',[d for d in r['rows'] if d.get('_audit',{}).get('applied')],'当前筛选：一箱例外')
    put('real_no_exception',bs.calculate_store(real,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':False,'budget':None},must))
    put('real_custom',bs.calculate_store(real,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':3000,'parameter_config':profiles['custom']},must))
    real_batch=bs.load([inp/'补货建议导出服务.xlsx'],inp/'混合仓商品资料.xlsx');put('real_batch_loaded',real_batch)
    put('real_batch_result',bs.calculate_store(real_batch,store,{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':None,'parameter_config':profiles['standard']},must))
    catalogs=json.loads((inp/'catalogs.json').read_text(encoding='utf-8'));dual=bs.load([inp/'sales.xlsx'],catalogs);put('dual_loaded',dual)
    settings=lambda primary,secondary,budget=None,config='standard':{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':budget,'supply_route':{'primary':primary,'secondary':secondary},'parameter_config':profiles[config]}
    jobs={'QA_A':settings('河北仓','山东仓',60,'custom'),'QA_B':settings('山东仓','河北仓',80)}
    for s,c in jobs.items():put('dual_serial_'+s,bs.calculate_store(dual,s,c,must))
    parallel=bs.run(dual,jobs,must,2);put('dual_parallel',parallel)
    put('single_route',bs.calculate_store(dual,'QA_A',settings('河北仓',None),must))
    single=bs.load([inp/'sales.xlsx'],{'河北仓':catalogs['河北仓']});put('single_catalog',bs.calculate_store(single,'QA_A',settings('河北仓',None),must))
    # Export input order is part of the fixture, not process completion timing.
    export_results={s:parallel[s] for s in jobs}
    statuses={s:{'name':r['store_name'],'state':'已完成','error':''} for s,r in export_results.items()};statuses['FAIL']={'name':'失败店','state':'计算失败','error':'预设失败'}
    bs.export_batch(export_results,statuses,out/'batch.xlsx')
    edge=bs.load([inp/'edge.xlsx'],inp/'edge_master.xlsx');put('edge_loaded',edge);put('edge_result',bs.calculate_store(edge,'EDGE',{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':None},must))
    conflict=bs.intake([inp/'edge.xlsx',inp/'conflict.xlsx'],inp/'edge_master.xlsx');put('conflicts',conflict);put('conflict_blocked',bs.prepare(conflict));conflict['conflicts'][0]['choice']=1;put('conflict_resolved',bs.prepare(conflict))
    frozen=copy.deepcopy(edge['intake']);frozen['frozen']=True;legacy=bs.prepare(frozen);put('legacy_frozen_loaded',legacy)
    put('legacy_frozen_result',bs.calculate_store(legacy,'EDGE',{'warehouses':e.DEFAULT_WAREHOUSES,'exception':True,'budget':None},must))
    errors={}
    for name in ['duplicate_header','numeric_store']:
        try:bs.load([inp/(name+'.xlsx')],inp/'edge_master.xlsx')
        except Exception as ex:errors[name]=(type(ex).__name__,str(ex))
        else:raise AssertionError('Invalid input accepted: '+name)
    put('errors',errors)
    # Freeze a genuine old SQLite/job payload for candidate restoration compatibility.
    import work_repository as wr
    payload={'schema':1,'day':'2026-10-29','model_version':'0.3.0-rules','states':{s:st for s,st in statuses.items() if s!='FAIL'},'results':parallel,'loaded':dual,'input_dirty':False}
    repo=wr.Repository(out/'settings');preset=repo.save_preset('固定自定义',profiles['custom']);repo.save_stores({'QA_A':{'preset':preset,'ui':{'mode':1,'amount':'','primary':'河北仓','secondary':'山东仓'}}});repo.save_job(payload['day'],payload)
    put('restored_payload',repo.get_job(payload['day'])['payload'])
    put('preview',e.preview(edge,'EDGE'))
    import parameter_trial
    put('parameter_trial',parameter_trial.compare(dual,jobs,must,profiles['custom']))
    write_json(out/'capture.json',{'source':str(source),'source_hashes':source_hashes(source),'cases':cases})
    print('Captured',len(cases),'scenarios in',out)

def clean(value,path=()):
    """Exact explicit dynamic leaf whitelist, never omit an entire metadata object."""
    if isinstance(value,dict):
        out={}
        is_result='rows' in value and 'store' in value and 'summary' in value
        is_loaded='normalized' in value and 'config' in value
        for k,v in value.items():
            if is_result and k=='created':v='<calculation-time>'
            elif is_result and k=='task':v={a:'<dynamic>' if a in {'pid','started','finished'} else b for a,b in v.items()}
            elif is_result and k=='version':v={a:'<build>' if a in {'version','source_commit','build_id','built_at'} else b for a,b in v.items()}
            elif is_loaded and k=='loaded_at':v='<load-time>'
            elif is_loaded and k in {'sales','products'} and isinstance(v,str) and ('replenishment-' in v or v in {'memory:batch.xlsx','memory:master.xlsx','memory:河北仓.xlsx','memory:山东仓.xlsx'}):v='<temporary>/'+(v[7:] if v.startswith('memory:') else Path(v).name)
            if k in {'rows','baseline'} and isinstance(v,list) and v and isinstance(v[0],dict) and 'B' in v[0] and 'E' in v[0]:
                keys=[(d['B'],d['E']) for d in v];assert len(set(keys))==len(keys),'nonunique business keys';v={key:row for key,row in zip(keys,v)}
            if k=='normalized' or k=='warehouse_rows':
                if k=='normalized':v=key_pairs(v)
                else:v={wh:key_pairs(pairs) for wh,pairs in v.items()}
            out[k]=clean(v,path+(k,))
        return out
    if isinstance(value,(list,tuple)):return type(value)(clean(v,path+(i,)) for i,v in enumerate(value))
    return value
def key_pairs(pairs):
    keys=[(d['B'],d['E']) for d,m in pairs];assert len(set(keys))==len(keys),'nonunique normalized keys'
    return {k:p for k,p in zip(keys,pairs)}
def differences(a,b,path='$',out=None):
    out=[] if out is None else out
    if type(a)!=type(b):out.append((path,repr(a)[:200],repr(b)[:200],'type'));return out
    if isinstance(a,dict):
        for k in a.keys()|b.keys():
            if k not in a or k not in b:out.append((path+'.'+str(k),'present' if k in a else 'missing','present' if k in b else 'missing','key'))
            else:differences(a[k],b[k],path+'.'+str(k),out)
    elif isinstance(a,(list,tuple)):
        if len(a)!=len(b):out.append((path,len(a),len(b),'length'))
        for i in range(min(len(a),len(b))):differences(a[i],b[i],path+'['+str(i)+']',out)
    elif a!=b:out.append((path,repr(a)[:200],repr(b)[:200],'value'))
    return out
def excel_snapshot(path):
    """Compare decoded sheet XML cells/types/styles without million-cell GUI objects.

    The full styles part and all worksheet structure are retained. XML spelling is
    canonicalized by ElementTree; only the same explicitly named clock/build cells
    as the original cell comparer are normalized. Cell coordinates locate differences.
    """
    import zipfile,xml.etree.ElementTree as ET
    ns={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    xml=lambda node:ET.tostring(node,encoding='unicode')
    def cell_text(c):return ''.join(c.itertext()) if c is not None else None
    with zipfile.ZipFile(path) as z:
        book=ET.fromstring(z.read('xl/workbook.xml'));sheets=book.find('s:sheets',ns)
        result={'styles':xml(ET.fromstring(z.read('xl/styles.xml'))),'sheets':[]}
        for i,sheet in enumerate(sheets,1):
            w=ET.fromstring(z.read('xl/worksheets/sheet'+str(i)+'.xml'));data=w.find('s:sheetData',ns);cells={};row_attributes=[]
            for row in data:
                row_attributes.append(dict(row.attrib));key=cell_text(next((c for c in row if c.get('r','').startswith('A') and c.get('r','')[1:].isdigit()),None))
                for c in row:
                    if c.get('r','').startswith('B') and c.get('r','')[1:].isdigit():
                        value=cell_text(c);replacement=None
                        if sheet.get('name')=='运行概览':
                            if key in {'计算时间','软件版本'}:replacement='<dynamic>'
                            if key=='构建信息':replacement=json.dumps({k:'<build>' if k in {'version','source_commit','build_id','built_at'} else v for k,v in json.loads(value).items()},sort_keys=True)
                        if sheet.get('name')=='版本及参数' and (key=='导出时间' or any(str(key).endswith(' / '+k) for k in ['version','source_commit','build_id','built_at'])):replacement='<dynamic>'
                        if replacement is not None:
                            for child in list(c):c.remove(child)
                            ET.SubElement(c,'normalized-dynamic-value').text=replacement
                    cells[c.get('r')]=xml(c)
            w.remove(data);result['sheets'].append({'identity':dict(sheet.attrib),'structure':xml(w),'row_attributes':row_attributes,'cells':cells})
    return result
def compare(qa,baseline,candidate,report):
    manifest=json.loads((baseline/'capture.json').read_text(encoding='utf-8'));results=[]
    for name in manifest['cases']:
        a=clean(read(baseline/(name+'.pklz')));b=clean(read(candidate/(name+'.pklz')));diff=differences(a,b)
        results.append({'case':name,'differences':len(diff),'details':diff[:100]})
    for p in baseline.glob('*.xlsx'):
        diff=differences(excel_snapshot(p),excel_snapshot(candidate/p.name));results.append({'case':'Excel '+p.name,'differences':len(diff),'details':diff[:100]})
    frozen=json.loads((qa/'frozen.json').read_text(encoding='utf-8'));preserved={**frozen['original_inputs'],**frozen['frozen_inputs'],**frozen['preserved_files']}
    changed=[p for p,h in preserved.items() if not Path(p).is_file() or sha(p)!=h]
    core={n:{'before':sha(qa/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in CORE}
    candidate_manifest=json.loads((candidate/'capture.json').read_text(encoding='utf-8'))
    source_drift=candidate_manifest['source_hashes']!=source_hashes(ROOT)
    commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    version=json.loads((ROOT/'src/version.json').read_text(encoding='utf-8'))['version']
    report_data={'source_commit':commit,'version':version,'source_changed_since_capture':source_drift,'passed':not source_drift and not changed and all(v['before']==v['after'] for v in core.values()) and not any(r['differences'] for r in results),'baseline_commit':BASE,'candidate_sources':candidate_manifest['source_hashes'],'whitelist':WHITELIST,'comparison':'exact type and value; business rows keyed by store and SKU; all nested metadata retained; Excel values/types/order/styles compared','core_hashes':core,'changed_preserved_files':changed,'cases':results,'difference_count':sum(r['differences'] for r in results)}
    write_json(report,report_data);print(json.dumps({'passed':report_data['passed'],'differences':report_data['difference_count'],'report':str(report)}));return report_data['passed']
def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['freeze','capture','compare']);p.add_argument('--qa',type=Path,required=True);p.add_argument('--source',type=Path,default=ROOT);p.add_argument('--out',type=Path);p.add_argument('--baseline',type=Path);a=p.parse_args();qa=a.qa.resolve()
    if a.mode=='freeze':freeze(qa)
    elif a.mode=='capture':capture(a.source.resolve(),qa,a.out.resolve())
    else:sys.exit(0 if compare(qa,a.baseline.resolve(),a.out.resolve(),a.out/'equivalence.json') else 1)
if __name__=='__main__':
    import multiprocessing
    multiprocessing.freeze_support();main()
