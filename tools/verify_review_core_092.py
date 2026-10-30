"""All matched frozen real rows: old/new model + exception exact field equality."""
import importlib.util,json,sys,pickle,zlib,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import model,one_box_exception
from parallel_qa import diff,sha
Q=ROOT/'qa/verified_092'
spec=importlib.util.spec_from_file_location('fixed_model_091',Q/'baseline/src/model.py');old=importlib.util.module_from_spec(spec);sys.modules[spec.name]=old;spec.loader.exec_module(old)
count=0;total=0;changed=[];started=time.perf_counter()
for path in sorted((ROOT/'qa/inbound_091/real').glob('*.pklz')):
    r=pickle.loads(zlib.decompress(path.read_bytes()))['payload']['result'];p=r['config']['parameters']
    for d in r['rows']:
        total+=1
        if d['_source']!='资料匹配' or d['_meta']['blocking']:continue
        a=old.calculate(d,old.Rules(**p));b=model.calculate(d,model.Rules(**p));changes=diff(a,b)
        if changes:changed.append({'store':r['store'],'sku':d['E'],'changes':changes})
        count+=1
    print(r['store'],count,flush=True)
report={'passed':not changed,'rows':total,'replayed':count,'differences':changed,'seconds':time.perf_counter()-started,'sources':{p.name:sha(p) for p in (ROOT/'src').glob('*.py')},'one_box_unchanged':sha(ROOT/'src/one_box_exception.py')==sha(Q/'baseline/src/one_box_exception.py')}
(Q/'real_core.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');assert report['passed'] and count==85545 and total==164722 and report['one_box_unchanged']
