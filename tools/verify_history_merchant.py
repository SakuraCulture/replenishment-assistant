"""Freeze same historical order for baseline and candidate merchant exports."""
import sys,subprocess,json,os
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];QA=ROOT/'qa/archive_072'

def main():
    if len(sys.argv)>1:
        source,out=map(Path,sys.argv[1:3]);sys.path.insert(0,str(source/'src'));import normal_orders,normal_exports,work_repository as wr,sqlite3
        from types import SimpleNamespace
        repo=normal_orders.Repository.__new__(normal_orders.Repository);repo.work=SimpleNamespace(connect=lambda:sqlite3.connect((QA/'snapshot.sqlite3').as_uri()+'?mode=ro',uri=True));head=repo.list()[0];order=repo.get(head['day'],head['store']);normal_exports.export(order,out);return
    out=QA/'merchant';out.mkdir(exist_ok=False)
    for name,source in [('old',QA/'baseline'),('new',ROOT)]:subprocess.run([sys.executable,__file__,str(source),str(out/(name+'.xlsx'))],check=True)
    sys.path.insert(0,str(ROOT/'tools'));from structure_oracle import excel_snapshot;from parallel_qa import diff
    differences=diff(excel_snapshot(out/'old.xlsx'),excel_snapshot(out/'new.xlsx'));report={'passed':not differences,'differences':differences,'checks':'same frozen real order; worksheets/order/cells/types/styles and XML structures; no row/column sorting','baseline':'a831049'};(out/'report.json').write_text(json.dumps(report,indent=2));assert report['passed'];print('merchant exact equality')
if __name__=='__main__':main()
