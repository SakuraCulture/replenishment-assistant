"""Run existing full oracle on frozen 0.18.1 with controlled completion order.

Only QA scheduling changes: workers still spawn and compute all data; both trees
deliver completed stores in submission order, so no dictionary order is hidden.
"""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--qa',type=Path,required=True);ap.add_argument('--worker',type=Path);ap.add_argument('--out',type=Path);a=ap.parse_args();q=a.qa.resolve()
    if a.worker:
        source=a.worker.resolve();sys.path[:0]=[str(source/'src'),str(source),str(ROOT/'tools')]
        os.environ['REPLENISHMENT_DATA_DIR']=str(a.out.resolve()/'settings')
        import batch_service as bs
        wait=bs.wait
        def ordered(active,timeout,return_when):
            first=next(iter(active));done,_=wait([first],timeout=timeout,return_when=return_when)
            return done,set(active)-done
        bs.wait=ordered
        from structure_oracle import capture
        return capture(source,q,a.out.resolve())
    env=os.environ.copy();env.update(PYTHONIOENCODING='utf-8',PYTHONHASHSEED='0')
    for name,source in [('model_old',q/'baseline'),('model_new',ROOT)]:
        with (q/(name+'.log')).open('w',encoding='utf-8') as log:
            subprocess.run([sys.executable,__file__,'--qa',str(q),'--worker',str(source),'--out',str(q/name)],env=env,stdout=log,stderr=log,check=True)
    sys.path.insert(0,str(ROOT/'tools'));from preissue_qa import compare
    compare(q)

if __name__=='__main__':main()
