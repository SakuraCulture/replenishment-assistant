"""Fixed e45b9d0 vs candidate, strict types/list/dict order; exact reason-cell allowlist."""
import argparse,copy,hashlib,json,os,pickle,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'tools'))
from parallel_qa import clean,diff,read,sha,WHITELIST
from structure_oracle import excel_snapshot

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--inputs',type=Path,default=ROOT/'qa/parallel_062');p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);env=os.environ.copy();env['PYTHONHASHSEED']='0';env['PYTHONIOENCODING']='utf-8';cases=[];allowed=[]
    inputs={str(f.resolve()):sha(f) for f in (a.inputs/'inputs').rglob('*') if f.is_file()};inputs[str(a.fixture.resolve())]=sha(a.fixture)
    for name,source in [('old',a.baseline.resolve()),('new',ROOT)]:
        subprocess.run([sys.executable,str(ROOT/'tools/parallel_qa.py'),'capture','--qa',str(a.inputs.resolve()),'--source',str(source),'--output',str(out/name)],env=env,check=True)
        subprocess.run([sys.executable,str(ROOT/'tools/verify_storage_equivalence.py'),'--source',str(source),'--fixture',str(a.fixture.resolve()),'--output',str(out/(name+'-storage'))],env=env,check=True)
    names=json.loads((out/'old/capture.json').read_text(encoding='utf-8'))['cases'];assert names==json.loads((out/'new/capture.json').read_text(encoding='utf-8'))['cases']
    for name in names:
        before=read(out/'old'/(name+'.pklz'));after=read(out/'new'/(name+'.pklz'));d=diff(clean(before),clean(after));cases.append({'case':name,'differences':d})
        if name.startswith('real_') and isinstance(after,dict) and 'rows' in after:
            keys=[(r['B'],r['E']) for r in after['rows']];assert len(keys)==len(set(keys));cases[-1].update(rows=len(keys),amount=after['summary']['amount'],positive=after['summary']['positive'])
    for f in (out/'old').glob('*.xlsx'):cases.append({'case':f.name,'differences':diff(excel_snapshot(f),excel_snapshot(out/'new'/f.name))})
    old=pickle.loads((out/'old-storage/capture.pkl').read_bytes());new=pickle.loads((out/'new-storage/capture.pkl').read_bytes())
    # Only V data-cell text may change. Cell type/style/position/XML attributes remain exact.
    for filename,book in old['workbooks'].items():
        other=new['workbooks'][filename]
        for x,y in zip(book['sheets'],other['sheets']):
            for cell,before in x['cells'].items():
                after=y['cells'].get(cell)
                if before==after or not cell.startswith('V') or not cell[1:].isdigit() or int(cell[1:])<=1:continue
                u,v=ET.fromstring(before),ET.fromstring(after);ut=list(u.iter())[-1];vt=list(v.iter())[-1];prev,nxt=ut.text,vt.text;ut.text=vt.text='<reason>'
                assert ET.tostring(u)==ET.tostring(v),'reason cell non-text structure changed'
                allowed.append({'file':filename,'sheet':x['identity']['name'],'cell':cell,'before':prev,'after':nxt});y['cells'][cell]=before
    cases.append({'case':'daily/archive SQL, immutable snapshots, events, revision conflicts, complete merchant workbook','differences':diff(old,new)})
    cores=['model.py','one_box_exception.py','allocation.py','default_config.json','default_must_sell.json','engine.py','warehouse_catalog.py','new_products.py','input_adapter.py','result_semantics.py'];core={n:{'old':sha(a.baseline/'src'/n),'new':sha(ROOT/'src'/n)} for n in cores};assert all(x['old']==x['new'] for x in core.values())
    drift=[s for s,h in inputs.items() if sha(s)!=h];count=sum(len(c['differences']) for c in cases)
    report={'passed':not count and not drift,'baseline_commit':'e45b9d08cddbced1bf8566281987431534bbf5b7','candidate_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),'cases':cases,'unexpected_differences':count,'allowed_changes':allowed,'whitelist':WHITELIST,'storage_whitelist':['physical chunk tables decoded by independent version codec','clock and UUID frozen identically, not ignored','merchant sheet V2:Vn text only; every actual reason change listed'],'core_hashes':core,'frozen_inputs':inputs,'input_drift':drift,'strict_order':True};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps({'passed':report['passed'],'unexpected':count,'reason_changes':len(allowed)}));assert report['passed']
if __name__=='__main__':main()
