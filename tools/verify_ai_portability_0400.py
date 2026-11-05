"""Frozen 0.39.0 comparison; no user data/config, strictly sequential old/new runs."""
import argparse,copy,hashlib,json,pickle,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BASE='86464a64584c17aa69d0269f77f14d212e75ea59'


def strict(a,b,path=''):
    if type(a) is not type(b):raise AssertionError('Type '+path)
    if isinstance(a,dict):
        if list(a)!=list(b):raise AssertionError('Fields/order '+path)
        for k in a:strict(a[k],b[k],path+'/'+str(k))
    elif isinstance(a,(list,tuple)):
        if len(a)!=len(b):raise AssertionError('Length '+path)
        for n,(x,y) in enumerate(zip(a,b)):strict(x,y,path+'/'+str(n))
    elif a!=b:raise AssertionError('Value '+path)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);args=p.parse_args()
    out=args.out.resolve();out.mkdir(parents=True,exist_ok=False)
    import verify_analytics_0390 as old
    calculation=old.equivalence(out,BASE)
    from serve_analytics_ai_acceptance import fixture
    from verify_ai_analytics_artifacts import compare_excel
    (out/'history').mkdir()
    spec=fixture(out/'history');db=out/'history/settings/workbench.sqlite3';original=hashlib.sha256(db.read_bytes()).hexdigest()
    (out/'spec.json').write_text(json.dumps(spec),'utf-8')
    worker=out/'history_worker.py';worker.write_text('''import sys,pickle,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import order_analytics as a,order_analytics_exports as e
r=a.collect(sys.argv[2],json.loads(Path(sys.argv[3]).read_text('utf-8')))
Path(sys.argv[4]+'.pkl').write_bytes(pickle.dumps(r))
e.export(sys.argv[2],r,Path(sys.argv[4]+'.xlsx'))
''','utf-8')
    for name,src in [('old',out/'baseline/src'),('new',ROOT/'src')]:
        subprocess.run([sys.executable,'-B',str(worker),str(src),str(db),str(out/'spec.json'),str(out/name)],check=True)
    before,after=[pickle.loads((out/(name+'.pkl')).read_bytes()) for name in ('old','new')]
    allowed=['history.items[].evidence_details','history.items[].observation_basis','history.items[].parameter_basis']
    added=[]
    for row in after['history']['items']:
        added.append({k:row.pop(k) for k in ('evidence_details','observation_basis','parameter_basis')})
    for r in (before,after):r.pop('as_of');r['history'].pop('as_of')
    strict(before,after)
    excel=compare_excel(out/'old.xlsx',out/'new.xlsx')
    if hashlib.sha256(db.read_bytes()).hexdigest()!=original:raise AssertionError('History database changed')
    result={'passed':True,'baseline':BASE,'calculation':calculation,'history_rows':len(after['rows']),
        'history_difference_count':0,'additive_fields':allowed,'dynamic_fields':['as_of','history.as_of'],
        'excel':excel,'database_unchanged':True,'added_evidence':added,
        'limits':'Synthetic fixed-input regression; not full real-business scale or fresh-machine acceptance.'}
    (out/'report.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),'utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='added_evidence'},ensure_ascii=False))


if __name__=='__main__':main()
