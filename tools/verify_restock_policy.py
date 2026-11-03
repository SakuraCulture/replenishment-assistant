"""Ordered default-on equivalence and explicit extreme-off impact; isolated frozen inputs."""
import argparse,copy,json,os,sys,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tools'),str(ROOT/'src')]
from parallel_qa import read,js,sha,clean,diff,WHITELIST
from structure_oracle import excel_snapshot,source_hashes

VERSIONS={'model_version':('0.3.3-rules','0.3.4-rules'),'parameter_schema_version':(2,3)}

def align_version(a,b,path,allowed):
    for key,(old,new) in VERSIONS.items():
        if a.get(key)==old and b.get(key)==new and type(a[key]) is type(old) and type(b[key]) is type(new):
            allowed.append({'path':path+'.'+key,'before':old,'after':new});a[key]=b[key]

def align_results(a,b,path,allowed):
    if type(a) is not type(b):return
    if isinstance(a,dict):
        if all(k in a and k in b for k in ('rows','summary','store','version')):align_version(a['version'],b['version'],path+'.version',allowed)
        for k in a:
            if k in b:align_results(a[k],b[k],path+'.'+str(k),allowed)
    elif isinstance(a,(tuple,list)):
        for i,(x,y) in enumerate(zip(a,b)):align_results(x,y,path+f'[{i}]',allowed)

def align_excel(a,b,path,allowed):
    # Keep cell attributes, style, type, coordinates and all other diagnostic keys.
    for i,(left,right) in enumerate(zip(a['sheets'],b['sheets'])):
        name=left['identity']['name']
        if name!=right['identity']['name'] or name not in ('运行概览','版本及参数'):continue
        for coord,raw in left['cells'].items():
            if not coord.startswith('B') or not coord[1:].isdigit() or coord not in right['cells']:continue
            label=''.join(ET.fromstring(left['cells'].get('A'+coord[1:],'<x/>')).itertext())
            x=ET.fromstring(raw);y=ET.fromstring(right['cells'][coord]);vx=''.join(x.itertext());vy=''.join(y.itertext());changed=False
            where=f'{path}.{name}.{coord}'
            if name=='运行概览' and label=='构建信息':
                jx=json.loads(vx);jy=json.loads(vy);n=len(allowed);align_version(jx,jy,where,allowed)
                if len(allowed)>n:x.find('normalized-dynamic-value').text=json.dumps(jx,sort_keys=True);changed=True
            else:
                key='model_version' if name=='运行概览' and label=='模型版本' else next((k for k in VERSIONS if name=='版本及参数' and label.endswith(' / '+k)),None)
                if key and (vx,vy)==tuple(str(v) for v in VERSIONS[key]):
                    leaves=[n for n in x.iter() if n.text==vx]
                    if len(leaves)!=1:raise ValueError('Unexpected version cell structure '+where)
                    leaves[0].text=vy;allowed.append({'path':where,'label':label,'before':vx,'after':vy});changed=True
            if changed:left['cells'][coord]=ET.tostring(x,encoding='unicode')

def compare(qa):
    old=qa/'before-final';new=qa/'after-final';ca=json.loads((old/'capture.json').read_text('utf-8'));cb=json.loads((new/'capture.json').read_text('utf-8'))
    if ca['cases']!=cb['cases'] or ca['PYTHONHASHSEED']!=cb['PYTHONHASHSEED'] or ca['PYTHONHASHSEED']!='0':raise ValueError('Capture inputs or hash seed differ')
    if cb['source_hashes']!=source_hashes(ROOT):raise ValueError('Source changed after capture')
    allowed=[];cases=[]
    for name in ca['cases']:
        a=clean(read(old/(name+'.pklz')));b=clean(read(new/(name+'.pklz')));align_results(a,b,name,allowed);changes=diff(a,b)
        cases.append({'case':name,'differences':len(changes),'details':changes})
    for file in old.glob('*.xlsx'):
        a=excel_snapshot(file);b=excel_snapshot(new/file.name);align_excel(a,b,file.name,allowed);changes=diff(a,b)
        cases.append({'case':file.name,'differences':len(changes),'details':changes})
    frozen=json.loads((qa/'baseline.json').read_text('utf-8'));changed=[p for p,h in frozen['protected'].items() if not Path(p).is_file() or sha(p)!=h]
    cores={n:{'before':sha(qa/'baseline/src'/n),'after':sha(ROOT/'src'/n)} for n in ('model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json')}
    if any(v['before']!=v['after'] for n,v in cores.items() if n!='model.py'):raise ValueError('Unapproved core change')
    count=sum(c['differences'] for c in cases)
    report={'passed':not count and not changed,'baseline_commit':frozen['baseline'],'source_hashes':cb['source_hashes'],'core':cores,'strict_values_types_and_all_order':True,'float_tolerance':0,'whitelist':WHITELIST,'allowed_version_transitions':allowed,'cases':cases,'differences':count,'protected_count':len(frozen['protected']),'changed_protected':changed,'input_sha256':{str(p):sha(p) for p in (qa/'inputs').iterdir() if p.is_file()},'first_comparison_note':'before/after and initial-diff.json retained. Unfixed PYTHONHASHSEED produced two existing category-set insertion-order differences; both fixed-source captures rerun with seed 0. No sorting or key-order exclusion used.'}
    js(qa/'equivalence.json',report);print('Default-on:',len(cases),'cases;',count,'unexpected differences;',len(allowed),'explicit version transitions')
    if not report['passed']:raise SystemExit(1)

