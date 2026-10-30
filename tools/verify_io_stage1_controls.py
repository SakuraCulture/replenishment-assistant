"""Selected-store fast export and progress contract, private synthetic fixtures."""
import argparse,copy,json,multiprocessing,os,sys,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tools')]


def main():
    p=argparse.ArgumentParser();p.add_argument('--fixture',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    os.environ['QT_QPA_PLATFORM']='offscreen';os.environ['REPLENISHMENT_DATA_DIR']=str(out/'settings')
    import work_repository as wr,normal_orders as no,normal_delivery as nd
    from ui_work_io import Session,Progress
    from parallel_qa import diff
    payload=wr.loads(a.fixture.read_text(encoding='utf-8'));payload['day']=wr.today();work=wr.Repository(out/'settings');repo=no.Repository(work);codes=list(payload['results']);selected=[codes[0]];checks=[]
    def slim(p):return {**{k:p[k] for k in ('day','input_dirty','new_focus','model_version')},'states':{c:p['states'][c] for c in selected},'results':{c:p['results'][c] for c in selected},'_draft_summary':wr.summary(p)}
    with Session(work.path) as s:
        review=s.call('delivery_probe',[slim(payload),selected],lambda _:None);assert review['summary']==wr.summary(payload)
        folder=out/'incomplete';folder.mkdir()
        try:s.call('delivery_execute',str(folder),lambda _:None)
        except RuntimeError as e:assert '完整签发草稿' in str(e)
        else:raise AssertionError('incomplete draft issued')
        assert not repo.headers() and not work.list_jobs()
        tail={**payload,'results':{c:r for c,r in payload['results'].items() if c not in selected}}
        s.call('delivery_expand',[tail,list(payload['results'])],lambda _:None)
        receipt=s.call('delivery_execute',str(folder),lambda _:None);assert receipt['signed']
    assert not diff(work.get_job(payload['day'])['payload'],payload)
    assert [h['store'] for h in repo.headers()]==selected;assert all('(1)' not in p for p in receipt['paths'])
    checks.append('probe carries selected results only; incomplete draft cannot issue; expansion preserves full draft/order and unselected store, filenames lack revision suffix')
    with Session(work.path) as s:
        review=s.call('delivery_probe',[slim(payload),selected],lambda _:None);assert all(p['same'] for p in review['plans'])
        folder=out/'signed';folder.mkdir();receipt=s.call('delivery_execute',str(folder),lambda _:None);assert not receipt['signed']
    assert repo.get(payload['day'],selected[0])['revision']==1
    checks.append('already-signed export requires no full draft expansion and creates no revision')
    with Session(work.path) as s:
        s.call('delivery_probe',[slim(payload),selected],lambda _:None)
        changed=copy.deepcopy(payload);changed['results'][selected[0]]['qa_revision']='external';repo.confirm(changed,selected,work.get_job(payload['day'])['revision'],{selected[0]:1})
        folder=out/'conflict';folder.mkdir()
        try:s.call('delivery_execute',str(folder),lambda _:None)
        except RuntimeError as e:assert '正常单已更新' in str(e)
        else:raise AssertionError('stale signed snapshot accepted')
        assert not list(folder.iterdir())
    checks.append('concurrent revision after selected-only probe fails before any export')
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QTimer
    from export_progress import RowProgress
    app=QApplication([])
    class FakeProcess:
        def is_alive(self):return True
    class Fake:
        process=FakeProcess();cancel=threading.Event()
        def call(self,operation,value,progress):
            progress(RowProgress('压缩测试：真实字节进度',2**32,2**33));time.sleep(.8);return True
    dialog=Progress(None,Fake(),'save',None);observed=[]
    def inspect():
        observed.append((dialog.bar.value(),dialog.health.text(),dialog.cancel_btn.isEnabled()));dialog.grab().save(str(out/'progress.png'))
    QTimer.singleShot(400,inspect);dialog.task.start();dialog.exec();dialog.task.wait()
    assert observed and observed[0][0]==500 and '已用时' in observed[0][1] and not observed[0][2];assert dialog.bar.value()==1000 and dialog.task.error is None
    dialog.deleteLater();app.processEvents()
    checks.append('Qt progress handles byte counts above 32-bit, real 50% stage, elapsed/status text, no write interruption and success-only final 100%')
    assert not multiprocessing.active_children();report={'passed':True,'checks':checks,'remaining_children':[]};(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=False))

if __name__=='__main__':multiprocessing.freeze_support();main()
