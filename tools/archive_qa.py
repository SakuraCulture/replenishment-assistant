"""Rerunnable fixed-source regression and cross-layer matrix, using isolated data.

matrix runs in a separate interpreter per source. compare uses strict ordered values
and types; only the existing explicit clock/build whitelist is permitted.
"""
import argparse,copy,hashlib,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,dump,diff,clean,WHITELIST,js
from structure_oracle import excel_snapshot


def matrix(source,output):
    sys.path.insert(0,str(source/'src'));output.mkdir(parents=True,exist_ok=False)
    os.environ['REPLENISHMENT_DATA_DIR']=str(output/'settings')
    import engine,warehouse_catalog as wc,batch_service as bs,parameter_schema as ps,priority_list
    from table_records import bridge_table
    # Synthetic, small complete route -> model -> box -> exclusion -> budget chain.
    sales=[]
    for store in ('T1','T2'):
        for sku,h,i,j,k,l,step in [('inbound',1.71,14,38,0,24,24),('empty',.07,1,1,0,0,24),('box',.5,4,12,1,0,24),('moving',5,35,150,0,0,6),('off',1,7,30,0,0,6),('stop',1,7,30,0,0,6)]:
            sales.append({'仓库/门店名称':store,'仓库/门店编码':store,'商品编码':sku,'商品名称':sku,'日销':h,'周销':i,'月销':j,'可用库存':k,'在途库存':l,'缺货天数（天）':0,'补货状态':'停采' if sku=='stop' else '正常','库存单位':'包'})
    headers=list(sales[0]);table=bridge_table(headers,sales,'销售');catalogs={}
    for wh in ('河北仓','山东仓'):
        items={}
        for sku,step in [('inbound',24),('empty',24),('box',24),('moving',6),('off',6),('stop',6)]:
            items[sku]={**{k:None for k in wc.FIELDS},'商品编码':sku,'商品名称':sku,'分类':'未知分类','基本售价':6.8,'出库箱规':step,'商品状态':'禁用' if sku=='off' else '启用','可用数':0 if wh=='河北仓' and sku=='moving' else 100,'_row':len(items)+2}
        catalogs[wh]={'schema':1,'warehouse':wh,'source_path':'synthetic-'+wh,'source_sha256':'synthetic','sheet':'商品资料','imported_at':'fixed','items':items,'revision':wc.revision(items)}
    loaded=wc.prepare_records(table,catalogs);profile=priority_list.make_profile([{'sku':'inbound','name':'在途必卖'},{'sku':'empty','name':'双零必卖'}],'合成必卖')
    cases={}
    for primary in ('河北仓','山东仓'):
        for limit in (None,0,30,3000):
            for enabled in (False,True):
                for inbound in (0,24):
                    for store in ('T1','T2'):
                        data=copy.deepcopy(loaded)
                        for pairs in data['warehouse_rows'].values():
                            for d,m in pairs:
                                if d['E']=='inbound':d['L']=inbound
                        settings={'warehouses':engine.DEFAULT_WAREHOUSES,'exception':enabled,'budget':limit,'parameter_config':ps.standard(),'supply_route':{'primary':primary,'secondary':'山东仓' if primary=='河北仓' else '河北仓'}}
                        key=f'{primary}-{limit}-{enabled}-{inbound}-{store}'
                        cases[key]=bs.calculate_store(data,store,settings,profile)
    dump(output/'matrix.pklz',cases);js(output/'matrix.json',{'cases':len(cases),'synthetic':True,'source':str(source),'summary':{k:v['summary'] for k,v in cases.items()}})


def compare(old,new,report):
    names=json.loads((old/'capture.json').read_text(encoding='utf-8'))['cases'];assert names==json.loads((new/'capture.json').read_text(encoding='utf-8'))['cases']
    results=[]
    for name in names:
        a,b=read(old/(name+'.pklz')),read(new/(name+'.pklz'))
        changes=diff(clean(a),clean(b));results.append({'case':name,'differences':changes})
    for p in old.glob('*.xlsx'):results.append({'case':p.name,'differences':diff(excel_snapshot(p),excel_snapshot(new/p.name))})
    count=sum(len(r['differences']) for r in results);js(report,{'passed':count==0,'cases':results,'differences':count,'whitelist':WHITELIST,'strict_order_and_types':True});print('cases',len(results),'differences',count)
    if count:raise AssertionError('business regression differs')


def matrix_compare(old,new,report):
    a=read(old/'matrix.pklz');b=read(new/'matrix.pklz');changes=diff(clean(a),clean(b));coverage={}
    for r in b.values():
        for d in r['rows']:
            for label,yes in [('one_box_applied',d.get('_audit',{}).get('applied')),('inbound_cap',d.get('_inbound_cap')),('budget_gap',d.get('_budget_gap',0)>0),('secondary',d.get('_meta',{}).get('routing',{}).get('assigned')!=d.get('_meta',{}).get('routing',{}).get('primary'))]:coverage[label]=coverage.get(label,0)+bool(yes)
    passed=not changes and all(coverage.values())
    js(report,{'passed':passed,'cases':len(a),'differences':changes,'coverage_rows':coverage,'whitelist':WHITELIST,'strict_order_and_types':True})
    if not passed:raise AssertionError('cross-layer matrix failed or missing triggered branch')


if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['matrix','compare','matrix-compare']);ap.add_argument('--source',type=Path);ap.add_argument('--output',type=Path,required=True);ap.add_argument('--old',type=Path);ap.add_argument('--new',type=Path);a=ap.parse_args()
    if a.mode=='matrix':matrix(a.source.resolve(),a.output.resolve())
    elif a.mode=='matrix-compare':matrix_compare(a.old,a.new,a.output)
    else:compare(a.old,a.new,a.output)
