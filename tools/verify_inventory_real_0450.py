"""Read only an existing trusted frozen QA result; never a user database."""
import argparse,json,sys,pickle
from decimal import Decimal
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import parallel_qa as q
import inventory_structure as service
from verify_analytics_0390 import timed


def main():
    p=argparse.ArgumentParser();p.add_argument('--result',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    source=a.result.resolve()
    if ROOT/'qa' not in source.parents or a.output.exists():raise ValueError('Use trusted project QA result and new report path')
    before=q.sha(source);result=q.read(source);frozen=pickle.dumps(result,protocol=5);runs=[]
    for _ in range(3):
        report,t=timed(lambda:service.preview({result['store']:result},'2026-10-27'));runs.append(t)
    # Independent arithmetic for actual valid warehouse rows; no reuse of metric/value helpers.
    expected=Decimal(0);count=0
    for r in result['rows']:
        if r.get('_source') in ('地采商品','匹配待核') or r.get('AU') not in ('山东仓','河北仓'):continue
        if not r.get('G'):continue
        try:k=Decimal(str(r['K']));price=Decimal(str(r['V']))
        except Exception:continue
        if not k.is_finite() or not price.is_finite() or k<0 or price<=0:continue
        expected+=k*price;count+=1
    center=report['overview']['sources']['中心仓']['known_value']
    if center!=expected or q.sha(source)!=before or frozen!=pickle.dumps(result,protocol=5):raise AssertionError('Real frozen inventory mismatch or mutation')
    out={'passed':True,'source':str(source),'sha256':before,'rows':len(result['rows']),'independently_valued_center_rows':count,'expected_center_value':expected,
         'overview':report['overview'],'runs':runs,'metric_version':report['metric_version'],
         'limits':'Existing historical 14,244-row real QA result. Preview date is a test label, not observed inventory timestamp. No live business database or platform. No reliable local-procurement price supplied.'}
    a.output.write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str),'utf-8',newline='\n');print(json.dumps({'passed':True,'rows':len(result['rows'])}))


if __name__=='__main__':main()
