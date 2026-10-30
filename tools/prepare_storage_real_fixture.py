"""Recalculate frozen real inputs into a separate current-model QA database."""
import argparse,copy,hashlib,json,sqlite3,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'src'))
import work_repository as wr,batch_service,new_products,normal_orders as no,model
from version_info import MODEL_VERSION,diagnostic

def main():
    a=argparse.ArgumentParser();a.add_argument('--snapshot',type=Path,required=True);a.add_argument('--output',type=Path,required=True);args=a.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False);source=args.snapshot.resolve()
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    ro=wr.Repository.__new__(wr.Repository);ro.path=source;ro.connect=lambda:sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)
    p=ro.get_job(ro.list_jobs()[0]['day'])['payload'];loaded=dict(p['loaded']);loaded.pop('intake',None)
    report={'passed':False,'source_sha256':before,'stores':[],'target_differences':[]}
    bases={}
    for code,old in list(p['results'].items()):
        one={**loaded,'normalized':[x for x in loaded['normalized'] if x[0]['B']==code]}
        if loaded.get('warehouse_rows'):one['warehouse_rows']={w:[x for x in rows if x[0]['B']==code] for w,rows in loaded['warehouse_rows'].items()}
        one['import_audit']=old.get('import_audit',{})
        base=batch_service.calculate_store(one,code,old['effective_settings'],p['profile'])
        selected=old.get('new_products',{}).get('selection',{})
        current=new_products.apply(base,selected) if selected else base
        if selected:bases[code]=base
        assert len(current['rows'])==len(old['rows'])
        for prev,new in zip(old['rows'],current['rows']):
            assert (prev['B'],prev['E'])==(new['B'],new['E'])
            if wr.dumps(prev)!=wr.dumps(new):
                assert model.number(prev.get('N')) and prev['N']>30 and model.number(prev.get('J')) and prev['J']>0,(code,prev['E'])
                fields=[k for k in sorted(set(prev)|set(new)) if wr.dumps(prev.get(k))!=wr.dumps(new.get(k))]
                report['target_differences'].append({'store':code,'sku':prev['E'],'fields':fields})
            for k in ['AS','BV','_candidate_qty','_execution_qty','_execution_amount']:assert wr.dumps(prev.get(k))==wr.dumps(new.get(k)),(code,prev['E'],k)
        assert current['summary']==old['summary']
        p['results'][code]=current
        report['stores'].append({'store':code,'rows':len(current['rows']),'amount':current['summary']['amount'],'selected_new':len(selected)})
        print(code,len(current['rows']),flush=True)
    p['new_product_bases']=bases;p['model_version']=MODEL_VERSION;p['version']=diagnostic()
    repo=wr.Repository(out/'settings');repo.save_job(p['day'],p)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    report.update(passed=True,model_version=MODEL_VERSION,snapshot=str(repo.path),sum_amount=round(sum(x['amount'] for x in report['stores']),2))
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
