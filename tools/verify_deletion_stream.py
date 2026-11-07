"""Compare streamed rewrites with the immutable pre-change implementation."""
import argparse,copy,hashlib,importlib.util,json,random,sqlite3,sys,zlib
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import maintenance_storage as current,storage_chunks as c,work_repository as wr
from test_deletion_bounded import BlockEdits

def main():
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    a.output.resolve().relative_to(ROOT/'qa');a.output.mkdir(parents=True,exist_ok=False)
    spec=importlib.util.spec_from_file_location('baseline_deletion_storage',a.baseline/'src/maintenance_storage.py')
    baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
    rng=random.Random(462);cases=[]
    for i in range(48):
        value=BlockEdits().payload()
        if i%4==0:value['loaded'].pop('normalized')
        if i%5==0:value['loaded']['intake']={'frozen':True}
        if i%6==0:value['loaded'].pop('warehouse_rows')
        if i%7==0:value['loaded']={}
        if i%8==0:value.pop('loaded')
        if i%3==0:value.pop('export_log')
        if i%9==0:value.update({f'extra-{n}':{'flag':False,'n':n} for n in range(300)})
        if i%10==0:value['states']={f's{n}':{'flag':False,'n':n} for n in range(700)}
        codes=rng.choice([set(),{'T'},{'U'},{'T','U'},{'missing'},{'s2','s256','s699'}])
        with closing(sqlite3.connect(':memory:')) as db:
            pack=c.prepare(value)
            if i%11==0:
                raw=wr.dumps(value).encode();h=hashlib.sha256(raw).hexdigest()
                pack=c.Prepared(['ref',h,len(raw)],h,len(raw),{h:(len(raw),zlib.compress(raw))})
            blob=c.put(db,pack);results=[]
            for implementation in (baseline,current):
                with closing(sqlite3.connect(':memory:')) as scratch:
                    editor=implementation.Editor(db,scratch)
                    result=editor.rewrite(blob,codes)
                    results.append((wr.dumps(editor.value(result.tree)),result.digest,result.size))
            if results[0]!=results[1]:raise AssertionError('Differential case '+str(i))
            cases.append({'case':i,'removed':sorted(codes),'digest':results[1][1],'bytes':results[1][2]})
    report={'passed':True,'cases':cases,'scope':'48 deterministic mixed-shape v2 payloads; exact canonical values, full digest and size; immutable 0.46.1 Editor oracle'}
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'cases':len(cases)}))
if __name__=='__main__':main()
