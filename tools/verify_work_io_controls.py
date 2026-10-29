"""New process boundary: cancellation, revision races, rollback and delivery retry."""
import argparse,copy,json,multiprocessing,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]
import work_repository as wr,normal_orders,normal_delivery
from ui_work_io import Session
from parallel_qa import diff

def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False);work=wr.Repository(out/'settings');repo=normal_orders.Repository(work);payload=wr.loads(a.fixture.read_text(encoding='utf-8'));payload['day']=wr.today();codes=list(payload['results']);checks=[]
    def head():return normal_delivery.job_head(work,payload['day'])
    with Session(work.path) as session:
        review=session.call('prepare_normal',[payload,codes],lambda _:None);session.call('review_export',str(out/'review.json'),lambda _:None)
        details=json.loads((out/'review.json').read_text(encoding='utf-8'));assert details[codes[0]]['changes']==normal_orders.revision_diff(None,payload['results'][codes[0]])
    assert head() is None and not repo.list();checks.append('prepare + full review export + cancel: no draft/order written; independent full revision_diff matches')
    with Session(work.path) as session:
        session.call('prepare_normal',[payload,codes],lambda _:None);receipt=session.call('confirm_normal',None,lambda _:None)
    assert len(receipt['orders'])==len(codes);assert not diff(work.get_job(payload['day'])['payload'],payload);first=head();old=repo.get(payload['day'],codes[0]);checks.append('child confirmation saves exact complete values/types and returns small heads')
    changed=copy.deepcopy(payload);changed['results'][codes[0]]['qa_note']='next revision'
    with Session(work.path) as session:
        session.call('prepare_normal',[changed,codes],lambda _:None);external=copy.deepcopy(payload);external['outside']=True;work.save_job(payload['day'],external,first['revision'])
        try:session.call('confirm_normal',None,lambda _:None)
        except RuntimeError as exc:assert '另一窗口' in str(exc)
        else:raise AssertionError('stale commit')
    assert repo.get(payload['day'],codes[0])==old and work.get_job(payload['day'])['payload']['outside'];checks.append('independent writer between prepare/commit rejected; original signed order retained')
    before=work.get_job(payload['day'])
    with work.connect() as db:db.execute("CREATE TRIGGER qa_abort BEFORE INSERT ON normal_revisions BEGIN SELECT RAISE(ABORT,'QA process rollback'); END")
    with Session(work.path) as session:
        session.call('prepare_normal',[changed,codes],lambda _:None)
        try:session.call('confirm_normal',None,lambda _:None)
        except RuntimeError as exc:assert 'QA process rollback' in str(exc)
        else:raise AssertionError('rollback trigger ignored')
    assert work.get_job(payload['day'])==before and repo.get(payload['day'],codes[0])==old
    with work.connect() as db:db.execute('DROP TRIGGER qa_abort')
    checks.append('child database failure after daily write rolls complete transaction back')
    cancelled=out/'cancelled';cancelled.mkdir()
    with Session(work.path) as session:
        session.call('delivery_prepare',[changed,codes],lambda _:None)
        def progress(value):
            if hasattr(value,'current') and value.current>0:session.cancel.set()
        try:session.call('delivery_execute',str(cancelled),progress)
        except RuntimeError as exc:assert '已归档' in str(exc) and '未生成' in str(exc)
        else:raise AssertionError('export cancellation ignored')
    assert repo.get(payload['day'],codes[0])['revision']==2 and not list(cancelled.glob('*.xlsx'));checks.append('cancel during actual row writing preserves committed revision, no partial final XLSX; explicit partial completion receipt')
    retry=out/'retry';retry.mkdir()
    with Session(work.path) as session:
        session.call('delivery_prepare',[changed,codes],lambda _:None);receipt=session.call('delivery_execute',str(retry),lambda _:None)
    assert len(receipt['paths'])==len(codes) and not receipt['signed'];assert repo.get(payload['day'],codes[0])['revision']==2;checks.append('retry exports same signed revision without new revision; no old cancellation contaminates next session')
    assert not multiprocessing.active_children();report={'passed':True,'checks':checks,'remaining_children':[]};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':multiprocessing.freeze_support();main()
