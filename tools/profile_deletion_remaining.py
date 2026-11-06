"""Instrument the remaining cold-cache work on a new isolated copy; no input writes."""
import argparse,hashlib,json,sys,time
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import maintenance_storage as storage
import verify_deletion_load as verify

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);a=parser.parse_args()
    qa=ROOT/'qa/recovery_0460';a.output.resolve().relative_to(qa.resolve())
    totals={}
    for name in ('legacy_info','references_for'):
        original=getattr(storage,name);totals[name]={'seconds':0.,'calls':0,'compressed_bytes':0,'cache_misses':0}
        def wrap(*args,_name=name,_original=original,**kw):
            blob=args[1] if _name=='legacy_info' else args[3]
            key=hashlib.sha256(blob).hexdigest() if _name=='legacy_info' else args[1]
            table='maintenance_legacy_refs' if _name=='legacy_info' else 'maintenance_chunk_refs'
            cached=args[0].execute('SELECT 1 FROM '+table+' WHERE hash=?',(key,)).fetchone()
            row=totals[_name];row['calls']+=1;row['compressed_bytes']+=len(blob);row['cache_misses']+=not bool(cached)
            start=time.perf_counter()
            try:return _original(*args,**kw)
            finally:row['seconds']+=time.perf_counter()-start
        setattr(storage,name,wrap)
    frozen=ROOT/'qa/delete_diagnosis_20261103/frozen'
    before={str(p.relative_to(frozen)):verify.sha(p) for p in frozen.rglob('*') if p.is_file()}
    verify.worker(SimpleNamespace(source=ROOT,fixture=frozen,output=a.output,qa_root=qa,warm=None,parents=1))
    after={str(p.relative_to(frozen)):verify.sha(p) for p in frozen.rglob('*') if p.is_file()}
    assert before==after
    report={'source_scope':'0.45.2 deletion implementation; function timing instrumentation only','timings':totals,'frozen_input_unchanged':True,'input_hashes':after}
    (a.output/'profile.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
