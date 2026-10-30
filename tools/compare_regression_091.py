"""Strict 33-case/Excel comparison; only explicit model-version metadata differs."""
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import read,clean,diff,sha,WHITELIST
from structure_oracle import excel_snapshot
q=ROOT/'qa/inbound_091';old=q/'regression_old';new=q/'regression_new';cases=[];versions=[]
names=json.loads((old/'capture.json').read_text())['cases'];assert names==json.loads((new/'capture.json').read_text())['cases']
def version_only(a,b,path='$'):
    if isinstance(a,dict) and isinstance(b,dict):
        for k in a:
            if k not in b:continue
            if k=='version' and isinstance(a[k],dict) and a[k].get('model_version')=='0.3.1-rules' and b[k].get('model_version')=='0.3.2-rules':
                versions.append(path+'.version.model_version');b[k]['model_version']=a[k]['model_version']
            else:version_only(a[k],b[k],path+'.'+str(k))
    elif isinstance(a,(list,tuple)) and isinstance(b,(list,tuple)):
        for i in range(min(len(a),len(b))):version_only(a[i],b[i],path+f'[{i}]')
for name in names:
    a,b=clean(read(old/(name+'.pklz'))),clean(read(new/(name+'.pklz')));version_only(a,b,name);d=diff(a,b);record={'case':name,'differences':d}
    if name.startswith('real_') and 'rows' in b:record.update(rows=len(b['rows']),amount=b['summary']['amount'],positive=b['summary']['positive'])
    cases.append(record)
excel_versions=[]
metadata_cells={'filtered.xlsx':('运行概览',['B3','B4']),'real.xlsx':('运行概览',['B3','B4']),'overlap.xlsx':('版本及参数',['B11','B28']),'reversed.xlsx':('版本及参数',['B11','B28']),'warehouse.xlsx':('版本及参数',['B11','B31','B51','B71'])}
for file in old.glob('*.xlsx'):
    a,b=excel_snapshot(file),excel_snapshot(new/file.name);sheet,coords=metadata_cells[file.name]
    x=next(s for s in a['sheets'] if s['identity']['name']==sheet);y=next(s for s in b['sheets'] if s['identity']['name']==sheet)
    for cell in coords:
        before,after=x['cells'][cell],y['cells'][cell]
        assert '0.3.1-rules' in before and '0.3.2-rules' in after
        assert after.replace('0.3.2-rules','0.3.1-rules')==before,(file.name,sheet,cell)
        excel_versions.append({'file':file.name,'sheet':sheet,'cell':cell,'before':'0.3.1-rules','after':'0.3.2-rules'});y['cells'][cell]=before
    cases.append({'case':file.name,'differences':diff(a,b)})
count=sum(len(c['differences']) for c in cases);report={'passed':count==0,'unexpected_differences':count,'cases':cases,'allowed_version_paths':versions,'clock_build_whitelist':WHITELIST,'hashseed':0,'strict_order_and_types':True,'preserved_cores':{n:sha(ROOT/'src'/n)==sha(q/'baseline/src'/n) for n in ['allocation.py','default_config.json','default_must_sell.json','normal_exports.py','new_products.py','warehouse_catalog.py']}}
report['allowed_excel_model_version_cells']=excel_versions
(q/'regression_comparison.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'passed':report['passed'],'cases':len(cases),'unexpected':count}));assert report['passed']
