"""A commits V001, B commits V002 before A reads; real timed pre-BEGIN cancellation."""
import argparse,copy,json,sys,threading,time,pickle,zlib
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import work_repository as wr,normal_orders as no,normal_delivery as nd,new_product_focus as focus,result_semantics as sem
from runtime_support import Cancelled

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--fixture',type=Path,required=True);ap.add_argument('--real-result',type=Path,required=True);ap.add_argument('--output',type=Path,required=True);a=ap.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);p=wr.loads(a.fixture.read_text(encoding='utf-8'));p['day']=wr.today();other=copy.deepcopy(p);other['results']['QA_A']=copy.deepcopy(p['results']['QA_B']);other['results']['QA_A'].update(store='QA_A',store_name=p['results']['QA_A']['store_name'])
    for d in other['results']['QA_A']['rows']:d.update(B='QA_A',A=p['results']['QA_A']['store_name'])
    work=wr.Repository(out/'race');first=no.Repository(work);second=no.Repository(work);batch=nd.prepare_batch(first,p,['QA_A']);committed=threading.Event();replaced=threading.Event();errors=[];readbacks=[];get=first.get
    def writer():
        if not committed.wait(30):errors.append('no first commit');return
        try:second.confirm(other,['QA_A'],nd.job_head(work,p['day'])['revision'],{'QA_A':1})
        except Exception as exc:errors.append(str(exc))
        finally:replaced.set()
    def readback(day,store,revision=None,**kw):
        if not readbacks:committed.set();assert replaced.wait(30)
        value=get(day,store,revision,**kw);readbacks.append(value['revision']);return value
    thread=threading.Thread(target=writer);thread.start();dest=out/'exports';dest.mkdir()
    with patch.object(first,'get',side_effect=readback):receipt=nd.execute_batch(first,batch,dest)
    thread.join(30);assert not errors and not thread.is_alive();assert readbacks==[1]
    import openpyxl
    book=openpyxl.load_workbook(receipt['paths'][0]);props={x.name:x.value for x in book.custom_doc_props};actual={r[4].value:r[16].value for ws in book for r in list(ws.rows)[1:]};book.close();assert props['修订']=='V001' and actual['new_must']==6 and actual.get('moving',0)==0
    events=first.events();export=[e for e in events if e['action']=='带号导出'];assert len(export)==1 and export[0]['revision']==1;assert get(p['day'],'QA_A')['revision']==2
    race={'readback_revisions':readbacks,'exported_revision':props['修订'],'export_log_revision':export[0]['revision'],'current_revision':2,'quantities':actual}
    # Same real 14244-row result, 100ms cancellation after execute enters archive.
    result=pickle.loads(zlib.decompress(a.real_result.read_bytes()));real=copy.deepcopy(p);real['results']={result['store']:result};real['states']={result['store']:{'name':result['store_name'],'state':'已完成'}};real['new_focus']=focus.empty(p['day'][:7]);work=wr.Repository(out/'timed-cancel');repo=no.Repository(work);batch=nd.prepare_batch(repo,real,[result['store']]);cancel=threading.Event();trace=[];timers=[];start=time.perf_counter();connect=work.connect
    def log(event):trace.append({'event':event,'seconds':time.perf_counter()-start,'cancel':cancel.is_set()})
    def stop():cancel.set();log('cancel requested')
    def progress(msg):
        if str(msg).startswith('后台归档'):
            t=threading.Timer(.1,stop);timers.append(t);t.start()
    def traced():
        db=connect();db.set_trace_callback(lambda sql:log(sql) if sql in ('BEGIN IMMEDIATE','COMMIT','ROLLBACK') else None);return db
    target=out/'cancel-exports';target.mkdir()
    with patch.object(work,'connect',side_effect=traced):
        try:nd.execute_batch(repo,batch,target,progress=progress,cancel=cancel)
        except Cancelled:log('cancelled safely')
        else:raise AssertionError('timed precommit cancellation signed')
    for t in timers:t.join()
    assert repo.get(p['day'],result['store']) is None and not work.list_jobs() and not repo.events() and not list(target.glob('*.xlsx'))
    report={'passed':True,'race':race,'real_rows':len(result['rows']),'timed_cancellation':trace,'cancelled_business_tables_empty':True,'side_effects':[p.name for p in work.path.parent.glob('*before*')],'note':'backups/storage metadata may remain; no claim database byte identity after cancellation'};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8',newline='\n');print(json.dumps(report,ensure_ascii=False))
if __name__=='__main__':main()
