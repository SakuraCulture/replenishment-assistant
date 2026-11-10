"""Offline fixed-commit comparison. Writes only a NEW explicitly named QA dir."""
import argparse
import itertools
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def write(path,value):path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8',newline='\n')


def worker(source,output):
    sys.path.insert(0,str(source/'src'))
    import engine,model,priority_list,result_semantics as sem
    cases=[]
    for must,sales,stock,inbound,step,n,budget in itertools.product(
            (False,True),((0,0,0),(.1,1,4),(2,14,60)),(0,.5,1,5),(0,1),(1,6,.3),(None,0),(None,0,5,100)):
        cases.append(dict(must=must,sales=sales,stock=stock,inbound=inbound,step=step,n=n,budget=budget,tags='',status='正常'))
    for must,tags,status in itertools.product((False,True),('日配','必卖；日配，活动','非日配'),('正常','禁采')):
        cases.append(dict(must=must,sales=(2,14,60),stock=0,inbound=0,step=1,n=0,budget=None,tags=tags,status=status))
    results=[]
    for case in cases:
        h,i,j=case['sales']
        d=dict(A='合成门店',B='T',D='合成商品',E='sku',H=h,I=i,J=j,K=case['stock'],L=case['inbound'],N=case['n'],O=case['step'],
               V=2,S='启用',AB=.2,AC=.4,AD=.3,AG=8,AH=5,AI=15,AJ=5,U='快销',AU='河北仓',_source='资料匹配')
        m=dict(match='商品编码精确匹配',blocking=[],notes=[],export_row=2,category='快销',product_tags=case['tags'],product_tags_known=True,export_status=case['status'])
        loaded=dict(normalized=[(d,m)],config=engine.config(),stores={'T':'合成门店'},hashes={})
        profile=priority_list.make_profile([{'sku':'sku' if case['must'] else 'absent','name':'合成'}],'合成清单')
        r=engine.run_calculation(loaded,'T',{'河北仓':(8,5)},False,case['budget'],profile)
        x=r['rows'][0]
        results.append({'case':case,'model':{k:x.get(k) for k in model.CALCULATED},'quantity':sem.order_quantity(x),
                        'amount':sem.order_amount(x),'excluded':sem.exclusion_reason(x),'holding_gap':x.get('_holding_gap',False)})
    write(output,results)


def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline');p.add_argument('--out',type=Path)
    p.add_argument('--worker-source',type=Path);p.add_argument('--worker-output',type=Path);a=p.parse_args()
    if a.worker_source:worker(a.worker_source.resolve(),a.worker_output);return
    if not a.baseline or not a.out:p.error('--baseline and --out required')
    out=a.out.resolve();out.mkdir(parents=True,exist_ok=False)
    commit=subprocess.check_output(['git','rev-parse',a.baseline+'^{commit}'],cwd=ROOT,text=True).strip()
    archive=out/'baseline-source.zip'
    subprocess.run(['git','archive','--format=zip','-o',str(archive),commit,'src'],cwd=ROOT,check=True)
    source=out/'baseline-source';source.mkdir()
    with zipfile.ZipFile(archive) as z:z.extractall(source)
    for name,folder in [('before',source),('after',ROOT)]:
        subprocess.run([sys.executable,__file__,'--worker-source',str(folder),'--worker-output',str(out/(name+'.json'))],cwd=ROOT,check=True)
    before=json.loads((out/'before.json').read_text('utf-8'));after=json.loads((out/'after.json').read_text('utf-8'))
    assert len(before)==len(after)
    changed=[];unchanged=0;outside=0
    for i,(old,new) in enumerate(zip(before,after)):
        assert old['case']==new['case'] and old['model']==new['model'],('forecast changed',i)
        case=new['case'];scope=case['must'] or case['status']=='禁采' or '日配' in case['tags'].replace('非日配','')
        difference=(old['quantity'],old['amount'])!=(new['quantity'],new['amount'])
        if not scope:
            assert not difference,('ordinary changed',i);outside+=1
        if difference:changed.append({'index':i,'case':case,'before_quantity':old['quantity'],'after_quantity':new['quantity'],'before_amount':old['amount'],'after_amount':new['amount']})
        else:unchanged+=1
        if case['tags'] in ('日配','必卖；日配，活动') or case['status']=='禁采':assert new['quantity']==new['amount']==0
        if case['must'] and case['budget'] is None and not new['excluded']:
            assert max(case['stock'],0)+max(case['inbound'],0)+new['quantity']>=1
    write(out/'comparison.json',dict(passed=True,synthetic=True,baseline=commit,cases=len(after),forecast_zero_differences=True,
          ordinary_outside_scope_unchanged=outside,unchanged_execution=unchanged,changed_execution=len(changed),changes=changed))
    print(json.dumps({'passed':True,'cases':len(after),'forecast_zero_differences':True,'ordinary_unchanged':outside,'changed_execution':len(changed)}))


if __name__=='__main__':main()
