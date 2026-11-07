"""Read-only checks on an existing isolated QA database. Writes only a new output folder."""
import argparse
from collections import defaultdict
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from stockout_repository import Repository
from stockout_common import file_hash
import stockout_metrics as metrics
from stockout_exports import export


def number(value):
    try:
        import math
        n=float(value)
        return n if math.isfinite(n) else None
    except (TypeError,ValueError):return None


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--settings',required=True);parser.add_argument('--output',required=True);a=parser.parse_args()
    settings=Path(a.settings).resolve();out=Path(a.output).resolve()
    if not settings.is_relative_to(ROOT/'qa') or not out.is_relative_to(ROOT/'qa') or settings==out or out.is_relative_to(settings):
        raise ValueError('Use an existing isolated QA settings folder and a separate new QA output directory')
    repo=Repository(settings)
    if not repo.path.is_file():raise FileNotFoundError(repo.path)
    out.mkdir(parents=True,exist_ok=False);before=file_hash(repo.path);start=time.perf_counter()
    with repo.connect() as db:
        days=[r[0] for r in db.execute('SELECT day FROM current_days ORDER BY day')]
        assert days
        expected_settings=repo.settings()
        originals={r['path']:file_hash(Path(r['path'])) for r in db.execute('SELECT path FROM originals')}
    q={'start':days[0],'end':days[-1],'view':'all','exclude_stopped':True,'exclude_disabled':True}
    combos=[{'name':'门店仓供货商类目必采','dimensions':['store','warehouse','supplier','category','mandatory']},
            {'name':'门店商品供货','dimensions':['store','sku','source','warehouse','category','mandatory']},
            {'name':'日期供货结构','dimensions':['day','source','warehouse','supplier','category','mandatory']}]
    references=[defaultdict(lambda:[0]*len(metrics.AGG_KEYS)) for _ in combos];total=[0]*len(metrics.AGG_KEYS)
    raw_rows=0
    with repo.connect() as db:
        # Independent raw numeric calculation; no use of the stored eligible/zero/bad aggregates.
        sql='''SELECT s.day,s.store,f.* FROM current_days d JOIN publications p ON p.id=d.publication
          JOIN run_stores r ON r.run=p.run JOIN store_snapshots s ON s.id=r.snapshot AND s.day=d.day
          JOIN facts f ON f.snapshot=s.id WHERE s.state='READY' ORDER BY s.day,s.store,f.sku'''
        for record in db.execute(sql):
            row=dict(record);raw_rows+=1;month=number(row['month_raw']);stock=number(row['stock_raw'])
            eligible=int(month is not None and month>0);zero=int(bool(eligible and stock is not None and stock<=0))
            bad=int(month is None or month<0 or eligible and stock is None)
            removed=row['replen_status'] in ('停采','禁采') or row['product_status']=='禁用'
            unknown=row['retired'] is None or row['assessment'] is None
            values=[eligible*int(not removed),zero*int(not removed),bad*int(not removed),eligible*int(unknown and not removed),zero*int(unknown and not removed),eligible*int(removed),zero*int(removed)]
            total=[a+b for a,b in zip(total,values)]
            if row['source']=='local':row['warehouse']='地采'
            for combo,reference in zip(combos,references):
                target=reference[tuple(row[k] for k in combo['dimensions'])]
                for i,value in enumerate(values):target[i]+=value
    actual=metrics.query(repo,q)['total'];assert [actual[k] for k in metrics.AGG_KEYS]==total
    cuts=[]
    for combo,reference in zip(combos,references):
        began=time.perf_counter();seen=set()
        for row in metrics.iter_cuts(repo,q,combo['dimensions']):
            key=tuple(row[k] for k in combo['dimensions']);assert key not in seen;seen.add(key)
            assert [row[k] for k in metrics.AGG_KEYS]==reference[key]
            assert row['rate']==(row['zero']/row['base'] if row['base'] and not row['bad'] else None)
        assert len(seen)==len(reference)
        cuts.append({'name':combo['name'],'dimensions':combo['dimensions'],'groups':len(seen),'seconds':round(time.perf_counter()-began,3),'differences':0})
    # Multi-select at real cardinality, canonical local labels, raw-vs-summary equality.
    checks=[]
    for filters in ({'warehouse':['地采']},{'source':['center','local'],'category':['食品']},{'mandatory':[0,1,None]}):
        options={**q,'filters':filters};base=metrics.query(repo,options)['total'];sums=[0]*len(metrics.AGG_KEYS)
        for row in metrics.iter_cuts(repo,options,combos[0]['dimensions']):sums=[a+row[k] for a,k in zip(sums,metrics.AGG_KEYS)]
        assert sums==[base[k] for k in metrics.AGG_KEYS];checks.append({'filters':filters,'base':base['base'],'zero':base['zero'],'differences':0})
    target=out/'门店缺货率_多维切面示例.xlsx';began=time.perf_counter()
    export(repo,q,target,combinations=combos)
    export_seconds=round(time.perf_counter()-began,3)
    import openpyxl
    book=openpyxl.load_workbook(target,read_only=True,data_only=True)
    try:
        for combo,reference in zip(combos,references):
            rows=iter(book[combo['name']].values);headers=next(rows);count=0;sums=[0]*len(metrics.AGG_KEYS)
            measure=['有销基数','缺货数','指标待核数','标签待核有销数','标签待核缺货数','剔除有销数','剔除缺货数']
            indexes=[headers.index(name) for name in measure]
            for row in rows:
                count+=1;sums=[a+row[i] for a,i in zip(sums,indexes)]
            assert count==len(reference) and sums==total
    finally:book.close()
    assert file_hash(repo.path)==before and repo.settings()==expected_settings
    assert all(file_hash(Path(p))==sha for p,sha in originals.items())
    report={'passed':True,'scope':'Existing isolated QA copy, read only; no platform calls, no snapshot or business writes',
            'source_database_sha256':before,'source_unchanged':True,'originals_unchanged':True,'settings_unchanged':True,
            'dates':days,'raw_rows':raw_rows,'total':actual,'cuts':cuts,'filters':checks,
            'export_seconds':export_seconds,'export_bytes':target.stat().st_size,'export_sha256':file_hash(target),
            'seconds':round(time.perf_counter()-start,3)}
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8',newline='\n')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