def impact(qa):
    import batch_service as bs,engine,parameter_schema,restock_policy as policy,result_semantics as sem
    inp=qa/'inputs';data=read(qa/'after-final/real_loaded.pklz');store=next(iter(data['stores']));params=json.loads((inp/'parameters.json').read_text('utf-8'));profile=json.loads((inp/'must.json').read_text('utf-8'))
    out=qa/'impact';out.mkdir(exist_ok=False);os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings');summaries=[];details=[];issues=[]
    for limit,exception in ((None,True),(0,True),(3000,True),(5000,True),(10000,True),(None,False)):
        name=f'real_{limit}_{exception}';before=read(qa/'after-final'/(name+'.pklz'));cfg=policy.patch(params['standard'],{'extreme_restore':False});after=bs.calculate_store(data,store,{'warehouses':engine.DEFAULT_WAREHOUSES,'exception':exception,'budget':limit,'parameter_config':cfg},profile)
        if [d['E'] for d in before['rows']]!=[d['E'] for d in after['rows']]:raise ValueError('Product order changed')
        changed=0;extreme=0
        for a,b in zip(before['rows'],after['rows']):
            if a['AW']!='极端缺货自动拉回':
                # Candidate and all model intermediates must be identical. Only budget
                # redistribution may affect execution when another candidate disappears.
                if limit is None:
                    issues.extend([[name,a['E'],x] for x in diff(a,b)])
                else:
                    keys=[k for k in a if not k.startswith('_')]
                    issues.extend([[name,a['E'],x] for x in diff({k:a[k] for k in keys},{k:b.get(k) for k in keys})])
                    if a.get('_candidate_qty')!=b.get('_candidate_qty'):issues.append([name,a['E'],'non-extreme candidate changed'])
            elif isinstance(b['AS'],(int,float)) and b['AS']!=0:issues.append([name,a['E'],'extreme off did not suppress quantity'])
            else:extreme+=1
            if sem.order_quantity(a)!=sem.order_quantity(b) or sem.order_amount(a)!=sem.order_amount(b):
                changed+=1;details.append({'case':name,'store':store,'sku':a['E'],'name':a.get('D'),'status':a['AW'],'monthly':a.get('J'),'effective_stockout':a.get('BA'),'available':a.get('K'),'inbound':a.get('L'),'step':a.get('BD'),'model_before':a['AS'],'model_after':b['AS'],'execution_before':sem.order_quantity(a),'execution_after':sem.order_quantity(b),'amount_before':sem.order_amount(a),'amount_after':sem.order_amount(b)})
        summaries.append({'case':name,'rows':len(before['rows']),'extreme_rows':extreme,'changed_execution_skus':changed,'amount_before':before['summary']['amount'],'amount_after':after['summary']['amount'],'amount_reduction':round(before['summary']['amount']-after['summary']['amount'],2)})
    js(out/'report.json',{'passed':not issues,'source':'Frozen actual sales and mixed master, 14244 input rows; no new products selected','summaries':summaries,'changed_products':details,'unexpected':issues,'note':'Finite-budget execution changes may include redistribution to unchanged ordinary candidates. Audit differences from explicitly OFF policy are intended. No input or archive rewritten.'});print(json.dumps(summaries,ensure_ascii=False))
    if issues:raise SystemExit(1)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['compare','impact']);p.add_argument('--qa',type=Path,required=True);a=p.parse_args();(compare if a.mode=='compare' else impact)(a.qa.resolve())
